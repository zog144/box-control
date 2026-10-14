"""Read-only diagnostic values; never authoritative recovery decisions."""
from dataclasses import asdict, dataclass
import traceback


@dataclass(frozen=True)
class Fault:
    code: str
    message: str
    blocking: bool = True
    guidance: str = 'inspect-evidence'
    operation_id: str | None = None
    request_id: str | None = None
    runtime_ids: tuple[str, ...] = ()
    phase: str | None = None
    evidence: tuple[str, ...] = ()
    causes: tuple[dict, ...] = ()

    def to_dict(self):
        return asdict(self)


def fault_from_exception(exc, **context):
    from .errors import ConfigurationError, PersistenceError, RecoveryRequired, RuntimeOperationError, ProjectBusy
    if isinstance(exc, ProjectBusy):
        code, guidance = 'project-busy', 'retry-same-request'
    elif isinstance(exc, RecoveryRequired):
        code, guidance = 'recovery-required', 'inspect-evidence'
    elif isinstance(exc, PersistenceError):
        code, guidance = 'storage-failure', 'restore-storage-then-evaluate'
    elif isinstance(exc, ConfigurationError):
        code, guidance = 'configuration-error', 'correct-configuration'
    elif isinstance(exc, RuntimeOperationError):
        code, guidance = 'runtime-operation-error', 'inspect-evidence'
    else:
        code, guidance = 'unexpected-error', 'inspect-evidence'
    causes, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(causes) < 16:
        seen.add(id(current))
        causes.append({'type': type(current).__name__, 'message': str(current), 'frames': [dict(file=f.filename, line=f.lineno, function=f.name) for f in traceback.extract_tb(current.__traceback__)[-32:]]})
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return Fault(code, str(exc), blocking=code != 'project-busy', guidance=guidance, causes=tuple(causes), **context)
