class BoxControlError(Exception):
    def diagnostic(self):
        """Return structured cause information without persistence or recovery."""
        from .diagnostics import fault_from_exception
        return fault_from_exception(self)


class ConfigurationError(BoxControlError):
    pass

class DependencyError(ConfigurationError):
    pass

class RuntimeOperationError(BoxControlError):
    pass


class LifecycleOperationPending(RuntimeOperationError):
    """A durable lifecycle operation must continue during a later evaluation."""

    def __init__(self, message: str, *, replaced_runtime_id: str | None = None):
        super().__init__(message)
        self.replaced_runtime_id = replaced_runtime_id


class PersistenceError(BoxControlError):
    """Recovery-critical storage failed; further lifecycle mutations must stop."""


class RecoveryRequired(PersistenceError):
    """An earlier mutation session needs recovery before another can start."""


class ProjectBusy(BoxControlError):
    """The project mutation lock was not acquired within the caller budget."""


class RootControlReplyTimeout(RuntimeOperationError):
    """Request was sent; server outcome must be reconciled, not assumed failed."""
    def __init__(self, operation, elapsed_seconds):
        self.operation = operation
        self.elapsed_seconds = elapsed_seconds
        super().__init__(f"root-control reply timed out for {operation} after {elapsed_seconds:.3f}s; "
                         "request sent; outcome may be unresolved")
