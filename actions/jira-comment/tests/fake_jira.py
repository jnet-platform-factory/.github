"""A small in-memory Jira Cloud stand-in for the jira-comment tests and self-test.

Implements just the endpoints jira_comment.py calls. Run it directly to serve
on a port (used by the self-test workflow):

    python3 fake_jira.py 8765 [--no-expand]
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ME = "cicd-account"
OTHER = "human-account"


class State:
    def __init__(self, expand_supported: bool = True):
        self.expand_supported = expand_supported
        self.fail_all = False
        self.put_404 = False
        self.comments: dict[str, list[dict]] = {}
        self.next_id = 10000
        self.statuses: dict[str, str] = {}
        self.transitions = {"In Progress": "11", "Testing": "21", "Done": "31"}
        self.log: list[str] = []
        self.clock = 0

    def add(self, key: str, body: dict, author: str = ME) -> dict:
        self.next_id += 1
        self.clock += 1
        c = {
            "id": str(self.next_id),
            "author": {"accountId": author},
            "body": body,
            "created": f"2026-10-05T10:{self.clock:02d}:00.000+0000",
            "properties": [],
        }
        self.comments.setdefault(key, []).append(c)
        return c

    def find(self, cid: str) -> dict | None:
        for comments in self.comments.values():
            for c in comments:
                if c["id"] == cid:
                    return c
        return None


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output quiet
            pass

        def _send(self, code: int, payload: object = None):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"null")

        def _route(self, method: str):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path = url.path
            state.log.append(f"{method} {path}" + (f"?{url.query}" if url.query else ""))
            if state.fail_all:
                return self._send(500, {"errorMessages": ["boom"]})
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self._send(401, {"errorMessages": ["no auth"]})

            if method == "GET" and path == "/rest/api/3/myself":
                return self._send(200, {"accountId": ME})

            m = re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)/comment", path)
            if m and method == "GET":
                comments = list(state.comments.get(m.group(1), []))
                if q.get("orderBy") == ["-created"]:
                    comments.reverse()
                expand = "properties" in (q.get("expand") or [""])[0]
                out = []
                for c in comments:
                    c2 = {k: v for k, v in c.items() if k != "properties"}
                    if expand and state.expand_supported:
                        c2["properties"] = c["properties"]
                    out.append(c2)
                return self._send(200, {"comments": out, "total": len(out)})
            if m and method == "POST":
                c = state.add(m.group(1), self._body()["body"])
                return self._send(201, {k: v for k, v in c.items() if k != "properties"})

            m = re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)/comment/(\d+)", path)
            if m and method == "PUT":
                c = next((c for c in state.comments.get(m.group(1), []) if c["id"] == m.group(2)), None)
                if not c or state.put_404:
                    return self._send(404, {"errorMessages": ["comment not found"]})
                c["body"] = self._body()["body"]
                c["notifyUsers"] = (q.get("notifyUsers") or ["true"])[0]
                return self._send(200, {k: v for k, v in c.items() if k != "properties"})

            m = re.fullmatch(r"/rest/api/3/comment/(\d+)/properties/([\w.-]+)", path)
            if m and method == "PUT":
                c = state.find(m.group(1))
                if not c:
                    return self._send(404, {"errorMessages": ["comment not found"]})
                value = self._body()
                c["properties"] = [p for p in c["properties"] if p["key"] != m.group(2)]
                c["properties"].append({"key": m.group(2), "value": value})
                return self._send(200)

            if path == "/rest/api/3/comment/list" and method == "POST":
                ids = {str(i) for i in self._body()["ids"]}
                values = [c for cs in state.comments.values() for c in cs if c["id"] in ids]
                return self._send(200, {"values": values, "total": len(values)})

            m = re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)/transitions", path)
            if m and method == "GET":
                ts = [{"id": tid, "to": {"name": name}} for name, tid in state.transitions.items()]
                return self._send(200, {"transitions": ts})
            if m and method == "POST":
                tid = self._body()["transition"]["id"]
                name = next(n for n, i in state.transitions.items() if i == tid)
                state.statuses[m.group(1)] = name
                return self._send(204)

            return self._send(404, {"errorMessages": [f"no route {method} {path}"]})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return Handler


def serve(state: State, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    st = State(expand_supported="--no-expand" not in sys.argv)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st))
    print(f"fake Jira on http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()
