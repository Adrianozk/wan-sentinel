"""Experimental router-originated probes; never modify router configuration."""
import ipaddress
import re
import statistics

import httpx

from .state import yes


def route_matches(rows, target, interface):
    """Require the selected main-table route to resolve only to this WAN."""
    candidates = []
    for row in rows:
        if row.get("routing-table", "main") != "main" or not yes(row.get("active")) or yes(row.get("disabled")):
            continue
        network = ipaddress.ip_network(row["dst-address"], strict=False)
        if ipaddress.ip_address(target) in network:
            candidates.append((network.prefixlen, row))
    if not candidates:
        return False
    longest = max(length for length, _ in candidates)
    best = [row for length, row in candidates if length == longest]
    # Reject ECMP even when the serialized gateway list looks similar.
    if len(best) != 1 or yes(best[0].get("ecmp")):
        return False
    gateway = best[0].get("immediate-gw", "")
    if not gateway or "," in gateway or ";" in gateway:
        return False
    return gateway == interface or gateway.rsplit("%", 1)[-1] == interface


def milliseconds(value):
    if not isinstance(value, str):
        raise ValueError("Unexpected RTT")
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|us|ns|s)", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise ValueError("Unexpected RTT format")
    factors = {"s": 1000, "ms": 1, "us": .001, "ns": .000001}
    return sum(float(number) * factors[unit] for number, unit in parts)


def metrics(rows, target, count=5):
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError("Incomplete ping response")
    times = []
    losses = {"timeout", "host unreachable", "net unreachable", "no route to host", "admin prohibited", "TTL exceeded"}
    for seq, row in enumerate(rows):
        if int(row["seq"]) != seq:
            raise ValueError("Missing or duplicate sequence")
        status = row.get("status", "")
        if status in losses:
            continue
        if status not in ("", "echo reply") or row.get("host") != target:
            raise ValueError("Unknown ping result")
        times.append(milliseconds(row["time"]))
    received = len(times)
    if int(rows[-1]["sent"]) != count or int(rows[-1]["received"]) != received:
        raise ValueError("Inconsistent ping summary")
    result = {"probe_ok": 1, "available": int(received > 0), "loss_pct": 100 * (count - received) / count}
    if times:
        result["latency_ms"] = statistics.mean(times)
    if len(times) > 1:
        result["jitter_ms"] = statistics.mean(abs(b - a) for a, b in zip(times, times[1:]))
    return result


class RouterProbe:
    def __init__(self, router, config):
        self.router, self.config = router, config

    async def __call__(self, wan, target):
        # Explicit opt-in is separate from the operator's isolation acceptance.
        if self.config.get("enabled") is not True or self.config.get("isolation_validated") is not True:
            return {"probe_ok": 0}
        try:
            ipaddress.IPv4Address(target)
            interface = wan["interface"]
            if not interface or any(c in interface for c in "%@\r\n\x00"):
                raise ValueError("Ambiguous interface selector")
            before = await self.router.routes()
            if not route_matches(before, target, interface):
                return {"probe_ok": 0}
            body = {"address": target, "count": "5", "interval": "1s"}
            binding = self.config.get("binding", "address-interface")
            if binding == "address-interface":
                body["address"] = target + "%" + interface
            elif binding == "interface-argument":
                body["interface"] = interface
            else:
                raise ValueError("Unsupported binding profile")
            response = await self.router.client.post("ping", json=body)
            response.raise_for_status()
            result = metrics(response.json(), target)
            if not route_matches(await self.router.routes(), target, interface):
                return {"probe_ok": 0}
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
            # Never retry with an unbound address or another WAN.
            return {"probe_ok": 0}
