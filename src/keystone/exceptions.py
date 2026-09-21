"""Exception taxonomy.

The split that matters is between a *record* problem and a *run* problem. A
record that cannot be mapped is rejected, recorded with its cause, and the
migration continues; a run that cannot proceed -- an unreachable target, an
invalid mapping file, a missing dry-run -- stops immediately. Conflating the
two produces either a migration that dies on the first bad row or one that
cheerfully writes nothing and reports success.
"""

from __future__ import annotations


class KeystoneError(Exception):
    """Base class for every error this package raises deliberately."""


# --- Run-level: the migration cannot proceed -------------------------------


class ConfigurationError(KeystoneError):
    """Settings are missing or contradictory."""


class DatabaseError(KeystoneError):
    """The database refused an operation."""


class MappingError(KeystoneError):
    """A mapping file is invalid, missing or inconsistent with the target."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message if path is None else f"{path}: {message}")
        self.path = path


class TargetError(KeystoneError):
    """The target system refused a request."""


class TransientTargetError(TargetError):
    """Worth retrying: a timeout, a 429, a 5xx.

    Carries the server's own advice when it gave any: ignoring a documented
    ``Retry-After`` is how a client gets blocked instead of throttled.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class PermanentTargetError(TargetError):
    """Not worth retrying: a 4xx that will be a 4xx again."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryBudgetExhausted(TargetError):
    """The retry policy gave up. The run fails with nothing half-written."""


class MigrationBlocked(KeystoneError):
    """A gate refused to let the load start.

    Raised when no dry-run exists for the current mapping version, or when the
    projected rejection rate is above the configured ceiling.
    """


# --- Record-level: rejected, recorded, and the run continues ---------------


class RecordRejected(KeystoneError):
    """One record could not be migrated.

    ``field`` and ``rule`` are what turn a rejection count into an actionable
    report: "412 records failed" is a number, "412 records have a
    `billing_country` that no lookup entry covers" is a task.
    """

    def __init__(
        self,
        message: str,
        *,
        entity: str,
        source_id: str,
        field: str | None = None,
        rule: str | None = None,
    ) -> None:
        super().__init__(message)
        self.entity = entity
        self.source_id = source_id
        self.field = field
        self.rule = rule


class MappingViolation(RecordRejected):
    """A transformation rule refused the value it was given."""


class ValidationViolation(RecordRejected):
    """A validation rule refused the mapped record."""


class ReferenceNotResolved(RecordRejected):
    """A lookup to another entity found nothing in the crosswalk."""
