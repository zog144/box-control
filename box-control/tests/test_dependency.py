from zog.box_control.dependency import resolve
from zog.box_control.errors import DependencyError
from zog.box_control.model import ApplicationSpec, ApplicationStartPolicy, ProgramSpec


def app(name, dependencies=(), command=None):
    return ApplicationSpec(
        name=name,
        dependencies=dependencies,
        programs=(ProgramSpec(name="main", command=command or ("/bin/true",)),),
    )


def test_application_dependency_order():
    specs = {
        "a": app("a", ("b",)),
        "b": app("b", ("c",)),
        "c": app("c"),
    }
    assert resolve(specs).applications == ("c", "b", "a")


def test_application_dependency_cycle_is_rejected():
    specs = {"a": app("a", ("b",)), "b": app("b", ("a",))}
    try:
        resolve(specs)
    except DependencyError:
        return
    assert False


def test_program_command_change_changes_application_runtime_fingerprint():
    old = app("app", command=("/bin/old",))
    new = app("app", command=("/bin/new",))
    assert (
        resolve({"app": old}).resolution_for("app").runtime_fingerprint
        != resolve({"app": new}).resolution_for("app").runtime_fingerprint
    )


def test_dependency_graph_is_application_only():
    specs = {"app": app("app", ("database",)), "database": app("database")}
    closure = resolve(specs)
    assert closure.graph.dependencies_for("app") == ("database",)
    assert closure.graph.reverse_dependencies_for("database") == ("app",)


def test_application_policy_instance_mode_and_program_identity_affect_fingerprint():
    base = app("app")
    changed = ApplicationSpec(
        name="app",
        programs=(ProgramSpec(name="main", command=("/bin/true",), user="service"),),
        start_policy=ApplicationStartPolicy.KEEP_RUNNING,
        multiple_instances=True,
    )
    assert (
        resolve({"app": base}).resolution_for("app").runtime_fingerprint
        != resolve({"app": changed}).resolution_for("app").runtime_fingerprint
    )
