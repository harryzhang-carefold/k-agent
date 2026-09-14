"""Unified error format: {"code", "message", "detail"} (PRD module 6)."""


class APIError(Exception):
    def __init__(self, status_code: int, message: str, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.detail = detail

    def to_json(self):
        return {"code": self.status_code, "message": self.message, "detail": self.detail}


class LLMError(Exception):
    """LLM endpoint failed after retries -> caller returns controlled degradation."""


class GraphError(Exception):
    """Memory graph design-principle violation (UPPER_SNAKE / universal edge)."""


class MCPError(Exception):
    """MCP stdio JSON-RPC failure."""
