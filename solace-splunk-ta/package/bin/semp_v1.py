"""SEMP v1 (XML) read-only ``show`` collector for broker-level / system data.

Why v1: the system-level data Dynatrace's Solace extension reports (HA/redundancy, message-spool
and disk utilisation, memory, config-sync, interfaces, services, certificate expiry...) is exposed
through SEMP v1 ``show`` commands. This module sends only ``<rpc><show>...</show></rpc>``.

Schema-agnostic on purpose: replies are flattened generically (leaf tag path -> value) instead of
hard-coding tag names, so a different broker version cannot silently break field extraction. The
exact command spellings below follow Solace's documented CLI/SEMP v1 ``show`` commands but have
NOT been verified against a live broker; a command the broker rejects is logged and skipped.

Security:
  * XML is parsed with defusedxml (no DTD / entity expansion / external entities).
  * Command words are validated against a strict allow-list pattern and the request is always a
    ``show``; nothing that can change broker state can be built here.
  * Reply size is capped by the client; element depth and leaf count are capped here.
"""

import re

import defusedxml.ElementTree as DET

import semp_client

# v1 requires a semp-version attribute. Brokers accept older versions; this one predates every
# currently supported release so the reply schema stays stable.
SEMP_V1_VERSION = "soltr/9_13VMR"
MAX_DEPTH = 12
MAX_LEAVES = 2000

_TOKEN = re.compile(r"^[a-z][a-z0-9-]*$")
_TAG_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_INT = re.compile(r"^-?(0|[1-9][0-9]{0,14})$")
_FLOAT = re.compile(r"^-?(0|[1-9][0-9]{0,14})\.[0-9]{1,9}$")

# (label, show-command words, sourcetype, record_tag)
#   record_tag: if set, emit one event per innermost element with this tag that has children
#   (for list-like replies such as interfaces or services); otherwise one event per command.
SYSTEM_COMMANDS = (
    ("redundancy", "redundancy detail", "solace:semp:redundancy", None),
    ("config-sync", "config-sync", "solace:semp:configsync", None),
    ("message-spool", "message-spool detail", "solace:semp:messagespool", None),
    ("memory", "memory", "solace:semp:memory", None),
    ("service", "service", "solace:semp:service", "service"),
    ("interface", "interface", "solace:semp:interface", "interface"),
    ("health", "system health", "solace:semp:health", None),
    ("global-stats", "stats client", "solace:semp:globalstats", None),
    ("replication", "replication", "solace:semp:replication", None),
    ("certificate", "ssl server-certificate", "solace:semp:certificate", None),
)


class SempV1Error(semp_client.SempError):
    """The broker answered a v1 RPC with execute-result code != ok, or unparsable XML."""


def build_rpc(words):
    """Return the XML request for ``show <words>``. Raises ValueError on anything unexpected."""
    toks = words.split()
    if not toks or not all(_TOKEN.match(t) for t in toks):
        raise ValueError(f"Unsupported SEMP v1 command: {words!r}")
    # tokens are validated above ([a-z0-9-] only), so no XML escaping is needed
    inner = "".join(f"<{t}>" for t in toks) + "".join(f"</{t}>" for t in reversed(toks))
    return (f'<rpc semp-version="{SEMP_V1_VERSION}"><show>{inner}</show></rpc>').encode("utf-8")


def _coerce(text):
    text = text.strip()
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


def _flatten(elem, prefix="", out=None, depth=0, budget=None):
    """Leaf tag paths -> values. Repeated paths collect into a list."""
    out = {} if out is None else out
    budget = budget if budget is not None else [MAX_LEAVES]
    if depth > MAX_DEPTH:
        return out
    children = list(elem)
    if not children:
        text = (elem.text or "").strip()
        if text and prefix and budget[0] > 0:
            budget[0] -= 1
            val = _coerce(text)
            if prefix in out:
                cur = out[prefix]
                out[prefix] = (cur if isinstance(cur, list) else [cur]) + [val]
            else:
                out[prefix] = val
        return out
    for child in children:
        tag = child.tag if isinstance(child.tag, str) else ""
        if not _TAG_OK.match(tag):
            continue
        _flatten(child, f"{prefix}.{tag}" if prefix else tag, out, depth + 1, budget)
    return out


def _innermost(root, tag):
    """Elements named ``tag`` that have child elements and no descendant also named ``tag``."""
    found = []
    for el in root.iter(tag):
        if len(el) and not any(d is not el for d in el.iter(tag)):
            found.append(el)
    return found


def parse_reply(raw):
    """Parse a v1 reply; return the ``<show>`` element. Raises SempV1Error on failure."""
    try:
        root = DET.fromstring(raw)
    except Exception as exc:  # defusedxml raises its own types for forbidden constructs
        raise SempV1Error(f"Unparsable SEMP v1 reply: {type(exc).__name__}") from None
    result = root.find("execute-result")
    code = (result.get("code") if result is not None else "") or ""
    if code != "ok":
        reason = ((result.get("reason") if result is not None else "") or "no reason")[:200]
        raise SempV1Error(f"SEMP v1 execute-result {code or 'missing'}: {reason}")
    show = root.find("./rpc/show")
    if show is None:
        raise SempV1Error("SEMP v1 reply has no <show> element")
    if root.find("more-cookie") is not None:
        # we never page v1; only small, single-reply commands are configured
        show.set("_truncated", "true")
    return show


def events_from_show(show, label, words, record_tag, broker):
    base = {"broker": broker, "semp_object": "system", "semp_command": f"show {words}",
            "semp_label": label}
    if show.get("_truncated"):
        base["truncated"] = True
    if record_tag:
        recs = _innermost(show, record_tag)
        if recs:
            events = []
            for rec in recs:
                ev = dict(base)
                ev.update(_flatten(rec))
                events.append(ev)
            return events
    ev = dict(base)
    ev.update(_flatten(show))
    return [ev]


def collect_system(client, broker, logger=None):
    """Run every configured ``show`` once. Per-command failures are logged and skipped."""
    out = []
    for label, words, sourcetype, record_tag in SYSTEM_COMMANDS:
        try:
            raw = client.post_xml(build_rpc(words), label=label)
            show = parse_reply(raw)
            for ev in events_from_show(show, label, words, record_tag, broker):
                out.append((sourcetype, ev))
        except semp_client.SempError as exc:  # incl. auth errors: keep the v2 data of this poll
            if logger:
                logger.warning("Skipping SEMP v1 'show %s': %s", words, exc)
    return out
