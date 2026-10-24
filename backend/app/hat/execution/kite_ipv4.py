"""Kite (Zerodha) HTTP over IPv4 only, as `fyers_ipv4` does for Fyers (HAT-BRK-093).

Kite accepts orders only from the app's whitelisted static IP, an IPv4 address, so every
connection to a Kite API host (`kite.trade` or a subdomain: `api.kite.trade` serves orders, the
token exchange and the instrument dump) goes over IPv4. This module is the only importer of the
`kiteconnect` SDK: `sdk` pins Kite's domain on `fyers_ipv4`'s one urllib3 hook, installs it, and
only then imports the SDK. The login page itself (`kite.zerodha.com`) opens in the owner's
browser, never from HAT.

A proxy would bypass the hook, so `sdk` refuses to hand out the SDK, and the production start with
Kite as the active broker refuses to start (`require_direct_kite`), while a proxy variable would
carry Kite traffic, unless every `no_proxy` variable set names the whole Kite domain or is `*`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import ModuleType

from ..errors import HatConfigError
from ..settings import process_env
from .fyers_ipv4 import domain_proxy, install, is_domain_host, pin

__all__ = ["KITE_DOMAIN", "is_kite_host", "kite_proxy", "require_direct_kite", "sdk"]

KITE_DOMAIN = "kite.trade"


def is_kite_host(host: object) -> bool:
    """Whether `host` is Kite's API domain or one of its subdomains."""
    return is_domain_host(host, KITE_DOMAIN)


def kite_proxy(env: Mapping[str, str]) -> str | None:
    """The name of the first proxy variable that would carry Kite traffic, or None."""
    return domain_proxy(env, KITE_DOMAIN)


def require_direct_kite(env: Mapping[str, str]) -> None:
    """Refuse (`HatConfigError`, naming the variable) while a proxy would carry Kite traffic past
    the IPv4 pin."""
    name = kite_proxy(env)
    if name is not None:
        raise HatConfigError(f"kite_proxy_set: {name}")


def sdk() -> ModuleType:
    """The `kiteconnect` package, with Kite's domain pinned to IPv4 and the hook installed first.
    Refused while a proxy would carry Kite traffic."""
    require_direct_kite(process_env())
    pin(KITE_DOMAIN)
    install()
    import kiteconnect

    return kiteconnect
