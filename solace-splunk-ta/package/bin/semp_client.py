"""Minimal, dependency-light SEMP client (v2 monitor + v1 XML) and event builders.

Deliberately free of any Splunk imports so it can be unit-tested outside Splunk.

Security properties (relevant to Splunk Cloud vetting):
  * HTTPS only. TLS certificate and hostname verification are always on
    (CA bundle = certifi). There is no "skip verification" switch.
  * HTTP redirects are NOT followed, so the Authorization header can never be
    replayed to a host other than the one the admin configured.
  * Paging uses only the opaque ``cursor`` token returned by the broker, appended
    to our own configured base URL. We never follow the broker-supplied
    ``nextPageUri`` host.
  * Only GETs (v2 monitor) and read-only ``<show>`` RPCs (v1) are ever sent. The v1 builder
    refuses anything that is not a ``show`` command.
  * The password is never logged.
"""

import base64
import fnmatch
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

SEMP_MONITOR_ROOT = "/SEMP/v2/monitor"
SEMP_V1_PATH = "/SEMP"
DEFAULT_PAGE_SIZE = 100  # SEMP v2 maximum page size for queues
MAX_PAGES = 1000  # hard stop against a runaway cursor loop
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_ITEMS = 500  # per (VPN, object type); stops a busy broker flooding the index

# object type -> (sourcetype, path template)
#   Placeholders: {vpn}; child types also get {bridge} / {rdp} (already URL-quoted).
OBJECT_TYPES = {
    "msgVpn": ("solace:semp:msgvpn", "/msgVpns"),
    "queue": ("solace:semp:queue", "/msgVpns/{vpn}/queues"),
    "topicEndpoint": ("solace:semp:topicendpoint", "/msgVpns/{vpn}/topicEndpoints"),
    "bridge": ("solace:semp:bridge", "/msgVpns/{vpn}/bridges"),
    "bridgeRemoteVpn": ("solace:semp:bridgeremotevpn",
                        "/msgVpns/{vpn}/bridges/{bridge}/remoteMsgVpns"),
    "restDeliveryPoint": ("solace:semp:rdp", "/msgVpns/{vpn}/restDeliveryPoints"),
    "restConsumer": ("solace:semp:restconsumer",
                     "/msgVpns/{vpn}/restDeliveryPoints/{rdp}/restConsumers"),
    "client": ("solace:semp:client", "/msgVpns/{vpn}/clients"),
}
# The v1 (XML) system collector is an extra "object type" with its own sourcetypes (see semp_v1).
SYSTEM_TYPE = "system"
ALL_TYPES = tuple(OBJECT_TYPES) + (SYSTEM_TYPE,)

_NAME_KEYS = {
    "msgVpn": "msgVpnName", "queue": "queueName", "topicEndpoint": "topicEndpointName",
    "bridge": "bridgeName", "bridgeRemoteVpn": "remoteMsgVpnName",
    "restDeliveryPoint": "restDeliveryPointName", "restConsumer": "restConsumerName",
    "client": "clientName",
}

# Config-only attributes: not useful as telemetry and they dominate event size.
_CONFIG_KEY = re.compile(
    r"^(authentication|authorization|bridgingTls|restTls|sempOverMsgBus)[A-Z]|^event[A-Z]"
)


class SempError(Exception):
    """Any SEMP / transport failure. Message never contains credentials."""


class SempAuthError(SempError):
    """HTTP 401/403 from the broker."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def validate_base_url(base_url):
    """Return a normalised https base URL or raise ValueError."""
    parsed = urllib.parse.urlsplit((base_url or "").strip())
    if parsed.scheme != "https":
        raise ValueError("SEMP base URL must use https://")
    if not parsed.hostname:
        raise ValueError("SEMP base URL has no host")
    if parsed.username or parsed.password:
        raise ValueError("Do not embed credentials in the SEMP base URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("SEMP base URL must be scheme://host[:port] only")
    return "https://" + parsed.netloc


class SempClient:
    def __init__(self, base_url, username, password, ca_file=None, timeout=30,
                 page_size=DEFAULT_PAGE_SIZE, ssl_context=None, logger=None):
        self.base_url = validate_base_url(base_url)
        self.host = urllib.parse.urlsplit(self.base_url).hostname
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._auth = "Basic " + token
        self.timeout = timeout
        self.page_size = page_size
        self.logger = logger
        ctx = ssl_context or ssl.create_default_context(cafile=ca_file)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ctx)
        )

    def _open(self, req, label):
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SempAuthError(f"HTTP {exc.code} from SEMP for {label}") from None
            raise SempError(f"HTTP {exc.code} from SEMP for {label}") from None
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            # str(exc) contains reason (e.g. certificate verify failed), never the password
            raise SempError(f"Transport error calling SEMP {label}: {exc}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SempError(f"SEMP response for {label} exceeded size cap")
        return raw

    def get_json(self, path, params=None):
        url = self.base_url + SEMP_MONITOR_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"Authorization": self._auth, "Accept": "application/json"}
        )
        raw = self._open(req, path)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise SempError(f"Non-JSON response from SEMP for {path}: {exc}") from None

    def post_xml(self, body, label="v1"):
        """POST one SEMP v1 RPC document to <base>/SEMP and return the raw XML bytes."""
        req = urllib.request.Request(
            self.base_url + SEMP_V1_PATH,
            data=body,
            method="POST",
            headers={"Authorization": self._auth, "Content-Type": "text/xml; charset=utf-8",
                     "Accept": "text/xml"},
        )
        return self._open(req, f"v1 {label}")

    def iter_collection(self, path):
        """Yield (data_item, collections_item_or_None) across all pages."""
        cursor = None
        for _ in range(MAX_PAGES):
            params = {"count": self.page_size}
            if cursor:
                params["cursor"] = cursor
            body = self.get_json(path, params)
            data = body.get("data") or []
            colls = body.get("collections") or []
            for idx, item in enumerate(data):
                yield item, (colls[idx] if idx < len(colls) else None)
            cursor = _next_cursor(body)
            if not cursor:
                return
        raise SempError(f"Paging limit ({MAX_PAGES}) hit for {path}")


def _next_cursor(body):
    paging = ((body.get("meta") or {}).get("paging")) or {}
    for key in ("cursorQuery", "nextPageUri"):
        val = paging.get(key)
        if not val:
            continue
        query = val.split("?", 1)[1] if "?" in val else val
        cur = urllib.parse.parse_qs(query).get("cursor")
        if cur:
            return cur[0]
    return None


def _matches(name, pattern):
    pattern = (pattern or "*").strip() or "*"
    return any(fnmatch.fnmatchcase(name or "", p.strip()) for p in pattern.split(",") if p.strip())


def _name_of(object_type, item):
    return item.get(_NAME_KEYS[object_type], "")


def trim_event(event, trim_config=True):
    """Shrink an event without losing telemetry.

    Always: SEMP v2 returns every counter/rate twice, flat (``rxMsgRate``) and inside nested
    ``rate`` / ``counter`` objects. Nested entries whose name already exists flat are dropped (any
    nested-only entry is kept), and null / empty values are dropped. With ``trim_config``: also
    drop config-only attributes (authentication*, authorization*, event*Threshold*, TLS settings).
    """
    out = {}
    for key, val in event.items():
        if val is None or val == "":
            continue
        if key in ("counter", "rate") and isinstance(val, dict):
            rest = {k: v for k, v in val.items() if k not in event and v is not None and v != ""}
            if rest:
                out[key] = rest
            continue
        for prefix in ("counter.", "rate."):  # dotted form, in case a broker flattens them
            if key.startswith(prefix) and key[len(prefix):] in event:
                break
        else:
            if trim_config and _CONFIG_KEY.match(key):
                continue
            out[key] = val
    return out


def build_event(object_type, broker, vpn, item, collections, parent=None, trim_config=True):
    """One flat-ish JSON-able dict per object per poll."""
    event = {"broker": broker, "semp_object": object_type}
    event.update(item)
    if vpn and "msgVpnName" not in event:
        event["msgVpnName"] = vpn
    for key, val in (parent or {}).items():
        event.setdefault(key, val)
    if collections:
        count = ((collections.get("msgs") or {}).get("count"))
        if count is not None:
            # *Current* number of messages on the endpoint. Unlike spooledMsgCount,
            # which is cumulative, this is the real queue depth.
            event["msgsCount"] = count
    return trim_event(event, trim_config)


def _quote(value):
    return urllib.parse.quote(value, safe="")


def collect(client, broker, object_types, vpn_filter="*", name_filter="*", logger=None,
            max_items=DEFAULT_MAX_ITEMS, trim_config=True):
    """Poll SEMP once. Returns list of (sourcetype, event_dict).

    A failure on a single VPN/object type (including 403 because the read-only
    user cannot see that VPN) is logged and skipped so it does not blank the
    whole poll. Only a failure to list the VPNs themselves aborts the poll.
    ``max_items`` caps events per (VPN, object type); truncation is logged.
    The ``system`` type (SEMP v1) is collected independently of the VPN walk.
    """
    out = []
    wanted = [t for t in object_types if t in OBJECT_TYPES]
    want = set(wanted)

    def warn(fmt, *args):
        if logger:
            logger.warning(fmt, *args)

    def fetch(path, otype, vpn, parent=None, name_check=True):
        """Yield (item, event) for one collection, honouring filters and the item cap."""
        n = 0
        for item, colls in client.iter_collection(path):
            if name_check and not _matches(_name_of(otype, item), name_filter):
                continue
            if max_items and n >= max_items:
                warn("Truncated %s in VPN %s at max_items=%d (raise it or narrow the name filter)",
                     otype, vpn, max_items)
                return
            n += 1
            yield item, build_event(otype, broker, vpn, item, colls, parent, trim_config)

    if want:
        vpns = []
        try:
            for item, _ in client.iter_collection("/msgVpns"):
                name = item.get("msgVpnName", "")
                if name and _matches(name, vpn_filter):
                    vpns.append(name)
                    if "msgVpn" in want:
                        out.append((OBJECT_TYPES["msgVpn"][0],
                                    build_event("msgVpn", broker, name, item, None,
                                                trim_config=trim_config)))
        except SempAuthError:
            raise
        except SempError as exc:
            if logger:
                logger.error("Could not list message VPNs: %s", exc)
            raise

        for vpn in vpns:
            qvpn = _quote(vpn)

            def run(otype, path, parent=None, name_check=True, sink=True):
                """Collect one collection; per-VPN failures are skipped, not fatal."""
                items = []
                try:
                    for item, ev in fetch(path, otype, vpn, parent, name_check):
                        items.append(item)
                        if sink:
                            out.append((OBJECT_TYPES[otype][0], ev))
                except SempError as exc:  # includes SempAuthError (no access to this VPN)
                    warn("Skipping %s in VPN %s: %s", otype, vpn, exc)
                return items

            for otype in ("queue", "topicEndpoint", "client"):
                if otype in want:
                    run(otype, OBJECT_TYPES[otype][1].format(vpn=qvpn))

            if "bridge" in want or "bridgeRemoteVpn" in want:
                bridges = run("bridge", OBJECT_TYPES["bridge"][1].format(vpn=qvpn),
                              sink="bridge" in want)
                if "bridgeRemoteVpn" in want:
                    for b in bridges:
                        bname, vr = b.get("bridgeName", ""), b.get("bridgeVirtualRouter", "")
                        if not bname or not vr:
                            continue
                        path = OBJECT_TYPES["bridgeRemoteVpn"][1].format(
                            vpn=qvpn, bridge=f"{_quote(bname)},{_quote(vr)}")
                        run("bridgeRemoteVpn", path, name_check=False,
                            parent={"bridgeName": bname, "bridgeVirtualRouter": vr})

            if "restDeliveryPoint" in want or "restConsumer" in want:
                rdps = run("restDeliveryPoint",
                           OBJECT_TYPES["restDeliveryPoint"][1].format(vpn=qvpn),
                           sink="restDeliveryPoint" in want)
                if "restConsumer" in want:
                    for r in rdps:
                        rname = r.get("restDeliveryPointName", "")
                        if not rname:
                            continue
                        path = OBJECT_TYPES["restConsumer"][1].format(
                            vpn=qvpn, rdp=_quote(rname))
                        run("restConsumer", path, name_check=False,
                            parent={"restDeliveryPointName": rname})

    if SYSTEM_TYPE in object_types:
        import semp_v1  # local import: needs defusedxml, only required when v1 is selected
        out.extend(semp_v1.collect_system(client, broker, logger=logger))
    return out
