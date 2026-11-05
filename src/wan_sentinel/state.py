def yes(value):
    return value is True or value == "true"


def connection(snapshot, wan):
    interface = next((r for r in snapshot["interfaces"] if r.get("name") == wan["interface"]), None)
    if interface is None:
        return "unknown"
    if "running" not in interface:
        return "unknown"
    if yes(interface.get("disabled")) or not yes(interface.get("running")):
        return "down"
    if wan["kind"] == "dhcp":
        client = next((r for r in snapshot["dhcp"] if r.get("interface") == wan["interface"]), None)
        if client is None:
            return "unknown"
        if "status" not in client:
            return "unknown"
        return "up" if client.get("status") == "bound" and not yes(client.get("disabled")) else "down"
    if wan["kind"] == "pppoe":
        client = next((r for r in snapshot["pppoe"] if r.get("name") == wan["interface"]), None)
        if client is None:
            return "unknown"
        if "running" not in client:
            return "unknown"
        return "up" if yes(client.get("running")) and not yes(client.get("disabled")) else "down"
    return "up"
