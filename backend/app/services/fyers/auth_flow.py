"""What the legacy app hangs on HAT's Fyers login (`app.hat.execution.fyers_auth`): after a token
change it resets the legacy `FyersClient`. HAT's auth service imports no feed, so the legacy side
registers this hook (`install_legacy_hooks`, called once at startup); HAT's own live feed registers
its login and logout hooks itself (`LiveFeed.install_auth_hooks`, K3). K4 deletes this module with
the legacy client.
"""

from app.hat.execution.fyers_auth import FyersAuthService, get_fyers_auth


def _reset_fyers_client() -> None:
    from app.services.fyers_client import reset_fyers_client

    reset_fyers_client()


def install_legacy_hooks(service: FyersAuthService | None = None) -> None:
    """Register the legacy client reset on `service` (the process-wide one by default); once."""
    service = service if service is not None else get_fyers_auth()
    service.add_hook("token_change", _reset_fyers_client)
