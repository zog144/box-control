# image-build execution types and mapping

Date: September 14, 2026.
In response to BOX-CONTROL-BUILD-JOB-PROPOSAL.md.
Source of truth for the existing types: image-build-source-pass1-checkpoint,
`src/zog/image_build/runner.py`. The definitions below were read directly from that file.
No request/result implementation changes are made by this handoff.

## Agreement

The proposal is compatible with image-build's intended boundary. Compilation is
unprivileged. box-control owns systemd execution and its resource/lifecycle
records; root-control performs privileged preparation. image-build has no
alternate runtime backend. The controller's richer job record must not be
reduced to the small terminal result type below.

## Exact current definitions

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuildExecutionRequest:
    root: Path
    source: Path
    output: Path
    command: tuple
    environment: dict
    working_directory: str
    timeout_seconds: float
    read_only_root: bool = True
    network_access: bool = False


@dataclass(frozen=True)
class BuildExecutionResult:
    runtime_id: str
    invocation_id: str
    exit_code: int
    cleanup_complete: bool
    journal_reference: str
```

The current annotations really are bare `tuple` and `dict`. At the caller site,
`command` is a tuple of strings and `environment` maps strings to strings.
Paths are absolute, resolved host paths. These are in-process dataclasses, not
an established JSON wire format. `frozen=True` does not deeply freeze the dict;
the controller must snapshot/canonicalize the full submitted intent.

The current synchronous port is:

```python
runner = BoxControlRunner(execute=adapter_callable, timeout=3600)
# adapter_callable(request: BuildExecutionRequest) -> BuildExecutionResult
```

`BoxControlRunner.run(root, source, output, arguments, environment, log)` creates
the request, invokes the callable, and writes a local `.execution.json` receipt.
The receipt supplements the authoritative controller record. Without an adapter,
the runner raises ImageBuildError and executes nothing.

## Selected request-field mapping

Retain the current image-build field names when implementing this adapter. Use
explicit controller names where the current name would conflate distinct things.
Names in the controller column are selected integration vocabulary for the
proposal, not claims about an already implemented box-control API.

| Current image-build field | Controller name / mapping | Meaning |
| --- | --- | --- |
| `root` | registration input `prepared_root`; submission `build_root_id` | Import the completed prepared root and bind its returned identity. Never pass it as a published application generation. |
| `source` | registration input `source_directory`; submission `source_workspace_id` | Exclusive writable workspace mounted at `/image-build/source`. |
| `output` | registration input `output_directory`; submission `output_workspace_id` | Writable output workspace mounted at `/image-build/output`; retain it for image-build. |
| `command` | `command` | Exact argv. Resolve bare/relative executable paths within the registered root and explicit working directory/PATH. Record the resolved executable separately as `resolved_executable`. |
| `environment` | `environment` | Explicit string mapping; do not inherit the host environment. |
| `working_directory` | `working_directory` | In-root path; currently `/image-build/source`. |
| `timeout_seconds` | `execution_timeout_seconds` | Enforced execution bound, currently defaulting to 3600 seconds in BoxControlRunner. It is not the caller's wait timeout. |
| `read_only_root` | `read_only_root` | Must be true for this integration. Unsupported enforcement rejects execution. |
| `network_access` | `network_access` | Must be false for this integration. Unsupported enforcement rejects execution. |

Current runner environment defaults are:

```python
{
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C",
    "LC_ALL": "C",
    # Recipe environment is merged here.
    "HOME": "/tmp",
    "DESTDIR": "/image-build/output",
    "IMAGE_BUILD_SOURCE": "/image-build/source",
    "IMAGE_BUILD_OUTPUT": "/image-build/output",
}
```

Recipes may override PATH/locale through their explicit environment. HOME,
DESTDIR and the IMAGE_BUILD paths are set by the runner. Source preparation and
output staging happen before the callback. All commands, including install
commands into DESTDIR, run unprivileged.

## Controller inputs absent from the current request

These fields must be supplied by explicit adapter/controller configuration and
bound into the controller's durable intent. Their absence from the old type does
not authorize guessing a user identity or omitting required controls.

| Selected name | Source / requirement |
| --- | --- |
| `request_id` | Obtain through `issue_build_request_id` and durably associate it with the image-build command attempt before submitting. Reuse it after caller loss. |
| `input_manifest_id` | Identity of the prepared root's verified input manifest, supplied at registration. The current callback lacks this value; extend adapter context or the request before production wiring. |
| `execution_user_id`, `execution_group_id` | Explicit nonzero numeric UID/GID from configured build identity; do not use root or infer these from the host caller. |
| `startup_timeout_seconds` | Separate configured startup bound. |
| `termination_grace_seconds` | Separate configured bounded termination grace. |
| `wait_timeout_seconds` | Caller wait bound only; not part of execution identity and not an implicit cancellation request. |
| `resource_limits` | Explicit validated controller limit configuration. Individual limit keys should follow the controller's existing resource model; this handoff adds no defaults. |

The current runner does not yet persist request IDs or registration identities.
Production adapter work must add that durable association. A process-local map or
fresh ID after a lost reply is insufficient. Failure before an ID is known must
not be reported as successful submission.

## Selected result mapping

Use `process_cleanup_complete` and `resources_released` as distinct controller
fields. Keep execution classification in `outcome`; keep asynchronous progress
in `state`. Retain signal/timeout/cancellation/fault details independently.

| Current image-build result field | Controller evidence | Mapping |
| --- | --- | --- |
| `runtime_id` | `runtime_id` | Exact controller-owned execution runtime identity. |
| `invocation_id` | `invocation_id` | Exact systemd Invocation ID, not the unit name or PID. |
| `exit_code` | `exit_code` | Numeric designated-command exit status, only when proven. Never invent zero for absent evidence. |
| `cleanup_complete` | `process_cleanup_complete` | All processes are gone and required service/slice/mount cleanup is complete. Output retention does not make this false. |
| `journal_reference` | `journal_reference` | Nonempty Invocation-scoped reference string. Its concrete locator syntax is controller-owned; it does not promise log retention. |

A successful result additionally requires controller `outcome` to represent
success, a proven zero exit and complete process cleanup. Do not erase a timeout,
cancellation or infrastructure fault merely because some numeric exit is zero.

`resources_released` is intentionally **not** mapped to `cleanup_complete`.
Output data normally remains registered/retained after process cleanup so
image-build can validate and compose it. Release through the controller only
after consumption, and never while execution or cleanup is uncertain.

## Pending and unsuccessful results

The existing BuildExecutionResult is a **terminal projection**, not a complete
job-status union. It cannot represent a pending wait, signal-only result,
pre-exec failure without Invocation ID, or unknown outcome faithfully.

- `inspect_build_job` / `refresh_build_job` should retain the proposal's richer
  status, outcome and evidence record.
- Caller wait expiry returns pending at that controller API. The adapter must
  retain the same request/job/resource identities. It must not create a terminal
  BuildExecutionResult or submit a replacement command.
- For the adapter, select a typed `BuildExecutionPending` exception carrying
  `request_id`, `job_id`, `build_root_id`, `source_workspace_id`,
  `output_workspace_id` and `state`. This planned exception is not in the current
  source. Add explicit resumable handling before wiring bounded waits; the current
  generic ImageBuildError handler does not implement resumption.
- Proven nonzero normal exit may use the existing terminal projection; the runner
  rejects it and preserves its receipt. Signal, timeout, cancellation, fault and
  unknown outcomes must keep their controller classification and cause no image
  publication; do not manufacture an exit code just to fit this dataclass.

No architectural decision is required to supply these exact current types.
The implementation must extend the narrow adapter where the accepted controller
semantics cannot be represented, rather than weakening those semantics.

## Workspace continuity across commands

The current engine calls the runner separately for prepare/configure/build/test/
install commands. It deliberately reuses one package's source and output paths
across those calls, because build outputs must survive into test/install steps.
Different packages and different attempts have separate paths.

Registration must therefore preserve that continuity while allowing at most one
active command to write those workspaces. Keep the package's registered resource
identities across its sequential commands; do not replace its source/output with
fresh empty copies for each command. The prepared root remains unchanged.
If registration relocates output data, the adapter must make the registered
output available at the path image-build consumes before returning completion;
it must not leave image-build inspecting an empty original directory.

## Historical fixture and maintained source ownership

The original integration handoff used a transport archive containing an
image-build checkout, runner types, tests and a `create_packages.py` fixture.
That archive was a dated transfer artifact, **not** the maintained source or a
required dependency of this public documentation. It is intentionally not
distributed with the canonical manual; do not infer current implementation
from an unavailable ZIP.

For the current controller boundary, use
[interfaces and integration](guide/interfaces-and-integration.md),
[finite build jobs](catalogue/build-jobs.md), and
[registration recovery](REGISTRATION-RECOVERY.md). The actual image-build
producer and recipes are selected from its **own exact source revision**;
its published interfaces and tests must be checked against the matching
box-control revision. No vendor copy or unstated transport archive establishes
compatibility.

The historical compilation fixture generated a static library and a
statically linked executable printing `42`, requiring cc, ar, shell/basic
tools and static libc support in the prepared toolchain. Its library was
build-only, not intended for the final image. That isolated example did
not constitute full controller integration, AWS execution acceptance,
toolchain self-hosting, or current recipe verification.
