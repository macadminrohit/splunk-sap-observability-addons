"""Tests for the 0.2.0 additions: trimming, bridge/RDP/client collection, caps, SEMP v1 system collector.

Run: python -m unittest discover -s tests   (needs `openssl` and `defusedxml`; no Splunk required)
Mock replies are my reading of the docs, not a real broker (see mock_semp.py).
"""
import os
import secrets
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))
sys.path.insert(0, os.path.dirname(__file__))

import mock_semp  # noqa: E402
import semp_client  # noqa: E402
import semp_v1  # noqa: E402
from test_semp_client import make_cert  # noqa: E402


class Log:
    def __init__(self):
        self.msgs = []

    def warning(self, fmt, *a):
        self.msgs.append(fmt % a)

    error = warning


class ExtendedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key, cls.crt = make_cert(cls.tmp.name)
        cls.user, cls.pw = "view-user", secrets.token_urlsafe(16)
        cls.srv, cls.port = mock_semp.serve(cls.crt, cls.key, cls.user, cls.pw)
        cls.url = f"https://localhost:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def client(self, **kw):
        args = dict(base_url=self.url, username=self.user, password=self.pw,
                    ca_file=self.crt, page_size=2)
        args.update(kw)
        return semp_client.SempClient(**args)

    # ---- trimming -------------------------------------------------------------------------
    def test_trim_drops_duplicates_config_and_empties(self):
        ev = semp_client.collect(self.client(), "b", ["msgVpn"], vpn_filter="prod-a")
        e = ev[0][1]
        for gone in ("counter", "authenticationBasicType",
                     "eventLogTag", "eventConnectionCountThreshold", "alias", "failureReason"):
            self.assertNotIn(gone, e)
        self.assertEqual(e["rate"], {"nestedOnlyRate": 3})  # duplicate dropped, nested-only kept
        self.assertEqual(e["txMsgRate"], 7)
        self.assertEqual(e["dataRxMsgCount"], 16)
        self.assertEqual((e["broker"], e["semp_object"], e["msgVpnName"]), ("b", "msgVpn", "prod-a"))

    def test_trim_config_off_keeps_config_but_still_dedupes(self):
        e = semp_client.collect(self.client(), "b", ["msgVpn"], vpn_filter="prod-a",
                                trim_config=False)[0][1]
        self.assertIn("authenticationBasicType", e)
        self.assertIn("eventLogTag", e)
        self.assertNotIn("counter", e)
        self.assertNotIn("alias", e)

    def test_zero_values_survive_trim(self):
        e = semp_client.trim_event({"a": 0, "b": False, "c": "", "d": None, "msgsCount": 0})
        self.assertEqual(e, {"a": 0, "b": False, "msgsCount": 0})

    # ---- bridges / RDP / clients ----------------------------------------------------------
    def test_bridge_remote_vpn_uses_name_comma_vr_and_tolerates_failures(self):
        log = Log()
        mock_semp.Handler.seen.clear()
        ev = semp_client.collect(self.client(), "b", ["bridgeRemoteVpn"], vpn_filter="prod-a",
                                 logger=log)
        self.assertEqual([st for st, _ in ev], ["solace:semp:bridgeremotevpn"])
        e = ev[0][1]
        self.assertEqual((e["bridgeName"], e["bridgeVirtualRouter"], e["remoteMsgVpnName"]),
                         ("b1", "auto", "far"))
        paths = [p for p, _ in mock_semp.Handler.seen]
        self.assertIn("/SEMP/v2/monitor/msgVpns/prod-a/bridges/b1,auto/remoteMsgVpns", paths)
        # the second bridge's remote list 404s in the mock -> warned and skipped, not fatal
        self.assertTrue(any("bridgeRemoteVpn" in m and "404" in m for m in log.msgs))

    def test_bridge_name_filter_applies_to_parent_bridge(self):
        ev = semp_client.collect(self.client(), "b", ["bridge", "bridgeRemoteVpn"],
                                 vpn_filter="prod-a", name_filter="b1")
        self.assertEqual(sorted(st for st, _ in ev),
                         ["solace:semp:bridge", "solace:semp:bridgeremotevpn"])

    def test_rdp_and_consumers_carry_parent_name(self):
        ev = semp_client.collect(self.client(), "b", ["restDeliveryPoint", "restConsumer"],
                                 vpn_filter="prod-a")
        by = {st: e for st, e in ev}
        self.assertEqual(by["solace:semp:rdp"]["restDeliveryPointName"], "rdp1")
        self.assertEqual(by["solace:semp:restconsumer"]["restDeliveryPointName"], "rdp1")
        self.assertIs(by["solace:semp:restconsumer"]["up"], False)

    def test_clients_capped_per_vpn_and_truncation_logged(self):
        log = Log()
        ev = semp_client.collect(self.client(), "b", ["client"], vpn_filter="prod-*",
                                 max_items=3, logger=log)
        names = [e["clientName"] for _, e in ev]
        self.assertEqual(len([n for n in names if n.startswith("c")]), 3)  # prod-a capped
        self.assertIn("solo", names)  # prod-b under the cap
        self.assertTrue(any("Truncated client in VPN prod-a" in m for m in log.msgs))

    def test_max_items_zero_means_unlimited(self):
        ev = semp_client.collect(self.client(), "b", ["client"], vpn_filter="prod-a", max_items=0)
        self.assertEqual(len(ev), 7)

    # ---- SEMP v1 --------------------------------------------------------------------------
    def test_system_collects_flattened_v1_events(self):
        log = Log()
        mock_semp.Handler.v1_seen.clear()
        ev = semp_client.collect(self.client(), "b", ["system"], logger=log)
        by = {}
        for st, e in ev:
            by.setdefault(st, []).append(e)
        self.assertEqual(
            {k: len(v) for k, v in by.items()},
            {"solace:semp:redundancy": 1, "solace:semp:service": 2,
             "solace:semp:interface": 2, "solace:semp:messagespool": 1})
        red = by["solace:semp:redundancy"][0]
        self.assertEqual(red["redundancy.redundancy-status"], "Up")
        self.assertEqual(red["redundancy.detail.adb-link-up"], "Yes")
        self.assertEqual(red["redundancy.detail.uptime-pct"], 99.5)  # numeric coercion
        self.assertEqual((red["broker"], red["semp_object"], red["semp_command"]),
                         ("b", "system", "show redundancy detail"))
        svc = {e["name"]: e for e in by["solace:semp:service"]}
        self.assertEqual(svc["SMF"]["listen-port"], 55555)
        self.assertEqual(svc["WEB"]["up"], "No")
        self.assertEqual({e["name"] for e in by["solace:semp:interface"]}, {"eth0", "eth1"})
        self.assertEqual(by["solace:semp:messagespool"][0]["message-spool.current-persist-usage-mb"], 12.5)
        # a broker-side failure and an unknown command are skipped with a logged reason
        self.assertTrue(any("config-sync" in m and "Not allowed" in m for m in log.msgs))
        self.assertTrue(any("show memory" in m and "Unknown command" in m for m in log.msgs))

    def test_v1_requests_are_read_only_show_rpcs(self):
        mock_semp.Handler.v1_seen.clear()
        semp_client.collect(self.client(), "b", ["system"])
        self.assertGreaterEqual(len(mock_semp.Handler.v1_seen), len(semp_v1.SYSTEM_COMMANDS))
        for body in mock_semp.Handler.v1_seen:
            root = ET.fromstring(body)
            self.assertEqual(root.tag, "rpc")
            self.assertEqual([c.tag for c in root], ["show"])

    def test_v1_entity_expansion_is_refused(self):
        log = Log()
        ev = semp_client.collect(self.client(), "b", ["system"], logger=log)
        self.assertFalse([e for st, e in ev if st == "solace:semp:globalstats"])
        self.assertTrue(any("show stats client" in m and "Unparsable" in m for m in log.msgs))

    def test_v1_bad_credentials_do_not_raise_and_do_not_leak(self):
        log = Log()
        bad = "wrong-" + secrets.token_hex(4)
        ev = semp_client.collect(self.client(password=bad), "b", ["system"], logger=log)
        self.assertEqual(ev, [])
        self.assertTrue(log.msgs and all(bad not in m and self.pw not in m for m in log.msgs))

    def test_v2_and_v1_in_one_poll(self):
        ev = semp_client.collect(self.client(), "b", ["msgVpn", "system"], vpn_filter="prod-a")
        sts = {st for st, _ in ev}
        self.assertIn("solace:semp:msgvpn", sts)
        self.assertIn("solace:semp:redundancy", sts)

    def test_system_only_does_not_touch_v2(self):
        mock_semp.Handler.seen.clear()
        semp_client.collect(self.client(), "b", ["system"])
        self.assertEqual(mock_semp.Handler.seen, [])

    def test_build_rpc_allowlist(self):
        self.assertEqual(
            semp_v1.build_rpc("message-spool detail"),
            b'<rpc semp-version="soltr/9_13VMR"><show><message-spool><detail></detail>'
            b'</message-spool></show></rpc>')
        for bad in ["", "Redundancy", "a;b", "a b/c", "x<y", "no shutdown!", "a  b$", "1abc"]:
            with self.assertRaises(ValueError, msg=bad):
                semp_v1.build_rpc(bad)

    def test_coerce(self):
        self.assertEqual(semp_v1._coerce("12"), 12)
        self.assertEqual(semp_v1._coerce(" 1.50 "), 1.5)
        self.assertEqual(semp_v1._coerce("007"), "007")  # ids with leading zeros stay strings
        self.assertEqual(semp_v1._coerce("Yes"), "Yes")
        self.assertEqual(semp_v1._coerce("12345678901234567890"), "12345678901234567890")

    def test_flatten_repeated_leaf_becomes_list_and_blank_leaves_skipped(self):
        el = ET.fromstring("<a><b>1</b><b>2</b><c><d>x</d></c><e>  </e></a>")
        self.assertEqual(semp_v1._flatten(el), {"b": [1, 2], "c.d": "x"})


if __name__ == "__main__":
    unittest.main()
