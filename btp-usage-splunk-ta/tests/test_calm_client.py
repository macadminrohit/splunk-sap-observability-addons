"""Run: python -m unittest discover -s tests -v   (needs `openssl` on PATH; no Splunk required)"""
import json
import os
import secrets
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))
sys.path.insert(0, os.path.dirname(__file__))

import calm_client  # noqa: E402
import mock_calm  # noqa: E402
from test_uas_client import make_cert  # noqa: E402


class ParseQueriesTests(unittest.TestCase):
    def test_valid(self):
        q = calm_client.parse_queries(json.dumps([{"name": "n", "provider": "P", "columns": {
            "dimensions": ["d"], "metrics": [{"measure": "m", "method": "avg"}]}}]))
        self.assertEqual(q[0]["provider"], "P")

    def test_rejects_bad_input(self):
        for bad in ("not json", "{}", "[]", "[1]", '[{"name":"n"}]', '[{"provider":"p"}]',
                    '[{"name":"n","provider":"p","columns":{"dimensions":"x"}}]',
                    '[{"name":"n","provider":"p","columns":{"metrics":[{"method":"avg"}]}}]'):
            with self.assertRaises(ValueError, msg=bad):
                calm_client.parse_queries(bad)


class StarterQueriesTests(unittest.TestCase):
    def test_starter_file_is_valid_for_the_input(self):
        path = os.path.join(os.path.dirname(__file__), "..", "starter-queries.json")
        text = open(path).read()
        qs = calm_client.parse_queries(text)
        self.assertEqual([q["provider"] for q in qs],
                         ["HM_ANALYTICS_DP", "DP_JOB_MONITORING", "EVENTS_DATA_SET"])
        self.assertLessEqual(len(text), 8192)  # the input field's max length


class CalmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key, cls.crt = make_cert(cls.tmp.name)
        # Throwaway values generated per run; nothing here is a real credential.
        cls.cid, cls.csecret = "sb-" + secrets.token_hex(4), secrets.token_urlsafe(24)
        cls.bearer = secrets.token_urlsafe(24)
        cls.srv, cls.port = mock_calm.serve(cls.crt, cls.key, cls.cid, cls.csecret, cls.bearer)
        cls.url = f"https://localhost:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        mock_calm.Handler.seen = []
        mock_calm.Handler.ignore_offset = False
        mock_calm.Handler.data_status = 200
        mock_calm.Handler.series_message = ""

    def client(self, secret=None):
        return calm_client.CalmClient(self.url, self.url, self.cid, secret or self.csecret,
                                      ca_file=self.crt, timeout=10)

    Q = [{"name": "q1", "provider": "MOCK_HEALTH", "version": "v1",
          "columns": {"dimensions": ["Service"], "metrics": [{"measure": "m", "method": "avg"}]}}]

    def test_catalogue_providers_and_dimensions(self):
        evs = calm_client.collect_catalogue(self.client(), "acct")
        prov = [e for st, e in evs if st == calm_client.ST_PROVIDER]
        dims = [e for st, e in evs if st == calm_client.ST_DIMENSION]
        self.assertEqual([p["name"] for p in prov], ["MOCK_HEALTH", "MOCK_JOBS"])
        self.assertTrue(all(p["calm_account"] == "acct" for p in prov))
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["value_count"], 2)
        self.assertNotIn("values", dims[0])               # large value lists are not indexed
        self.assertEqual(dims[0]["calm_provider_version"], "v2")   # latest version used
        self.assertEqual(dims[0]["calm_provider"], "MOCK_HEALTH")

    def test_data_request_shape_and_events(self):
        evs = calm_client.collect_data(self.client(), "acct", self.Q, time_range="L1H",
                                       table_type="last", page_size=100)
        self.assertEqual(len(evs), 5)
        st, e = evs[1]
        self.assertEqual(st, calm_client.ST_DATA)
        self.assertEqual((e["Service"], e["Value"]), ("svc1", 1.5))
        self.assertEqual((e["calm_account"], e["calm_query"], e["calm_provider"], e["calm_serie"]),
                         ("acct", "q1", "MOCK_HEALTH", "q1"))
        req = [b for m, p, b, h in mock_calm.Handler.seen if p.endswith("/providers/data")][0]
        self.assertEqual(req["timeRange"], {"semantic": "L1H"})
        self.assertEqual((req["format"], req["tableType"], req["limit"], req["offset"]),
                         ("table", "last", 100, 0))
        self.assertEqual(req["queries"], self.Q)
        self.assertNotIn("resolution", req)

    def test_paging_uses_total_count(self):
        evs = calm_client.collect_data(self.client(), "acct", self.Q, page_size=2)
        self.assertEqual([e["Service"] for _, e in evs], [f"svc{i}" for i in range(5)])
        offsets = [b["offset"] for m, p, b, h in mock_calm.Handler.seen if p.endswith("/data")]
        self.assertEqual(offsets, [0, 2, 4])

    def test_server_ignoring_offset_does_not_duplicate(self):
        mock_calm.Handler.ignore_offset = True
        evs = calm_client.collect_data(self.client(), "acct", self.Q, page_size=2)
        self.assertEqual([e["Service"] for _, e in evs], ["svc0", "svc1"])

    def test_timeseries(self):
        evs = calm_client.collect_data(self.client(), "acct", self.Q, fmt="timeseries",
                                       resolution="5Mi")
        self.assertEqual(len(evs), 2)
        _, e = evs[0]
        self.assertEqual((e["value"], e["timestamp_raw"], e["Service"], e["measure"]),
                         (1.0, 1700000000, "svc0", "m"))
        req = [b for m, p, b, h in mock_calm.Handler.seen if p.endswith("/data")][0]
        self.assertEqual(req["resolution"], "5Mi")
        self.assertNotIn("limit", req)

    def test_series_message_is_skipped(self):
        mock_calm.Handler.series_message = "provider not found"
        self.assertEqual(calm_client.collect_data(self.client(), "acct", self.Q), [])

    def test_failing_query_does_not_blank_the_rest(self):
        two = self.Q + [dict(self.Q[0], name="q2")]
        mock_calm.Handler.data_status = 500
        self.assertEqual(calm_client.collect_data(self.client(), "acct", two), [])
        mock_calm.Handler.data_status = 200
        self.assertEqual(len(calm_client.collect_data(self.client(), "acct", two)), 10)

    def test_bad_secret_raises_auth_error_without_leak(self):
        bad = "wrong-" + secrets.token_hex(6)
        with self.assertRaises(calm_client.UasAuthError) as cm:
            calm_client.collect_catalogue(self.client(secret=bad), "acct")
        self.assertNotIn(bad, str(cm.exception))
        self.assertNotIn(self.csecret, str(cm.exception))

    def test_bearer_token_sent_on_every_api_call(self):
        calm_client.collect_catalogue(self.client(), "acct")
        calls = [(m, p, h.get("Authorization", "")) for m, p, b, h in mock_calm.Handler.seen]
        self.assertTrue(all(a == "Bearer " + self.bearer for m, p, a in calls if p != "/oauth/token"))

    def test_invalid_format_rejected(self):
        with self.assertRaises(ValueError):
            calm_client.collect_data(self.client(), "a", self.Q, fmt="csv")
        with self.assertRaises(ValueError):
            calm_client.collect_data(self.client(), "a", self.Q, table_type="all")


if __name__ == "__main__":
    unittest.main()
