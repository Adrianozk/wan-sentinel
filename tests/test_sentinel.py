import copy
import json

import httpx
import pytest

from wan_sentinel.config import load
from wan_sentinel.main import collect
from wan_sentinel.probes import probe
from wan_sentinel.routeros import RouterOS
from wan_sentinel.state import connection
from wan_sentinel.influx import Influx, line


@pytest.fixture
def snapshot():
    return {
        "interfaces": [{"name": "ether2", "running": "true"},
                       {"name": "pppoe-backup", "running": "false"}],
        "dhcp": [{"interface": "ether2", "status": "searching"}],
        "pppoe": [{"name": "pppoe-backup", "running": "false"}],
        "routes": [{"dst-address": "0.0.0.0/0", "active": "false", "gateway": "pppoe-backup"}],
    }


@pytest.fixture
def wan():
    return dict(name="secondary", interface="ether2", kind="dhcp", namespace="test-wan", device="wan0", targets=["1.1.1.1"])


def test_active_ethernet_without_lease_and_inactive_backup(snapshot, wan):
    assert connection(snapshot, wan) == "down"
    assert connection(snapshot, dict(wan, kind="pppoe", interface="pppoe-backup")) == "down"
    snapshot["dhcp"][0]["status"] = "bound"
    assert connection(snapshot, wan) == "up"


def test_missing_interface_unknown(snapshot, wan):
    assert connection(snapshot, dict(wan, interface="absent")) == "unknown"


@pytest.mark.parametrize("status", ["renewing...", "rebinding..."])
def test_dhcp_keeps_connection_during_lease_renewal(snapshot, wan, status):
    client = snapshot["dhcp"][0]
    client["status"] = status
    assert connection(snapshot, wan) == "down"
    client["address"] = "192.0.2.2/24"
    assert connection(snapshot, wan) == "up"


def test_missing_operational_fields_unknown(snapshot, wan):
    del snapshot["interfaces"][0]["running"]
    assert connection(snapshot, wan) == "unknown"
    snapshot["interfaces"][0]["running"] = "true"
    del snapshot["dhcp"][0]["status"]
    assert connection(snapshot, wan) == "unknown"


async def test_rest_only_gets():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])
    async with httpx.AsyncClient(base_url="https://router/rest/", transport=httpx.MockTransport(handler)) as client:
        assert (await RouterOS(client).snapshot())["routes"] == []
    assert len(seen) == 4
    assert all(r.method == "GET" and str(r.url).startswith("https://router/rest/") for r in seen)


async def test_collection_failure_does_not_imply_link_failure(wan):
    class Broken:
        async def snapshot(self):
            raise httpx.ConnectError("private details")
    async def good(*args):
        return {"probe_ok": 1, "available": 1}
    lines = await collect(Broken(), [wan], good)
    assert any("collection_ok=0.0" in row for row in lines)
    assert any("state=unknown" in row for row in lines)
    assert any("available=1.0" in row for row in lines)


def runner(output, code=0, extra=False):
    calls = []
    async def run(*args):
        calls.append(args)
        if "link" in args:
            return 0, json.dumps([{"ifname": "lo"}, {"ifname": "wan0"}] + ([{"ifname": "eth1"}] if extra else []))
        if "route" in args:
            return 0, json.dumps([{"dst": "default", "dev": "wan0"}])
        return code, output
    return run, calls


async def test_bound_probe_metrics(wan):
    run, calls = runner("time=10.0 ms\ntime=14.0 ms\n5 packets transmitted, 2 received")
    assert await probe(wan, "1.1.1.1", run) == {"probe_ok": 1, "available": 1, "loss_pct": 60, "latency_ms": 12, "jitter_ms": 4}
    assert calls[-1][:4] == ("ip", "netns", "exec", "test-wan")
    assert calls[-1][5:7] == ("-n", "-I")
    assert "wan0" in calls[-1]


async def test_no_fallback(wan):
    run, calls = runner("", extra=True)
    assert await probe(wan, "1.1.1.1", run) == {"probe_ok": 0}
    assert len(calls) == 1


@pytest.mark.parametrize("output,code,expected", [
    ("5 packets transmitted, 0 received", 1, {"probe_ok": 1, "available": 0, "loss_pct": 100}),
    ("permission denied", 2, {"probe_ok": 0}),
    ("", 1, {"probe_ok": 0}),
    ("5 packets transmitted, 2 received", 0, {"probe_ok": 0}),
])
async def test_loss_vs_execution_error(wan, output, code, expected):
    run, _ = runner(output, code)
    assert await probe(wan, "1.1.1.1", run) == expected


async def test_export():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(204)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Influx(client, dict(url="https://influx", org="test", bucket="test", token="fake")).write([line({"wan": "a b"}, {"probe_ok": 1})])
    assert seen[0].url.path == "/api/v2/write"
    assert seen[0].content == b"wan_sentinel,wan=a\\ b probe_ok=1.0"


def test_config(tmp_path, monkeypatch, wan):
    import yaml
    monkeypatch.setenv("TEST_PASSWORD", 'fake: # [special] "quoted"\nnext-line')
    data = dict(router=dict(url="https://router", password="${TEST_PASSWORD}"), wans=[wan])
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert load(path)["router"]["password"] == 'fake: # [special] "quoted"\nnext-line'
    data["wans"].append(copy.deepcopy(wan))
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load(path)


@pytest.mark.parametrize("routes", [[{"dev": "eth9"}], [{"dev": "wan0", "nexthops": [{"dev": "eth9"}]}]])
async def test_ambiguous_route_never_probes(wan, routes):
    calls = []
    async def run(*args):
        calls.append(args)
        if "link" in args:
            return 0, '[{"ifname":"lo"},{"ifname":"wan0"}]'
        return 0, json.dumps(routes)
    assert await probe(wan, "1.1.1.1", run) == {"probe_ok": 0}
    assert len(calls) == 2


async def test_missing_namespace_never_probes(wan):
    calls = []
    async def run(*args):
        calls.append(args)
        return 1, ""
    assert await probe(wan, "1.1.1.1", run) == {"probe_ok": 0}
    assert len(calls) == 1


async def test_successful_collection_exports_routes_and_counters(snapshot, wan):
    snapshot["interfaces"][0]["rx-byte"] = "1234"
    class FakeRouter:
        async def snapshot(self):
            return snapshot
    async def failed_probe(*args):
        return {"probe_ok": 0}
    lines = await collect(FakeRouter(), [wan], failed_probe)
    assert any("collection_ok=1.0" in row for row in lines)
    assert any("rx_bytes=1234.0" in row and "state=down" in row for row in lines)
    assert any("active_default_routes=0.0,route_count=1.0" in row for row in lines)
    assert any("probe_ok=0.0" in row for row in lines)


@pytest.mark.parametrize("status,payload", [(403, {}), (200, {"unexpected": "object"})])
async def test_rest_errors(status, payload):
    async with httpx.AsyncClient(base_url="https://router/rest/", transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload))) as client:
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await RouterOS(client).snapshot()


def test_dashboard_shape():
    from pathlib import Path
    dashboard = json.loads((Path(__file__).parents[1] / "grafana/wan-sentinel.json").read_text())
    assert len(dashboard["panels"]) == 6
    quality = next(panel for panel in dashboard["panels"] if panel["type"] == "timeseries")
    assert quality["repeat"] == "wan"
    assert quality["options"]["legend"]["calcs"] == ["mean", "max", "min"]
    assert [
        next(prop["value"] for prop in override["properties"] if prop["id"] == "custom.axisPlacement")
        for override in quality["fieldConfig"]["overrides"]
    ] == ["left", "left", "right"]
    assert all("fill(" not in p["targets"][0]["query"] for p in dashboard["panels"])


def test_routeros_interface_names_preserved(tmp_path, wan, snapshot):
    import yaml
    name = "ether2 WAN secundária"
    wan["interface"] = name
    data = dict(router=dict(url="https://router"), wans=[wan])
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    loaded = load(path)["wans"][0]
    assert loaded["interface"] == name
    snapshot["interfaces"][0]["name"] = name
    snapshot["dhcp"][0]["interface"] = name
    assert connection(snapshot, loaded) == "down"
    snapshot["dhcp"][0]["status"] = "bound"
    assert connection(snapshot, loaded) == "up"


@pytest.mark.parametrize("name", ["", " ", "ether2\nWAN", "ether2\x00WAN"])
def test_invalid_routeros_interface_names(tmp_path, wan, name):
    import yaml
    wan["interface"] = name
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(dict(router=dict(url="https://router"), wans=[wan])), encoding="utf-8")
    with pytest.raises(ValueError):
        load(path)
