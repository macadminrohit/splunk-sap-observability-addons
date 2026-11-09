"""Mock SAP UAS (XSUAA token + Resource Consumption API) HTTPS server for testing the add-on.

IMPORTANT: record field names come from SAP's OpenAPI spec (APIUasReportingService,
MonthlyUsageResponseObject / MonthlyCostResponseObject); the values are invented and the
server behaviour is *my reading* of the spec, not a real SAP landscape. Real behaviour
(paging, row counts, rate limits, error bodies) is unverified.
"""
import base64
import json
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

USAGE = [
    {"globalAccountId": "ga-1", "globalAccountName": "Mock GA", "subaccountId": "sa-1",
     "subaccountName": "dev", "serviceId": "hana-cloud", "serviceName": "SAP HANA Cloud",
     "planName": "hana", "metricName": "Capacity Units", "measureId": "capacity_units",
     "reportYearMonth": 202610, "usage": 12.5, "unitPlural": "capacity units",
     "startIsoDate": "2026-10-01", "endIsoDate": "2026-10-31"},
    {"globalAccountId": "ga-1", "globalAccountName": "Mock GA", "subaccountId": "sa-2",
     "subaccountName": "prod", "serviceId": "application-logs", "serviceName": "Application Logging",
     "planName": "lite", "metricName": "Gigabytes", "measureId": "gb", "reportYearMonth": 202610,
     "usage": 3.25, "unitPlural": "GB", "startIsoDate": "2026-10-01", "endIsoDate": "2026-10-31"},
]
COST = [
    {"globalAccountId": "ga-1", "subaccountId": "sa-1", "subaccountName": "dev",
     "serviceId": "hana-cloud", "serviceName": "SAP HANA Cloud", "metricName": "Capacity Units",
     "reportYearMonth": 202610, "usage": 12.5, "cost": 41.0, "currency": "EUR",
     "estimated": True, "cloudCreditsCost": 41.0, "paygCost": 0.0},
]


class Handler(BaseHTTPRequestHandler):
    expected_basic = None     # token endpoint client auth
    issued_token = None       # bearer the reporting API will accept
    cost_status = 200         # set to 404 to simulate a non-CPEA account
    seen = []                 # (method, path, query dict, headers)

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

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        Handler.seen.append(("POST", u.path, form, dict(self.headers)))
        if u.path != "/oauth/token":
            return self._send(404, {})
        if self.headers.get("Authorization") != self.expected_basic:
            return self._send(401, {"error": "unauthorized"})
        if form.get("grant_type") != "client_credentials":
            return self._send(400, {"error": "unsupported_grant_type"})
        return self._send(200, {"access_token": self.issued_token, "token_type": "bearer",
                                "expires_in": 43199})

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        Handler.seen.append(("GET", u.path, q, dict(self.headers)))
        if u.path == "/redirect":
            return self._send(302, {}, {"Location": "https://evil.invalid/"})
        if self.headers.get("Authorization") != "Bearer " + self.issued_token:
            return self._send(401, {"error": "invalid_token"})
        if not (q.get("fromDate", "").isdigit() and q.get("toDate", "").isdigit()):
            return self._send(400, {"error": "fromDate/toDate required"})
        if u.path == "/reports/v1/monthlyUsage":
            return self._send(200, {"content": USAGE})
        if u.path == "/reports/v1/monthlySubaccountsCost":
            if self.cost_status != 200:
                return self._send(self.cost_status, {})
            return self._send(200, {"content": COST})
        return self._send(404, {})


def serve(certfile, keyfile, client_id, client_secret, bearer):
    Handler.expected_basic = "Basic " + base64.b64encode(
        f"{client_id}:{client_secret}".encode()).decode()
    Handler.issued_token = bearer
    Handler.cost_status = 200
    Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]
