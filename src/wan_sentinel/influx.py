def escape(value):
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=").replace("\n", "").replace("\r", "")


def line(tags, fields):
    return "wan_sentinel," + ",".join(f"{escape(k)}={escape(v)}" for k, v in sorted(tags.items())) + " " + ",".join(f"{k}={float(v)}" for k, v in sorted(fields.items()))


class Influx:
    def __init__(self, client, config):
        self.client, self.config = client, config

    async def write(self, lines):
        response = await self.client.post(self.config["url"].rstrip("/") + "/api/v2/write",
            params={"org": self.config["org"], "bucket": self.config["bucket"]},
            headers={"Authorization": "Token " + self.config["token"]}, content="\n".join(lines))
        response.raise_for_status()
