"""Which browser origins count as the Hire a Trader frontend.

One rule for the CORS middleware: an origin in ALLOWED_ORIGINS (CORS_ORIGINS), or any
http loopback origin on any port, so a frontend served on another local port (the Vite dev server
moves off 3000 when it is taken) keeps working.
A loopback page is already local; the rule exists to keep pages on other sites out.
"""

import os
from urllib.parse import urlsplit

from fastapi.middleware.cors import CORSMiddleware

from .env_loader import load_dotenv

load_dotenv()

# The frontend origins (the Vite dev server ports) allowed by CORS. Override with CORS_ORIGINS,
# a comma-separated list.
DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001"
)


def parse_origins(raw: str) -> tuple[str, ...]:
    """Split a comma-separated origin list, dropping blanks and stray whitespace."""
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


ALLOWED_ORIGINS = parse_origins(os.getenv("CORS_ORIGINS", DEFAULT_ALLOWED_ORIGINS))

# The serialized opaque origin browsers send from sandboxed iframes and file:// pages.
OPAQUE_ORIGIN = "null"
# Exact hostnames of a page served from this machine (urlsplit drops the IPv6 brackets).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_origin(origin: str) -> bool:
    """Whether origin is exactly http://<loopback host>[:port], with no userinfo or path.

    The hostname must match exactly, so localhost.evil.com, 127.0.0.1.evil.com and
    http://localhost@evil.com (whose host is evil.com) are not loopback.
    """
    try:
        parts = urlsplit(origin)
        port = parts.port  # ValueError for a malformed or out-of-range port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and port != 0
        and parts.hostname in LOOPBACK_HOSTS
        and parts.username is None
        and parts.password is None
        and not (parts.path or parts.query or parts.fragment)
    )


def is_frontend_origin(origin: str) -> bool:
    """Whether a browser page with this Origin may call the API or open the stream.

    The opaque origin "null" is refused even if CORS_ORIGINS lists it, since any page can
    produce it.
    """
    if origin == OPAQUE_ORIGIN:
        return False
    return origin in ALLOWED_ORIGINS or is_loopback_origin(origin)


class FrontendCORSMiddleware(CORSMiddleware):
    """CORSMiddleware that allows exactly the origins is_frontend_origin allows."""

    def is_allowed_origin(self, origin: str) -> bool:
        return is_frontend_origin(origin)
