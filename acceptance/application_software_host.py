"""Disposable-host acceptance for immutable application software views."""
import json
from pathlib import Path
import re
import shutil
import subprocess

root = Path.cwd()
evidence = root / "evidence"
evidence.mkdir(exist_ok=True)
environment = root / "environment"
fixture = root / "fixture"


def run(arguments):
    subprocess.run([str(item) for item in arguments], check=True)


run(["dnf", "install", "-y", "python3.11", "python3.11-pip", "dbus-daemon"])
run(["python3.11", "-m", "venv", environment])
python = environment / "bin/python"
run([
    python,
    "-m",
    "pip",
    "install",
    "-e",
    "../dependencies/zog144/image-build",
    "-e",
    "../dependencies/zog144/root-control",
    "-e",
    "./box-control[test]",
])

for name in ("bin", "etc", "data", "tmp", "proc", "sys", "dev"):
    (fixture / name).mkdir(parents=True, exist_ok=True)

for executable in ("/bin/sh", "/bin/sleep", "/bin/true"):
    shutil.copy2(executable, fixture / executable.lstrip("/"))
    output = subprocess.check_output(["ldd", executable], text=True)
    for library in re.findall(r"(/[^\s()]+)", output):
        target = fixture / library.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(library, target)

(fixture / "etc/passwd").write_text(
    "root:x:0:0:root:/root:/bin/sh\n"
    "nobody:x:65534:65534:nobody:/:/sbin/nologin\n"
)
(fixture / "etc/group").write_text(
    "root:x:0:\n"
    "nobody:x:65534:\n"
)

run([
    python,
    "-m",
    "pytest",
    "-v",
    "box-control/tests/test_systemd_integration.py",
    "-k",
    "live_application_software_view_is_read_only_and_state_survives_replacement or live_nested_writable_overlay_preserves_parent_and_state",
    "--run-systemd-integration",
    "--systemd-rootfs=" + str(fixture),
    "--junitxml=" + str(evidence / "application-software-live.xml"),
    "-o",
    "pythonpath=box-control/src ../dependencies/zog144/image-build",
])

report = evidence / "application-software-live.json"
if not report.is_file():
    raise SystemExit("live application software test did not publish JSON evidence")
raw = json.loads(report.read_text())
if raw.get("persistent_values") != ["view-one", "view-two"]:
    raise SystemExit("persistent replacement evidence is incomplete")
if raw.get("immutable_executable_unchanged") is not True:
    raise SystemExit("immutable software evidence is incomplete")


for key in ("first_kernel_mount_observation", "second_kernel_mount_observation"):
    observation = raw.get(key)
    if not isinstance(observation, dict):
        raise SystemExit(f"{key} evidence is missing")
    if observation.get("schema") != "zog-runtime-mount-observation-v1":
        raise SystemExit(f"{key} uses an unexpected observation schema")
    if observation.get("basis") != "kernel-mount-namespace":
        raise SystemExit(f"{key} is not kernel mount-namespace evidence")
    if observation.get("status") != "observed":
        raise SystemExit(f"{key} did not produce a complete observed result")
    programs = observation.get("programs") or []
    if len(programs) != 1 or programs[0].get("status") != "observed":
        raise SystemExit(f"{key} program observation is incomplete")
    mounts = {
        item.get("destination"): item
        for item in programs[0].get("mounts", [])
        if isinstance(item, dict)
    }
    view = mounts.get("/applications/software-probe")
    data = mounts.get("/data")
    artifacts = [
        item for destination, item in mounts.items()
        if isinstance(destination, str)
        and destination.startswith("/applications/.store/")
    ]
    if not view or view.get("observed_access") != "read-only" or view.get("backing_identity") != "verified":
        raise SystemExit(f"{key} did not verify the immutable software view")
    if not artifacts or any(
        item.get("observed_access") != "read-only"
        or item.get("backing_identity") != "verified"
        for item in artifacts
    ):
        raise SystemExit(f"{key} did not verify immutable artifact mounts")
    if not data or data.get("observed_access") != "read-write" or data.get("backing_identity") != "verified":
        raise SystemExit(f"{key} did not verify writable application data")

for key in ("first_after_stop_mount_observation", "second_after_stop_mount_observation"):
    stopped = raw.get(key)
    if not isinstance(stopped, dict) or stopped.get("status") != "absent":
        raise SystemExit(
            f"{key} did not become explicitly absent from mount observation"
        )

nested_report = evidence / "nested-writable-live.json"
if not nested_report.is_file():
    raise SystemExit("nested writable-overlay evidence is missing")
nested = json.loads(nested_report.read_text())
if nested.get("persistent_values") != ["first", "second"]:
    raise SystemExit("nested writable state did not survive relaunch")
if nested.get("generation_unchanged") is not True:
    raise SystemExit("nested writable acceptance changed the generation")
for observation in nested.get("kernel_mount_observations", []):
    if observation.get("status") != "observed":
        raise SystemExit("nested writable mount observation is incomplete")
    programs = observation.get("programs") or []
    if len(programs) != 1:
        raise SystemExit("nested writable observation has unexpected program count")
    mounts = {
        item.get("destination"): item
        for item in programs[0].get("mounts", [])
        if isinstance(item, dict)
    }
    parent = mounts.get("/var")
    child = mounts.get("/var/lib/nested-state")
    if (
        not parent
        or parent.get("observed_access") != "read-only"
        or parent.get("backing_identity") != "verified"
    ):
        raise SystemExit("nested writable acceptance did not verify read-only /var")
    if (
        not child
        or child.get("observed_access") != "read-write"
        or child.get("backing_identity") != "verified"
    ):
        raise SystemExit(
            "nested writable acceptance did not verify writable nested state"
        )

raw["acceptance_environment"] = {
    "os_release": Path("/etc/os-release").read_text(encoding="utf-8")[:16384]
        if Path("/etc/os-release").is_file() else None,
    "systemd_version": subprocess.check_output(
        ["systemd", "--version"], text=True
    ).splitlines()[0][:256],
}
report.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
