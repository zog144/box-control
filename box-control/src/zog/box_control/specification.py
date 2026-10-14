from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError
from .model import ApplicationSpec, ApplicationStartPolicy, ProgramSpec

_NAME = re.compile(r"^[A-Za-z0-9-]+$")


@dataclass(frozen=True)
class _ProgramDeclaration:
    values: dict


class _ValueEvaluator(ast.NodeVisitor):
    def visit_Constant(self, node):
        return node.value

    def visit_List(self, node):
        return [self.visit(value) for value in node.elts]

    def visit_Tuple(self, node):
        return tuple(self.visit(value) for value in node.elts)

    def visit_Dict(self, node):
        return {
            self.visit(key): self.visit(value)
            for key, value in zip(node.keys, node.values)
        }

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name) or node.func.id != "program":
            raise ConfigurationError(
                "only program(...) calls may be nested in application specifications"
            )
        return _ProgramDeclaration(_keywords(node, self))

    def generic_visit(self, node):
        raise ConfigurationError(
            f"unsupported syntax in application specification: {type(node).__name__}"
        )


def _keywords(call: ast.Call, evaluator: _ValueEvaluator | None = None) -> dict:
    if call.args:
        raise ConfigurationError("declarations accept keyword arguments only")
    evaluator = evaluator or _ValueEvaluator()
    values = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ConfigurationError("**kwargs are not allowed")
        values[keyword.arg] = evaluator.visit(keyword.value)
    return values


def _name(path: Path, kind: str, value) -> str:
    if not isinstance(value, str) or not value or not _NAME.fullmatch(value):
        raise ConfigurationError(
            f"{path}: {kind} name must contain only letters, digits, and hyphens"
        )
    return value


def _strings(path: Path, field: str, value) -> tuple[str, ...]:
    try:
        result = tuple(value or ())
    except TypeError as exc:
        raise ConfigurationError(f"{path}: {field} must be a sequence of strings") from exc
    if not all(isinstance(item, str) for item in result):
        raise ConfigurationError(f"{path}: {field} must contain strings")
    return result


def _environment(path: Path, value) -> tuple[tuple[str, str], ...]:
    if isinstance(value, dict):
        return tuple(sorted((str(key), str(item)) for key, item in value.items()))
    try:
        return tuple((str(key), str(item)) for key, item in (value or ()))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"{path}: environment must be a mapping or sequence of key/value pairs"
        ) from exc


def _mounts(path: Path, value) -> tuple[tuple[str, str], ...]:
    if isinstance(value, dict):
        mounts = tuple(sorted((str(key), str(item)) for key, item in value.items()))
    else:
        try:
            mounts = tuple((str(key), str(item)) for key, item in (value or ()))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"{path}: mounts must be a mapping or sequence of name/target pairs"
            ) from exc
    for mount_name, target in mounts:
        if (
            not mount_name
            or mount_name in {".", ".."}
            or "/" in mount_name
            or "\\" in mount_name
        ):
            raise ConfigurationError(
                f"{path}: mount name must be one path component: {mount_name!r}"
            )
        if not target.startswith("/"):
            raise ConfigurationError(
                f"{path}: mount target must be an absolute path: {target!r}"
            )
    return mounts


def _parse_program(path: Path, declaration: _ProgramDeclaration) -> ProgramSpec:
    values = declaration.values
    allowed = {
        "name",
        "command",
        "environment",
        "working_directory",
        "mounts",
        "user",
        "group",
        "description",
        "execution_timeout_seconds",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ConfigurationError(
            f"{path}: unknown program fields: {', '.join(sorted(unknown))}"
        )
    name = _name(path, "program", values.get("name"))
    command = _strings(path, "program command", values.get("command", ()))
    description = values.get("description")
    if description is not None and not isinstance(description, str):
        raise ConfigurationError(f"{path}: program description must be a string")
    working_directory = values.get("working_directory")
    if working_directory is not None and not isinstance(working_directory, str):
        raise ConfigurationError(f"{path}: working_directory must be a string")
    timeout = values.get('execution_timeout_seconds')
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 86400):
        raise ConfigurationError('execution_timeout_seconds must be 1 through 86400')
    mounts = _mounts(path, values.get("mounts", ()))
    from .mounts import validate
    from .errors import RuntimeOperationError
    try:
        validate(None, mounts, command)
    except RuntimeOperationError as exc:
        raise ConfigurationError(f'{path}: {exc}') from exc
    return ProgramSpec(
        name=name,
        command=command,
        execution_timeout_seconds=timeout,
        environment=_environment(path, values.get("environment", ())),
        working_directory=working_directory,
        mounts=mounts,
        user=str(values.get("user", "regular")),
        group=(str(values["group"]) if values.get("group") is not None else None),
        description=description,
    )


def parse_application(path: Path) -> ApplicationSpec:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise ConfigurationError(f"{path}: syntax error: {exc}") from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"{path}: cannot read application declaration: {exc}") from exc

    calls: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            calls.append(node.value)
        elif isinstance(node, ast.Pass):
            continue
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        else:
            raise ConfigurationError(
                f"{path}: unsupported top-level syntax: {type(node).__name__}"
            )

    if (
        len(calls) != 1
        or not isinstance(calls[0].func, ast.Name)
        or calls[0].func.id != "application"
    ):
        raise ConfigurationError(
            f"{path}: expected exactly one application(...) declaration"
        )

    values = _keywords(calls[0])
    allowed = {
        "name",
        "workspace_role",
        "programs",
        "dependencies",
        "start_policy",
        "multiple_instances",
        "persistent",
        "execution_identity",
        "writable_mounts",
        "preparation_revision",
        "preparation",
        "software_view",
        "description",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ConfigurationError(
            f"{path}: unknown application fields: {', '.join(sorted(unknown))}"
        )

    name = _name(path, "application", values.get("name"))
    raw_programs = values.get("programs", ())
    if not isinstance(raw_programs, (tuple, list)):
        raise ConfigurationError(f"{path}: programs must be a sequence of program(...) calls")
    programs = tuple(
        _parse_program(path, declaration)
        if isinstance(declaration, _ProgramDeclaration)
        else _raise_program_declaration(path)
        for declaration in raw_programs
    )
    if not programs:
        raise ConfigurationError(f"{path}: application must declare at least one program")
    names = [program.name for program in programs]
    if len(set(names)) != len(names):
        raise ConfigurationError(f"{path}: duplicate program name in application {name}")

    # certify-manage's review-only candidate declares issue, renew and
    # inspect as simultaneous systemd programs. Launching that declaration
    # would issue and renew concurrently, not select an authorized operation.
    # Until the controller supports durable individual-program execution,
    # a certificate lifecycle app may contain only one runnable program.
    if name == "certify-manage" and len(programs) != 1:
        raise ConfigurationError(
            f"{path}: certify-manage requires one exclusively selected runtime program; "
            "the multi-operation prototype must not be activated"
        )

    description = values.get("description")
    if description is not None and not isinstance(description, str):
        raise ConfigurationError(f"{path}: application description must be a string")

    raw_start_policy = values.get(
        "start_policy", ApplicationStartPolicy.EXTERNALLY_CONTROLLED.value
    )
    if not isinstance(raw_start_policy, str):
        raise ConfigurationError(f"{path}: start_policy must be a string")
    try:
        start_policy = ApplicationStartPolicy(raw_start_policy)
    except ValueError as exc:
        allowed_policies = ", ".join(policy.value for policy in ApplicationStartPolicy)
        raise ConfigurationError(
            f"{path}: unknown start_policy {raw_start_policy!r}; expected one of "
            f"{allowed_policies}"
        ) from exc

    multiple_instances = values.get("multiple_instances", False)
    if not isinstance(multiple_instances, bool):
        raise ConfigurationError(f"{path}: multiple_instances must be a boolean")

    execution_identity = values.get("execution_identity")
    if execution_identity not in (None, "application"):
        raise ConfigurationError("execution_identity must be 'application' when specified")
    if execution_identity is not None and (
        values.get("persistent") is not True or values.get("multiple_instances", False)
    ):
        raise ConfigurationError("application execution identity requires a persistent singleton")
    if execution_identity is not None:
        for declaration in tuple(raw_programs) + tuple(values.get("preparation", ())):
            if not isinstance(declaration, _ProgramDeclaration):
                raise ConfigurationError("invalid execution identity program declaration")
            if "user" in declaration.values or "group" in declaration.values:
                raise ConfigurationError("application execution identity forbids explicit program user/group")
    persistent = values.get('persistent', False)
    if type(persistent) is not bool:
        raise ConfigurationError('persistent must be a boolean')
    mounts = _strings(path, 'writable_mounts', values.get('writable_mounts'))
    for mount in mounts:
        _name(path, 'writable mount', mount)
    if len(set(mounts)) != len(mounts):
        raise ConfigurationError('duplicate writable mount')
    revision = values.get('preparation_revision')
    raw_preparation = values.get('preparation', ())
    if not isinstance(raw_preparation, (list, tuple)):
        raise ConfigurationError('preparation must be a sequence of program declarations')
    preparation = tuple(_parse_program(path, item) if isinstance(item, _ProgramDeclaration)
                        else _raise_program_declaration(path) for item in raw_preparation)
    if persistent:
        if not preparation or any(p.execution_timeout_seconds is None for p in preparation):
            raise ConfigurationError('persistent preparation requires commands with explicit execution_timeout_seconds')
        if set(dict(preparation[0].mounts)) != set(mounts):
            raise ConfigurationError('first preparation command must mount every named writable directory')
        if multiple_instances or not mounts or not isinstance(revision, str) or not revision:
            raise ConfigurationError('persistent applications require named mounts, a preparation revision, and a single instance')
        if len({step.name for step in preparation}) != len(preparation):
            raise ConfigurationError('duplicate preparation step name')
        for program in programs + preparation:
            if not program.command or not program.command[0].startswith('/'):
                raise ConfigurationError('persistent programs require an absolute executable')
            if program.user in ('root', '0'):
                raise ConfigurationError('persistent programs and preparation must run unprivileged')
            if any(name not in mounts for name, target in program.mounts):
                raise ConfigurationError('program refers to undeclared persistent mount')
        if len({(program.user, program.group) for program in programs + preparation}) != 1:
            raise ConfigurationError('persistent programs must use the same user and group')
    elif mounts or revision or preparation:
        raise ConfigurationError('preparation and shared writable mounts require persistent=True')

    software_view = values.get("software_view")
    if software_view is not None and (
        not isinstance(software_view, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", software_view)
    ):
        raise ConfigurationError(
            f"{path}: software_view must be a sha256 application software view identity"
        )

    role = values.get('workspace_role')
    if role not in (None, 'client', 'desktop'):
        raise ConfigurationError('workspace_role must be client or desktop')
    if role:
        if start_policy != ApplicationStartPolicy.EXTERNALLY_CONTROLLED:
            raise ConfigurationError('workspace applications must be externally-controlled')
        if role == 'desktop' and (not multiple_instances or persistent or len(programs) != 1):
            raise ConfigurationError('desktop requires one program, multiple instances, and nonpersistent storage')
        for program in programs:
            if program.user in ('root', '0'):
                raise ConfigurationError('workspace applications must be unprivileged')
            if {'DISPLAY', 'XAUTHORITY'} & set(dict(program.environment)):
                raise ConfigurationError('workspace display environment is controller-owned')
            if any(target in ('/tmp/.X11-unix', '/run/zog-workspace') for _, target in program.mounts):
                raise ConfigurationError('workspace socket mounts are controller-owned')

    return ApplicationSpec(
        name=name,
        programs=programs,
        dependencies=_strings(path, "dependencies", values.get("dependencies", ())),
        start_policy=start_policy,
        multiple_instances=multiple_instances,
        workspace_role=role,
        persistent=persistent, writable_mounts=mounts, preparation_revision=revision, preparation=preparation,
        execution_identity=execution_identity,
        software_view=software_view, description=description,
    )


def _raise_program_declaration(path: Path):
    raise ConfigurationError(
        f"{path}: programs must contain program(...) declarations only"
    )


def discover_applications(application_root: Path) -> dict[str, ApplicationSpec]:
    applications: dict[str, ApplicationSpec] = {}
    if not application_root.is_dir():
        return applications
    for directory in sorted(application_root.iterdir()):
        if not directory.is_dir():
            continue
        path = directory / "application.py"
        if not path.is_file():
            continue
        spec = parse_application(path)
        if spec.name in applications:
            raise ConfigurationError(f"duplicate application name: {spec.name}")
        applications[spec.name] = spec
    return applications
