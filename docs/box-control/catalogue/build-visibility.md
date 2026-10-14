# Build visibility integration

Image-build owns the grouping: attempt → stage → package → phase and command index → controller job. These are finite build jobs. Source/output workspaces persist between commands; application restart isolation is unchanged.

## Controller API

`control.build_job_logs(job_id, cursor=None, limit=50)` returns job_id, entries, next_cursor, has_more, status. Entry shape and polling rules match application logs: limits 1–100, five-second journal subprocess deadline, 256 KiB output budget. Both log APIs share two reader slots independent of serialized lifecycle work.

`control.list_build_jobs(after=None, limit=50)` returns jobs, next_after, has_more, ordered by job ID. Summaries expose state, outcome, exit code/signal, process cleanup, resource release and log identity availability. This is persisted state, not a systemd refresh. `inspect_build_job(job_id)` supplies the full record; authorize access before exposing it.

Log queries use saved boot and invocation identities. Cursors cannot cross jobs. Missing identity returns identity-unavailable; no entries returns empty-history; a lost journal anchor returns cursor-unavailable; reader failure returns unavailable without advancing the cursor. Unknown/pruned job records raise an error. Cleanup and resource release retain journal identities; controller pruning eventually removes this lookup independently of journald retention. Existing application cursors remain compatible.

## Image-build and station-access handoff

The bundled integration checkout writes *.view.json before invoking package commands and toolchain probes. `image_build.build_views.read_build_commands(authorized_attempt_directory, after=None, limit=50)` joins these views to durable controller checkpoints. Entries include attempt ID, stage ID, package (null for probes), phase, command index, argv, command key, request ID and job ID. Missing checkpoints produce null IDs; a mapping alone does not prove acceptance. Job identity is saved before submission; older checkpoints derive it from request ID.

Station-access authorizes the attempt/job before calling these APIs. Paths are trusted local arguments, not browser inputs. Group commands into package views and reuse the application log viewer per command. Poll summaries separately from selected logs. Preserve a cursor per job and show explicit gaps on cursor-unavailable. Render messages as text. Mappings and logs are not authoritative execution outcomes.

Pages are not atomic snapshots. Entries may arrive before a returned key; restart listing from the beginning on each new polling cycle. Response sizes are bounded; filesystem enumeration still scales with retained records. No GUI, HTTP layer, merged log stream or full pipeline resume is added.

This image-build checkout is an integration handoff, not a replacement for independently developed newer code. Current bootstrap stages have individual attempt IDs; an umbrella bootstrap run ID belongs to image-build. Host seed preparation remains orchestration, while source toolchain commands use the same build views.

## Cancellation results

For an unfinished job with a recorded cancellation request, completion is reported as `cancelled`, including when systemd has to escalate after its stop timeout. The raw `service_result` and exit signal remain available in the persisted job record. Already completed outcomes are never reclassified by a later cancellation request. An execution timeout without a cancellation request remains `timeout`.
