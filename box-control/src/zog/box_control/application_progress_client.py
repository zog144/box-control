"""Strict client for the controller-owner's read-only progress socket.

The caller must pin the expected *controller owner* UID from a separately
trusted installation policy. Socket location and peer identity are not learned
from an untrusted page or the reply's JSON.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import socket
import struct

from .application_progress import SCHEMA, _HEX, _RUNTIME, _TOKEN, _APPLICATION, _VIEW, _BOOT
from .application_progress_broker import WIRE
from .role_broker import _json_bytes, _peer_uid, _read_exact, _unique_pairs


class ProgressClientError(ValueError):
    pass


class ApplicationProgressClient:
    def __init__(self, socket_path, *, controller_uid, timeout_seconds=30):
        path = Path(socket_path)
        if (not path.is_absolute() or path == Path("/") or ".." in path.parts
                or str(path) != str(path.resolve())):
            raise ProgressClientError("invalid-pinned-controller-socket")
        if type(controller_uid) is not int or not 1 <= controller_uid < 2**31:
            raise ProgressClientError("invalid-controller-uid")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 35:
            raise ProgressClientError("invalid-timeout")
        self.socket_path = path
        self.controller_uid = controller_uid
        self.timeout_seconds = timeout_seconds

    def _request(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout_seconds)
            conn.connect(str(self.socket_path))
            if _peer_uid(conn) != self.controller_uid:
                raise ProgressClientError("controller-peer-identity-mismatch")
            conn.sendall(_json_bytes(payload, 4096))
            count = struct.unpack("!I", _read_exact(conn, 4))[0]
            if not 0 < count <= 32768:
                raise ProgressClientError("invalid-progress-reply-length")
            raw = _read_exact(conn, count)
        try:
            result = json.loads(raw, object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeError):
            raise ProgressClientError("invalid-progress-reply") from None
        if (type(result) is not dict or set(result) != {"ok", "result"}
                or result["ok"] is not True):
            raise ProgressClientError("progress-unavailable")
        return result["result"]

    def snapshot(self, *, after_build=None, after_runtime=None, limit=4):
        if (type(limit) is not int or not 1 <= limit <= 4
                or after_build is not None and (
                    type(after_build) is not str or not _HEX.fullmatch(after_build))
                or after_runtime is not None and (
                    type(after_runtime) is not str or not _RUNTIME.fullmatch(after_runtime))):
            raise ProgressClientError("invalid-progress-cursor")
        value = self._request({
            "schema": WIRE, "operation": "snapshot",
            "after_build": after_build, "after_runtime": after_runtime,
            "limit": limit,
        })
        if (type(value) is not dict or
                set(value) != {"schema", "host_id", "installation_id",
                               "snapshot_id", "observed_at", "boot_id",
                               "items", "page"}
                or value["schema"] != SCHEMA
                or value["host_id"] is not None or value["installation_id"] is not None
                or type(value["snapshot_id"]) is not str
                or not _HEX.fullmatch(value["snapshot_id"])
                or type(value["items"]) is not list
                or len(value["items"]) > 2 * limit
                or (value["boot_id"] is not None and
                    (type(value["boot_id"]) is not str or
                     not _BOOT.fullmatch(value["boot_id"])))):
            raise ProgressClientError("untrusted-progress-envelope")
        try:
            observed = datetime.fromisoformat(value["observed_at"])
            if observed.tzinfo is None:
                raise ValueError("naive")
            observed = observed.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            if not now - timedelta(minutes=2) <= observed <= now + timedelta(minutes=2):
                raise ValueError("stale")
        except (ValueError, TypeError, OverflowError):
            raise ProgressClientError("untrusted-progress-time") from None
        expected_fields = {"application", "producer", "operation_id",
                           "attempt_id", "stage_id", "package", "command_index",
                           "build_job_id", "controller_request_id", "runtime_id",
                           "selected_software_id", "phase", "state", "observed_at",
                           "evidence_ref", "progress", "message_code"}
        phases = {"queued", "building", "launching"}
        states = {"waiting", "running", "succeeded", "blocked", "failed",
                  "cancelled", "uncertain", "unknown"}
        codes = {"build-awaiting-start", "build-job-running",
                 "build-command-succeeded", "build-command-cancelled",
                 "build-command-failed", "build-outcome-uncertain",
                 "build-state-unrecognized", "build-state-unobserved", "build-evidence-unavailable",
                 "runtime-observation-unavailable", "runtime-observed-active",
                 "runtime-terminated-successfully", "runtime-terminal-unverified",
                 "runtime-failed", "runtime-cleanup-or-state-uncertain"}
        for item in value["items"]:
            if (type(item) is not dict or set(item) != expected_fields
                    or item["producer"] != "box-control"
                    or item["phase"] not in phases or item["state"] not in states
                    or item["message_code"] not in codes or item["progress"] is not None
                    or item["observed_at"] != value["observed_at"]
                    or item["attempt_id"] is not None or item["stage_id"] is not None
                    or item["package"] is not None or item["command_index"] is not None
                    or type(item["operation_id"]) is not str
                    or len(item["operation_id"]) > 128):
                raise ProgressClientError("invalid-progress-item")
            build_id = item["build_job_id"]
            runtime_id = item["runtime_id"]
            token = item["controller_request_id"]
            if token is not None and (type(token) is not str or
                                      not _TOKEN.fullmatch(token)):
                raise ProgressClientError("invalid-progress-request-id")
            if build_id is not None:
                if (type(build_id) is not str or not _HEX.fullmatch(build_id)
                        or runtime_id is not None or item["application"] is not None
                        or item["selected_software_id"] is not None
                        or item["operation_id"] != "build:" + build_id
                        or item["evidence_ref"] != "build-job:" + build_id
                        or item["phase"] not in {"queued", "building"}
                        or not item["message_code"].startswith("build-")):
                    raise ProgressClientError("invalid-build-progress-identity")
            else:
                name = item["application"]
                view = item["selected_software_id"]
                if (type(runtime_id) is not str or not _RUNTIME.fullmatch(runtime_id)
                        or type(name) is not str or not _APPLICATION.fullmatch(name)
                        or view is not None and (
                            type(view) is not str or not _VIEW.fullmatch(view))
                        or item["operation_id"] != "runtime:" + runtime_id
                        or item["evidence_ref"] != "runtime:" + runtime_id
                        or item["phase"] != "launching"
                        or not item["message_code"].startswith("runtime-")):
                    raise ProgressClientError("invalid-runtime-progress-identity")
        page = value["page"]
        if (type(page) is not dict or
                set(page) != {"build_has_more", "runtime_has_more",
                              "next_build", "next_runtime"}
                or type(page["build_has_more"]) is not bool
                or type(page["runtime_has_more"]) is not bool
                or (page["next_build"] is not None and
                    (type(page["next_build"]) is not str or
                     not _HEX.fullmatch(page["next_build"])))
                or (page["next_runtime"] is not None and
                    (type(page["next_runtime"]) is not str or
                     not _RUNTIME.fullmatch(page["next_runtime"])))):
            raise ProgressClientError("invalid-progress-page")
        return value

    def local_session(self):
        result = self._request({"schema": WIRE, "operation": "local-session"})
        from .application_progress import local_session_capabilities
        if result != local_session_capabilities():
            raise ProgressClientError("invalid-local-session-capability")
        return result
