"""The daily Kite (Zerodha) login, as four routes under `/api/hat/auth/zerodha`:
`api/broker_auth.py`'s routes over `KiteAuthService`. Kite redirects the owner's login to
`KITE_REDIRECT_URI` (`http://127.0.0.1:8000/api/hat/auth/zerodha/callback`, which HAT does not
serve) with a `request_token` in the address; the owner pastes it into the HAT page, which posts it
to `token` as Fyers' auth code is posted.
"""

from __future__ import annotations

from app.hat.api.broker_auth import RouteNames, auth_router
from app.hat.execution.kite_auth import get_kite_auth
from app.hat.models.strategy import BROKER_ZERODHA

__all__ = ["router"]

router = auth_router(
    broker=BROKER_ZERODHA,
    label="Kite",
    service=get_kite_auth,
    names=RouteNames(
        status="zerodha_status",
        url="zerodha_login_url",
        exchange="exchange_zerodha_request_token",
        disconnect="zerodha_disconnect",
    ),
)
