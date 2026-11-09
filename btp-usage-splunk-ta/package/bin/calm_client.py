"""Minimal client for the SAP Cloud ALM *Analytics* API (calm-analytics v1).

No Splunk imports, so it can be unit-tested outside Splunk. Reuses the transport and OAuth
client-credentials code (HTTPS only, certificate + hostname verification on, redirects refused,
secrets never logged) from ``uas_client``.

Endpoints and request/response shapes come from SAP's own OpenAPI file ``CALM_ANALYTICS.json``
(SAP Cloud ALM Analytics 1.0.0):
  GET  /api/calm-analytics/v1/analytics/providers          -> list of data providers
  POST /api/calm-analytics/v1/analytics/providers/filters  -> dimensions of one provider
  POST /api/calm-analytics/v1/analytics/providers/data     -> data for one or more queries

What the spec does NOT say (so this module takes it as configuration, not as fact):
  * which providers exist for Health Monitoring, Job & Automation Monitoring and Status Events
    (the spec's own example lists only DEMO_TASKS, DP_TASKS, DP_PROJECTS, CPI_MESSAGES, ...);
  * their dimension and measure names;
  * rate limits, maximum ``limit``, and the unit of the ``unix`` timestamp format.
Run the catalogue input against a real tenant to find out, then configure queries.
"""

import json
import urllib.request

from uas_client import UasAuthError, UasClient, UasError, validate_base_url  # noqa: F401

ANALYTICS_PATH = "/api/calm-analytics/v1/analytics"
ST_PROVIDER = "sap:calm:provider"
ST_DIMENSION = "sap:calm:dimension"
ST_DATA = "sap:calm:analytics"

MAX_PAGES = 20
FORMATS = ("table", "timeseries")
TABLE_TYPES = ("snapshot", "raw", "last")


def parse_queries(text):
    """Parse and sanity-check the admin-supplied JSON list of Query objects."""
    try:
        queries = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Queries must be valid JSON: {exc}") from None
    if not isinstance(queries, list) or not queries:
        raise ValueError("Queries must be a non-empty JSON array")
    for i, q in enumerate(queries):
        where = f"queries[{i}]"
        if not isinstance(q, dict):
            raise ValueError(f"{where} must be an object")
        for req in ("name", "provider"):
            if not isinstance(q.get(req), str) or not q[req].strip():
                raise ValueError(f"{where}.{req} is required")
        cols = q.get("columns")
        if cols is not None:
            if not isinstance(cols, dict):
                raise ValueError(f"{where}.columns must be an object")
            dims = cols.get("dimensions", [])
            mets = cols.get("metrics", [])
            if not (isinstance(dims, list) and all(isinstance(d, str) for d in dims)):
                raise ValueError(f"{where}.columns.dimensions must be a list of strings")
            if not (isinstance(mets, list) and all(
                    isinstance(m, dict) and isinstance(m.get("measure"), str) for m in mets)):
                raise ValueError(f"{where}.columns.metrics must be a list of {{measure[, method]}}")
    return queries


class CalmClient(UasClient):
    """``api_url`` is the tenant host only, e.g. https://<tenant>.<region>.alm.cloud.sap."""

    def __init__(self, token_url, api_url, client_id, client_secret, **kw):
        super().__init__(token_url, api_url, client_id, client_secret, **kw)

    def _call(self, method, path, payload=None):
        if not self._token:
            self.get_token()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"  # spec: 415 if missing
        req = urllib.request.Request(
            self.reporting_base + ANALYTICS_PATH + path, data=data, headers=headers, method=method)
        return self._open(req, ANALYTICS_PATH + path, with_headers=True)

    def list_providers(self):
        body, _ = self._call("GET", "/providers")
        if not isinstance(body, list):
            raise UasError("Unexpected response shape from /providers (not an array)")
        return body

    def provider_dimensions(self, provider, version=None):
        payload = {"providerName": provider}
        if version:
            payload["providerVersion"] = version
        body, _ = self._call("POST", "/providers/filters", payload)
        if not isinstance(body, list):
            raise UasError("Unexpected response shape from /providers/filters (not an array)")
        return body

    def query_data(self, request):
        """POST /providers/data. Returns (list_of_series, total_count_or_None)."""
        body, headers = self._call("POST", "/providers/data", request)
        series = body if isinstance(body, list) else [body]
        total = headers.get("x-total-count")
        return series, (int(total) if total and total.isdigit() else None)


def collect_catalogue(client, account, include_dimensions=True, logger=None):
    """One event per provider (+ one per dimension). Used to discover real provider/field names."""
    out = []
    providers = client.list_providers()
    for p in providers:
        ev = dict(p) if isinstance(p, dict) else {"raw": p}
        ev["calm_account"] = account
        out.append((ST_PROVIDER, ev))
    if not include_dimensions:
        return out
    for p in providers:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        versions = p.get("version") or [None]
        version = versions[-1] if isinstance(versions, list) and versions else None
        try:
            dims = client.provider_dimensions(p["name"], version)
        except UasAuthError:
            raise
        except UasError as exc:
            if logger:
                logger.warning("Skipping dimensions of %s: %s", p["name"], exc)
            continue
        for d in dims:
            if not isinstance(d, dict):
                continue
            ev = {k: v for k, v in d.items() if k != "values"}  # values can be large
            ev["value_count"] = len(d.get("values") or [])
            ev["calm_provider"] = p["name"]
            ev["calm_provider_version"] = version
            ev["calm_account"] = account
            out.append((ST_DIMENSION, ev))
    return out


def _table_events(serie, base):
    cols = [c.get("text", f"col{i}") if isinstance(c, dict) else str(c)
            for i, c in enumerate(serie.get("columns") or [])]
    seen, names = {}, []
    for c in cols:  # make duplicate column names unique rather than silently overwrite
        seen[c] = seen.get(c, 0) + 1
        names.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
    for row in serie.get("rows") or []:
        ev = dict(zip(names, row))
        ev.update(base)
        yield ev


def _timeseries_events(serie, base):
    attrs = {}
    for a in serie.get("attributes") or []:
        if isinstance(a, dict) and "key" in a:
            vals = a.get("values")
            attrs[a["key"]] = vals[0] if isinstance(vals, list) and len(vals) == 1 else vals
    for pt in serie.get("dataPoints") or []:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            ts, val = pt[0], pt[1]
        elif isinstance(pt, dict):
            ts, val = pt.get("x"), pt.get("y")
        else:
            continue
        ev = dict(attrs)
        ev.update({"measure": serie.get("measure"), "group": serie.get("group"),
                   "timestamp_raw": ts, "value": val})
        ev.update(base)
        yield ev


def collect_data(client, account, queries, time_range="L1H", fmt="table", table_type="last",
                 resolution=None, page_size=500, logger=None):
    """Run each query on its own (so paging is unambiguous). Returns [(sourcetype, event)]."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    if table_type not in TABLE_TYPES:
        raise ValueError(f"tableType must be one of {TABLE_TYPES}")
    out = []
    for q in queries:
        base = {"calm_account": account, "calm_query": q["name"], "calm_provider": q["provider"]}
        offset, prev_first = 0, None
        for _page in range(MAX_PAGES):
            req = {"format": fmt, "timeRange": {"semantic": time_range}, "queries": [q]}
            if fmt == "table":
                req.update({"tableType": table_type, "limit": page_size, "offset": offset})
            if resolution:
                req["resolution"] = resolution
            try:
                series, total = client.query_data(req)
            except UasAuthError:
                raise
            except UasError as exc:
                if logger:
                    logger.warning("Skipping query %r: %s", q["name"], exc)
                break
            n_rows = 0
            first = None
            for serie in series:
                if not isinstance(serie, dict):
                    continue
                if serie.get("message"):
                    if logger:
                        logger.warning("Query %r returned message: %s", q["name"], serie["message"])
                    continue
                b = dict(base, calm_serie=serie.get("serieName"))
                evs = list(_table_events(serie, b) if "rows" in serie else _timeseries_events(serie, b))
                if first is None and evs:
                    first = json.dumps(evs[0], sort_keys=True, default=str)
                n_rows += len(evs)
                out.extend((ST_DATA, e) for e in evs)
            offset += n_rows
            if fmt != "table" or n_rows < page_size:
                break
            if total is not None and offset >= total:
                break
            if first is not None and first == prev_first:
                # server ignored offset and re-sent the same page: stop rather than loop/duplicate
                if logger:
                    logger.warning("Query %r: server ignored offset; stopping paging", q["name"])
                out[:] = out[:-n_rows]
                break
            prev_first = first
    return out
