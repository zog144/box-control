"""Controller-side strictly allowlisted Workspace stage evidence contract."""
from dataclasses import replace
import time

from test_workspaces import setup, desktop


SCHEMA = "zog-workspace-stage-readiness-v1"


def proof(binding, **changes):
    row = dict(schema=SCHEMA, workspace_id=binding["workspace_id"],
        desktop_runtime_id=binding["desktop_runtime_id"],
        boot_id=binding["boot_id"], incarnation=binding["incarnation"],
        observed_at=time.time(), endpoint_identity="a" * 64,
        x11={"status": "confirmed", "code": "ok"},
        rfb={"status": "failed", "code": "rfb-invalid"})
    row.update(changes)
    return row


def test_separate_stage_evidence_and_legacy_access_stay_independent(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    server = desktop(control, workspace)
    original = transport.workspace_call
    def inspect(op, *, project_root, binding, **kwargs):
        if op == "readiness":
            assert binding["desktop_runtime_id"] == server.runtime_id
            return proof(binding)
        return original(op, project_root=project_root, binding=binding, **kwargs)
    transport.workspace_call = inspect
    result = control.workspace_desktop_readiness(workspace)
    assert result["x11"]["status"] == "confirmed"
    assert result["rfb"] == {"status": "failed", "code": "rfb-invalid"}
    assert result["advisory"] is True
    assert "vnc_endpoint" not in result and "access_directory" not in result
    assert control.workspace_desktop_access(workspace)["ready"] is True


def test_transport_loss_stale_observation_unknown_not_confirmed(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    desktop(control, workspace)
    def lost(op, *, project_root, binding, **kwargs):
        if op == "readiness": raise TimeoutError("cookie and private socket")
        return {"ready": True}
    transport.workspace_call = lost
    assert control.workspace_desktop_readiness(workspace)["x11"]["status"] == "unknown"
    transport.workspace_call = lambda op, *, project_root, binding, **kw: (
        proof(binding, observed_at=time.time() - 20) if op == "readiness" else {"ready": True})
    assert control.workspace_desktop_readiness(workspace)["rfb"]["status"] == "unknown"


def test_forged_aggregate_or_private_endpoint_is_never_a_stage_proof(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    desktop(control, workspace)
    for change in ({"ready": True}, {"cookie_hex": "1" * 32},
                   {"rfb": {"status": "confirmed", "code": "secret"}},
                   {"endpoint_identity": "/run/private/vnc.sock"},
                   {"x11": {"status": "failed", "code": "rfb-invalid"}}):
        transport.workspace_call = lambda op, *, project_root, binding, **kw: (
            proof(binding, **change))
        assert control.workspace_desktop_readiness(workspace)["x11"]["status"] == "unknown"


def test_runtime_replacement_during_probe_rejects_both_stages(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    server = desktop(control, workspace)
    original = transport.workspace_call
    def replace_during_read(op, *, project_root, binding, **kw):
        if op == "readiness":
            # Deliberately change the durable runtime between independent root
            # observation and controller's reselect; no desktop restart.
            from zog.box_control.runtime.reference import RuntimeReferenceStore
            from zog.box_control.model import ApplicationRuntimeState
            store = RuntimeReferenceStore(control.project.runtime_reference_file)
            refs = store.load()
            refs[server.runtime_id] = replace(
                refs[server.runtime_id], state=ApplicationRuntimeState.TERMINATED)
            store.save(refs)
            return proof(binding)
        return original(op, project_root=project_root, binding=binding, **kw)
    transport.workspace_call = replace_during_read
    result = control.workspace_desktop_readiness(workspace)
    assert result["x11"]["status"] == result["rfb"]["status"] == "unknown"


def test_stale_boot_and_unselected_workspace_remain_unknown(tmp_path):
    _, control, transport, workspace, boot = setup(tmp_path)
    desktop(control, workspace)
    boot[0] = "different-boot"
    assert control.workspace_desktop_readiness(workspace)["x11"]["status"] == "unknown"
