"""A stand-in for api.telegram.org, run as its own container in the Docker
smoke test. Standard library only, so it runs on the service image.

It accepts any Bot API method for the expected token, answers like Telegram
does, and prints one line per call so the test can see what arrived.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "424242424:DOCKERsmokeDOCKERsmokeDOCKERsmokeDOCKER"
CHAT_ID = -1009876543210


class Handler(BaseHTTPRequestHandler):
    counter = 100

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        prefix = f"/bot{TOKEN}/"
        if not self.path.startswith(prefix):
            self._reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
            return
        method = self.path.removeprefix(prefix)
        Handler.counter += 1
        kind = self.headers.get("content-type", "").split(";")[0]
        print(f"FAKE-TELEGRAM {method} {kind} {len(body)} bytes", flush=True)
        if kind == "application/json":
            print(f"FAKE-TELEGRAM-BODY {body.decode()}", flush=True)
        message = {
            "message_id": Handler.counter,
            "date": 1_800_000_000,
            "chat": {"id": CHAT_ID, "type": "channel", "title": "Smoke"},
        }
        result = True if method.startswith("delete") else message
        self._reply(200, {"ok": True, "result": result})

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104
