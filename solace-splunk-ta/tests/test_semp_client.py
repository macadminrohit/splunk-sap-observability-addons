"""Run: python -m unittest discover -s tests   (needs `openssl` on PATH; no Splunk required)"""
import os
import secrets
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))
sys.path.insert(0, os.path.dirname(__file__))

import mock_semp  # noqa: E402
import semp_client  # noqa: E402


def make_cert(d, cn="localhost"):
    key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", key, "-out", crt, "-days", "1", "-subj", f"/CN={cn}",
         "-addext", f"subjectAltName=DNS:{cn},IP:127.0.0.1"],
        check=True, capture_output=True)
    return key, crt


class SempTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key, cls.crt = make_cert(cls.tmp.name)
        cls.user, cls.pw = "view-user", secrets.token_urlsafe(16)  # throwaway, generated per run
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

    def test_paging_follows_cursor_not_advertised_host(self):
        items = list(self.client().iter_collection("/msgVpns/prod-a/queues"))
        self.assertEqual([i[0]["queueName"] for i in items], [f"q{i}" for i in range(5)])
        # collections[] are index-aligned with data[] across pages
        self.assertEqual([i[1]["msgs"]["count"] for i in items], [0, 1, 2, 3, 4])

    def test_collect_queues_depth_and_filters(self):
        ev = semp_client.collect(self.client(), "brk1", ["msgVpn", "queue"], vpn_filter="prod-*")
        q = [e for st, e in ev if st == "solace:semp:queue"]
        self.assertEqual(len(q), 6)  # 5 in prod-a + 1 in prod-b; 'skipme' filtered out
        self.assertEqual({e["msgVpnName"] for e in q}, {"prod-a", "prod-b"})
        orders = next(e for e in q if e["queueName"] == "orders/in")
        self.assertEqual(orders["msgsCount"], 42)  # current depth
        self.assertEqual(orders["spooledMsgCount"], 999999)  # cumulative, kept separate
        self.assertEqual(orders["broker"], "brk1")
        self.assertEqual(sum(1 for st, _ in ev if st == "solace:semp:msgvpn"), 2)

    def test_name_filter(self):
        ev = semp_client.collect(self.client(), "b", ["queue"], vpn_filter="prod-a", name_filter="q1,q3")
        self.assertEqual(sorted(e["queueName"] for _, e in ev), ["q1", "q3"])

    def test_one_vpn_forbidden_does_not_blank_poll(self):
        # bridges on prod-b return 403 (user can't see it) -> skipped, queues still collected
        logs = []

        class L:
            def warning(self, fmt, *a): logs.append(fmt % a)
            error = warning

        ev = semp_client.collect(self.client(), "b", ["bridge", "queue"], vpn_filter="prod-*", logger=L())
        self.assertEqual(sum(1 for st, _ in ev if st == "solace:semp:queue"), 6)
        self.assertTrue(any("prod-b" in m and "403" in m for m in logs))

    def test_bad_password_is_auth_error_without_leaking_it(self):
        with self.assertRaises(semp_client.SempAuthError) as cm:
            list(self.client(password="wrong-" + secrets.token_hex(4)).iter_collection("/msgVpns"))
        self.assertNotIn(self.pw, str(cm.exception))

    def test_untrusted_cert_is_rejected(self):
        d = tempfile.mkdtemp()
        _, other_ca = make_cert(d)
        with self.assertRaises(semp_client.SempError) as cm:
            list(self.client(ca_file=other_ca).iter_collection("/msgVpns"))
        self.assertIn("certificate verify failed", str(cm.exception))

    def test_hostname_mismatch_is_rejected(self):
        with self.assertRaises(semp_client.SempError):
            list(self.client(base_url=f"https://127.0.0.2:{self.port}").iter_collection("/msgVpns"))

    def test_redirect_not_followed(self):
        with self.assertRaises(semp_client.SempError) as cm:
            self.client().get_json("/redirect")
        self.assertIn("302", str(cm.exception))

    def test_url_validation(self):
        for bad in ["http://h:943", "https://u:p@h", "https://h/SEMP", "ftp://h", "", "https://"]:
            with self.assertRaises(ValueError, msg=bad):
                semp_client.validate_base_url(bad)
        self.assertEqual(semp_client.validate_base_url("https://h.example:943/"), "https://h.example:943")


if __name__ == "__main__":
    unittest.main()
