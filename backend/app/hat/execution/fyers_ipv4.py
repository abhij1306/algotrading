"""Fyers HTTP over IPv4 only (HAT-BRK-093).

Fyers accepts orders only from the account's whitelisted static IP, which is an IPv4 address. A
machine with IPv6 reaches `api-t1.fyers.in` over IPv6 by default, and Fyers then refuses every
write with code -50 ("only whitelisted IP"), as the live write test of 2026-10-22 found.

This module is the one place that sets it: it is the only importer of the SDK's HTTP client
(`fyers_apiv3.fyersModel`, whose `FyersModel` sends orders, GTTs, history and quotes and whose
`SessionModel` exchanges the login code), and it installs the hook below before it hands the
SDK out. The hook wraps urllib3's `create_connection`, which every `requests` call uses: a
connection to a Fyers host (`fyers.in` or a subdomain) is opened only to the host's IPv4
addresses, and with none it fails rather than fall back to IPv6. Any other host is untouched.
TLS still checks the Fyers host name: only the TCP connect gets the address literal.

A proxy would bypass the hook: `requests` (and the data socket's client) would connect to the
proxy, which may reach Fyers over IPv6. So HAT refuses to start while a proxy variable would
carry Fyers traffic (`require_direct_fyers`, PR 73, PR 25 follow-up 6), and `sdk` refuses to hand
out a client, unless every `no_proxy` variable set names the whole Fyers domain or is `*`.

The hook pins every domain in `pinned_domains()`: Fyers' always, and a domain `pin` adds (Kite's,
from `kite_ipv4`, which whitelists a static IPv4 too). One hook serves both, so the order the two
SDKs are first used in does not matter.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Mapping
from types import ModuleType

import urllib3.util.connection as urllib3_connection

from ..errors import HatConfigError
from ..settings import process_env

__all__ = [
    "FYERS_DOMAIN",
    "PROXY_VARIABLES",
    "connect_ipv4",
    "domain_proxy",
    "fyers_proxy",
    "install",
    "is_domain_host",
    "is_fyers_host",
    "pin",
    "pinned_domains",
    "require_direct_fyers",
    "sdk",
]

FYERS_DOMAIN = "fyers.in"

# what `requests` and the socket client read for an https or wss connection (either case)
PROXY_VARIABLES = ("https_proxy", "all_proxy", "http_proxy")
_NO_PROXY = "no_proxy"
_lock = threading.Lock()
_original: Callable[..., socket.socket] | None = None
_pinned: frozenset[str] = frozenset({FYERS_DOMAIN})


class FyersIPv4Error(OSError):
    """A pinned broker host has no IPv4 address: the connection is refused, never made over
    IPv6."""


def is_domain_host(host: object, domain: str) -> bool:
    """Whether `host` is `domain` or one of its subdomains (case and a trailing dot ignored); a
    look-alike such as `notfyers.in` or `fyers.in.example` is not."""
    if not isinstance(host, str):
        return False
    name = host.strip("[]").rstrip(".").lower()
    return name == domain or name.endswith("." + domain)


def is_fyers_host(host: object) -> bool:
    """Whether `host` is Fyers' domain or one of its subdomains."""
    return is_domain_host(host, FYERS_DOMAIN)


def pin(domain: str) -> None:
    """Connect to `domain` and its subdomains over IPv4 only from now on (idempotent)."""
    global _pinned
    with _lock:
        _pinned = _pinned | {domain.lower()}


def pinned_domains() -> frozenset[str]:
    return _pinned


def connect_ipv4(
    connect: Callable[..., socket.socket], address: tuple[str, int], *args: object, **kwargs: object
) -> socket.socket:
    """Open `address` with `connect` (urllib3's `create_connection`), a host of a pinned domain
    through its IPv4 addresses only, in the resolver's order; the last failure is raised."""
    host, port = address
    if not any(is_domain_host(host, domain) for domain in _pinned):
        return connect(address, *args, **kwargs)
    found = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(str(sockaddr[0]) for *_, sockaddr in found))
    if not addresses:
        raise FyersIPv4Error("no IPv4 address for the broker host")
    error: OSError | None = None
    for ip in addresses:
        try:
            return connect((ip, port), *args, **kwargs)
        except OSError as exc:
            error = exc
    assert error is not None
    raise error


def _set(env: Mapping[str, str], name: str) -> str | None:
    """The first non-empty value of `name` in lower then upper case (as urllib reads it)."""
    return next((env[key] for key in (name, name.upper()) if env.get(key, "").strip()), None)


def _bypasses(value: str, domain: str) -> bool:
    """Whether one `NO_PROXY` value bypasses the whole `domain` for every client: `*` alone
    (among other entries it bypasses nothing, as urllib reads it), or an entry naming the domain
    or a parent, in lower case (requests and urllib ignore case, but the data socket's client is
    not relied on to). An entry for one host is not enough: the API, login and data socket hosts
    differ."""
    value = value.strip()
    entries = [e.strip().lstrip(".") for e in value.split(",")]
    return value == "*" or any(e and (e == domain or domain.endswith("." + e)) for e in entries)


def domain_proxy(env: Mapping[str, str], domain: str) -> str | None:
    """The name of the first proxy variable that would carry traffic to `domain`, or None: none
    is set, or every `no_proxy` variable set (either case, an empty one included: clients
    disagree on which they read and on an empty one) bypasses the whole domain. The value is
    never returned (it can carry a password)."""
    no_proxy = [env[key] for key in (_NO_PROXY, _NO_PROXY.upper()) if key in env]
    if no_proxy and all(_bypasses(value, domain) for value in no_proxy):
        return None
    return next((name for name in PROXY_VARIABLES if _set(env, name) is not None), None)


def fyers_proxy(env: Mapping[str, str]) -> str | None:
    """`domain_proxy` for the Fyers domain."""
    return domain_proxy(env, FYERS_DOMAIN)


def require_direct_fyers(env: Mapping[str, str]) -> None:
    """Refuse (`HatConfigError`, naming the variable) while a proxy would carry Fyers traffic
    past the IPv4 pin: at the production start, and in `sdk` before any Fyers client is made."""
    name = fyers_proxy(env)
    if name is not None:
        raise HatConfigError(f"fyers_proxy_set: {name}")


def install() -> None:
    """Route urllib3's connections through `connect_ipv4` (once per process; idempotent)."""
    global _original
    with _lock:
        if _original is not None:
            return
        original = urllib3_connection.create_connection
        _original = original

        def create_connection(
            address: tuple[str, int], *args: object, **kwargs: object
        ) -> socket.socket:
            return connect_ipv4(original, address, *args, **kwargs)

        urllib3_connection.create_connection = create_connection  # type: ignore[assignment]


def sdk() -> ModuleType:
    """The SDK's `fyersModel` module, with the IPv4 hook installed first. Refused while a proxy
    would carry Fyers traffic (`require_direct_fyers`), so no Fyers HTTP path (the start-up
    token check, a login with HAT disabled, a job) goes through one."""
    require_direct_fyers(process_env())
    install()
    from fyers_apiv3 import fyersModel

    return fyersModel
