# Solace dashboard panels (SPL, for the new dashboard experience / Log Observer Connect)

Every query below was run against a Splunk Cloud test stack (`index=solace`, real Solace Cloud trial broker) and returned rows with the
shown column names (2026-11-06). The time range is deliberately **not** in the SPL: use the dashboard time picker
(1 h is a good default; state panels use `latest()` so any range works). If the stack has other data in `index=solace`, keep the
`sourcetype=` filters.

Notes that apply to several panels:
- Most counters (`spooledMsgCount`, `discarded*MsgCount`) are **cumulative**, so the panels use `streamstats` deltas.
- Fields from SEMP v1 contain `-` and `.`, so SPL needs single quotes in `eval` (`'a.b-c'`).
- `msgSpoolUsage` on queues is bytes per Solace's SEMP v2 docs (not independently confirmed here). Health latencies are raw broker
  values (the broker's alert thresholds suggest microseconds; unconfirmed).
- Not verified because the trial broker has none: bridges, REST delivery points, any non-zero traffic (rates and discards are all 0 today).

## Row 1: status tiles (single value / table)

**1. HA / redundancy status** (table)
```spl
index=solace sourcetype="solace:semp:redundancy"
| sort - _time | head 1
| table broker redundancy.config-status redundancy.redundancy-status redundancy.active-standby-role redundancy.operating-mode redundancy.last-failure-reason redundancy.last-failure-time redundancy.oper-status.adb-link-up redundancy.oper-status.adb-hello-up
```
`config-status=Shutdown` / `redundancy-status=Down` is normal on a standalone broker. For an HA pair, alert on `redundancy-status!=Up`.

**2. Enabled services not Up** (table; count = single value)
```spl
index=solace sourcetype="solace:semp:service"
| stats latest(enabled) as enabled latest(listen-port-operational-status) as status by name vrf listen-port
| where enabled="true" AND status!="Up"
| sort name listen-port
```
On this trial broker 4 rows appear (REDUNDANCY / MATELINK style services that need an HA mate), which is expected, not an outage.
For single value: append `| stats count as services_down`.

**3. Total queue depth right now** (single value)
```spl
index=solace sourcetype="solace:semp:queue"
| stats latest(msgsCount) as depth by msgVpnName queueName
| stats sum(depth) as total_queue_depth
```

**4. Data freshness** (table; any `age_seconds` over ~180 means the poller stopped)
```spl
index=solace
| stats latest(_time) as last_seen count by sourcetype
| eval age_seconds=now()-last_seen, last_seen=strftime(last_seen,"%Y-%m-%d %H:%M:%S")
| sort - age_seconds
```

## Row 2: queues

**5. Queue depth over time** (line chart)
```spl
index=solace sourcetype="solace:semp:queue"
| timechart span=1m max(msgsCount) by queueName limit=20
```

**6. Messages spooled per minute** (column chart; uses cumulative `spooledMsgCount`)
```spl
index=solace sourcetype="solace:semp:queue"
| streamstats current=f last(spooledMsgCount) as prev by broker msgVpnName queueName
| eval spooled_in_interval=if(isnull(prev), 0, if(spooledMsgCount>=prev, spooledMsgCount-prev, spooledMsgCount))
| timechart span=1m sum(spooled_in_interval) by queueName limit=20
```

**7. Queue detail** (table, sorted by depth)
```spl
index=solace sourcetype="solace:semp:queue"
| stats latest(msgsCount) as depth latest(msgSpoolUsage) as spool_usage_bytes latest(txUnackedMsgCount) as unacked latest(spooledMsgCount) as spooled_total latest(bindSuccessCount) as bind_success latest(maxBindCount) as max_bind latest(ingressEnabled) as ingress_enabled latest(egressEnabled) as egress_enabled by msgVpnName queueName
| sort - depth
```

## Row 3: message VPNs and clients

**8. VPN message rates** (line chart; the trial broker is idle so lines sit at 0)
```spl
index=solace sourcetype="solace:semp:msgvpn"
| timechart span=1m avg(rxMsgRate) as rx_msg_rate avg(txMsgRate) as tx_msg_rate by msgVpnName
```

**9. VPN discards per minute** (column chart; delta of cumulative counters)
```spl
index=solace sourcetype="solace:semp:msgvpn"
| streamstats current=f last(discardedRxMsgCount) as prev_rx last(discardedTxMsgCount) as prev_tx by broker msgVpnName
| eval rx_discards=if(isnull(prev_rx),0,max(discardedRxMsgCount-prev_rx,0)), tx_discards=if(isnull(prev_tx),0,max(discardedTxMsgCount-prev_tx,0))
| timechart span=1m sum(rx_discards) as ingress_discards sum(tx_discards) as egress_discards
```

**10. Clients** (table; capped by the input's `max_items`, so this is the top-N, not necessarily all clients)
```spl
index=solace sourcetype="solace:semp:client"
| stats latest(clientAddress) as address latest(clientUsername) as username latest(uptime) as uptime_s latest(rxMsgRate) as rx_msg_rate latest(txMsgRate) as tx_msg_rate latest(rxDiscardedMsgCount) as rx_discards latest(txDiscardedMsgCount) as tx_discards latest(slowSubscriber) as slow_subscriber latest(softwareVersion) as sdk_version by msgVpnName clientName
| sort - rx_msg_rate
```

## Row 4: broker health (SEMP v1)

**11. Message spool utilisation %** (line chart)
```spl
index=solace sourcetype="solace:semp:messagespool"
| eval persist_mb='message-spool.message-spool-info.current-persist-usage',
       max_mb='message-spool.message-spool-info.max-disk-usage',
       spool_usage_pct=round(persist_mb/max_mb*100,3),
       msg_count_util_pct='message-spool.message-spool-info.message-count-utilization-percentage',
       spool_files_util_pct='message-spool.message-spool-info.spool-files-utilization-percentage',
       disk_partition_pct='message-spool.message-spool-info.active-disk-partition-usage'
| timechart span=1m max(spool_usage_pct) as spool_usage_pct max(msg_count_util_pct) as message_count_util_pct max(spool_files_util_pct) as spool_files_util_pct max(disk_partition_pct) as disk_partition_pct
```

**12. Memory %** (line chart)
```spl
index=solace sourcetype="solace:semp:memory"
| eval physical_pct='memory.physical-memory-usage-percent', subscription_pct='memory.subscription-memory-usage-percent'
| timechart span=1m max(physical_pct) as physical_memory_pct max(subscription_pct) as subscription_memory_pct
```

**13. Broker latencies** (line chart; raw values, see notes)
```spl
index=solace sourcetype="solace:semp:health"
| eval disk='system.health.disk-latency-current-value', compute='system.health.compute-latency-current-value',
       network='system.health.network-latency-current-value', mate_link='system.health.mate-link-latency-current-value'
| timechart span=1m max(disk) as disk_latency max(compute) as compute_latency max(network) as network_latency max(mate_link) as mate_link_latency
```

## Alert ideas (search-based, via Log Observer Connect)

| Alert | SPL (trigger when rows are returned) |
|---|---|
| Queue backlog | `index=solace sourcetype="solace:semp:queue" \| stats latest(msgsCount) as depth by msgVpnName queueName \| where depth>1000` |
| Spool nearly full | `index=solace sourcetype="solace:semp:messagespool" \| eval p='message-spool.message-spool-info.message-count-utilization-percentage' \| stats latest(p) as p \| where p>80` |
| Memory high | `index=solace sourcetype="solace:semp:memory" \| stats latest('memory.physical-memory-usage-percent') as p \| where p>90` |
| Poller stalled | `index=solace \| stats latest(_time) as t \| where now()-t>300` |
| HA down (HA pairs only) | `index=solace sourcetype="solace:semp:redundancy" \| stats latest('redundancy.redundancy-status') as s \| where s!="Up"` |

Thresholds are placeholders, not tuned to your traffic. Of the alert queries only "Queue backlog" was run (it executes and returns 0 rows because
no queue is over 1000); the other four are untested. The panel queries were run with an explicit `earliest=-15m` or `-60m`; with the
dashboard time picker instead they are the same SPL.
