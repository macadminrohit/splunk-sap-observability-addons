"""Mock SAP UAS (XSUAA token + Resource Consumption API) HTTPS server for testing the add-on.

IMPORTANT: record field names come from SAP's OpenAPI spec (APIUasReportingService: MonthlyUsage,
MonthlyCost, MonthlyDirectoryUsage, SubaccountUsage and CloudCreditsDetails response objects); the values are invented and the
server behaviour is *my reading* of the spec, not a real SAP landscape. Real behaviour
(paging, row counts, rate limits, error bodies) is unverified.
"""
import base64
import datetime
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
DIRECTORY = [
    {"globalAccountId": "ga-1", "directoryId": "dir-1", "directoryName": "Finance", "subaccountId": "sa-1",
     "subaccountName": "dev", "serviceId": "hana-cloud", "serviceName": "SAP HANA Cloud",
     "metricName": "Capacity Units", "reportYearMonth": 202610, "usage": 12.5},
]
CREDITS = {
    "globalAccountId": "ga-1", "globalAccountName": "Mock GA",
    "contracts": [
        {"contractStartDate": "2026-01-01", "contractEndDate": "2028-12-31", "currency": "EUR",
         "phases": [
             {"phaseStartDate": "2026-01-01", "phaseEndDate": "2026-12-31",
              "phaseUpdates": [
                  {"balance": 900.0, "cloudCreditsForPhase": 1000.0, "phaseUpdatedOn": "2026-09-30"},
                  {"balance": 850.5, "cloudCreditsForPhase": 1000.0, "phaseUpdatedOn": "2026-10-31"}]},
             {"phaseStartDate": "2027-01-01", "phaseEndDate": "2027-12-31", "phaseUpdates": []}]},
    ],
}
SUBACCOUNT = {
    "sa-1": [{"globalAccountId": "ga-1", "subaccountId": "sa-1", "subaccountName": "dev",
              "serviceId": "hana-cloud", "metricName": "Capacity Units", "usage": 0.4,
              "periodStartDate": 20261028, "periodEndDate": 20261028},
             {"globalAccountId": "ga-1", "subaccountId": "sa-1", "subaccountName": "dev",
              "serviceId": "hana-cloud", "metricName": "Capacity Units", "usage": 0.5,
              "periodStartDate": 20261029, "periodEndDate": 20261029}],
    "sa-2": [{"globalAccountId": "ga-1", "subaccountId": "sa-2", "subaccountName": "prod",
              "serviceId": "application-logs", "metricName": "Gigabytes", "usage": 0.1,
              "periodStartDate": 20261029, "periodEndDate": 20261029}],
}
PERIOD_MAX_DAYS = {"DAY": 123, "WEEK": 366, "MONTH": 10 ** 6}  # SAP: "four months", "one year"


def _days(yyyymmdd):
    return datetime.datetime.strptime(str(yyyymmdd), "%Y%m%d").date()


class Handler(BaseHTTPRequestHandler):
    expected_basic = None     # token endpoint client auth
    issued_token = None       # bearer the reporting API will accept
    cost_status = 200         # set to 404 to simulate a non-CPEA account
    credits_status = 200      # 404 for a subscription-based account
    directory_status = 200    # 403 when the key is a global-account key, not a directory key
    subaccount_status = {}    # subaccountId -> status override
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
        if u.path == "/reports/v1/cloudCreditsDetails":
            if q.get("viewPhases", "CURRENT") not in ("CURRENT", "ALL"):
                return self._send(400, {"error": "viewPhases"})
            if self.credits_status != 200:
                return self._send(self.credits_status, {})
            return self._send(200, CREDITS)
        if u.path == "/reports/v1/subaccountUsage":
            f, t = q.get("fromDate", ""), q.get("toDate", "")
            if not (len(f) == 8 and f.isdigit() and len(t) == 8 and t.isdigit() and q.get("subaccountId")):
                return self._send(400, {"error": "subaccountId and fromDate/toDate (YYYYMMDD) required"})
            per = q.get("periodPerspective")
            if per not in PERIOD_MAX_DAYS:
                return self._send(400, {"error": "periodPerspective"})
            if (_days(t) - _days(f)).days + 1 > PERIOD_MAX_DAYS[per]:
                return self._send(400, {"error": "search period too long"})
            status = self.subaccount_status.get(q["subaccountId"], 200)
            if status != 200:
                return self._send(status, {})
            return self._send(200, {"content": SUBACCOUNT.get(q["subaccountId"], [])})
        f, t = q.get("fromDate", ""), q.get("toDate", "")
        if not (len(f) == 6 and f.isdigit() and len(t) == 6 and t.isdigit()):
            return self._send(400, {"error": "fromDate/toDate (YYYYMM) required"})
        if u.path == "/reports/v1/monthlyUsage":
            return self._send(200, {"content": USAGE})
        if u.path == "/reports/v1/monthlySubaccountsCost":
            if self.cost_status != 200:
                return self._send(self.cost_status, {})
            return self._send(200, {"content": COST})
        if u.path == "/reports/v1/monthlyDirectoryUsage":
            if self.directory_status != 200:
                return self._send(self.directory_status, {})
            return self._send(200, {"content": DIRECTORY})
        return self._send(404, {})


def serve(certfile, keyfile, client_id, client_secret, bearer):
    Handler.expected_basic = "Basic " + base64.b64encode(
        f"{client_id}:{client_secret}".encode()).decode()
    Handler.issued_token = bearer
    Handler.cost_status = 200
    Handler.credits_status = 200
    Handler.directory_status = 200
    Handler.subaccount_status = {}
    Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]
