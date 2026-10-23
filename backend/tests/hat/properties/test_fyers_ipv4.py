"""PBT fyers_ipv4.* (HAT-BRK-093): Fyers HTTP leaves over IPv4 only.

The live write test of 2026-10-22 found `requests` reaching Fyers over IPv6 by default, and Fyers
refused every write from it with code -50 ("only whitelisted IP"). These properties drive a real
`requests.Session` (the client inside the SDK's `FyersModel`) through urllib3 with the hook
installed, over a scripted resolver and socket: nothing reaches the network.
"""

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from unittest.mock import patch

import pytest
import requests
import urllib3.util.connection as urllib3_connection
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.daily.production import production_parts
from app.hat.errors import HatConfigError
from app.hat.execution import fyers_ipv4
from app.hat.settings import load_settings

FYERS_HOSTS = ["api-t1.fyers.in", "api.fyers.in", "FYERS.IN", "api-t1.fyers.in.", "myapi.fyers.in"]
OTHER_HOSTS = ["notfyers.in", "fyers.in.example.com", "www.nseindia.com", "api.telegram.org"]
V4 = ["10.0.0.1", "10.0.0.2", "192.0.2.7"]
V6 = ["2001:db8::1", "2001:db8::2"]


class _Socket:
    """A socket that records its family and the address it was asked to connect to, and connects
    only to an address in `accepting` (the request then fails wrapping it in TLS: nothing is
    sent)."""

    made: list[tuple[int, object]]
    accepting: set[str]

    def __init__(self, family: int, kind: int = socket.SOCK_STREAM, proto: int = 0) -> None:
        self.family = family

    def setsockopt(self, *_args: object) -> None:
        pass

    def settimeout(self, _timeout: object) -> None:
        pass

    def connect(self, address: tuple) -> None:
        type(self).made.append((self.family, address[0]))
        if address[0] not in type(self).accepting:
            raise ConnectionRefusedError("scripted refusal")

    def close(self) -> None:
        pass


@contextmanager
def _scripted(records: list[tuple[int, str]], accepting: set[str]) -> Iterator[list]:
    """`socket.getaddrinfo` answers a host name with `records` (filtered by the family asked
    for, as the OS does) and records the family of each such lookup; an address literal is
    answered with itself. Sockets are `_Socket`."""
    lookups: list[int] = []

    def getaddrinfo(host: str, port: int, family: int = 0, *_rest: object) -> list:
        literal = next((af for af, ip in [(socket.AF_INET, h) for h in V4] if ip == host), None)
        literal = literal or (socket.AF_INET6 if host in V6 else None)
        if literal is not None:
            sockaddr = (host, port) if literal == socket.AF_INET else (host, port, 0, 0)
            return [(literal, socket.SOCK_STREAM, 6, "", sockaddr)]
        lookups.append(family)
        return [
            (
                af,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, port) if af == socket.AF_INET else (ip, port, 0, 0),
            )
            for af, ip in records
            if family in (0, socket.AF_UNSPEC, af)
        ]

    _Socket.made, _Socket.accepting = [], accepting
    with patch("socket.getaddrinfo", getaddrinfo), patch("socket.socket", _Socket):
        yield lookups


addresses = st.lists(
    st.one_of(
        st.sampled_from(V4).map(lambda ip: (socket.AF_INET, ip)),
        st.sampled_from(V6).map(lambda ip: (socket.AF_INET6, ip)),
    ),
    max_size=5,
)


@given(
    host=st.sampled_from(FYERS_HOSTS + OTHER_HOSTS),
    records=addresses,
    accepting=st.sets(st.sampled_from(V4 + V6)),
    path=st.sampled_from(["/api/v3/orders/sync", "/api/v3/gtt/orders/sync", "/data/history"]),
)
@settings(max_examples=150, deadline=None)
def test_fyers_http_leaves_over_ipv4(
    host: str, records: list[tuple[int, str]], accepting: set[str], path: str
) -> None:
    """For any resolver answer, a request to a Fyers host (`fyers.in` or a subdomain, any case,
    a trailing dot) opens sockets of family AF_INET only, to its IPv4 addresses in the
    resolver's order, trying the next one when a connect fails, and none at all when it has no
    IPv4 address (never falling back to IPv6). A request to any other host, a look-alike
    included, is untouched: it is resolved and connected as urllib3 does by itself."""
    fyers_ipv4.install()
    fyers_ipv4.install()  # idempotent: one hook, not two
    with _scripted(records, accepting) as lookups:
        # refused, or the fake socket cannot be wrapped in TLS: nothing is ever sent
        with suppress(Exception):
            requests.Session().get(f"https://{host}{path}", timeout=1)
    made = _Socket.made
    v4 = list(dict.fromkeys(ip for af, ip in records if af == socket.AF_INET))
    if fyers_ipv4.is_fyers_host(host):
        assert lookups == [socket.AF_INET]
        assert all(family == socket.AF_INET for family, _ip in made)
        tried = [ip for _f, ip in made]
        # each IPv4 address in order until one connects
        first_ok = next((i for i, ip in enumerate(v4) if ip in accepting), len(v4) - 1)
        assert tried == v4[: first_ok + 1]
    else:
        assert lookups == [urllib3_connection.allowed_gai_family()]
        # urllib3's own order: every record (both families) until one connects
        expected = []
        unspec = urllib3_connection.allowed_gai_family() != socket.AF_INET
        for af, ip in records:
            if not unspec and af != socket.AF_INET:
                continue
            expected.append((af, ip))
            if ip in accepting:
                break
        assert made == expected


@given(
    host=st.one_of(
        st.sampled_from(FYERS_HOSTS + OTHER_HOSTS),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz.-", max_size=30),
    )
)
def test_is_fyers_host_exact(host: str) -> None:
    """Exactly `fyers.in` and its subdomains are Fyers hosts, whatever the case or a trailing
    dot; a name merely containing it is not."""
    name = host.rstrip(".").lower()
    assert fyers_ipv4.is_fyers_host(host) == (name == "fyers.in" or name.endswith(".fyers.in"))


PROXY_NAMES = [
    *fyers_ipv4.PROXY_VARIABLES,
    *(name.upper() for name in fyers_ipv4.PROXY_VARIABLES),
]
PROXY_URL = "http://user:pw-77ee@proxy.example:3128"
NO_PROXY_ENTRIES = [
    "fyers.in",
    ".fyers.in",
    "in",
    "api-t1.fyers.in",
    "socket.fyers.in",
    "*",
    "example.com",
    "notfyers.in",
    " fyers.in ",
]
# the hosts HAT reaches: the API (orders, GTTs, quotes), the login and the data socket
REACHED = ["api-t1.fyers.in", "api.fyers.in", "socket.fyers.in"]
no_proxy_values = st.lists(st.sampled_from(NO_PROXY_ENTRIES), max_size=3).map(",".join)
ENVS = st.fixed_dictionaries(
    {},
    optional={
        **{name: st.sampled_from(["", PROXY_URL]) for name in PROXY_NAMES},
        "no_proxy": no_proxy_values,
        "NO_PROXY": no_proxy_values,
    },
)


@given(env=ENVS)
@example(env={"HTTPS_PROXY": PROXY_URL})
@example(env={"https_proxy": PROXY_URL, "NO_PROXY": "fyers.in,*"})
@example(env={"https_proxy": PROXY_URL, "no_proxy": "*"})
@example(env={"ALL_PROXY": PROXY_URL, "no_proxy": "api-t1.fyers.in"})
def test_no_proxy_carries_fyers(env: dict[str, str]) -> None:
    """PBT fyers_ipv4.no_proxy_carries_fyers (HAT-BRK-093; PR 73, PR 25 follow-up 6): whenever
    `require_direct_fyers` lets HAT start, `requests` (the SDK's client) uses no proxy for any
    Fyers host HAT reaches, as it reads this environment; and when it refuses, the start of the
    production parts fails with `HatConfigError` naming the variable, never its value. Harm: a
    proxy carries orders to Fyers over IPv6 past the IPv4 pin, and Fyers refuses every write
    from a non-whitelisted address (-50)."""
    named = fyers_ipv4.fyers_proxy(env)
    with patch.dict(os.environ, env, clear=True):
        if named is None:
            fyers_ipv4.require_direct_fyers(env)
            for host in REACHED:
                url = f"https://{host}/api/v3/orders"
                proxies = requests.utils.get_environ_proxies(url)
                assert requests.utils.select_proxy(url, proxies) is None, host
        else:
            assert named in fyers_ipv4.PROXY_VARIABLES
            build = production_parts(load_settings(os.environ), feed=None)  # type: ignore[arg-type]
            with pytest.raises(HatConfigError) as refused:
                build()
            assert str(refused.value) == f"fyers_proxy_set: {named}"
            assert "pw-77ee" not in str(refused.value)
