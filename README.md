# Splunk add-ons for SAP observability

Two Splunk Cloud add-ons that **pull data over HTTPS and index it**, so nothing has to run inside your SAP
environment (no OpenTelemetry collector, no Cloud Foundry sidecar). Indexed data can be charted in Splunk
Observability Cloud through Log Observer Connect.

| Add-on | Folder | Pulls |
|---|---|---|
| SAP BTP Usage + Cloud ALM (`ta_sap_btp_uas`) | [`btp-usage-splunk-ta/`](btp-usage-splunk-ta/) | BTP usage and cost (Usage Data Management API) and SAP Cloud ALM analytics. **Start here for configuration steps.** |
| Solace SEMP (`ta_solace_semp`) | [`solace-splunk-ta/`](solace-splunk-ta/) | Solace PubSub+ / SAP Advanced Event Mesh broker metrics over SEMP v1/v2. |

Both are **prototypes** built with the Splunk UCC framework. They pass AppInspect (`cloud` and `private_victoria`
tags) and their unit tests, but the SAP-side calls have not all been verified against live tenants. Each README has a
"Verification status" table that says exactly what was and was not tested.

Ready-to-install packages are in each add-on's `dist/` folder (`btp-usage-splunk-ta/dist/ta_sap_btp_uas-0.2.0.tar.gz`,
`solace-splunk-ta/dist/ta_solace_semp-0.2.1.tar.gz`). To rebuild, run `./build.sh` in the add-on folder (see the Development
section of each README; it needs the UCC framework 6.x and Python 3.12).

**License:** none has been applied yet, so by default all rights are reserved. Add a `LICENSE` file before others reuse the code.
