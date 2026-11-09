"""Mock SEMP v2 monitor + SEMP v1 server (HTTPS) for testing the add-on.

IMPORTANT: this is *my reading* of the documented SEMP response shapes (v2: data[] / parallel
collections[] / meta.paging.cursorQuery; v1: <rpc-reply><rpc><show>..</show></rpc><execute-result/>),
not a real broker. Real-broker behaviour is unverified; the v1 XML below is invented to exercise
the generic flattener.
"""
import base64
import json
import ssl
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

VPNS = ["prod-a", "prod-b", "skipme"]
QUEUES = {  # vpn -> [(queueName, msgSpoolUsage, msgs.count)]
    "prod-a": [(f"q{i}", i * 10, i) for i in range(5)],
    "prod-b": [("orders/in", 4096, 42)],
    "skipme": [("x", 1, 1)],
}
CLIENTS = {"prod-a": [f"c{i}" for i in range(7)], "prod-b": ["solo"]}

V1_REPLIES = {
    "redundancy detail": (
        "<redundancy><config-status>Enabled</config-status>"
        "<redundancy-status>Up</redundancy-status><mode>Active/Active</mode>"
        "<detail><adb-link-up>Yes</adb-link-up><hello-up>Yes</hello-up>"
        "<uptime-pct>99.50</uptime-pct></detail></redundancy>"),
    "service": (
        "<service><services>"
        "<service><name>SMF</name><listen-port>55555</listen-port><up>Yes</up></service>"
        "<service><name>WEB</name><listen-port>8008</listen-port><up>No</up></service>"
        "</services></service>"),
    "interface": (
        "<interface><interfaces>"
        "<interface><name>eth0</name><enabled>Yes</enabled><link-detected>Yes</link-detected></interface>"
        "<interface><name>eth1</name><enabled>No</enabled><link-detected>No</link-detected></interface>"
        "</interfaces></interface>"),
    "message-spool detail": (
        "<message-spool><config-status>Enabled</config-status>"
        "<current-persist-usage-mb>12.5</current-persist-usage-mb>"
        "<max-disk-usage>1000</max-disk-usage></message-spool>"),
}
# DOCTYPE / entity bomb: the add-on must refuse to parse this (defusedxml).
EVIL = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<rpc-reply><rpc><show><stats>&b;</stats></show></rpc><execute-result code="ok"/></rpc-reply>')


class Handler(BaseHTTPRequestHandler):
    expected_auth = None  # set by serve()
    seen = []  # (path, query dict)
    v1_seen = []  # raw request bodies

    def log_message(self, *a):
        pass

    def _send(self, code, body, extra=None, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        if self.headers.get("Authorization") != self.expected_auth:
            return self._send(401, {})
        if self.path != "/SEMP":
            return self._send(404, {})
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Handler.v1_seen.append(body)
        try:
            root = ET.fromstring(body)
            show = root.find("show")
            words = []
            node = show[0]
            while True:
                words.append(node.tag)
                if not len(node):
                    break
                node = node[0]
            cmd = " ".join(words)
        except Exception:  # noqa: BLE001
            return self._send(400, b"bad", ctype="text/xml")
        if cmd == "stats client":
            return self._send(200, EVIL.encode(), ctype="text/xml")
        if cmd == "config-sync":  # broker-side failure with a reason
            return self._send(
                200, b'<rpc-reply><rpc><show/></rpc><execute-result code="fail" '
                     b'reason="Not allowed on this platform"/></rpc-reply>', ctype="text/xml")
        inner = V1_REPLIES.get(cmd)
        if inner is None:  # unknown command
            return self._send(
                200, b'<rpc-reply><rpc><show/></rpc><execute-result code="fail" '
                     b'reason="Unknown command"/></rpc-reply>', ctype="text/xml")
        xml = (f'<rpc-reply semp-version="soltr/9_13VMR"><rpc><show>{inner}</show></rpc>'
               f'<execute-result code="ok"/></rpc-reply>')
        return self._send(200, xml.encode(), ctype="text/xml")

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != self.expected_auth:
            return self._send(401, {"meta": {"responseCode": 401}})
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        Handler.seen.append((u.path, q))
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        # /SEMP/v2/monitor/msgVpns[/{vpn}/queues]
        if parts[:3] != ["SEMP", "v2", "monitor"]:
            return self._send(404, {})
        rest = parts[3:]
        if rest == ["redirect"]:
            return self._send(302, {}, {"Location": "https://evil.invalid/"})
        count = int(q.get("count", 10))
        start = int(q.get("cursor", 0))
        if rest == ["msgVpns"]:
            data = [{"msgVpnName": v, "enabled": True, "connectionCount": 3,
                     # as on a real broker: every counter/rate appears flat AND inside nested
                     # "rate"/"counter" objects; plus config noise (nested threshold objects) + empties
                     "txMsgRate": 7, "rate": {"txMsgRate": 7, "nestedOnlyRate": 3},
                     "dataRxMsgCount": 16, "counter": {"dataRxMsgCount": 16},
                     "authenticationBasicType": "internal",
                     "eventLogTag": "tag", "eventConnectionCountThreshold": {"setPercent": 80},
                     "alias": "", "failureReason": None} for v in VPNS]
            return self._page(u, data, None, start, count)
        if len(rest) >= 3 and rest[0] == "msgVpns":
            vpn, kind = rest[1], rest[2]
            if kind == "queues" and len(rest) == 3:
                rows = QUEUES.get(vpn)
                if rows is None:
                    return self._send(404, {})
                data = [{"queueName": n, "msgVpnName": vpn, "msgSpoolUsage": s,
                         "spooledMsgCount": 999999} for n, s, _ in rows]
                colls = [{"msgs": {"count": c}} for _, _, c in rows]
                return self._page(u, data, colls, start, count)
            if kind in ("bridges", "topicEndpoints") and len(rest) == 3:
                if vpn == "prod-b":
                    return self._send(403, {"meta": {"responseCode": 403}})  # per-VPN failure
                data = []
                if kind == "bridges" and vpn == "prod-a":
                    data = [{"bridgeName": "b1", "bridgeVirtualRouter": "auto", "msgVpnName": vpn,
                             "enabled": True},
                            {"bridgeName": "other", "bridgeVirtualRouter": "auto",
                             "msgVpnName": vpn, "enabled": True}]
                return self._page(u, data, None, start, count)
            if kind == "bridges" and len(rest) == 5 and rest[4] == "remoteMsgVpns":
                if rest[3] != "b1,auto":  # bridge key must be "name,virtualRouter"
                    return self._send(404, {})
                return self._page(u, [{"remoteMsgVpnName": "far", "up": True,
                                       "queueOperationalState": "up"}], None, start, count)
            if kind == "clients" and len(rest) == 3:
                names = CLIENTS.get(vpn, [])
                data = [{"clientName": n, "msgVpnName": vpn, "rxMsgRate": 1,
                         "clientUsername": "u", "alias": ""} for n in names]
                return self._page(u, data, None, start, count)
            if kind == "restDeliveryPoints" and len(rest) == 3:
                data = [{"restDeliveryPointName": "rdp1", "msgVpnName": vpn, "up": True}] \
                    if vpn == "prod-a" else []
                return self._page(u, data, None, start, count)
            if kind == "restDeliveryPoints" and len(rest) == 5 and rest[4] == "restConsumers":
                return self._page(u, [{"restConsumerName": "cons1", "up": False,
                                       "lastConnectionFailureReason": "refused"}],
                                  None, start, count)
        return self._send(404, {})

    def _page(self, u, data, colls, start, count):
        chunk = data[start:start + count]
        body = {"data": chunk, "links": [{} for _ in chunk],
                "meta": {"responseCode": 200, "count": len(data)}}
        if colls is not None:
            body["collections"] = colls[start:start + count]
        if start + count < len(data):
            nxt = start + count
            # Deliberately advertise an *internal* host, like a broker behind an LB would.
            body["meta"]["paging"] = {
                "cursorQuery": f"count={count}&cursor={nxt}",
                "nextPageUri": f"http://10.0.0.99:8080{u.path}?count={count}&cursor={nxt}",
            }
        self._send(200, body)


def serve(certfile, keyfile, username, password):
    Handler.expected_auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    Handler.seen = []
    Handler.v1_seen = []
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]
