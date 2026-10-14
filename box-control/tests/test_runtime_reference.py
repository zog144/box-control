from zog.box_control.model import ApplicationRuntimeState
from zog.box_control.runtime.reference import (
    ApplicationRuntimeReference,
    ProgramRuntimeReference,
    RuntimeReferenceStore,
    new_application_instance_id,
    new_application_runtime_id,
)


def test_application_runtime_reference_round_trip(tmp_path):
    path = tmp_path / "references.json"
    reference = ApplicationRuntimeReference(
        runtime_id="A7K3Q2",
        application="desktop",
        instance_id="desktop",
        generation="generation-1",
        state=ApplicationRuntimeState.RUNNING,
        slice_name="zog-project-A7K3Q2.slice",
        programs=(
            ProgramRuntimeReference(
                program="browser",
                unit_name="zog-project-A7K3Q2-browser.service",
                command=("/usr/bin/browser",),
                invocation_id="invocation-id",
                expected_properties=(("Type", "exec"), ("RemainAfterExit", False)),
                active_state="active",
                sub_state="running",
                result="success",
                main_pid=123,
                control_group="/zog-project-A7K3Q2-browser.service",
            ),
        ),
    )
    store = RuntimeReferenceStore(path)
    store.save({reference.runtime_id: reference})
    assert store.load() == {reference.runtime_id: reference}


def test_runtime_identity_generator_is_service_name_safe():
    runtime_id = new_application_runtime_id()
    assert len(runtime_id) == 6
    assert runtime_id.isalnum()
    assert runtime_id.upper() == runtime_id


def test_completed_runtime_history_keeps_latest_25_and_never_prunes_live(tmp_path):
    store = RuntimeReferenceStore(tmp_path / "references.json")
    references = {}
    for index in range(30):
        runtime_id = f"R{index:05d}"
        references[runtime_id] = ApplicationRuntimeReference(
            runtime_id=runtime_id,
            application="desktop",
            instance_id="desktop",
            generation="generation-1",
            state=ApplicationRuntimeState.TERMINATED,
            created_at=float(index),
            completed_at=float(index),
            cleanup_pending=False,
        )
    references["LIVE01"] = ApplicationRuntimeReference(
        runtime_id="LIVE01",
        application="desktop",
        instance_id="desktop",
        generation="generation-1",
        state=ApplicationRuntimeState.RUNNING,
        created_at=-1.0,
    )
    removed = store.prune_completed(references)
    assert len(removed) == 5
    assert "LIVE01" in references
    assert len([r for r in references.values() if r.state == ApplicationRuntimeState.TERMINATED]) == 25


def test_multi_instance_identity_is_stable_for_request_and_singleton_is_name():
    assert new_application_instance_id(
        "desktop", multiple_instances=False, request_id="REQUEST-1"
    ) == "desktop"
    first = new_application_instance_id(
        "desktop", multiple_instances=True, request_id="REQUEST-1"
    )
    second = new_application_instance_id(
        "desktop", multiple_instances=True, request_id="REQUEST-1"
    )
    assert first == second
    assert first.startswith("desktop-")


def test_schema_two_reference_loads_with_singleton_instance_identity(tmp_path):
    path = tmp_path / "references.json"
    path.write_text(
        '{"schema":2,"runtimes":{"OLD001":{"runtime_id":"OLD001",'
        '"application":"desktop","generation":"generation-1",'
        '"state":"terminated","programs":[]}}}',
        encoding="utf-8",
    )
    reference = RuntimeReferenceStore(path).load()["OLD001"]
    assert reference.instance_id == "desktop"


def test_history_orders_by_completion_not_launch_time(tmp_path):
    store = RuntimeReferenceStore(tmp_path / "references.json")
    older_launch_later_completion = ApplicationRuntimeReference(
        runtime_id="OLDER1",
        application="desktop",
        instance_id="desktop",
        generation="generation-1",
        state=ApplicationRuntimeState.TERMINATED,
        created_at=1.0,
        completed_at=4.0,
    )
    newer_launch_earlier_completion = ApplicationRuntimeReference(
        runtime_id="NEWER1",
        application="desktop",
        instance_id="desktop",
        generation="generation-1",
        state=ApplicationRuntimeState.TERMINATED,
        created_at=2.0,
        completed_at=3.0,
    )
    references = {
        item.runtime_id: item
        for item in (older_launch_later_completion, newer_launch_earlier_completion)
    }
    assert [item.runtime_id for item in store.history(references)] == [
        "OLDER1",
        "NEWER1",
    ]


def test_schema_two_save_reload_preserves_identity_and_state(tmp_path):
    import json

    path = tmp_path / "references.json"
    path.write_text(json.dumps({"schema": 2, "runtimes": {"OLD001": {
        "runtime_id": "OLD001", "application": "desktop",
        "generation": "generation-1", "state": "failed",
        "created_at": 10.0, "completed_at": 11.0, "error": "exec failed",
        "programs": [{"program": "browser", "unit_name": "browser.service",
                      "invocation_id": "original", "result": "exit-code",
                      "expected_properties": [["ExecStart", ["/usr/bin/browser"]]]}],
    }}}), encoding="utf-8")
    store = RuntimeReferenceStore(path)
    original = store.load()
    store.save(original)
    assert json.loads(path.read_text())["schema"] == 5
    assert store.load() == original
    assert original["OLD001"].instance_id == "desktop"


def test_cleanup_pending_record_survives_history_pruning(tmp_path):
    from dataclasses import replace
    store = RuntimeReferenceStore(tmp_path / "refs.json")
    pending = ApplicationRuntimeReference(runtime_id="PEND01", application="desktop",
        instance_id="desktop", generation="old", state=ApplicationRuntimeState.FAILED,
        error="exec failed", cleanup_pending=True, cleanup_error="bus unavailable")
    finished = replace(pending, runtime_id="DONE01", cleanup_pending=False, cleanup_error=None)
    references = {item.runtime_id: item for item in (pending, finished)}
    assert store.prune_completed(references, limit=0) == ("DONE01",)
    store.save(references)
    assert store.load() == {"PEND01": pending}


def test_schema_three_completed_history_requires_cleanup_after_migration(tmp_path):
    import json
    store = RuntimeReferenceStore(tmp_path / "refs.json")
    store.path.write_text(json.dumps({"schema": 3, "runtimes": {"OLD001": {
        "runtime_id": "OLD001", "application": "desktop", "instance_id": "desktop",
        "generation": "old", "state": "failed", "error": "exec failed"}}}))
    references = store.load()
    assert references["OLD001"].cleanup_pending
    assert references["OLD001"].error == "exec failed"
    store.save(references)
    assert store.load() == references
