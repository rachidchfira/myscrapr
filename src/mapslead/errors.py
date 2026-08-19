class MapsLeadError(Exception):
    """Base error for expected MapsLead failures."""


class QuotaExceededError(MapsLeadError):
    """Raised when a run would exceed the fixed daily new-record quota."""


class UnsafeUrlError(MapsLeadError):
    """Raised when a URL fails the website safety policy."""


class ProviderSetupError(MapsLeadError):
    """Raised when the local Maps provider prerequisites are unavailable."""


class RunStateError(MapsLeadError):
    """Raised when a requested run transition is not allowed."""


class ExportError(MapsLeadError):
    """Raised when exporting a run snapshot fails."""
