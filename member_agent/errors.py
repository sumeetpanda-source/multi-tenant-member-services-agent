class AgentError(Exception):
    """A public error code; downstream messages must not be propagated."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PolicyDenied(AgentError):
    pass


class BudgetExceeded(AgentError):
    pass


class UpstreamError(AgentError):
    def __init__(self, status: int, retry_after: float = 0):
        super().__init__("upstream_unavailable")
        self.status = status
        self.retry_after = retry_after
