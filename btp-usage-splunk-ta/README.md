# SAP BTP Usage + Cloud ALM Add-on for Splunk (`ta_sap_btp_uas`)

A Splunk add-on that **pulls SAP data over HTTPS and indexes it**, so nothing has to run inside your SAP
Cloud Foundry / BTP environment and no OpenTelemetry collector is needed:

- **SAP BTP usage and cost** from the SAP Usage Data Management ("Resource Consumption") API.
- **SAP Cloud ALM analytics** (health monitoring, job monitoring, status events, and any other analytics
  data provider) from the Cloud ALM Analytics API.

The indexed events can be searched in Splunk Cloud and charted in Splunk Observability Cloud through
Log Observer Connect.

```
Splunk Cloud search head (modular inputs, Victoria Experience)
   │  HTTPS 443 only, OAuth 2.0 client credentials
   ├──▶ SAP Usage Data Management API   (token: XSUAA, data: uas-reporting.cfapps.<landscape>)
   └──▶ SAP Cloud ALM Analytics API     (token: XSUAA, data: <tenant>.<region>.alm.cloud.sap)

Splunk index ──Log Observer Connect──▶ Splunk Observability Cloud (SPL log charts, search-based alerts)
```

> **Status: prototype.** It has been tested against mock servers written from SAP's OpenAPI specs, and it
> passes AppInspect (`cloud` and `private_victoria`). It has not yet been run against a live SAP tenant. See
> [Verification status](#verification-status) before relying on it.

---

## Contents

| Input | What it does | Sourcetype(s) |
|---|---|---|
| `sap_btp_uas` | Polls `GET /reports/v1/monthlyUsage` and `GET /reports/v1/monthlySubaccountsCost`. Default interval daily. | `sap:btp:uas:monthlyusage`, `sap:btp:uas:monthlycost` |
| `sap_calm_catalogue` | Lists Cloud ALM data providers (`GET /analytics/providers`) and each provider's dimensions (`POST /analytics/providers/filters`). Run it first to learn the real provider, dimension and measure names. | `sap:calm:provider`, `sap:calm:dimension` |
| `sap_calm_analytics` | Runs the queries you configure against `POST /analytics/providers/data`. | `sap:calm:analytics` |

Fields added to every event: `btp_account` and `uas_endpoint` (usage), `calm_account`, `calm_query`,
`calm_provider`, `calm_serie` (Cloud ALM). The event host is the SAP API hostname.

## Prerequisites

- Splunk Cloud, **Victoria Experience**, and a user with the `sc_admin` role. The add-on was vetted for Victoria only;
  Classic stacks install private apps through a different process, and Splunk Enterprise is untested.
- Outbound HTTPS (443) from the search head to the SAP hosts: `*.hana.ondemand.com`, `*.alm.cloud.sap`.
  No other port is used.
- Admin access on the SAP side (see below) to create service keys. The add-on never needs a human SAP login.
- For charting in Observability Cloud: Splunk Cloud 9.0.2209 or later, token authentication, and a Log Observer
  Connect connection (one per Observability Cloud org).

## Install

1. Download `dist/ta_sap_btp_uas-<version>.tar.gz` (or build it, see [Development](#development)).
2. **Apps > Manage Apps > Install app from file**, or `acs apps install private --acs-legal-ack Y --app-package <file>`.
   Splunk vets the package and asks for your splunk.com credentials.
3. Create an index, for example `sap_btp` (Settings > Indexes). For Log Observer Connect, make sure the service
   account's role can read it.
4. Restart if Splunk asks you to.

## Configure

You configure two things in the add-on UI: **accounts** (Configuration tab) and **inputs** (Inputs tab).
Client secrets are stored encrypted in Splunk's credential store, never in `inputs.conf`.

### A. BTP usage and cost

**On the SAP side (a BTP global account administrator does this once):**

1. In the BTP cockpit, open the global account > **Entitlements** > add the **Usage Data Management Service**
   with plan **`reporting-ga-admin`** to a subaccount.
2. In a Cloud Foundry subaccount: **Instances and Subscriptions > Create** > service *Usage Data Management
   Service*, plan *reporting-ga-admin*.
3. Create a **service key** on the instance and copy these values (names can differ slightly between landscapes,
   so check them against your own key): `clientid`, `clientsecret`, the UAA URL, and `target_url`.

**In Splunk:**

1. **Configuration > BTP usage accounts > Add**

   | Field | Value |
   |---|---|
   | Name | A label for this global account, for example `prod_ga`. Becomes `btp_account` on every event. |
   | Token URL | UAA base URL only, for example `https://<subdomain>.authentication.<region>.hana.ondemand.com`. **Do not** add `/oauth/token`; the add-on appends it. |
   | Reporting URL | `target_url` from the key, host only, for example `https://uas-reporting.cfapps.<region>.hana.ondemand.com`. **Do not** add `/reports/v1`. |
   | Client ID / Client secret | `clientid` / `clientsecret` from the key. |

2. **Inputs > Create new input > SAP BTP usage poller**

   | Field | Value |
   |---|---|
   | Name | For example `btp_usage`. |
   | Interval | Seconds. Default `86400` (the API returns monthly figures). Range 3600 to 86400. |
   | Index | For example `sap_btp`. |
   | BTP account | The account created above. |
   | Endpoints | `monthlyUsage` and/or `monthlySubaccountsCost`. The cost endpoint only returns data for **CPEA consumption-based** contracts; on other contracts it returns 404 and is skipped with a log line. |
   | Months to fetch | `2` (previous plus current month, so late corrections are picked up). Range 1 to 12. |

Every poll rewrites the same month, so in searches use `latest()` per key, **never `sum()` across polls**.

### B. Cloud ALM analytics

**On the SAP side (the Cloud ALM tenant administrator does this once):**

1. In the BTP cockpit, open the global account and subaccount that contain the **SAP Cloud ALM subscription**.
   The API service is only available there (it does not appear in trial accounts, and Cloud ALM has no trial tenant).
2. **Entitlements > Edit > Add Service Plans** > *SAP Cloud ALM API*, plan **standard**.
3. **Instances and Subscriptions > Create** > service *SAP Cloud ALM API*, plan *standard*, runtime *Other*.
   In the parameters step, declare **read-only** scopes, for example:

   ```json
   {
     "xs-security": {
       "xsappname": "<your-instance-name>",
       "authorities": [
         "$XSMASTERAPPNAME.calm-api.analytics.read",
         "$XSMASTERAPPNAME.calm-api.analytics.providers.read",
         "$XSMASTERAPPNAME.calm-api.hm.read",
         "$XSMASTERAPPNAME.calm-api.jobs.read",
         "$XSMASTERAPPNAME.calm-api.bsm.read"
       ]
     }
   }
   ```

   `analytics.read` and `analytics.providers.read` are needed for every provider; the others are per provider
   (health monitoring, job monitoring, status events). Add only what you use. Scopes cannot be added to an
   existing key: update the instance, then create a new binding. A missing scope returns HTTP 403 for that provider.
4. **Create a binding** (service key) and copy: the API endpoint (`endpoints.Api`), `uaa.url`, `uaa.clientid`,
   `uaa.clientsecret`. Use a dedicated binding for Splunk so it can be revoked on its own.

**In Splunk:**

1. **Configuration > Cloud ALM accounts > Add**

   | Field | Value |
   |---|---|
   | Name | A label for this tenant, for example `calm_prod`. Becomes `calm_account` on every event. |
   | Token URL | `uaa.url` from the key, host only, for example `https://<subdomain>.authentication.<region>.hana.ondemand.com`. **Do not** add `/oauth/token`. |
   | API URL | Tenant host only: `https://<tenant>.<region>.alm.cloud.sap`. Remove any trailing `/api` from `endpoints.Api`. |
   | Client ID / Client secret | `uaa.clientid` / `uaa.clientsecret`. |

2. **Run the catalogue first.** **Inputs > Create new input > Cloud ALM provider catalogue**: pick the account
   and an index. Interval `86400` is plenty. After the first run:

   ```spl
   index=sap_btp sourcetype=sap:calm:provider | table name version scope calmMarket
   index=sap_btp sourcetype=sap:calm:dimension | table calm_provider key name type isAttribute value_count
   ```

   This gives the real provider names, versions and dimension names for your tenant. The Cloud ALM OpenAPI file
   does not list them.

3. **Then create the analytics input.** **Inputs > Create new input > Cloud ALM analytics poller**:

   | Field | Value |
   |---|---|
   | Interval | Seconds, default `900`. The API's rate limits are not documented in its spec; start conservatively. |
   | Queries (JSON) | A JSON array of Cloud ALM query objects. See below. Max 8192 characters. Validated when you save. |
   | Time range | Semantic period: `L` or `C` (last / current) + number + `H`, `D`, `W`, `M` or `Y`. For example `L1H`. |
   | Format | `table` (rows become events) or `timeseries` (data points become events). |
   | Table type | `last` (latest values with timestamps, default), `snapshot` (aggregated, no timestamps), or `raw` (every row; consecutive polls overlap). Table format only. |
   | Resolution | Optional: `R`, `P`, `H`, `D`, `W`, `M`, `Y` or an `Mi` value such as `5Mi`. Blank uses the API default. |
   | Page size | Rows per request (table format). Default `500`. |

   Query object (from SAP's `CALM_ANALYTICS.json`):

   ```json
   [{
     "name": "jobs",
     "provider": "<provider name from the catalogue>",
     "version": "<version from the catalogue>",
     "columns": {
       "dimensions": ["<dimension key>"],
       "metrics": [{"measure": "<measure name>", "method": "sum"}]
     },
     "filters": [{"key": "<dimension key>", "values": ["<value>"]}]
   }]
   ```

   `method` is one of `avg`, `sum`, `min`, `max`. Each query is sent as its own request and paged separately.
   A query the API rejects is logged and skipped; the others still run.

   **Starter queries.** `starter-queries.json` targets three common feature sets: `HM_ANALYTICS_DP` (health),
   `DP_JOB_MONITORING` (jobs) and `EVENTS_DATA_SET` (status events). The provider IDs come from a third party's
   documentation (Dynatrace's SAP Cloud ALM extension), **not from SAP**, and the measure names, version `v1`,
   aggregation methods and empty dimension lists are **unverified guesses**. Compare them with the catalogue output and correct
   them before relying on the data.

## Example searches

```spl
# BTP usage: latest value per key
index=sap_btp sourcetype="sap:btp:uas:monthlyusage" reportYearMonth=202610
| stats latest(usage) as usage by subaccountName serviceName metricName unitPlural
| sort - usage

# BTP cost (CPEA contracts only)
index=sap_btp sourcetype="sap:btp:uas:monthlycost"
| stats latest(cost) as cost latest(currency) as currency by reportYearMonth subaccountName serviceName
| sort - cost

# Cloud ALM: what arrived per query
index=sap_btp sourcetype=sap:calm:analytics | stats count latest(_time) as last by calm_query calm_provider
```

## Observability Cloud (Log Observer Connect)

Connect the Observability Cloud org to the Splunk Cloud stack (Settings > Log Observer Connect) using a service
account whose role can read the index, then use SPL in Log Observer or save log charts to dashboards. Splunk's
documentation says fields used in Log Observer Connect charts must be **indexed**. The sourcetypes here use
search-time JSON extraction (`KV_MODE = json`), so check your first chart; if a field is not available, switch that
sourcetype to indexed extractions in `props.conf`. Detectors and SignalFlow do not apply to log data.

## Troubleshooting

Add-on logs: `index=_internal source=*ta_sap_btp_uas*`. Set the level on **Configuration > Logging**.

| Symptom | Likely cause |
|---|---|
| `authentication/authorisation failed` (HTTP 401) | Wrong client ID or secret, or the Token URL points at the wrong identity zone. |
| HTTP 403 | A required scope is missing on the service instance (Cloud ALM), or the wrong service plan (usage: needs `reporting-ga-admin`). Add the scope, create a new binding, update the account. |
| `Transport error ... certificate verify failed` | A TLS-intercepting proxy or a wrong host. Certificate and hostname verification cannot be switched off. |
| `Skipping monthlySubaccountsCost: HTTP 404` | The contract is not CPEA consumption-based. Expected; remove that endpoint from the input. |
| `Skipping query "<name>"` | Cloud ALM rejected the query (unknown provider, dimension or measure, or a bad version). Check the catalogue output. |
| No events at all | Account name mismatch, input disabled, index not readable by your search role, or outbound 443 blocked. |
| Form rejects a URL | Token, API and Reporting URLs must be `https://host[:port]` with no path, query or credentials. |

## Security notes

- HTTPS only; certificate and hostname verification are always on (bundled `certifi` CA store). There is no
  option to skip verification.
- HTTP redirects are **not** followed, so credentials and tokens cannot be replayed to a different host.
- Client secrets live in Splunk's encrypted credential store and are never logged or placed in error messages.
- Only `client_credentials` OAuth is used (no password grant), so no human SAP credential is stored.
- Use read-only scopes and a dedicated service key per consumer.

## Verification status

| Item | Status |
|---|---|
| Usage API paths and field names | **Verified against SAP's OpenAPI spec** (`APIUasReportingService.json`, Resource Consumption 1.0) |
| Cloud ALM Analytics paths and request/response shapes | **Verified against SAP's OpenAPI spec** (`CALM_ANALYTICS.json`, 1.0.0) |
| Client logic: token request, windows, query params, paging, error handling, URL validation, TLS and redirect refusal, no secret in errors | **Verified** by 31 unit tests against mock HTTPS servers (`tests/`), including end-to-end runs of the three built entrypoints. The mocks' provider names and values are **invented**. |
| UCC build and AppInspect (`cloud` + `private_victoria`) | **Verified locally**: 0 failures, 0 errors |
| Running inside a real Splunk (credential decrypt, forms, scheduling) | **Not verified** |
| Against a real SAP tenant (service-key field names, paging, rate limits, provider and measure names, timestamp unit, page-size cap) | **Not verified** |
| Whether the Usage `subaccountUsage` operation accepts client credentials | Unresolved (SAP's spec and help text disagree); not implemented |
| Whether Cloud ALM restricts API access by source IP | **Not verified**; if so, allowlist the Splunk Cloud search-head IPs |
| Whether Log Observer Connect can chart these fields | **Not verified** (see above) |

## Not covered

- Usage `/subaccountUsage`, `/cloudCreditsDetails`, `/monthlyDirectoryUsage`, and the SAC OData endpoint.
- Cost-centre attribution. No label or cost-centre field exists in the usage API responses; labels live on subaccounts
  in the BTP Accounts service, which would be a second API.
- Cloud ALM Status Events API (`/bsm-service/v1/events`) and the raw metrics, logs and traces APIs. Only the Analytics API is used.
- Cloud ALM sandbox (API-key) access; OAuth only.

## Development

```
python3 -m venv .venv && . .venv/bin/activate
pip install splunk-add-on-ucc-framework splunk-appinspect
python -m unittest discover -s tests -v     # needs openssl on PATH; no Splunk required
VERSION=0.2.1 ./build.sh                    # build, package and AppInspect-vet (macOS: export DYLD_LIBRARY_PATH=/opt/homebrew/lib)
```

`build.sh` writes to a temp directory, normalises file modes (some synced folders report every file as executable,
which AppInspect rejects) and removes `__pycache__` before packaging. Layout: `package/bin/uas_client.py` and
`calm_client.py` hold the API logic with no Splunk imports; `*_helper.py` and `calm_splunk.py` are the Splunk glue;
`globalConfig.json` defines the UI.
