"""End-to-end test of the *built* add-on entrypoints (script -> helper -> client -> events).

Needs a prior `./build.sh` (reads $TA_BUILD_DIR, default $TMPDIR/ta-btp-uas-out/ta_sap_btp_uas) and the
same venv. Splunk-only pieces (credential store, log-level conf) are patched; everything else is real.
This exists because 0.1.0 and 0.2.0 shipped UCC's stub entrypoints (they only dumped the input's own
config as an event) when `inputHelperModule` was missing from globalConfig.json, and nothing caught it.
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
import mock_calm  # noqa: E402
import mock_uas  # noqa: E402
from test_uas_client import make_cert  # noqa: E402

BUILD = os.environ.get(
    "TA_BUILD_DIR",
    os.path.join(os.environ.get("TMPDIR", "/tmp"), "ta-btp-uas-out", "ta_sap_btp_uas"))


class EW:
    def __init__(self):
        self.written = []

    def write_event(self, ev):
        self.written.append(ev)


@unittest.skipUnless(os.path.isdir(BUILD), "run ./build.sh first")
class EntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(BUILD, "bin"))
        cls.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(cls.tmp.name, "splunkhome", "var", "log", "splunk"))
        os.environ["SPLUNK_HOME"] = os.path.join(cls.tmp.name, "splunkhome")
        cls.logdir = os.path.join(cls.tmp.name, "splunkhome", "var", "log", "splunk")
        cls.key, cls.crt = make_cert(cls.tmp.name)
        cls.cid, cls.csecret = "sb-" + secrets.token_hex(4), secrets.token_urlsafe(16)
        cls.bearer = secrets.token_urlsafe(16)
        cls.calm, cls.calm_port = mock_calm.serve(cls.crt, cls.key, cls.cid, cls.csecret, cls.bearer)
        cls.uas, cls.uas_port = mock_uas.serve(cls.crt, cls.key, cls.cid, cls.csecret, cls.bearer)

    @classmethod
    def tearDownClass(cls):
        cls.calm.shutdown()
        cls.uas.shutdown()
        cls.tmp.cleanup()

    def test_entrypoints_delegate_to_helpers(self):
        for script in ("sap_btp_uas", "sap_calm_catalogue", "sap_calm_analytics"):
            src = open(os.path.join(BUILD, "bin", script + ".py")).read()
            self.assertIn("return stream_events(inputs, ew)", src, script)
            self.assertNotIn("input_items", src, script)  # the UCC stub that dumps config

    def _run(self, module, patch_target, port, inputs):
        helper = importlib.import_module(module)
        glue = importlib.import_module(patch_target)
        acct = {"token_url": f"https://localhost:{port}", "api_url": f"https://localhost:{port}",
                "reporting_url": f"https://localhost:{port}",
                "client_id": self.cid, "client_secret": self.csecret}

        class Inputs:
            metadata = {"session_key": "x"}

        Inputs.inputs = inputs
        ew = EW()
        with mock.patch("solnlib.log.make_splunkhome_path", return_value=self.logdir), \
                mock.patch.object(glue, "get_account", return_value=acct), \
                mock.patch.object(glue.conf_manager, "get_log_level", return_value=20), \
                mock.patch.object(glue.certifi, "where", return_value=self.crt):
            helper.stream_events(Inputs(), ew)
        return ew.written

    def test_catalogue_input_writes_real_events(self):
        mock_calm.Handler.seen = []
        out = self._run("sap_calm_catalogue_helper", "calm_splunk", self.calm_port,
                        {"sap_calm_catalogue://t": {"account": "calm_test", "index": "sap_btp"}})
        st = {e.sourceType for e in out}
        self.assertEqual(st, {"sap:calm:provider", "sap:calm:dimension"})
        self.assertEqual(len([e for e in out if e.sourceType == "sap:calm:provider"]), 2)
        self.assertTrue(all(e.index == "sap_btp" and e.host == "localhost" for e in out))
        self.assertNotIn(self.csecret, "".join(e.data for e in out))

    def test_analytics_input_writes_real_events(self):
        queries = json.dumps([{"name": "q1", "provider": "MOCK_HEALTH", "version": "v1",
                               "columns": {"dimensions": ["Service"],
                                           "metrics": [{"measure": "m", "method": "avg"}]}}])
        out = self._run("sap_calm_analytics_helper", "calm_splunk", self.calm_port,
                        {"sap_calm_analytics://t": {
                            "account": "calm_test", "index": "sap_btp", "queries": queries,
                            "time_range": "L1H", "format": "table", "table_type": "last",
                            "resolution": "", "page_size": "500"}})
        self.assertEqual({e.sourceType for e in out}, {"sap:calm:analytics"})
        rows = [json.loads(e.data) for e in out]
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["calm_query"] for r in rows}, {"q1"})
        self.assertNotIn(self.csecret, "".join(e.data for e in out))

    def test_usage_input_writes_real_events(self):
        out = self._run("sap_btp_uas_helper", "sap_btp_uas_helper", self.uas_port,
                        {"sap_btp_uas://t": {"account": "ga", "index": "sap_btp",
                                             "endpoints": "monthlyUsage,monthlySubaccountsCost",
                                             "months_back": "2"}})
        self.assertEqual({e.sourceType for e in out},
                         {"sap:btp:uas:monthlyusage", "sap:btp:uas:monthlycost"})
        self.assertEqual(len(out), 3)  # 2 usage records + 1 cost record
        self.assertNotIn(self.csecret, "".join(e.data for e in out))


if __name__ == "__main__":
    unittest.main()
