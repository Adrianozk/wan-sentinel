"""Linux-only probes. Never invoke a shell or change network configuration."""
import asyncio
import json
import os
import re
import statistics


async def command(*args):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE,
                                                  env=dict(os.environ, LC_ALL="C"))
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 15)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return process.returncode, stdout.decode()


async def probe(wan, target, run=command):
    prefix = ("ip", "netns", "exec", wan["namespace"])
    try:
        code, raw = await run(*prefix, "ip", "-j", "link", "show")
        if code:
            raise ValueError("Namespace unavailable")
        links = json.loads(raw)
        if {r["ifname"] for r in links} != {"lo", wan["device"]}:
            raise ValueError("Namespace must have exactly one egress device")
        code, raw = await run(*prefix, "ip", "-j", "-4", "route", "show", "table", "all")
        if code:
            raise ValueError("Cannot validate routes")
        routes = json.loads(raw)
        if any(r.get("nexthops") or r.get("dev") not in (None, "lo", wan["device"]) for r in routes):
            raise ValueError("Ambiguous egress")
        code, raw = await run(*prefix, "ping", "-n", "-I", wan["device"], "-c", "5", "-i", "0.2", "-W", "1", target)
        if code not in (0, 1):
            raise ValueError("Ping execution failed")
        summary = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", raw)
        if not summary or int(summary[1]) != 5:
            raise ValueError("Incomplete probe")
        sent, received = map(int, summary.groups())
        times = [float(t) for t in re.findall(r"time[=<]([\d.]+) ms", raw)]
        if len(times) != received or received > sent:
            raise ValueError("Invalid probe replies")
        result = {"probe_ok": 1, "available": int(received > 0), "loss_pct": 100 * (sent - received) / sent}
        if times:
            result["latency_ms"] = statistics.mean(times)
        if len(times) > 1:
            result["jitter_ms"] = statistics.mean(abs(b - a) for a, b in zip(times, times[1:]))
        return result
    except (OSError, ValueError, KeyError, TypeError, asyncio.TimeoutError):
        return {"probe_ok": 0}
