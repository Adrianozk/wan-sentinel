import argparse
import asyncio
import logging
import signal

import httpx

from .config import load
from .influx import Influx, line
from .probes import probe
from .routeros import RouterOS
from .router_probe import RouterProbe
from .state import connection, yes

log = logging.getLogger("wan_sentinel")


async def collect(router, wans, prober=None):
    if prober is None:
        prober = RouterProbe(router, {})
    try:
        snapshot = await router.snapshot()
    except (httpx.HTTPError, ValueError):
        snapshot = None
        log.warning("RouterOS collection failed; connection state is unknown")
    lines = [line({"scope": "collector"}, {"collection_ok": int(snapshot is not None)})]

    async def one(wan):
        state = connection(snapshot, wan) if snapshot is not None else "unknown"
        fields = {"state_code": {"unknown": -1, "down": 0, "up": 1}[state]}
        if snapshot is not None:
            interface = next((r for r in snapshot["interfaces"] if r.get("name") == wan["interface"]), {})
            for source, field in (("rx-byte", "rx_bytes"), ("tx-byte", "tx_bytes")):
                try:
                    fields[field] = int(interface[source])
                except (KeyError, ValueError, TypeError):
                    pass
        rows = [line({"wan": wan["name"], "scope": "connection", "state": state},
                     fields)]
        for target in wan["targets"]:
            result = await prober(wan, target)
            if not result["probe_ok"]:
                log.warning("Probe failed for WAN %s target %s", wan["name"], target)
            rows.append(line({"wan": wan["name"], "scope": "probe", "target": target}, result))
        return rows

    for rows in await asyncio.gather(*(one(w) for w in wans)):
        lines.extend(rows)
    if snapshot is not None:
        lines.append(line({"scope": "routes"}, {
            "route_count": len(snapshot["routes"]),
            "active_default_routes": sum(yes(r.get("active")) and r.get("dst-address") == "0.0.0.0/0" for r in snapshot["routes"]),
        }))
    return lines


async def serve(config):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    router_config = config["router"]
    async with httpx.AsyncClient(base_url=router_config["url"].rstrip("/") + "/rest/",
        auth=(router_config["username"], router_config["password"]), timeout=10,
        verify=router_config.get("ca_bundle", True)) as client, httpx.AsyncClient(timeout=10) as sink:
        router, influx = RouterOS(client), Influx(sink, config["influx"])
        probe_config = config.get("probes", {})
        backend = probe_config.get("backend", "routeros")
        prober = RouterProbe(router, probe_config) if backend == "routeros" else probe
        if backend == "routeros" and not probe_config.get("enabled", False):
            log.warning("RouterOS probes disabled pending isolation validation")
        while not stop.is_set():
            lines = await collect(router, config["wans"], prober)
            try:
                await influx.write(lines)
            except httpx.HTTPError:
                log.error("Influx export failed; current sample dropped")
            try:
                await asyncio.wait_for(stop.wait(), config.get("interval", 30))
            except asyncio.TimeoutError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/config.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = load(args.config)
    except Exception:
        parser.exit(2, "Invalid configuration; check required fields and environment variables\n")
    asyncio.run(serve(config))
