"""Read-only, producer-owned progress projection. Never admits build or runtime work.

Controller records are evidence of controller work, not image-build publication,
installation completion or graphical login readiness. image-build joins its own
attempt/stage/package/command records independently.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import secrets

SCHEMA = "zog-application-progress-v1"
_LOCAL = "zog-local-session-capabilities-v1"
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_RUNTIME = re.compile(r"[A-Za-z0-9-]{1,80}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9-]{1,200}\Z")
_APPLICATION = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
_VIEW = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BOOT = re.compile(r"[0-9a-fA-F-]{32,40}\Z")


def _identifier(value, pattern):
    return value if type(value) is str and pattern.fullmatch(value) else None


def local_session_capabilities():
    """No local seat, PAM, VT or logind authority exists in workspace-v1."""
    return {
        "schema": _LOCAL,
        "supported": False,
        "status": "not-implemented",
        "physical_seat": "unavailable",
        "virtual_seat": "unavailable",
        "login_role": "not-authorized",
        "workspace_v1_is_local_seat": False,
    }


def _item(*, application, producer, operation_id, phase, state, observed_at,
          message_code, attempt_id=None, stage_id=None, package=None,
          command_index=None, build_job_id=None, controller_request_id=None,
          runtime_id=None, selected_software_id=None, evidence_ref=None):
    return dict(
        application=application, producer=producer, operation_id=operation_id,
        attempt_id=attempt_id, stage_id=stage_id, package=package,
        command_index=command_index, build_job_id=build_job_id,
        controller_request_id=controller_request_id, runtime_id=runtime_id,
        selected_software_id=selected_software_id, phase=phase, state=state,
        observed_at=observed_at, evidence_ref=evidence_ref, progress=None,
        message_code=message_code,
    )


def _build_job(control, summary, now):
    if type(summary) is not dict or _identifier(summary.get("job_id"), _HEX) is None:
        raise ValueError("invalid persisted build listing identity")
    job_id = summary["job_id"]
    fields = dict(application=None, producer="box-control",
                  operation_id="build:" + job_id, build_job_id=job_id,
                  evidence_ref="build-job:" + job_id, observed_at=now)
    try:
        record = control.inspect_build_job(job_id)
        if (type(record) is not dict or record.get("job_id") != job_id
                or record.get("entity") != "job:" + job_id
                or summary.get("state") != record.get("state")
                or summary.get("outcome") != record.get("outcome")):
            raise ValueError("inconsistent build record")
        request_id = _identifier(record.get("request_id"), _TOKEN)
        if request_id is None:
            raise ValueError("invalid build request")
        fields["controller_request_id"] = request_id
        phase = "queued" if record["state"] == "prepared" else "building"
        state = record["state"]
        outcome = record.get("outcome")
        if state == "prepared":
            status, code = "waiting", "build-awaiting-start"
        elif state in ("starting", "running") and outcome is None:
            # A persisted job state is not a fresh kernel observation.
            # After a lost response or reboot it may no longer be running.
            status, code = "unknown", "build-state-unobserved"
        elif state == "completed" and record.get("process_cleanup_complete") is True:
            if outcome == "success":
                status, code = "succeeded", "build-command-succeeded"
            elif outcome == "cancelled":
                status, code = "cancelled", "build-command-cancelled"
            elif outcome in ("timeout", "nonzero-exit", "signal", "fault"):
                status, code = "failed", "build-command-failed"
            else:
                status, code = "uncertain", "build-outcome-uncertain"
        elif state in ("unknown", "cancelling", "cleanup", "starting", "running"):
            status, code = "uncertain", "build-outcome-uncertain"
        else:
            status, code = "unknown", "build-state-unrecognized"
        # A successful build command does not prove package verification,
        # immutable software publication or graphical runtime readiness.
        return _item(**fields, phase=phase, state=status, message_code=code)
    except Exception:
        return _item(**fields, phase="building", state="unknown",
                     message_code="build-evidence-unavailable")


def _runtime_item(control, ref, now, boot_id):
    runtime_id = _identifier(getattr(ref, "runtime_id", None), _RUNTIME)
    app = _identifier(getattr(ref, "application", None), _APPLICATION)
    if runtime_id is None or app is None:
        raise ValueError("invalid persisted runtime listing identity")
    request_id = _identifier(getattr(ref, "request_id", None), _TOKEN)
    view = _identifier(getattr(ref, "software_view", None), _VIEW)
    fields = dict(
        application=app, producer="box-control", operation_id="runtime:" + runtime_id,
        runtime_id=runtime_id, controller_request_id=request_id,
        selected_software_id=view, evidence_ref="runtime:" + runtime_id,
        observed_at=now, phase="launching",
    )
    state = getattr(getattr(ref, "state", None), "value", getattr(ref, "state", None))
    # Saved state alone does NOT assert the systemd service still exists.
    if state in ("starting", "running", "terminating"):
        status, code = "unknown", "runtime-observation-unavailable"
        if (boot_id is not None and getattr(ref, "boot_id", None) == boot_id):
            try:
                observed = control.observe_application_runtime(runtime_id)
                programs = observed.get("programs")
                if (observed.get("runtime_id") == runtime_id
                        and observed.get("status") == "observed"
                        and type(programs) is list and programs):
                    states = [p.get("observation", {}).get("active_state")
                              if p.get("status") == "observed" and
                              type(p.get("observation")) is dict else None
                              for p in programs]
                    if state == "running" and all(s == "active" for s in states):
                        status, code = "running", "runtime-observed-active"
            except Exception:
                pass
    elif state == "terminated" and not getattr(ref, "cleanup_pending", True):
        programs = getattr(ref, "programs", ())
        if programs and all(getattr(p, "result", None) == "success" for p in programs) and not getattr(ref, "error", None):
            status, code = "succeeded", "runtime-terminated-successfully"
        else:
            status, code = "unknown", "runtime-terminal-unverified"
    elif state == "failed":
        status, code = "failed", "runtime-failed"
    else:
        status, code = "uncertain", "runtime-cleanup-or-state-uncertain"
    return _item(**fields, state=status, message_code=code)


def observe_application_progress(control, *, after_build=None, after_runtime=None, limit=4):
    """Bounded, read-only, non-atomic pages. Never follows requested host paths.

    Application/name associations for build jobs MUST be supplied by the
    separately authenticated image-build producer; absent here is intentional.
    """
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("progress page limit must be 1..4")
    if after_build is not None and _identifier(after_build, _HEX) is None:
        raise ValueError("invalid build cursor")
    if after_runtime is not None and _identifier(after_runtime, _RUNTIME) is None:
        raise ValueError("invalid runtime cursor")
    now = datetime.now(timezone.utc).isoformat()
    try:
        value = control.boot_id_provider()
        boot_id = _identifier(value, _BOOT)
    except Exception:
        boot_id = None
    page = control.list_build_jobs(after=after_build, limit=limit)
    if (type(page) is not dict or type(page.get("jobs")) is not list
            or len(page["jobs"]) > limit
            or type(page.get("has_more")) is not bool):
        raise ValueError("untrusted build listing")
    jobs = [_build_job(control, item, now) for item in page["jobs"]]
    references = control.application_runtimes()
    if type(references) is not dict:
        raise ValueError("untrusted runtime listing")
    keys = sorted(key for key in references
                  if _identifier(key, _RUNTIME) is not None
                  and (after_runtime is None or key > after_runtime))
    selected = keys[:limit]
    runtimes = [_runtime_item(control, references[key], now, boot_id)
                for key in selected]
    if any(row["runtime_id"] != key for row, key in zip(runtimes, selected)):
        raise ValueError("runtime listing identity mismatch")
    return {
        "schema": SCHEMA, "host_id": None, "installation_id": None,
        "snapshot_id": secrets.token_hex(16), "observed_at": now, "boot_id": boot_id,
        "items": jobs + runtimes,
        "page": {
            "build_has_more": page["has_more"],
            "runtime_has_more": len(keys) > limit,
            "next_build": jobs[-1]["build_job_id"] if jobs else after_build,
            "next_runtime": selected[-1] if selected else after_runtime,
        },
    }
