import ipaddress
import os
import re
from pathlib import Path

import yaml


def load(path):
    def substitute(match):
        return os.environ[match[1]]

    def expand(value):
        if isinstance(value, str):
            return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", substitute, value)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value

    data = expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    if not data["router"]["url"].startswith("https://"):
        raise ValueError("RouterOS requires HTTPS")
    if data.get("interval", 30) < 1:
        raise ValueError("interval must be positive")
    if data["router"].get("ca_bundle") is False:
        raise ValueError("TLS verification cannot be disabled")
    names, namespaces = set(), set()
    probes = data.get("probes", {})
    backend = probes.get("backend", "routeros")
    if backend not in ("routeros", "linux-namespace"):
        raise ValueError("Unknown probe backend")
    if backend == "routeros":
        if type(probes.get("enabled", False)) is not bool or type(probes.get("isolation_validated", False)) is not bool:
            raise ValueError("Probe switches must be booleans")
        if probes.get("binding", "address-interface") not in ("address-interface", "interface-argument"):
            raise ValueError("Unknown RouterOS binding profile")
        if probes.get("enabled") is True and probes.get("isolation_validated") is not True:
            raise ValueError("Validate isolation before enabling RouterOS probes")
    for wan in data["wans"]:
        # RouterOS names are exact-match data, not Linux command identifiers.
        interface = wan["interface"]
        if not isinstance(interface, str) or not interface.strip() or not interface.isprintable():
            raise ValueError("Invalid RouterOS interface name")
        keys = ("name", "namespace", "device") if backend == "linux-namespace" else ("name",)
        for key in keys:
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+", wan[key]) or wan[key].startswith("-"):
                raise ValueError("Invalid identifier")
        if wan["name"] in names or (backend == "linux-namespace" and wan["namespace"] in namespaces):
            raise ValueError("WAN names and namespaces must be unique")
        names.add(wan["name"])
        if backend == "linux-namespace":
            namespaces.add(wan["namespace"])
        if wan["kind"] not in ("dhcp", "pppoe", "static"):
            raise ValueError("Invalid WAN kind")
        if not wan["targets"]:
            raise ValueError("At least one target required")
        for target in wan["targets"]:
            if ipaddress.ip_address(target).version != 4:
                raise ValueError("MVP probes require IPv4 literals")
    return data
