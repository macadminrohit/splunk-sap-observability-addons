"""Mock SAP Cloud ALM (XSUAA token + calm-analytics) HTTPS server for testing the add-on.

IMPORTANT: paths, request/response *shapes* follow SAP's OpenAPI file CALM_ANALYTICS.json.
Provider names, dimensions, measures and values are INVENTED (the spec only lists DEMO_TASKS,
DP_TASKS, ... as examples; it does not document Health/Job/Status-Event providers).
Behaviour (paging, header names, limits, error bodies) is my reading of the spec, not a real tenant.
"""
import base64
import json
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PROVIDERS = [
    {"name": "MOCK_HEALTH", "description": "mock health", "service": "mock-srv", "domain": "Analytics",
     "scope": "calm-api.mock.read", "version": ["v1", "v2"], "plan": "no", "calmMarket": "RUN"},
    {"name": "MOCK_JOBS", "description": "mock jobs", "service": "mock-srv", "domain": "Analytics",
     "scope": "calm-api.mock.read", "version": ["v1"], "plan": "no", "calmMarket": "RUN"},
]
DIMENSIONS = {
    "MOCK_HEALTH": [{"key": "Service", "name": "Service", "description": "svc", "isAttribute": True,
                     "type": "dimension", "values": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                     "isMultiple": False, "triggerRefresh": False, "group": "dimensions_group"}],
}
ROWS = [[f"svc{i}", i * 1.5] for i in range(5)]


class Handler(BaseHTTPRequestHandler):
    expected_basic = None
    issued_token = None
    ignore_offset = False
    data_status = 200
    series_message = ""
    seen = []   # (method, path, body_or_form, headers)

    def log_message(self, *a):
        pass

    def _send(self, code, body, extra=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n).decode()

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        raw = self._body()
        if u.path == "/oauth/token":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            Handler.seen.append(("POST", u.path, form, dict(self.headers)))
            if self.headers.get("Authorization") != self.expected_basic:
                return self._send(401, {"error": "unauthorized"})
            if form.get("grant_type") != "client_credentials":
                return self._send(400, {"error": "unsupported_grant_type"})
            return self._send(200, {"access_token": self.issued_token, "token_type": "bearer"})
        if self.headers.get("Authorization") != "Bearer " + self.issued_token:
            return self._send(401, {"error": {"code": "401", "message": "bad token"}})
        if self.headers.get("Content-Type") != "application/json":
            return self._send(415, {"error": {"code": "415", "message": "Content-Type missing"}})
        body = json.loads(raw)
        Handler.seen.append(("POST", u.path, body, dict(self.headers)))
        base = "/api/calm-analytics/v1/analytics/providers"
        if u.path == base + "/filters":
            return self._send(200, DIMENSIONS.get(body["providerName"], []))
        if u.path == base + "/data":
            if self.data_status != 200:
                return self._send(self.data_status, {"error": {"code": "x", "message": "boom"}})
            q = body["queries"][0]
            if body.get("format") == "timeseries":
                return self._send(200, [{"serieName": q["name"], "group": "g", "measure": "m",
                                         "attributes": [{"key": "Service", "values": ["svc0"]}],
                                         "dataPoints": [[1700000000, 1.0], [1700000060, 2.0]],
                                         "message": self.series_message}])
            off, lim = body.get("offset", 0), body.get("limit", 1000)
            if self.ignore_offset:
                off = 0
            page = ROWS[off:off + lim]
            serie = {"serieName": q["name"], "dimensions": ["Service"], "measures": ["avg"],
                     "columns": [{"text": "Service", "type": "string"}, {"text": "Value", "type": "number"}],
                     "rows": page}
            if self.series_message:
                serie = {"serieName": q["name"], "message": self.series_message}
            return self._send(200, [serie], {"x-total-count": str(len(ROWS))})
        return self._send(404, {})

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        Handler.seen.append(("GET", u.path, {}, dict(self.headers)))
        if u.path == "/redirect":
            return self._send(302, {}, {"Location": "https://evil.invalid/"})
        if self.headers.get("Authorization") != "Bearer " + self.issued_token:
            return self._send(401, {"error": {"code": "401", "message": "bad token"}})
        if u.path == "/api/calm-analytics/v1/analytics/providers":
            return self._send(200, PROVIDERS)
        return self._send(404, {})


def serve(certfile, keyfile, client_id, client_secret, bearer):
    Handler.expected_basic = "Basic " + base64.b64encode(
        f"{client_id}:{client_secret}".encode()).decode()
    Handler.issued_token = bearer
    Handler.ignore_offset = False
    Handler.data_status = 200
    Handler.series_message = ""
    Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
