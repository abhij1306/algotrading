"""The daily Fyers OAuth login, as four routes under `/api/hat/auth/fyers` (K2, owner default Q27):
`api/broker_auth.py`'s routes over `FyersAuthService`. This is the one Fyers login and, through the
service, the one Fyers token store. Fyers is logged in every day whichever broker takes orders: the
market data comes from it.
"""

from __future__ import annotations

from app.hat.api.broker_auth import RouteNames, auth_router
from app.hat.execution.fyers_auth import get_fyers_auth
from app.hat.models.strategy import BROKER_FYERS

__all__ = ["router"]

router = auth_router(
    broker=BROKER_FYERS,
    label="Fyers",
    service=get_fyers_auth,
    names=RouteNames(
        status="fyers_status",
        url="fyers_login_url",
        exchange="exchange_auth_code",
        disconnect="fyers_disconnect",
    ),
)
