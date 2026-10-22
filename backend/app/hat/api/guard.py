"""The guard on HAT's two write routes, the Fyers login's `POST /token` and `POST /disconnect`
(HAT-DEC-BD5, HAT-DEC-T3).

The API has no auth by decision: it binds to loopback (or a VPN address) and serves one owner. A
page on another site that the owner's browser opens can still send a request there, so every
write must show it came from the HAT page:

- the custom header `X-HAT-Request: 1`. A cross-site page cannot set it without a CORS preflight,
  and the CORS rule answers a preflight only for the frontend's origins;
- an `Origin`, when the browser sends one, that the app's frontend-origin rule accepts;
- a `Sec-Fetch-Site` of `same-origin` or `none` when no `Origin` is sent. A cross-site or
  same-site request must name its origin.

A request with neither header, such as `curl` on the machine, passes on the custom header alone.
The frontend-origin rule is the CORS rule of the app (`app.utils.origins.is_frontend_origin`),
handed over by the app wiring in `app.state.frontend_origin`. HAT may not import it (CONF
`imports__hat_decoupled_from_legacy`). With no rule wired, every request that carries an `Origin`
is refused.

A refusal is a 403 with one fixed message. The service is never called, so nothing is written
and no hook runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import HTTPException, Request

__all__ = [
    "HEADER",
    "HEADER_VALUE",
    "ORIGIN_RULE",
    "WRITE_REFUSED",
    "frontend_write",
    "is_frontend_write",
]

logger = logging.getLogger(__name__)

HEADER = "X-HAT-Request"
HEADER_VALUE = "1"
ORIGIN_RULE = "frontend_origin"  # the `app.state` attribute holding the frontend-origin rule
WRITE_REFUSED = "Refused: write requests must come from the HAT page"
_UNNAMED_OK = frozenset({"same-origin", "none"})


def is_frontend_write(
    header: str | None,
    origin: str | None,
    fetch_site: str | None,
    rule: Callable[[str], bool] | None,
) -> bool:
    """Whether a write request with these headers came from the HAT page (module docstring)."""
    if header != HEADER_VALUE:
        return False
    if origin is not None:
        return rule is not None and rule(origin)
    return fetch_site is None or fetch_site in _UNNAMED_OK


def frontend_write(request: Request) -> None:
    """FastAPI dependency of every HAT write route: refuse with 403 unless `is_frontend_write`."""
    rule = getattr(request.app.state, ORIGIN_RULE, None)
    headers = request.headers
    if not is_frontend_write(
        headers.get(HEADER), headers.get("origin"), headers.get("sec-fetch-site"), rule
    ):
        logger.warning("HAT write refused: %s %s", request.method, request.url.path)
        raise HTTPException(status_code=403, detail=WRITE_REFUSED)
