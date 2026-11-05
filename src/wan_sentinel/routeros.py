import asyncio

PATHS = {
    "interfaces": "interface", "pppoe": "interface/pppoe-client",
    "dhcp": "ip/dhcp-client", "routes": "ip/route",
}
PROPERTIES = {
    "interfaces": "name,running,disabled,type,rx-byte,tx-byte",
    "pppoe": "name,running,disabled,interface",
    "dhcp": "interface,status,disabled,address,gateway",
    "routes": "dst-address,gateway,immediate-gw,active,disabled,routing-table,ecmp",
}


class RouterOS:
    def __init__(self, client):
        self.client = client

    async def routes(self):
        response = await self.client.get(PATHS["routes"], params={".proplist": PROPERTIES["routes"]})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Unexpected route response")
        return rows

    async def snapshot(self):
        async def fetch(key, path):
            response = await self.client.get(path, params={".proplist": PROPERTIES[key]})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Unexpected RouterOS response")
            return key, rows

        return dict(await asyncio.gather(*(fetch(k, v) for k, v in PATHS.items())))
