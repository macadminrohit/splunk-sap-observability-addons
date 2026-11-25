"""Run: python -m unittest discover -s tests -v   (needs `openssl` on PATH; no Splunk required)"""
import base64
import datetime
import os
import secrets
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))
sys.path.insert(0, os.path.dirname(__file__))

import mock_uas  # noqa: E402
import uas_client  # noqa: E402


def make_cert(d, cn="localhost"):
    key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", key, "-out", crt, "-days", "1", "-subj", f"/CN={cn}",
         "-addext", f"subjectAltName=DNS:{cn},IP:127.0.0.1"],
        check=True, capture_output=True)
    return key, crt


class MonthWindowTests(unittest.TestCase):
    def test_single_month(self):
        now = datetime.datetime(2026, 10, 29, tzinfo=datetime.timezone.utc)
        self.assertEqual(uas_client.month_window(now, 1), (202610, 202610))

    def test_crosses_year_boundary(self):
        now = datetime.datetime(2027, 1, 5, tzinfo=datetime.timezone.utc)
        self.assertEqual(uas_client.month_window(now, 2), (202612, 202701))
        self.assertEqual(uas_client.month_window(now, 12), (202602, 202701))

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            uas_client.month_window(datetime.datetime(2026, 1, 1), 0)


class DayWindowTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 29, tzinfo=datetime.timezone.utc)

    def test_window_ends_today(self):
        self.assertEqual(uas_client.day_window(self.NOW, 3, "DAY"), (20261027, 20261029, False))

    def test_crosses_year_boundary(self):
        now = datetime.datetime(2027, 1, 2, tzinfo=datetime.timezone.utc)
        self.assertEqual(uas_client.day_window(now, 5, "WEEK"), (20261229, 20270102, False))

    def test_clamps_to_sap_limits(self):
        f, t, clamped = uas_client.day_window(self.NOW, 366, "DAY")
        self.assertTrue(clamped)
        span = (datetime.date(2026, 10, 29) - datetime.datetime.strptime(str(f), "%Y%m%d").date()).days + 1
        self.assertEqual(span, 120)
        self.assertFalse(uas_client.day_window(self.NOW, 365, "WEEK")[2])
        self.assertTrue(uas_client.day_window(self.NOW, 366, "WEEK")[2])

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            uas_client.day_window(self.NOW, 0, "DAY")
        with self.assertRaises(ValueError):
            uas_client.day_window(self.NOW, 3, "HOUR")


class FlattenCreditsTests(unittest.TestCase):
    def test_one_record_per_update_and_empty_phase_kept(self):
        recs = uas_client.flatten_credits(mock_uas.CREDITS)
        self.assertEqual(len(recs), 3)
        self.assertEqual([r.get("balance") for r in recs], [900.0, 850.5, None])
        self.assertTrue(all(r["globalAccountId"] == "ga-1" and r["currency"] == "EUR" for r in recs))
        self.assertEqual(recs[2]["phaseStartDate"], "2027-01-01")

    def test_contract_without_phases_kept(self):
        recs = uas_client.flatten_credits({"globalAccountId": "g", "contracts": [{"currency": "USD"}]})
        self.assertEqual(recs, [{"globalAccountId": "g", "globalAccountName": None, "contractStartDate": None,
                                 "contractEndDate": None, "currency": "USD"}])

    def test_bad_shape_is_error(self):
        for bad in ({}, {"contracts": "x"}, []):
            with self.assertRaises(uas_client.UasError):
                uas_client.flatten_credits(bad)


class EventTimeTests(unittest.TestCase):
    def test_subaccount_usage_uses_period_start(self):
        st = "sap:btp:uas:subaccountusage"
        self.assertEqual(uas_client.event_time(st, {"periodStartDate": 20261029}),
                         datetime.datetime(2026, 10, 29, tzinfo=datetime.timezone.utc).timestamp())
        self.assertIsNone(uas_client.event_time(st, {"periodStartDate": "junk"}))
        self.assertIsNone(uas_client.event_time(st, {}))

    def test_other_sourcetypes_keep_index_time(self):
        self.assertIsNone(uas_client.event_time("sap:btp:uas:monthlyusage", {"periodStartDate": 20261029}))


class UrlValidationTests(unittest.TestCase):
    def test_rejects_unsafe_urls(self):
        for bad in ("http://x.example", "https://u:p@x.example", "https://x.example/path",
                    "https://x.example/?q=1", "", "ftp://x.example"):
            with self.assertRaises(ValueError, msg=bad):
                uas_client.validate_base_url(bad)

    def test_accepts_plain_https(self):
        self.assertEqual(uas_client.validate_base_url("https://x.example:8443/"),
                         "https://x.example:8443")


class UasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key, cls.crt = make_cert(cls.tmp.name)
        # Throwaway values generated per run; nothing here is a real credential.
        cls.cid, cls.csecret = "sb-" + secrets.token_hex(4), secrets.token_urlsafe(24)
        cls.bearer = secrets.token_urlsafe(24)
        cls.srv, cls.port = mock_uas.serve(cls.crt, cls.key, cls.cid, cls.csecret, cls.bearer)
        cls.url = f"https://localhost:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        mock_uas.Handler.cost_status = 200
        mock_uas.Handler.credits_status = 200
        mock_uas.Handler.directory_status = 200
        mock_uas.Handler.subaccount_status = {}
        mock_uas.Handler.seen = []

    NOW = datetime.datetime(2026, 10, 29, tzinfo=datetime.timezone.utc)

    def gets(self, path=None):
        return [s for s in mock_uas.Handler.seen if s[0] == "GET" and (path is None or s[1] == path)]

    def test_cloud_credits_flattened(self):
        ev = uas_client.collect(self.client(), "ga_main", ["cloudCreditsDetails"], now=self.NOW,
                                credits_view_phases="ALL")
        self.assertEqual(len(ev), 3)
        self.assertEqual({st for st, _ in ev}, {"sap:btp:uas:cloudcredits"})
        self.assertEqual(ev[0][1]["uas_endpoint"], "cloudCreditsDetails")
        self.assertEqual(self.gets()[0][2], {"viewPhases": "ALL"})

    def test_credits_404_is_skipped(self):
        mock_uas.Handler.credits_status = 404  # subscription-based contract
        ev = uas_client.collect(self.client(), "ga_main", ["monthlyUsage", "cloudCreditsDetails"], now=self.NOW)
        self.assertEqual({st for st, _ in ev}, {"sap:btp:uas:monthlyusage"})

    def test_rejects_unknown_view_phases(self):
        with self.assertRaises(ValueError):
            uas_client.collect(self.client(), "ga_main", ["cloudCreditsDetails"], credits_view_phases="SOME")

    def test_directory_usage(self):
        ev = uas_client.collect(self.client(), "dir_key", ["monthlyDirectoryUsage"], months_back=1, now=self.NOW)
        self.assertEqual([(st, e["directoryName"]) for st, e in ev],
                         [("sap:btp:uas:monthlydirectoryusage", "Finance")])
        self.assertEqual(self.gets()[0][2], {"fromDate": "202610", "toDate": "202610"})

    def test_403_on_one_report_skips_only_that_report(self):
        mock_uas.Handler.directory_status = 403  # global-account key lacks Directory_Admin
        logs = []

        class L:
            def warning(self, *a):
                logs.append(a[0] % a[1:])

        ev = uas_client.collect(self.client(), "ga_main", ["monthlyDirectoryUsage", "monthlyUsage"],
                                now=self.NOW, logger=L())
        self.assertEqual({st for st, _ in ev}, {"sap:btp:uas:monthlyusage"})
        self.assertTrue(any("Directory_Admin" in m for m in logs), logs)

    def test_401_on_a_report_is_still_fatal(self):
        c = self.client()
        c._token = "expired-" + secrets.token_hex(4)
        with self.assertRaises(uas_client.UasAuthError) as cm:
            uas_client.collect(c, "ga_main", ["monthlyUsage"], now=self.NOW)
        self.assertEqual(cm.exception.status, 401)

    def test_subaccounts_discovered_from_monthly_usage(self):
        ev = uas_client.collect(self.client(), "ga_main", ["subaccountUsage"], now=self.NOW)
        self.assertEqual({st for st, _ in ev}, {"sap:btp:uas:subaccountusage"})  # discovery is not emitted
        self.assertEqual(len(ev), 3)
        calls = self.gets("/reports/v1/subaccountUsage")
        self.assertEqual([c[2]["subaccountId"] for c in calls], ["sa-1", "sa-2"])
        self.assertTrue(all(c[2]["fromDate"] == "20261027" and c[2]["toDate"] == "20261029"
                            and c[2]["periodPerspective"] == "DAY" for c in calls))
        self.assertEqual(len(self.gets("/reports/v1/monthlyUsage")), 1)

    def test_monthly_usage_fetched_once_when_also_selected(self):
        ev = uas_client.collect(self.client(), "ga_main", ["subaccountUsage", "monthlyUsage"], now=self.NOW)
        self.assertEqual(len([1 for st, _ in ev if st == "sap:btp:uas:monthlyusage"]), 2)
        self.assertEqual(len(self.gets("/reports/v1/monthlyUsage")), 1)

    def test_explicit_subaccounts_and_one_failing(self):
        mock_uas.Handler.subaccount_status = {"sa-9": 404}
        ev = uas_client.collect(self.client(), "ga_main", ["subaccountUsage"], now=self.NOW,
                                subaccount_ids=["sa-1", "sa-9"], subaccount_period="WEEK", days_back=14)
        self.assertEqual(len(ev), 2)
        self.assertEqual(self.gets("/reports/v1/monthlyUsage"), [])
        self.assertEqual({c[2]["periodPerspective"] for c in self.gets()}, {"WEEK"})

    def test_oversized_day_window_is_clamped_before_calling_sap(self):
        ev = uas_client.collect(self.client(), "ga_main", ["subaccountUsage"], now=self.NOW,
                                subaccount_ids=["sa-1"], days_back=366)
        self.assertEqual(len(ev), 2)  # the mock rejects anything over SAP's limit with 400

    def client(self, **kw):
        args = dict(token_url=self.url, reporting_url=self.url, client_id=self.cid,
                    client_secret=self.csecret, ca_file=self.crt)
        args.update(kw)
        return uas_client.UasClient(**args)

    def test_token_uses_client_credentials_with_basic_client_auth(self):
        self.assertEqual(self.client().get_token(), self.bearer)
        method, path, form, _ = mock_uas.Handler.seen[0]
        self.assertEqual((method, path), ("POST", "/oauth/token"))
        self.assertEqual(form, {"grant_type": "client_credentials"})  # never the password grant
        # client secret travels in the Authorization header, never in the body or URL
        self.assertNotIn(self.csecret, str(form))

    def test_collect_both_endpoints(self):
        now = datetime.datetime(2026, 10, 29, tzinfo=datetime.timezone.utc)
        ev = uas_client.collect(self.client(), "ga_main", ["monthlyUsage", "monthlySubaccountsCost"],
                                months_back=2, now=now)
        usage = [e for st, e in ev if st == "sap:btp:uas:monthlyusage"]
        cost = [e for st, e in ev if st == "sap:btp:uas:monthlycost"]
        self.assertEqual(len(usage), 2)
        self.assertEqual(len(cost), 1)
        self.assertEqual(usage[0]["btp_account"], "ga_main")
        self.assertEqual(usage[0]["uas_endpoint"], "monthlyUsage")
        self.assertEqual(usage[0]["usage"], 12.5)
        self.assertEqual(cost[0]["currency"], "EUR")
        gets = [s for s in mock_uas.Handler.seen if s[0] == "GET"]
        self.assertTrue(all(g[2] == {"fromDate": "202609", "toDate": "202610"} for g in gets))
        # bearer, not basic, on reporting calls
        self.assertTrue(all(g[3]["Authorization"].startswith("Bearer ") for g in gets))

    def test_our_fields_win_over_api_fields(self):
        orig = list(mock_uas.USAGE)
        try:
            mock_uas.USAGE[0] = dict(orig[0], btp_account="spoofed")
            ev = uas_client.collect(self.client(), "ga_main", ["monthlyUsage"], months_back=1)
            self.assertEqual(ev[0][1]["btp_account"], "ga_main")
        finally:
            mock_uas.USAGE[:] = orig

    def test_cost_404_is_skipped_not_fatal(self):
        mock_uas.Handler.cost_status = 404  # e.g. a non-CPEA account
        ev = uas_client.collect(self.client(), "ga_main", ["monthlyUsage", "monthlySubaccountsCost"],
                                months_back=1)
        self.assertEqual({st for st, _ in ev}, {"sap:btp:uas:monthlyusage"})

    def test_bad_client_secret_is_auth_error_without_leak(self):
        c = self.client(client_secret="wrong-" + secrets.token_hex(4))
        with self.assertRaises(uas_client.UasAuthError) as cm:
            c.get_token()
        self.assertNotIn(self.csecret, str(cm.exception))
        self.assertNotIn("wrong-", str(cm.exception))

    def test_untrusted_cert_is_refused(self):
        # no ca_file -> default store does not trust our throwaway CA
        c = uas_client.UasClient(self.url, self.url, self.cid, self.csecret)
        with self.assertRaises(uas_client.UasError) as cm:
            c.get_token()
        self.assertNotIn(self.csecret, str(cm.exception))

    def test_hostname_mismatch_is_refused(self):
        # Certificate is trusted (we pass it as the CA) but is issued to a different name
        # than the host we connect to, so only hostname verification can reject it.
        with tempfile.TemporaryDirectory() as d:
            key, crt = make_cert(d, cn="other.example")
            # SAN lists only other.example (no IP / no localhost)
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                 "-nodes", "-keyout", key, "-out", crt, "-days", "1", "-subj", "/CN=other.example",
                 "-addext", "subjectAltName=DNS:other.example"],
                check=True, capture_output=True)
            srv, port = mock_uas.serve(crt, key, self.cid, self.csecret, self.bearer)
            try:
                c = uas_client.UasClient(f"https://localhost:{port}", f"https://localhost:{port}",
                                         self.cid, self.csecret, ca_file=crt, timeout=5)
                with self.assertRaises(uas_client.UasError) as cm:
                    c.get_token()
                self.assertIn("Transport error", str(cm.exception))
            finally:
                srv.shutdown()
                # serve() resets the shared handler state; put the main server's values back
                mock_uas.Handler.expected_basic = "Basic " + base64.b64encode(
                    f"{self.cid}:{self.csecret}".encode()).decode()
                mock_uas.Handler.issued_token = self.bearer

    def test_redirect_is_not_followed(self):
        c = self.client()
        c._token = self.bearer
        with self.assertRaises(uas_client.UasError) as cm:
            c.get_report("/redirect", {"fromDate": 202610, "toDate": 202610})
        self.assertIn("302", str(cm.exception))
        # the mock never saw a request to the redirect target; only our own host is contacted
        self.assertTrue(all(s[1] == "/redirect" for s in mock_uas.Handler.seen))

    def test_unexpected_shape_is_error(self):
        c = self.client()
        c._token = self.bearer
        with self.assertRaises(uas_client.UasError):
            c.get_report("/reports/v1/nope", {"fromDate": 202610, "toDate": 202610})


if __name__ == "__main__":
    unittest.main()
