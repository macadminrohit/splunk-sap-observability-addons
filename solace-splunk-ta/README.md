# Solace SEMP Add-on for Splunk Cloud (`ta_solace_semp`)

SaaS-to-SaaS monitoring of Solace PubSub+ / SAP Advanced Event Mesh (AEM), as an alternative to running a collector or sidecar inside the
SAP Cloud Foundry environment (nothing runs in your CF/SAP; the add-on only makes outbound HTTPS calls).

```
Splunk Cloud search head (modular input, Victoria Experience)
   │  HTTPS, SEMP v2 monitor, read-only user, outbound only
   ▼
Solace / SAP AEM broker  (SaaS)

Splunk Cloud index  ──Log Observer Connect──▶  Splunk Observability Cloud (SPL log charts, search-based log alerts)
```

Nothing is deployed in your CF/BTP or on your hosts. Whether traffic also avoids your network depends
on where the broker is hosted and what the SEMP endpoint allowlist looks like (see caveats).

## What it collects

One JSON event per object per poll (default 60 s). Sourcetypes:

**SEMP v2 monitor (JSON)**

| Sourcetype | Path | Notes |
|---|---|---|
| `solace:semp:msgvpn` | `/msgVpns` | one per VPN: rates, connections, discards, service up/enabled, spool |
| `solace:semp:queue` | `/msgVpns/{vpn}/queues` | `msgsCount` = **current depth** (from `collections[].msgs.count`); `spooledMsgCount` is cumulative; bind counts, rates, unacked, discards |
| `solace:semp:topicendpoint` | `/msgVpns/{vpn}/topicEndpoints` | same `msgsCount` treatment |
| `solace:semp:bridge` | `/msgVpns/{vpn}/bridges` | |
| `solace:semp:bridgeremotevpn` | `/msgVpns/{vpn}/bridges/{name},{virtualRouter}/remoteMsgVpns` | remote VPN connection state per bridge (0.2.0) |
| `solace:semp:rdp` | `/msgVpns/{vpn}/restDeliveryPoints` | REST delivery point up/enabled (0.2.0) |
| `solace:semp:restconsumer` | `.../restDeliveryPoints/{rdp}/restConsumers` | REST consumer up + last failure reason (0.2.0) |
| `solace:semp:client` | `/msgVpns/{vpn}/clients` | per-client rates/counters/discards; **capped** by `max_items` (0.2.0) |

**SEMP v1 (XML, `POST /SEMP`, read-only `show` RPCs): object type "System"** (0.2.0)

| Sourcetype | `show` command | Dynatrace feature sets it targets |
|---|---|---|
| `solace:semp:redundancy` | `show redundancy detail` | redundancy / HA |
| `solace:semp:configsync` | `show config-sync` | config-sync |
| `solace:semp:messagespool` | `show message-spool detail` | message-spool, disk usage, utilisation |
| `solace:semp:memory` | `show memory` | memory |
| `solace:semp:service` | `show service` (one event per service) | service listen-port status |
| `solace:semp:interface` | `show interface` (one event per interface) | network-interface |
| `solace:semp:health` | `show system health` | system-health latencies |
| `solace:semp:globalstats` | `show stats client` | stats-clients |
| `solace:semp:replication` | `show replication` | replication |
| `solace:semp:certificate` | `show ssl server-certificate` | server-certificate days-to-expire |

The v1 replies are flattened generically (leaf tag path -> value, numbers coerced) instead of hard-coding tag names, so
field names mirror the broker's XML (for example `redundancy.detail.adb-link-up`). **The command spellings and resulting field
names have not been verified against a live broker**; a command the broker rejects is skipped with a logged reason
(`index=_internal source=*ta_solace_semp*  "Skipping SEMP v1"`). Check what actually arrived with
`index=solace sourcetype=solace:semp:* semp_object=system | stats count by sourcetype semp_command`.

Every event carries `broker` (the account name) and `semp_object`. Host is set to the SEMP hostname.
Filters (VPN and object name, wildcards), `max_items` (per VPN and type, default 500) and the *Trim config-only fields*
checkbox (default on: drops `authentication*`, `authorization*`, `event*` threshold config, duplicated `counter.*`/`rate.*`, empty values)
control event volume.

## Install on Splunk Cloud

1. Package: `dist/ta_solace_semp-0.2.1.tar.gz` (rebuild with `./build.sh`).
2. Private app install on Splunk Cloud (Victoria): Apps → Manage Apps → *Install app from file*, or ACS
   (`acs apps install private`). The package already passes AppInspect (see below); ACS requires 0 failures/errors/manual checks.
3. Create an index (for example `solace`) and, for LOC, a role with read access to it.
4. Configuration → add a broker: SEMP URL (`https://host:port`, no path), the **read-only** user (for Solace Cloud, the `-view` user), password
   (stored encrypted in Splunk's credential store).
5. Inputs → add a Solace SEMP poller: broker, object types, filters, interval, index.
6. Network: the search head(s) must reach the broker's SEMP TLS port. Outbound 443 is open by default on Splunk Cloud;
   other ports (SEMP over TLS is commonly 943) need a Splunk Support request, and the broker side may need to allowlist the
   Splunk Cloud search-head IPs.

## Splunk Cloud → Observability Cloud (Log Observer Connect)

Connect the O11y org to the Splunk Cloud stack (Settings → Log Observer Connect) using a service account whose role can read the index.
Then use SPL in Log Observer / log-based charts, for example:

```spl
index=solace sourcetype="solace:semp:queue" msgVpnName=prod-*
| timechart span=1m max(msgsCount) by queueName limit=20

index=solace sourcetype="solace:semp:queue"
| streamstats current=f last(spooledMsgCount) as prev by broker msgVpnName queueName
| eval spooled_in_interval=if(spooledMsgCount>=prev, spooledMsgCount-prev, spooledMsgCount)
| timechart span=1m sum(spooled_in_interval) by queueName limit=20
```

Search-based alert idea: `max(msgsCount) > N for a queue` over 5 minutes.

## Security properties (and the rule check behind them)

- HTTPS only; TLS certificate **and hostname** verification always on (certifi CA bundle). No "skip verify" option exists.
- Redirects are never followed, so credentials cannot be replayed to another host. Paging uses only the opaque `cursor` value, appended to the configured URL; the broker's `nextPageUri` host is ignored.
- Password lives only in Splunk's encrypted credential store; never in `inputs.conf`, code, or logs. No credentials are in this repo (test password is generated per run).
- No bundled binaries; pure Python (3.9 and 3.13 declared via `python.required`).
- Custom CA / private PKI brokers are not supported (would need a CA field).
- SEMP v1: only `<rpc><show>` documents can be built (command words must match `[a-z][a-z0-9-]*`), XML is parsed with `defusedxml` (DOCTYPE / entity expansion refused; tested), and a failing command never aborts the v2 data of the same poll.
- Event volume is bounded: `max_items` per VPN and type with a logged warning on truncation.

## Verification status

| Item | Status |
|---|---|
| UCC build; **AppInspect** with `cloud` + `private_victoria` tags | **Verified locally** (appinspect 4.4.0): 0 failures, 0 errors, 0 manual checks, 14 informational warnings (UCC UI bundle telemetry notices, bundled solnlib/splunk-sdk, etc.) |
| Poller logic: paging, `collections[]` alignment, filters, TLS verification (bad CA, hostname mismatch), redirect refusal, 401/403 handling, no password leakage | **Verified** by `tests/` (11 tests, incl. end-to-end run of the built entrypoint) against a mock HTTPS SEMP server *that I wrote from the documented response shape* |
| Running inside real Splunk Cloud (Victoria 10.7, test stack): install via ACS, config forms, encrypted credential, scheduling, indexing | **Verified 2026-11-06**: input ran every ~60 s, `events_ingested n_events=4`, events searchable in `index=solace` |
| Against a real Solace Cloud free-trial broker (SEMP v2 over TLS :943, `-view` user) | **Verified (small scale)**: queue `msgsCount` matched actual depth (10 vs 0), `msgSpoolUsage`, `spooledMsgCount`, VPN rate fields present, real certificate verified. One VPN + 2 queues only; page limits, large-broker volume and broker load are **NOT verified** |
| Log Observer Connect from the O11y org | **NOT verified** (UI-only step; LOC is unavailable on trial stacks) |

**Lesson recorded:** version 0.1.0 shipped UCC's placeholder entrypoint (the `inputHelperModule` line was missing from `globalConfig.json`), so it indexed only the input's own config. `tests/test_entrypoint.py` now guards against that. Observed in real data: `msgVpn` events carry ~80 fields with duplicated `rate.*` values (candidate for trimming); `bindCount` is not returned.

**0.2.1 fix:** real SEMP v2 replies carry every counter and rate twice, flat and inside nested `rate` / `counter` objects. 0.2.0's trimming
only looked for dotted keys (my mock had dotted keys, the real broker has nested objects), so it left those duplicates in. 0.2.1 drops nested
entries that already exist flat. Verify the size drop after install. Ready-made dashboard SPL is in `DASHBOARD_PANELS.md`.

**0.2.0 status:** 29 tests pass (incl. the built entrypoint with the new input fields) against my mock broker; AppInspect
(`cloud` + `private_victoria`) 0 failures / 0 errors. **Verified 2026-11-06 on a Splunk Cloud test stack against the real Solace Cloud trial broker
(`-view` user):** all ten SEMP v1 `show` commands returned data with no warnings (redundancy, config-sync, message-spool, memory,
service x12, interface, health, global stats, replication, certificate), clients were collected, and trimming cut `msgvpn` events from
~9.8 KB to ~6.7 KB. **Not verified:** `bridgeremotevpn`, `rdp`, `restconsumer` (the trial broker has no bridges or RDPs, so those paths have
never returned data), and anything at production scale. Known quirks seen in the real replies:
- `show ssl server-certificate` returns a thumbprint and configuration time, **not** an expiry date; there is no `days_to_expire`
  equivalent yet (needs a different command or a TLS probe).
- Lists inside one reply (for example `memory.physical-memory.memory-info.*`) arrive as parallel multivalue fields (Memory/Swap);
  use `mvzip`/`mvexpand` in SPL. Splitting them into per-row events is a candidate improvement.
- `redundancy.redundancy-status=Down` / `config-status=Shutdown` is normal on a standalone broker; alert on it only for HA pairs.

## Known gaps vs Dynatrace's Solace extension

- Not implemented: Kafka senders/receivers, cache clusters/instances, DMR links, replay logs, MQTT sessions, `show hardware`/POST (add if you use them).
- No `bindCount` / consumer count: in SEMP v2 that is the count of `queues/{q}/txFlows`, not a queue attribute (per Solace community); not implemented. Queues do carry `bindRequestCount`, `bindSuccessCount`, `maxBindCount`.
- Client events are capped, not aggregated; VPN-level totals are in the `msgvpn` events.
- No `select` tuning: queues are fetched with all attributes (page size 100). Solace's docs warn large unfiltered polls load the broker; use filters and a longer interval on busy brokers.
- Data are **events in Splunk Cloud**, not O11y metric time series: no SignalFlow detectors, only SPL charts and search-based alerts, and it consumes Splunk Cloud ingest and search resources.

## Dev

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install splunk-add-on-ucc-framework splunk-appinspect   # macOS: brew install libmagic
python -m unittest discover -s tests -v                      # needs openssl
./build.sh                                                   # build + package + AppInspect
```
