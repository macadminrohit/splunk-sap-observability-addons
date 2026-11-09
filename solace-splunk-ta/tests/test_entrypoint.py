"""End-to-end test of the *built* add-on entrypoint (solace_semp.py -> helper -> client -> events).

Needs a prior `./build.sh` (reads $TA_BUILD_DIR, default $TMPDIR/ta-out/ta_solace_semp) and the
same venv. Splunk-only pieces (credential store, log-level conf) are patched; everything else is real.
This exists because an earlier build shipped the UCC stub entrypoint (it only dumped input config)
and nothing caught it.
"""
import importlib
import json
import os
import secrets
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import mock_semp  # noqa: E402
from test_semp_client import make_cert  # noqa: E402

BUILD = os.environ.get("TA_BUILD_DIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "ta-out", "ta_solace_semp"))


@unittest.skipUnless(os.path.isdir(BUILD), "run ./build.sh first")
class EntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(BUILD, "bin"))
        cls.tmp = tempfile.TemporaryDirectory()
        # solnlib's file logger wants a SPLUNK_HOME to write var/log/splunk into
        os.makedirs(os.path.join(cls.tmp.name, "splunkhome", "var", "log", "splunk"))
        os.environ["SPLUNK_HOME"] = os.path.join(cls.tmp.name, "splunkhome")
        cls.key, cls.crt = make_cert(cls.tmp.name)
        cls.user, cls.pw = "view-user", secrets.token_urlsafe(16)
        cls.srv, cls.port = mock_semp.serve(cls.crt, cls.key, cls.user, cls.pw)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def test_entrypoint_delegates_to_helper(self):
        src = open(os.path.join(BUILD, "bin", "solace_semp.py")).read()
        self.assertIn("return stream_events(inputs, ew)", src)
        self.assertNotIn("input_items", src)  # the UCC stub that dumps config

    def test_poll_writes_real_events(self):
        helper = importlib.import_module("solace_semp_helper")
        written = []

        class EW:
            def write_event(self, ev):
                written.append(ev)

        class Inputs:
            metadata = {"session_key": "x"}
            inputs = {"solace_semp://t": {
                "account": "trial_broker", "index": "solace",
                "object_types": "msgVpn,queue", "msg_vpn_filter": "prod-*", "name_filter": "*"}}

        acct = {"semp_url": f"https://localhost:{self.port}", "username": self.user, "password": self.pw}
        logdir = os.path.join(self.tmp.name, "splunkhome", "var", "log", "splunk")
        with mock.patch("solnlib.log.make_splunkhome_path", return_value=logdir), \
                mock.patch.object(helper, "get_account", return_value=acct), \
                mock.patch.object(helper.conf_manager, "get_log_level", return_value=20), \
                mock.patch.object(helper.certifi, "where", return_value=self.crt):
            helper.stream_events(Inputs(), EW())

        self.assertEqual(len(written), 2 + 6)  # 2 VPNs + 6 queues
        st = {e.sourceType for e in written}
        self.assertEqual(st, {"solace:semp:msgvpn", "solace:semp:queue"})
        q = [json.loads(e.data) for e in written if e.sourceType == "solace:semp:queue"]
        self.assertEqual({d["broker"] for d in q}, {"trial_broker"})
        self.assertIn(42, [d["msgsCount"] for d in q])
        self.assertTrue(all(e.index == "solace" and e.host == "localhost" for e in written))
        self.assertNotIn(self.pw, "".join(e.data for e in written))

    def test_poll_new_types_cap_and_trim_flags_from_input(self):
        """0.2.0 inputs: client cap (max_items), trim checkbox ("0"), and the v1 'system' type."""
        helper = importlib.import_module("solace_semp_helper")
        written = []

        class EW:
            def write_event(self, ev):
                written.append(ev)

        class Inputs:
            metadata = {"session_key": "x"}
            inputs = {"solace_semp://t2": {
                "account": "trial_broker", "index": "solace",
                "object_types": "msgVpn,client,system", "msg_vpn_filter": "prod-a",
                "name_filter": "*", "max_items": "3", "trim_config_fields": "0"}}

        acct = {"semp_url": f"https://localhost:{self.port}", "username": self.user, "password": self.pw}
        logdir = os.path.join(self.tmp.name, "splunkhome", "var", "log", "splunk")
        with mock.patch("solnlib.log.make_splunkhome_path", return_value=logdir), \
                mock.patch.object(helper, "get_account", return_value=acct), \
                mock.patch.object(helper.conf_manager, "get_log_level", return_value=20), \
                mock.patch.object(helper.certifi, "where", return_value=self.crt):
            helper.stream_events(Inputs(), EW())

        by = {}
        for e in written:
            by.setdefault(e.sourceType, []).append(json.loads(e.data))
        self.assertEqual(len(by["solace:semp:client"]), 3)  # capped by max_items
        self.assertIn("solace:semp:redundancy", by)  # v1 system collector ran
        self.assertEqual(len(by["solace:semp:service"]), 2)
        # trim checkbox "0" -> config-only attributes are kept
        self.assertIn("authenticationBasicType", by["solace:semp:msgvpn"][0])
        self.assertNotIn(self.pw, "".join(e.data for e in written))


if __name__ == "__main__":
    unittest.main()
