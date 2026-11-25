"""Minimal client for the SAP Usage Data Management (UAS) "Resource Consumption" API.

Deliberately free of any Splunk imports so it can be unit-tested outside Splunk.

Endpoints and response field names come from SAP's own OpenAPI spec for
``APIUasReportingService`` (Resource Consumption 1.0):
  GET /reports/v1/monthlyUsage           fromDate/toDate = YYYYMM (integer)
  GET /reports/v1/monthlySubaccountsCost fromDate/toDate = YYYYMM (integer)
  GET /reports/v1/monthlyDirectoryUsage  fromDate/toDate = YYYYMM (integer); scope Directory_Admin
  GET /reports/v1/subaccountUsage        subaccountId, fromDate/toDate = YYYYMMDD (integer),
                                         periodPerspective = DAY (max 4 months) | WEEK (max 1 year) | MONTH
These return ``{"content": [ {...record...}, ... ]}``.
  GET /reports/v1/cloudCreditsDetails    viewPhases = CURRENT | ALL
returns ``{"globalAccountId", "globalAccountName", "contracts": [{..., "phases": [{..., "phaseUpdates": [...]}]}]}``,
which is flattened to one record per phase update.

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
    "monthlyDirectoryUsage": ("sap:btp:uas:monthlydirectoryusage", "/reports/v1/monthlyDirectoryUsage"),
    "cloudCreditsDetails": ("sap:btp:uas:cloudcredits", "/reports/v1/cloudCreditsDetails"),
    "subaccountUsage": ("sap:btp:uas:subaccountusage", "/reports/v1/subaccountUsage"),
}
MONTHLY_ENDPOINTS = ("monthlyUsage", "monthlySubaccountsCost", "monthlyDirectoryUsage")

# SAP's limits on the subaccountUsage search period, per periodPerspective ("four months" -> 120 days)
PERIOD_MAX_DAYS = {"DAY": 120, "WEEK": 365, "MONTH": 3660}
VIEW_PHASES = ("CURRENT", "ALL")

SCOPE_HINT = {
    "monthlyDirectoryUsage": "needs a service key created in a directory (scope Directory_Admin)",
}


class UasError(Exception):
    """Any UAS / transport failure. Message never contains credentials or tokens."""


class UasAuthError(UasError):
    """HTTP 401/403 from the token endpoint or the reporting API."""

    def __init__(self, message, status=None, from_token_endpoint=False):
        super().__init__(message)
        self.status = status
        self.from_token_endpoint = from_token_endpoint


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


def day_window(now, days_back, perspective):
    """(fromDate, toDate, clamped) as YYYYMMDD integers covering ``days_back`` days ending today.

    ``days_back`` is cut to SAP's maximum search period for ``perspective``; ``clamped`` says so.
    """
    if days_back < 1:
        raise ValueError("days_back must be >= 1")
    if perspective not in PERIOD_MAX_DAYS:
        raise ValueError("periodPerspective must be DAY, WEEK or MONTH")
    limit = PERIOD_MAX_DAYS[perspective]
    days = min(days_back, limit)
    to_day = now.date()
    from_day = to_day - datetime.timedelta(days=days - 1)
    as_int = lambda d: d.year * 10000 + d.month * 100 + d.day  # noqa: E731
    return as_int(from_day), as_int(to_day), days_back > limit


def flatten_credits(body):
    """One record per phase update, carrying its contract and phase dates.

    A contract with no phases, or a phase with no updates, still yields one record so it is visible.
    """
    if not isinstance(body, dict) or not isinstance(body.get("contracts"), list):
        raise UasError("Unexpected response shape from cloudCreditsDetails (no 'contracts' array)")
    base = {"globalAccountId": body.get("globalAccountId"),
            "globalAccountName": body.get("globalAccountName")}
    out = []
    for contract in body["contracts"]:
        if not isinstance(contract, dict):
            continue
        c = dict(base, contractStartDate=contract.get("contractStartDate"),
                 contractEndDate=contract.get("contractEndDate"), currency=contract.get("currency"))
        phases = [p for p in (contract.get("phases") or []) if isinstance(p, dict)]
        if not phases:
            out.append(c)
            continue
        for phase in phases:
            p = dict(c, phaseStartDate=phase.get("phaseStartDate"), phaseEndDate=phase.get("phaseEndDate"))
            updates = [u for u in (phase.get("phaseUpdates") or []) if isinstance(u, dict)]
            if not updates:
                out.append(p)
                continue
            for u in updates:
                out.append(dict(p, balance=u.get("balance"),
                                cloudCreditsForPhase=u.get("cloudCreditsForPhase"),
                                phaseUpdatedOn=u.get("phaseUpdatedOn")))
    return out


def event_time(sourcetype, event):
    """Epoch seconds (UTC midnight) of a subaccount-usage period start, else None (index time)."""
    if sourcetype != ENDPOINTS["subaccountUsage"][0]:
        return None
    try:
        d = datetime.datetime.strptime(str(int(event.get("periodStartDate"))), "%Y%m%d")
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=datetime.timezone.utc).timestamp()


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
                raise UasAuthError(f"HTTP {exc.code} from {label}", status=exc.code,
                                   from_token_endpoint=(label == "token endpoint")) from None
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

    def get_json(self, path, params):
        if not self._token:
            self.get_token()
        url = self.reporting_base + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"})
        return self._open(req, path)

    def get_report(self, path, params):
        body = self.get_json(path, params)
        if not isinstance(body, dict) or not isinstance(body.get("content"), list):
            raise UasError(f"Unexpected response shape from {path} (no 'content' array)")
        return body["content"]


def _is_fatal(exc):
    """A bad credential stops the poll; a 403 on one report only means that key lacks its scope."""
    return exc.from_token_endpoint or exc.status != 403


def collect(client, account, endpoints, months_back=2, now=None, logger=None,
            credits_view_phases="CURRENT", subaccount_ids=None, subaccount_period="DAY", days_back=3):
    """Poll the chosen endpoints once. Returns list of (sourcetype, event_dict).

    A failure on one endpoint is logged and skipped so it does not blank the whole poll. A 401, or
    any auth failure at the token endpoint, is re-raised because every endpoint would fail the same way.
    ``subaccountUsage`` polls each of ``subaccount_ids``; when none are given they are taken from
    this poll's ``monthlyUsage`` records, fetching them if ``monthlyUsage`` is not selected.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    from_month, to_month = month_window(now, months_back)
    if credits_view_phases not in VIEW_PHASES:
        raise ValueError("viewPhases must be CURRENT or ALL")
    usage_cache = {}

    def monthly(key):
        if key not in usage_cache:
            usage_cache[key] = client.get_report(ENDPOINTS[key][1], {"fromDate": from_month, "toDate": to_month})
        return usage_cache[key]

    def subaccount_usage():
        ids = [s for s in (subaccount_ids or []) if s]
        if not ids:
            ids = sorted({r.get("subaccountId") for r in monthly("monthlyUsage") if r.get("subaccountId")})
        if not ids:
            if logger:
                logger.warning("subaccountUsage: no subaccount IDs configured or found in monthlyUsage")
            return []
        from_day, to_day, clamped = day_window(now, days_back, subaccount_period)
        if clamped and logger:
            logger.warning("subaccountUsage: %s days exceeds SAP's limit for %s; fetching %s days",
                           days_back, subaccount_period, PERIOD_MAX_DAYS[subaccount_period])
        records = []
        for sid in ids:
            try:
                records.extend(client.get_report(ENDPOINTS["subaccountUsage"][1], {
                    "subaccountId": sid, "fromDate": from_day, "toDate": to_day,
                    "periodPerspective": subaccount_period}))
            except UasAuthError as exc:
                if _is_fatal(exc):
                    raise
                if logger:
                    logger.warning("Skipping subaccountUsage for %s: %s", sid, exc)
            except UasError as exc:
                if logger:
                    logger.warning("Skipping subaccountUsage for %s: %s", sid, exc)
        return records

    out = []
    for key in endpoints:
        if key not in ENDPOINTS:
            continue
        sourcetype, path = ENDPOINTS[key]
        try:
            if key in MONTHLY_ENDPOINTS:
                records = monthly(key)
            elif key == "cloudCreditsDetails":
                records = flatten_credits(client.get_json(path, {"viewPhases": credits_view_phases}))
            else:
                records = subaccount_usage()
        except UasAuthError as exc:
            if _is_fatal(exc):
                raise
            if logger:
                hint = SCOPE_HINT.get(key, "the service key lacks the scope for this report")
                logger.warning("Skipping %s: %s (%s)", key, exc, hint)
            continue
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
