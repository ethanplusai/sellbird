"""Serve Sellbird's dashboard so this cloud session's preview pane can show it.

CLOUD CONTAINER ONLY. On a normal computer run `sellbird dashboard` instead.

Sellbird's dashboard answers only requests addressed to localhost, a guard
against DNS rebinding and cross-site requests. The preview pane reaches this
machine through a forwarder that uses another address, so every request would
be refused. This wrapper rewrites the Host header (and Origin, when present) to
the loopback address before the dashboard sees the request. That switches the
dashboard's address and origin checks off for this port. Everything else is
untouched, including the sign-in token: every API call still needs it.

The token is created in data/dashboard-token and never printed.
Start: nixafin/dashboard.sh    Stop: nixafin/dashboard.sh stop
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("NIXAFIN_DASHBOARD_PORT", "5555"))
LOOPBACK = f"127.0.0.1:{PORT}".encode()

from sellbird import dashboard  # noqa: E402
from sellbird import dashboard_instance  # noqa: E402


async def preview_app(scope, receive, send):
    if scope["type"] in ("http", "websocket"):
        headers = []
        for name, value in scope["headers"]:
            if name == b"host":
                value = LOOPBACK
            elif name == b"origin":
                value = b"http://" + LOOPBACK
            headers.append((name, value))
        scope = {**scope, "headers": headers}
    await dashboard.app(scope, receive, send)


def main():
    import uvicorn

    dashboard.token_store.get()        # creates data/dashboard-token if needed; not printed
    dashboard_instance.write(dashboard.DB_PATH.parent, host="127.0.0.1", port=PORT,
                             instance=dashboard.INSTANCE_ID)
    try:
        uvicorn.run(preview_app, host="127.0.0.1", port=PORT, log_level="warning",
                    proxy_headers=False, server_header=False, date_header=False)
    finally:
        dashboard_instance.remove(dashboard.DB_PATH.parent, instance=dashboard.INSTANCE_ID)


if __name__ == "__main__":
    main()
