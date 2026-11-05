import json

import httpx
import pytest

from wan_sentinel.config import load
from wan_sentinel.routeros import RouterOS
from wan_sentinel.router_probe import RouterProbe, metrics, milliseconds, route_matches


TARGET = "198.51.100.10"
INTERFACE = "ether2 WAN secondary"
ROUTE = {"dst-address": TARGET + "/32", "active": "true", "routing-table": "main", "immediate-gw": "192.0.2.1%" + INTERFACE}
ENABLED = {"enabled": True, "isolation_validated": True, "binding": "address-interface"}


def replies():
    return [{"seq": str(i), "host": TARGET, "time": f"{i+1}ms500us", "sent": str(i+1), "received": str(i+1)} for i in range(5)]


@pytest.mark.parametrize("text,expected", [("551us", .551), ("1ms45us", 1.045), ("1s2ms", 1002), ("42ns", .000042)])
def test_duration(text, expected):
    assert milliseconds(text) == pytest.approx(expected)


def test_reply_metrics():
    assert metrics(replies(), TARGET) == {"probe_ok": 1, "available": 1, "loss_pct": 0, "latency_ms": 3.5, "jitter_ms": 1}


@pytest.mark.parametrize("fixture,latency,jitter", [
    ("routeros_ping.json", 3.6412, .20025),
    ("routeros_ping_dhcp.json", 83.668, 58.6775),
])
def test_routeros_observed_response_shape(fixture, latency, jitter):
    # Observed RouterOS 7.25 structure, with target replaced by a documentation IP.
    from pathlib import Path
    rows = json.loads((Path(__file__).parent / "fixtures" / fixture).read_text())
    result = metrics(rows, TARGET)
    assert result["probe_ok"] == 1
    assert result["available"] == 1
    assert result["loss_pct"] == 0
    assert result["latency_ms"] == pytest.approx(latency)
    assert result["jitter_ms"] == pytest.approx(jitter)


def test_total_loss_is_not_execution_failure():
    rows = [{"seq": str(i), "status": "timeout", "sent": str(i+1), "received": "0"} for i in range(5)]
    assert metrics(rows, TARGET) == {"probe_ok": 1, "available": 0, "loss_pct": 100}


@pytest.mark.parametrize("change", ["unknown-status", "missing-row", "duplicate-seq", "wrong-host", "invalid-time", "bad-summary"])
def test_malformed_results_rejected(change):
    rows = replies()
    if change == "unknown-status":
        rows[0]["status"] = "unsupported diagnostic"
    elif change == "missing-row":
        rows.pop()
    elif change == "duplicate-seq":
        rows[1]["seq"] = "0"
    elif change == "wrong-host":
        rows[0]["host"] = "192.0.2.99"
    elif change == "invalid-time":
        rows[0]["time"] = "invalid"
    else:
        rows[-1]["received"] = "4"
    with pytest.raises((ValueError, KeyError)):
        metrics(rows, TARGET)


@pytest.mark.parametrize("routes", [[], [dict(ROUTE, active="false")], [dict(ROUTE, disabled="true")], [dict(ROUTE, ecmp="true")], [dict(ROUTE, **{"immediate-gw": "192.0.2.2%other"})], [ROUTE, ROUTE]])
def test_route_guard_rejects_unsafe_routes(routes):
    assert not route_matches(routes, TARGET, INTERFACE)


def test_pinned_route_overrides_shared_default():
    default = dict(ROUTE, **{"dst-address": "0.0.0.0/0", "immediate-gw": "192.0.2.2%other"})
    assert route_matches([default, ROUTE], TARGET, INTERFACE)
    assert not route_matches([default], TARGET, INTERFACE)


async def execute(config=ENABLED, status=200, routes=None, binding=None, route_changes=False):
    seen = []
    get_count = 0
    def handler(request):
        nonlocal get_count
        seen.append(request)
        if request.method == "GET":
            get_count += 1
            result = [ROUTE] if routes is None else routes
            if route_changes and get_count > 1:
                result = [dict(ROUTE, **{"immediate-gw": "192.0.2.2%other"})]
            return httpx.Response(200, json=result)
        return httpx.Response(status, json=replies() if status == 200 else {"message": "error"})
    cfg = dict(config)
    if binding:
        cfg["binding"] = binding
    async with httpx.AsyncClient(base_url="https://router/rest/", transport=httpx.MockTransport(handler)) as client:
        result = await RouterProbe(RouterOS(client), cfg)({"interface": INTERFACE}, TARGET)
    return result, seen


@pytest.mark.parametrize("binding", ["address-interface", "interface-argument"])
async def test_explicit_binding_and_allowlisted_endpoint(binding):
    result, seen = await execute(binding=binding)
    assert result["probe_ok"] == 1
    assert [r.method for r in seen] == ["GET", "POST", "GET"]
    request = seen[1]
    assert request.url.path == "/rest/ping"
    body = json.loads(request.content)
    assert body["count"] == "5"
    if binding == "address-interface":
        assert body["address"] == TARGET + "%" + INTERFACE
    else:
        assert body["interface"] == INTERFACE


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_api_errors_never_retry_unbound(status):
    result, seen = await execute(status=status)
    assert result == {"probe_ok": 0}
    assert sum(r.method == "POST" for r in seen) == 1


async def test_other_wan_route_never_starts_ping():
    result, seen = await execute(routes=[dict(ROUTE, **{"immediate-gw": "192.0.2.2%other"})])
    assert result == {"probe_ok": 0}
    assert all(r.method == "GET" for r in seen)


async def test_changed_route_discards_result():
    result, _ = await execute(route_changes=True)
    assert result == {"probe_ok": 0}


@pytest.mark.parametrize("config", [{}, {"enabled": True}, {"enabled": False, "isolation_validated": True}])
async def test_no_diagnostics_until_explicitly_enabled_and_validated(config):
    result, seen = await execute(config=config)
    assert result == {"probe_ok": 0}
    assert seen == []


def test_router_backend_config_needs_no_namespaces(tmp_path):
    import yaml
    config = {"router": {"url": "https://router"}, "wans": [{"name": "primary", "kind": "dhcp", "interface": INTERFACE, "targets": [TARGET]}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert load(path)["wans"][0]["interface"] == INTERFACE
    config["probes"] = {"enabled": True}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="Validate isolation"):
        load(path)
