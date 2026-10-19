"""Bounded, read-only build-job status for approved image-build/gateway adapters.

Authoritative raw controller records remain private to the local build owner.
This projection cannot start, resume, cancel or release a job.
"""
from __future__ import annotations

import math
import re

from .build_protocol import identity
from .errors import RecoveryRequired

_SCHEMA = "zog-controller-build-job-status-v1"
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_STATES = {"prepared", "starting", "running", "unknown", "cancelling", "cleanup", "completed"}
_OUTCOMES = {None, "success", "nonzero-exit", "timeout", "signal",
             "fault", "cancelled", "unknown"}


def _hex(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def safe_status(raw, job_id):
    """Fail closed on a contradictory status; never project raw argv or env."""
    identity(job_id)
    if (type(raw) is not dict or raw.get("schema") != 1
            or raw.get("entity") != "job:" + job_id
            or raw.get("job_id") != job_id
            or raw.get("state") not in _STATES
            or raw.get("outcome") not in _OUTCOMES
            or not _hex(raw.get("request", {}).get("build_root_id"))
            or raw.get("runtime_id") != "build-" + job_id
            or type(raw.get("request_id")) is not str
            or not 1 <= len(raw["request_id"]) <= 200
            or type(raw.get("process_cleanup_complete")) is not bool
            or type(raw.get("resources_released")) is not bool):
        raise RecoveryRequired("invalid saved build status")
    if (raw["resources_released"] and not raw["process_cleanup_complete"]
            or raw["state"] == "completed" and not raw["process_cleanup_complete"]):
        raise RecoveryRequired("build cleanup and terminal state disagree")
    completed = raw.get("completed_at")
    if completed is not None and (type(completed) not in (float, int)
                                  or not math.isfinite(completed) or completed < 0):
        raise RecoveryRequired("invalid build completion timestamp")
    result = raw.get("service_result")
    if result is not None and (type(result) is not str or len(result) > 64
                               or not re.fullmatch(r"[a-z0-9-]+", result)):
        raise RecoveryRequired("invalid build service result")
    code = raw.get("exit_code")
    signal = raw.get("signal")
    if any(x is not None and (type(x) is not int or not 0 <= x < 2**31)
           for x in (code, signal)):
        raise RecoveryRequired("invalid build exit evidence")
    boot, invocation = raw.get("boot_id"), raw.get("invocation_id")
    # A locatable journal stream is not proof that its contents are retained.
    evidence = (
        {"kind": "systemd-journal-invocation", "boot_id": boot,
         "invocation_id": invocation}
        if _hex(boot) and _hex(invocation) else None
    )
    if raw["outcome"] == "success" and raw["process_cleanup_complete"] and (
            code != 0 or evidence is None):
        raise RecoveryRequired("completed build success lacks invocation evidence")
    return {
        "schema": _SCHEMA,
        "job_id": job_id,
        "request_id": raw["request_id"],
        "build_root_id": raw["request"]["build_root_id"],
        "runtime_id": raw["runtime_id"],
        "state": raw["state"],
        "outcome": raw["outcome"],
        "process_cleanup_complete": raw["process_cleanup_complete"],
        "resources_released": raw["resources_released"],
        "exit_code": code,
        "signal": signal,
        "service_result": result,
        "completed_at": completed,
        "uncertain": raw["state"] in {"starting", "unknown"}
                     or raw["outcome"] == "unknown",
        "evidence_reference": evidence,
    }
