class MapsLeadError(Exception):
    """Base error for expected MapsLead failures."""


class CampaignError(MapsLeadError):
    """Base error for expected campaign failures."""


class InvalidCampaignError(CampaignError):
    """Raised when campaign input is invalid."""


class CampaignNotFoundError(CampaignError):
    """Raised when a requested campaign does not exist."""


class CampaignBusinessTypeError(CampaignError):
    """Raised when a run business type does not match the campaign."""


class CampaignRunAssignmentError(CampaignError):
    """Raised when a run is already assigned to another campaign."""


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
