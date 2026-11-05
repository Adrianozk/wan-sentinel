"""Optional live InfluxDB check. Uses synthetic RouterOS/probe data only.

Run in the built image with read-only token files at /run/secrets/influx-write
and /run/secrets/influx-read. Never prints credentials or HTTP response bodies.
"""
import asyncio
import csv
import io
import json
import os
from pathlib import Path
import uuid

import httpx

from wan_sentinel.influx import Influx
from wan_sentinel.main import collect


class SimulatedRouter:
    async def snapshot(self):
        return {
            "interfaces": [
                {"name": "pppoe-primary", "running": "true", "rx-byte": "1234", "tx-byte": "5678"},
                {"name": "ether-secondary", "running": "true"},
            ],
            "pppoe": [{"name": "pppoe-primary", "running": "true"}],
            "dhcp": [{"interface": "ether-secondary", "status": "searching"}],
            "routes": [{"dst-address": "0.0.0.0/0", "active": "true"}],
        }


async def simulated_probe(wan, target):
    if wan["name"] == "primary":
        return {"probe_ok": 1, "available": 1, "latency_ms": 3.6412, "jitter_ms": .20025, "loss_pct": 0}
    return {"probe_ok": 0}


async def check():
    url = os.environ["INFLUX_URL"].rstrip("/")
    org = os.environ["INFLUX_ORG"]
    bucket = os.environ["INFLUX_BUCKET"]
    write_token = Path("/run/secrets/influx-write").read_text().strip()
    read_token = Path("/run/secrets/influx-read").read_text().strip()
    if not write_token or not read_token or any(c.isspace() for c in write_token + read_token):
        raise ValueError("Token files must contain only a token")
    run_id = uuid.uuid4().hex
    wans = [
        {"name": "primary", "interface": "pppoe-primary", "kind": "pppoe", "targets": ["198.51.100.10"]},
        {"name": "secondary", "interface": "ether-secondary", "kind": "dhcp", "targets": ["198.51.100.10"]},
    ]
    samples = await collect(SimulatedRouter(), wans, simulated_probe)
    samples = [sample.replace("wan_sentinel,", "wan_sentinel,source=simulated,run=" + run_id + ",", 1) for sample in samples]
    query = (
        "from(bucket: " + json.dumps(bucket) + ") |> range(start: -15m) "
        '|> filter(fn: (r) => r._measurement == "wan_sentinel" and r.run == "' + run_id + '") '
        '|> group(columns: []) |> keep(columns: ["_field", "_value", "scope", "wan", "state"])'
    )
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        await Influx(client, {"url": url, "org": org, "bucket": bucket, "token": write_token}).write(samples)
        print("PASS: production collector/exporter wrote six synthetic samples")
        response = await client.post(url + "/api/v2/query", params={"org": org},
            headers={"Authorization": "Token " + read_token, "Accept": "application/csv"},
            json={"query": query, "type": "flux"})
        response.raise_for_status()
        rows = list(csv.DictReader(io.StringIO("\n".join(line for line in response.text.splitlines() if line and not line.startswith("#")))))
        values = {(row.get("scope"), row.get("wan", ""), row.get("_field")): float(row["_value"])
                  for row in rows if row.get("_value")}
        expected = {
            ("collector", "", "collection_ok"): 1,
            ("connection", "primary", "state_code"): 1,
            ("connection", "secondary", "state_code"): 0,
            ("probe", "primary", "latency_ms"): 3.6412,
            ("probe", "primary", "jitter_ms"): .20025,
            ("probe", "primary", "loss_pct"): 0,
            ("probe", "secondary", "probe_ok"): 0,
        }
        if any(values.get(key) != value for key, value in expected.items()):
            raise ValueError("Stored values did not match the synthetic sample")
        if ("probe", "secondary", "latency_ms") in values or ("probe", "secondary", "loss_pct") in values:
            raise ValueError("Blocked probe unexpectedly has reachability metrics")
        print("PASS: Flux read verifies connection, RTT, jitter, loss and omitted blocked-probe metrics")
        writer_read = await client.post(url + "/api/v2/query", params={"org": org},
            headers={"Authorization": "Token " + write_token}, json={"query": query, "type": "flux"})
        # Flux can hide a bucket from tokens without read access as 404.
        # The same bucket was positively verified with the read token above.
        hidden_bucket = False
        if writer_read.status_code == 404:
            try:
                error = writer_read.json()
                hidden_bucket = error.get("code") == "not found" and "could not find bucket" in error.get("message", "").lower()
            except ValueError:
                pass
        if writer_read.status_code not in (401, 403) and not hidden_bucket:
            raise ValueError("Write token unexpectedly has read permission")
        reader_write = await client.post(url + "/api/v2/write", params={"org": org, "bucket": bucket},
            headers={"Authorization": "Token " + read_token}, content=samples[0])
        if reader_write.status_code not in (401, 403):
            raise ValueError("Read token unexpectedly has write permission")
        print("PASS: read/write permissions are separate")
    print("INTEGRATION PASSED: test bucket only; no router contacted; run=" + run_id)


if __name__ == "__main__":
    try:
        asyncio.run(check())
    except (httpx.HTTPError, ValueError, OSError, KeyError) as error:
        print("INTEGRATION FAILED (details and credentials suppressed): " + type(error).__name__)
        raise SystemExit(1)
