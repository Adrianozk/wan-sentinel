# WAN Sentinel

An open source, Docker-first, asynchronous Python MVP for multi-WAN observability on MikroTik RouterOS 7. Provider names and failover priorities are configurable. MIT licensed. Router-originated probes require per-installation isolation validation.

## Architecture

`YAML + environment → RouterOS HTTPS snapshot → connection evaluator`

`optional RouterOS-originated ICMP → route guards + explicit interface binding`

`connection, collection and probe samples → InfluxDB 2.x → Grafana`

The default container uses an ordinary Docker bridge, a non-root user and no Linux capabilities. No dedicated server NIC, managed switch, host networking or namespaces are required. Router probes are disabled by default; collection remains available.

Modules: `config`, `routeros`, `state`, `router_probe`, `influx`, and `main`. WANs execute concurrently; targets within a WAN run sequentially. The configured interval is a pause after collection/export, not a fixed wall-clock rate. HTTP/subprocess boundaries are injectable for simulated tests.

## Permissions and configuration safety

Snapshot collection only issues GET requests for interfaces, PPPoE clients, DHCP clients and IPv4 routes, with restricted property lists. A dedicated account needs `read,api,rest-api`. Router-originated ping also needs `test`. No `write`, `sensitive`, `password`, `policy` or `reboot` permission is needed. The diagnostic POST does not change configuration. Sentinel never creates routes, failover rules, users, firewall rules or interfaces.

Use HTTPS restricted to the collector management address and a trusted certificate or mounted `router.ca_bundle`. Verification cannot be disabled. Account/service/certificate provisioning is an operator task. Secrets belong in local `.env` or secret-injection tooling, never the repository. Environment substitution happens after YAML parsing. RouterOS interface names preserve spaces and Unicode.

The collector uses `INFLUX_WRITE_TOKEN`; a separate `INFLUX_READ_TOKEN` is used only for integration validation/Grafana setup. The collector does not require read access to InfluxDB. Tokens should be scoped to the destination bucket.

## Router probes: validate isolation before enabling

The experimental backend sends only bounded `POST /rest/ping` requests. Two explicit syntax profiles are available: `address-interface` uses `IPv4%interface`; `interface-argument` sends a separate `interface` argument. Confirm support on the exact RouterOS version. There is no automatic profile change or unbound retry.

Before and after ping, the most-specific active IPv4 route in `main` must uniquely resolve through the expected interface. Missing `immediate-gw`, another egress, ECMP, malformed replies, permission errors or route changes produce `probe_ok=0`, with reachability statistics omitted. These checks are conservative: they cannot prove forwarding behavior between snapshots.

**A real isolation acceptance test remains mandatory.** Check each WAN during link failure, PPPoE reconnect/DHCP renewal and loss of target-specific routes, observing actual egress. Confirm that the other WAN never carries those probes. Shared defaults with failover are insufficient evidence. Any required pinned routes or other isolation configuration must be separately reviewed and provisioned; Sentinel never creates them. VRF/policy-table probing is not implemented; a policy table must not be assumed to be a VRF.

Keep `probes.enabled: false` and `probes.isolation_validated: false` until acceptance passes. Then explicitly enable both. The flag records operator acceptance. The `address-interface` syntax and response parser were checked on RouterOS 7.25 with PPPoE and a temporary Ethernet/DHCP uplink. Disconnecting that uplink blocked its probe before transmission. Acceptance still requires observing actual egress during failures, including route changes during a probe. Tests include sanitized response fixtures with documentation addresses.

The previous namespace backend remains in source for specialized deployments using `backend: linux-namespace` and WAN `namespace`/`device` fields. It requires a custom image with `iproute2`/`iputils-ping`, namespace mounts, suitable privileges and actual upstream confinement. The standard image/Compose does not supply this runtime. Namespaces sharing an ordinary LAN with router failover do not establish WAN isolation.

## Development and Docker setup

Python 3.11+:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest -q
```

After the first public image is available:

1. Prepare the HTTPS account and an existing InfluxDB 2.x organization/bucket with a bucket-scoped write token.
2. Copy `.env.example` to `.env` and `config.example.yaml` to `config.yaml`. Set local endpoints, credentials and exact interface names. Leave probes disabled pending isolation validation.
3. Ensure the Influx endpoint is reachable from the container. Service names resolve only on a shared Docker network; otherwise use a reachable host endpoint.
4. For a private CA, set `router.ca_bundle: /config/router-ca.pem` and add a read-only bind mount for that file under the service's `volumes`. Ensure UID 65532 can read the configuration and CA files.
5. Run `docker compose pull` and `docker compose up -d`.
6. Import `grafana/wan-sentinel.json`, select a Flux datasource and bucket. The dashboard includes collection, freshness and route stats, repeating WAN summaries, a connection-quality chart per WAN and connection history. The chart shares a millisecond axis for latency and jitter and uses a separate percentage axis for packet loss. Its legend shows mean, maximum and minimum per series. The dashboard discovers WAN names from the bucket. Set `Max sample age` to at least a few polling cycles (default 90 seconds). Recent-value panels become empty when samples expire.

To regenerate the portable dashboard, run `python grafana/build_dashboard.py`. Current probe values are selected from the latest complete probe record, so blocked probes mask older RTT/jitter/loss values with nulls. Historical charts preserve gaps rather than filling them with healthy values. Installing the dashboard does not start a collector.

Compose pulls `ghcr.io/adrianozk/wan-sentinel:latest`. To pin a release, replace `latest` with its version tag. The release workflow tests the project before publishing the release tag and `latest` on a stable GitHub release. Set the GHCR package visibility to public on its first release and verify an anonymous pull.

For development before the first release, build and use the same local tag with `docker build -t ghcr.io/adrianozk/wan-sentinel:latest .` and `docker compose up --pull never -d`.

## Metrics

Measurement: `wan_sentinel`. No previous health values are carried forward.

| Scope | Fields | Meaning |
| --- | --- | --- |
| collector | collection_ok | 1 only when all four snapshot reads succeed |
| connection | state_code, rx_bytes, tx_bytes | -1 unknown, 0 down, 1 up; counters when present |
| routes | route_count, active_default_routes | Router-wide IPv4 counts; no guessed recursive-route ownership |
| probe | probe_ok, available, loss_pct, latency_ms, jitter_ms | Per WAN/target; reachability statistics absent on disabled/error probes |

Carrier without DHCP lease is down. PPPoE is evaluated independently of an unused DHCP client on its physical port. Collection failure means unknown, never down. Ping results and connection state remain independent. Five timeouts mean 100% loss; disabled probes or invalid isolation are probe errors, not 100% loss.

Five packets are sent per target. Latency is mean successful RTT. Jitter is mean absolute difference between consecutive received RTTs, not RFC 3550 jitter; it is absent with fewer than two replies. Latency is absent with zero replies. Use multiple targets: ICMP filtering alone does not establish ISP failure.

Influx assigns receipt timestamps. Snapshot reads are concurrent, not atomic. Export failures drop the current sample and log a sanitized error; there is no persistent queue. Grafana does not fill gaps. A stopped collector cannot report itself: configure external no-data alerts and monitor export logs.

See [validation results](VALIDATION.md) for the tested configurations and remaining hardware acceptance work.

References: [REST API and permissions](https://manual.mikrotik.com/docs/developer-guides/rest-api/), [Ping CLI](https://manual.mikrotik.com/docs/cli-reference/tool/ping/), [Ping behavior and VRF distinction](https://manual.mikrotik.com/docs/diagnostics-monitoring-and-troubleshooting/ping/).

