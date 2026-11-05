# MVP validation

## Automated tests and container

- 58 pytest tests passed on Windows and inside the Linux Docker image. RouterOS and InfluxDB boundaries are simulated in the unit tests.
- Coverage includes Ethernet carrier with no DHCP lease and an inactive backup, API errors reported as unknown, separate probe execution and loss states, no retry without interface binding, ECMP and route changes, and mixed RouterOS RTT units.
- The production image built and ran as non-root with all capabilities dropped and a read-only root filesystem.

## RouterOS 7.25 lab

- A dedicated account without write permissions successfully read interfaces, PPPoE, DHCP and IPv4 routes over HTTPS.
- Five interface-bound REST pings succeeded on the primary PPPoE link and on a temporary Ethernet/DHCP uplink. The production parser accepted both response formats. Sanitized fixtures use documentation IP addresses.
- After the temporary uplink was disconnected, the primary check still succeeded and the secondary check blocked before sending a ping because its pinned route was unavailable.
- Hardware acceptance still requires checking actual egress during a mid-probe route change or link failure. The example keeps probes disabled pending local acceptance.

## InfluxDB and Grafana

- A one-shot Docker integration test exported six batches with simulated router inputs to an isolated InfluxDB 2.9.1 bucket. Flux readback confirmed the stored states, RTT, jitter and loss. The read and write tokens were tested separately, including rejection of the opposite operation.
- All 13 dashboard Flux queries returned successfully against synthetic data, including blank current RTT, jitter and loss for a blocked probe. The Grafana dashboard showed the primary link reachable and the secondary link down with its probe blocked. The sample bucket used seven-day retention.
- End-to-end collection from the two production uplinks into InfluxDB is pending. The public image workflow is prepared and has yet to run.
