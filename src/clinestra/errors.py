"""Errors raised by the harness. The server maps each to an HTTP status."""

__all__ = ["ClinestraError", "NotFound", "PermissionDenied", "InvariantViolation", "Invalid"]


class ClinestraError(Exception):
    """Base class for every harness error."""
    status_code = 400


class NotFound(ClinestraError):
    status_code = 404


class PermissionDenied(ClinestraError):
    """The actor's role or type does not allow this action."""
    status_code = 403


class InvariantViolation(ClinestraError):
    """The action would break a clinical safety rule (for example, editing a signed note)."""
    status_code = 409


class Invalid(ClinestraError):
    status_code = 422

    def __init__(self, detail: str = "", *args, fields=None):
        super().__init__(detail, *args)
        self.fields = list(fields or [])
