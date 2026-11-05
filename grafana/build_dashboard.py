"""Build the portable Grafana dashboard using native panels only."""
import json
from pathlib import Path

DATASOURCE = {"type": "influxdb", "uid": "${DS_INFLUXDB}"}
FRESH = '-${freshness}'


def source(scope, field, start="v.timeRangeStart", wan=False):
    query = (f'from(bucket: "${{bucket}}")\n'
             f'  |> range(start: {start}, stop: v.timeRangeStop)\n'
             f'  |> filter(fn: (r) => r._measurement == "wan_sentinel" and r.scope == "{scope}" and r._field == "{field}")\n')
    if wan:
        query += '  |> filter(fn: (r) => r.wan =~ /^${wan:regex}$/)\n'
    return query


def target(query, reference="A"):
    return {"refId": reference, "query": query, "datasource": DATASOURCE}


def panel(identifier, title, kind, position, queries, unit="short"):
    return {"id": identifier, "title": title, "type": kind, "datasource": DATASOURCE,
            "gridPos": dict(zip(("x", "y", "w", "h"), position)),
            "targets": [target(query, chr(65 + index)) for index, query in enumerate(queries)],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "—", "decimals": 1,
                "color": {"mode": "palette-classic"}, "custom": {"spanNulls": False}}, "overrides": []}}


def value_mapping(values):
    return {"type": "value", "options": {str(value): {"text": text, "color": color, "index": index}
            for index, (value, text, color) in enumerate(values)}}


def stat(identifier, title, position, queries, unit="short"):
    result = panel(identifier, title, "stat", position, queries, unit)
    result["options"] = {"reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
                         "orientation": "auto", "textMode": "value_and_name", "colorMode": "value",
                         "graphMode": "none", "justifyMode": "auto", "wideLayout": False,
                         "text": {"titleSize": 12, "valueSize": 25}}
    return result


def probe_query(field, label, latest=False, reachability=False):
    query = ('import "internal/debug"\n'
             f'from(bucket: "${{bucket}}")\n'
             f'  |> range(start: {FRESH if latest else "v.timeRangeStart"}, stop: v.timeRangeStop)\n'
             '  |> filter(fn: (r) => r._measurement == "wan_sentinel" and r.scope == "probe" and r.wan =~ /^${wan:regex}$/)\n'
             f'  |> filter(fn: (r) => r._field == "probe_ok" or r._field == "{field}")\n'
             '  |> group(columns: ["wan", "target"])\n'
             '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n')
    if latest:
        query += '  |> sort(columns: ["_time"], desc: true) |> limit(n: 1)\n'
    missing = '-1.0' if reachability else 'debug.null(type: "float")'
    query += (f'  |> map(fn: (r) => ({{r with _field: "{label}", _value: '
              f'if exists r.probe_ok and r.probe_ok == 1.0 and exists r.{field} then r.{field} else {missing}}}))\n'
              '  |> keep(columns: ["_time", "_value", "_field", "wan", "target", "_start", "_stop"])\n'
              '  |> group(columns: ["wan", "target", "_field"])\n')
    if not latest:
        query += '  |> aggregateWindow(every: if uint(v: v.windowPeriod) < uint(v: 1m) then 1m else v.windowPeriod, fn: mean, createEmpty: true)\n'
    else:
        query += f'  |> rename(columns: {{_value: "{label}"}})\n'
    return query


collection_query = source("collector", "collection_ok", FRESH) + '  |> group(columns: []) |> last()\n'
collection = stat(1, "RouterOS collection", (0, 0, 8, 3), [collection_query])
collection["fieldConfig"]["defaults"]["mappings"] = [value_mapping([(0, "Collection failed", "red"), (1, "Collection healthy", "green")])]
collection["fieldConfig"]["defaults"]["noValue"] = "No recent sample"

age_query = (source("collector", "collection_ok") + '  |> filter(fn: (r) => r._value == 1.0)\n'
             '  |> group(columns: []) |> last()\n'
             '  |> map(fn: (r) => ({r with _field: "Age", _value: float(v: uint(v: now()) - uint(v: r._time)) / 1000000000.0}))\n')
age = stat(2, "Last successful sample", (8, 0, 8, 3), [age_query], "s")

route_query = ('from(bucket: "${bucket}")\n'
               f'  |> range(start: {FRESH}, stop: v.timeRangeStop)\n'
               '  |> filter(fn: (r) => r._measurement == "wan_sentinel" and '
               '((r.scope == "routes" and r._field == "active_default_routes") or '
               '(r.scope == "collector" and r._field == "collection_ok" and r._value == 0.0)))\n'
               '  |> map(fn: (r) => ({r with _field: "Active defaults", _value: if r.scope == "collector" then -1.0 else r._value}))\n'
               '  |> group(columns: []) |> last()\n')
routes = stat(3, "Active default routes", (16, 0, 8, 3), [route_query])
routes["fieldConfig"]["defaults"]["decimals"] = 0
routes["fieldConfig"]["defaults"]["mappings"] = [value_mapping([(-1, "Unknown", "orange")])]

connection_query = (source("connection", "state_code", FRESH, wan=True)
                    + '  |> group(columns: ["wan"]) |> last()\n'
                    '  |> map(fn: (r) => ({r with _field: "Connection"}))\n'
                    '  |> keep(columns: ["_time", "_value", "_field", "wan"])\n')
connection_query += '  |> rename(columns: {_value: "Connection"})\n'
summary = stat(4, "${wan}", (0, 3, 12, 6), [connection_query,
    probe_query("available", "Reachability", True, True), probe_query("latency_ms", "RTT", True),
    probe_query("jitter_ms", "Jitter", True), probe_query("loss_pct", "Loss", True)])
summary.update({"repeat": "wan", "repeatDirection": "h", "maxPerRow": 2,
                "description": "Connection, target reachability and RTT are independent. Blocked probes leave RTT/jitter/loss blank. Freshness limits old samples."})
summary["fieldConfig"]["defaults"]["displayName"] = "${__field.name}"
summary["fieldConfig"]["overrides"] = [
    {"matcher": {"id": "byName", "options": "Connection"}, "properties": [{"id": "mappings", "value": [value_mapping([(-1, "Unknown", "orange"), (0, "Down", "red"), (1, "Connected", "green")])]}]},
    {"matcher": {"id": "byName", "options": "Reachability"}, "properties": [{"id": "mappings", "value": [value_mapping([(-1, "Probe blocked", "orange"), (0, "Unreachable", "red"), (1, "Reachable", "green")])]}]},
]
for field, unit in (("RTT", "ms"), ("Jitter", "ms"), ("Loss", "percent")):
    summary["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": field},
        "properties": [{"id": "unit", "value": unit}, {"id": "displayName", "value": field + " · ${__field.labels.target}"}]})

quality_metrics = (
    ("latency_ms", "Latency", "ms", "green", "left"),
    ("jitter_ms", "Jitter", "ms", "orange", "left"),
    ("loss_pct", "Packet loss", "percent", "red", "right"),
)
quality = panel(5, "Connection quality · ${wan}", "timeseries", (0, 9, 24, 11),
                [probe_query(field, label) for field, label, *_ in quality_metrics], "ms")
quality.update({"repeat": "wan", "repeatDirection": "v",
                "description": "Latency and jitter use the left axis. Packet loss uses the right axis. Gaps indicate unavailable measurements."})
quality["fieldConfig"]["defaults"].update({"min": 0, "displayName": "${__field.labels._field} · ${__field.labels.target}"})
quality["fieldConfig"]["defaults"]["custom"].update({
    "lineWidth": 1, "fillOpacity": 8, "showPoints": "auto", "pointSize": 3,
})
for reference, (_, label, unit, color, axis) in zip(("A", "B", "C"), quality_metrics):
    quality["fieldConfig"]["overrides"].append({
        "matcher": {"id": "byFrameRefID", "options": reference},
        "properties": [
            {"id": "displayName", "value": label + " · ${__field.labels.target}"},
            {"id": "unit", "value": unit},
            {"id": "color", "value": {"mode": "fixed", "fixedColor": color}},
            {"id": "custom.axisPlacement", "value": axis},
        ],
    })
quality["options"] = {
    "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "min"], "showLegend": True},
    "tooltip": {"mode": "multi", "sort": "none"},
}

history_query = (source("connection", "state_code", wan=True)
                 + '  |> group(columns: ["wan"])\n'
                 '  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n'
                 '  |> keep(columns: ["_time", "_value", "wan"])\n')
history = panel(8, "Connection history", "state-timeline", (0, 20, 24, 5), [history_query])
history["fieldConfig"]["defaults"].update({"displayName": "${__field.labels.wan}", "mappings": [value_mapping([(-1, "Unknown", "orange"), (0, "Down", "red"), (1, "Connected", "green")])]})
history["options"] = {"mergeValues": True, "showValue": "auto", "rowHeight": .8, "alignValue": "left", "legend": {"displayMode": "hidden"}, "tooltip": {"mode": "single"}}

wan_query = ('import "influxdata/influxdb/schema"\n'
             'schema.tagValues(bucket: "${bucket}", tag: "wan", predicate: (r) => r._measurement == "wan_sentinel" and r.scope == "connection")')
dashboard = {
    "__inputs": [{"name": "DS_INFLUXDB", "label": "InfluxDB (Flux)", "type": "datasource", "pluginId": "influxdb", "pluginName": "InfluxDB"}],
    "title": "WAN Sentinel", "uid": "wan-sentinel", "schemaVersion": 39, "version": 1,
    "description": "Read-only multi-WAN observability. Connection, collection and probe states are independent. Null probe metrics remain blank.",
    "tags": ["wan-sentinel", "routeros"], "timezone": "browser", "refresh": "30s", "time": {"from": "now-30m", "to": "now"},
    "templating": {"list": [
        {"name": "bucket", "type": "textbox", "label": "Bucket", "query": "wan-sentinel", "current": {"text": "wan-sentinel", "value": "wan-sentinel"}},
        {"name": "freshness", "type": "textbox", "label": "Max sample age", "query": "90s", "current": {"text": "90s", "value": "90s"}},
        {"name": "wan", "type": "query", "label": "WAN", "datasource": DATASOURCE, "query": wan_query, "definition": wan_query,
         "multi": True, "includeAll": True, "allValue": ".*", "refresh": 1, "sort": 1, "options": [], "current": {"text": "All", "value": "$__all"}},
    ]},
    "panels": [collection, age, routes, summary, quality, history],
}

if __name__ == "__main__":
    Path(__file__).with_name("wan-sentinel.json").write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
