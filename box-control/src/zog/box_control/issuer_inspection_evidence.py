"""Strict redaction and schema validation for root-authenticated one-shot evidence."""
from __future__ import annotations

from datetime import datetime
import re

from .role_broker import RoleBrokerError, _REQUEST, _RUNTIME

_DIGEST = re.compile(r"[0-9a-f]{64}\\Z")
_POLICY = re.compile(r"sha256:[0-9a-f]{64}\\Z")
_STORAGE = re.compile(r"[0-9a-f]{32}\\Z")
_CERT = re.compile(r"[a-z][a-z0-9-]{0,62}\\Z")
_FIELDS = {"schema", "request_id", "runtime_id", "program", "boot_id",
           "invocation_id", "storage_id", "grant_id", "issuer_uid", "issuer_gid",
           "host_policy_digest", "inspection", "status", "tls_ready"}
_RECORD = {"schema", "certificate_id", "hostnames", "policy_sha256", "health",
           "certificate_fingerprint_sha256", "valid_from", "expires_at",
           "days_remaining", "operation", "outcome", "operation_updated_at",
           "issuer_verified", "observed_at", "tls_ready"}
_HEALTH = {"valid", "missing", "invalid", "uncertain", "expired-or-not-yet-valid"}


def _reject():
    raise RoleBrokerError("untrusted selected issuer inspection", code="evidence-unavailable")


def _match(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def sanitize_issuer_inspection(value, request_id):
    if (type(value) is not dict or set(value) != _FIELDS
            or not _match(request_id, _REQUEST)
            or value["schema"] != "zog-trusted-issuer-inspection-v1"
            or value["request_id"] != request_id
            or value["program"] != "inspect"
            or not _match(value["runtime_id"], _RUNTIME)
            or any(not _match(value[k], _STORAGE) for k in ("storage_id", "grant_id"))
            or not _match(value["host_policy_digest"], _POLICY)
            or value["tls_ready"] is not False
            or value["status"] not in ("verified", "unverified")
            or not all(type(value[k]) is int and 1000 <= value[k] < 2**31
                       for k in ("issuer_uid", "issuer_gid"))
            or not all(type(value[k]) is str
                       and re.fullmatch(r"[0-9a-f]{32}", value[k].replace("-", "").lower())
                       for k in ("boot_id", "invocation_id"))):
        _reject()
    raw = value["inspection"]
    if (type(raw) is not dict or set(raw) != _RECORD
            or raw["schema"] != "zog-certify-inspection-result-v1"
            or not _match(raw["certificate_id"], _CERT)
            or not _match(raw["policy_sha256"], _DIGEST)
            or raw["health"] not in _HEALTH
            or raw["tls_ready"] is not False
            or type(raw["issuer_verified"]) is not bool
            or (value["status"] == "verified") != raw["issuer_verified"]
            or raw["operation"] not in (None, "issue", "renew", "recover")
            or raw["outcome"] not in (None, "issued", "renewed", "unchanged",
                                      "recovered-unverified", "failed")):
        _reject()
    hosts = raw["hostnames"]
    if (type(hosts) is not list or not 1 <= len(hosts) <= 100
            or not all(type(x) is str and 1 <= len(x) <= 253 and "\x00" not in x
                       for x in hosts)):
        _reject()
    leaf = raw["certificate_fingerprint_sha256"]
    if leaf is not None and not _match(leaf, _DIGEST):
        _reject()
    days = raw["days_remaining"]
    if days is not None and (type(days) is not int or not -365000 <= days <= 365000):
        _reject()
    for key in ("observed_at", "operation_updated_at", "valid_from", "expires_at"):
        v = raw[key]
        if v is None and key != "observed_at":
            continue
        if type(v) is not str or len(v) > 80:
            _reject()
        try:
            if datetime.fromisoformat(v).utcoffset() is None:
                _reject()
        except ValueError:
            _reject()
    if raw["issuer_verified"] and (
            raw["health"] != "valid" or leaf is None
            or raw["operation"] not in ("issue", "renew")
            or raw["outcome"] not in ("issued", "renewed", "unchanged")
            or days is None
            or any(raw[x] is None for x in ("valid_from", "expires_at", "operation_updated_at"))):
        _reject()
    return {**{key: value[key] for key in sorted(_FIELDS - {"inspection"})},
            "inspection": {key: raw[key] for key in sorted(_RECORD)}}
