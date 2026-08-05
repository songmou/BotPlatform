"""Custom error type for datasource operations. All user-facing messages are in Chinese."""


class DataSourceError(RuntimeError):
    """Raised when a datasource operation (connect, query, etc.) fails."""
