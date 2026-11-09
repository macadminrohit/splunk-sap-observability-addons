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
        mock_uas.Handler.seen = []

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
