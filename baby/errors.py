"""Exception types for BABY."""


class BabyException(Exception):
    """Base exception for BABY."""

    pass


class ConfigurationError(BabyException):
    """Error in configuration."""

    pass


class ValidationError(BabyException):
    """Error in validation."""

    pass


class PathTraversalError(ValidationError):
    """Attempted path traversal outside allowed repository boundary."""

    pass


class RepositoryBoundaryError(ValidationError):
    """Attempted access violating repository boundary (e.g. .git access or unconfigured root)."""

    pass


class PermissionDeniedError(BabyException):
    """Permission denied."""

    pass


class ApprovalRequiredError(BabyException):
    """Approval required for action."""

    pass


class AgentNotFoundError(BabyException):
    """Agent not found."""

    pass


class ToolNotFoundError(BabyException):
    """Tool not found."""

    pass


class ExecutionError(BabyException):
    """Error during execution."""

    pass


class VerificationError(BabyException):
    """Error during verification."""

    pass


class TaskNotFoundError(BabyException):
    """Task not found."""

    pass
