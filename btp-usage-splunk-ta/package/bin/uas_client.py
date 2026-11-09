"""Minimal client for the SAP Usage Data Management (UAS) "Resource Consumption" API.

Deliberately free of any Splunk imports so it can be unit-tested outside Splunk.

Endpoints and response field names come from SAP's own OpenAPI spec for
``APIUasReportingService`` (Resource Consumption 1.0):
  GET /reports/v1/monthlyUsage          fromDate/toDate = YYYYMM (integer)
  GET /reports/v1/monthlySubaccountsCost fromDate/toDate = YYYYMM (integer)
Both return ``{"content": [ {...record...}, ... ]}``.

Authentication: OAuth 2.0 *client credentials* (the spec also allows the password
grant on every operation; this client never uses it, so no human credential is needed).

Security properties (relevant to Splunk Cloud vetting):
  * HTTPS only. TLS certificate and hostname verification are always on
    (CA bundle = certifi). There is no "skip verification" switch.
  * HTTP redirects are NOT followed, so the client secret / bearer token can never be
    replayed to a host other than the one the admin configured.
  * The client secret and the access token are never logged and never put in exceptions.
"""

import base64
import datetime
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# endpoint key -> (sourcetype, path)
ENDPOINTS = {
    "monthlyUsage": ("sap:btp:uas:monthlyusage", "/reports/v1/monthlyUsage"),
    "monthlySubaccountsCost": ("sap:btp:uas:monthlycost", "/reports/v1/monthlySubaccountsCost"),
}


class UasError(Exception):
    """Any UAS / transport failure. Message never contains credentials or tokens."""


class UasAuthError(UasError):
    """HTTP 401/403 from the token endpoint or the reporting API."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def validate_base_url(url, what="URL"):
    """Return a normalised https base URL (scheme://host[:port]) or raise ValueError."""
    parsed = urllib.parse.urlsplit((url or "").strip())
    if parsed.scheme != "https":
        raise ValueError(f"{what} must use https://")
    if not parsed.hostname:
        raise ValueError(f"{what} has no host")
    if parsed.username or parsed.password:
        raise ValueError(f"Do not embed credentials in the {what}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(f"{what} must be scheme://host[:port] only")
    return "https://" + parsed.netloc


def month_window(now, months_back):
    """(fromDate, toDate) as YYYYMM integers covering ``months_back`` months ending at ``now``.

    months_back=1 -> current month only; 2 -> previous + current (catches late corrections).
    """
    if months_back < 1:
        raise ValueError("months_back must be >= 1")
    total = now.year * 12 + (now.month - 1) - (months_back - 1)
    from_year, from_month = divmod(total, 12)
    return from_year * 100 + (from_month + 1), now.year * 100 + now.month


class UasClient:
    def __init__(self, token_url, reporting_url, client_id, client_secret,
                 ca_file=None, timeout=60, ssl_context=None, logger=None):
        self.token_base = validate_base_url(token_url, "Token URL")
        self.reporting_base = validate_base_url(reporting_url, "Reporting URL")
        self.host = urllib.parse.urlsplit(self.reporting_base).hostname
        self._client_id = client_id
        self._client_secret = client_secret
        self.timeout = timeout
        self.logger = logger
        ctx = ssl_context or ssl.create_default_context(cafile=ca_file)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ctx)
        )
        self._token = None

    def _open(self, req, label, with_headers=False):
        """Return the parsed JSON body (or ``(body, headers)`` when ``with_headers``)."""
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                headers = resp.headers
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise UasAuthError(f"HTTP {exc.code} from {label}") from None
            raise UasError(f"HTTP {exc.code} from {label}") from None
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            # str(exc) carries the reason (e.g. certificate verify failed), never the secret
            raise UasError(f"Transport error calling {label}: {exc}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise UasError(f"Response from {label} exceeded size cap")
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise UasError(f"Non-JSON response from {label}: {exc}") from None
        return (body, headers) if with_headers else body

    def get_token(self):
        """Client-credentials grant against <token_url>/oauth/token (HTTP Basic client auth)."""
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            self.token_base + "/oauth/token",
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
            headers={"Authorization": "Basic " + basic,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            method="POST",
        )
        body = self._open(req, "token endpoint")
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise UasError("Token endpoint response had no access_token")
        self._token = token
        return token

    def get_report(self, path, params):
        if not self._token:
            self.get_token()
        url = self.reporting_base + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"})
        body = self._open(req, path)
        if not isinstance(body, dict) or not isinstance(body.get("content"), list):
            raise UasError(f"Unexpected response shape from {path} (no 'content' array)")
        return body["content"]


def collect(client, account, endpoints, months_back=2, now=None, logger=None):
    """Poll the chosen endpoints once. Returns list of (sourcetype, event_dict).

    A failure on one endpoint is logged and skipped so it does not blank the whole poll,
    except an auth failure, which is re-raised (every endpoint would fail the same way).
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    from_date, to_date = month_window(now, months_back)
    out = []
    for key in endpoints:
        if key not in ENDPOINTS:
            continue
        sourcetype, path = ENDPOINTS[key]
        try:
            records = client.get_report(path, {"fromDate": from_date, "toDate": to_date})
        except UasAuthError:
            raise
        except UasError as exc:
            if logger:
                logger.warning("Skipping %s: %s", key, exc)
            continue
        for rec in records:
            event = dict(rec)
            event["btp_account"] = account  # ours win over any same-named API field
            event["uas_endpoint"] = key
            out.append((sourcetype, event))
    return out
