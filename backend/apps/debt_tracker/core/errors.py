class DebtTrackerError(Exception):
    """Base error that can be converted to a safe client response."""


class WorkbookSecurityError(DebtTrackerError):
    """Raised when an uploaded workbook fails the XLSX security gate."""


class WorkbookSchemaError(DebtTrackerError):
    """Raised when a workbook does not contain the required schema."""


class WorkbookOutputLimitError(DebtTrackerError):
    """Raised before a generated workbook can exceed its configured size bound."""


class FundMatchingError(DebtTrackerError):
    """Raised when fund matching cannot be performed safely."""
