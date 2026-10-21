"""The one application error the API maps to a response: the database is unreachable."""

from typing import Any

from fastapi import status


class DatabaseUnavailableError(Exception):
    """Raised when the database cannot be reached (down, refusing or dropping connections).

    The client gets a fixed 503 envelope; the driver error is only logged server-side.
    """

    code = "DATABASE_UNAVAILABLE"
    message = "Database unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """The response body: a fixed code and message, no driver text."""
        return {"error": {"code": self.code, "message": self.message}}
