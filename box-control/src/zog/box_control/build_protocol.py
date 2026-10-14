"""Unprivileged validation for build RPC identities and requests.

Root-control independently validates the same wire request at the privilege
boundary. Keeping this copy in box-control avoids importing privileged server
implementation code into the controller.
"""
import json
import math
from pathlib import PurePosixPath

from .errors import RuntimeOperationError


def identity(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise RuntimeOperationError("invalid build identity")
    return value


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 604800:
        raise RuntimeOperationError(f"invalid {label}")
    return value


def canonical_request(raw):
    try:
        value = json.loads(json.dumps(raw, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise RuntimeOperationError("invalid build request") from exc
    required = {
        "build_root_id", "source_workspace_id", "output_workspace_id", "command", "environment",
        "working_directory", "execution_user_id", "execution_group_id", "startup_timeout_seconds",
        "execution_timeout_seconds", "termination_grace_seconds", "resource_limits", "read_only_root",
        "network_access",
    }
    if set(value) - {"device_profile"} != required:
        raise RuntimeOperationError("build request has missing or unsupported fields")
    if "device_profile" in value and value["device_profile"] != "private-null-permission-test":
        raise RuntimeOperationError("unsupported build device profile")
    root = identity(value["build_root_id"])
    if value["source_workspace_id"] != root + "-source" or value["output_workspace_id"] != root + "-output":
        raise RuntimeOperationError("workspace does not belong to registered build root")
    if value["read_only_root"] is not True or value["network_access"] is not False:
        raise RuntimeOperationError("build requires read-only root and disabled network")
    for key in ("execution_user_id", "execution_group_id"):
        if type(value[key]) is not int or not 0 < value[key] < 2**31:
            raise RuntimeOperationError("explicit nonzero build UID/GID required")
    for key in ("startup_timeout_seconds", "execution_timeout_seconds", "termination_grace_seconds"):
        _positive(value[key], key)
    if (
        not isinstance(value["command"], list)
        or not value["command"]
        or any(not isinstance(s, str) or "\0" in s for s in value["command"])
        or not value["command"][0]
    ):
        raise RuntimeOperationError("build command must be nonempty string argv")
    env = value["environment"]
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not k or "=" in k or "\0" in k
        or not isinstance(v, str) or "\0" in v
        for k, v in env.items()
    ):
        raise RuntimeOperationError("invalid explicit build environment")
    directory = value["working_directory"]
    if not isinstance(directory, str) or not directory.startswith("/") or ".." in PurePosixPath(directory).parts or "\0" in directory:
        raise RuntimeOperationError("invalid in-root working directory")
    limits = value["resource_limits"]
    if not isinstance(limits, dict) or set(limits) - {"stack-maximum-bytes", "cpu-weight"} != {"thread-count-maximum", "memory-maximum-bytes"}:
        raise RuntimeOperationError("explicit thread-count-maximum and memory-maximum-bytes required")
    if any(type(v) is not int or not 0 < v < 2**63 for v in limits.values()):
        raise RuntimeOperationError("invalid build resource limit")
    if "cpu-weight" in limits and not 1 <= limits["cpu-weight"] <= 10000:
        raise RuntimeOperationError("cpu-weight must be 1..10000")
    return value
