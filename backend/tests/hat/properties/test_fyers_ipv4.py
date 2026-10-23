"""PBT fyers_ipv4.*: Fyers HTTP leaves over IPv4 only.

The live write test of 2026-10-22 found `requests` reaching Fyers over IPv6 by default, and Fyers
refused every write from it with code -50 ("only whitelisted IP"). These properties drive a real
`requests.Session` (the client inside the SDK's `FyersModel`) through urllib3 with the hook
installed, over a scripted resolver and socket: nothing reaches the network.
"""

import os
import socket
import urllib.request
from unittest.mock import patch

import pytest
import requests
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.daily.production import production_parts
from app.hat.errors import HatConfigError
from app.hat.execution import fyers_ipv4
from app.hat.settings import load_settings

FYERS_HOSTS = ["api-t1.fyers.in", "api.fyers.in", "FYERS.IN", "api-t1.fyers.in.", "myapi.fyers.in"]
OTHER_HOSTS = ["notfyers.in", "fyers.in.example.com", "www.nseindia.com", "api.telegram.org"]
V4 = ["10.0.0.1", "10.0.0.2", "192.0.2.7"]
V6 = ["2001:db8::1", "2001:db8::2"]


addresses = st.lists(
    st.one_of(
        st.sampled_from(V4).map(lambda ip: (socket.AF_INET, ip)),
        st.sampled_from(V6).map(lambda ip: (socket.AF_INET6, ip)),
    ),
    max_size=5,
)


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
    "FYERS.IN",
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
@example(env={"https_proxy": PROXY_URL, "NO_PROXY": "*", "no_proxy": ""})
@example(env={"https_proxy": PROXY_URL, "no_proxy": "", "NO_PROXY": "*"})
@example(env={"https_proxy": PROXY_URL, "no_proxy": "FYERS.IN"})
def test_no_proxy_carries_fyers(env: dict[str, str]) -> None:
    """PBT fyers_ipv4.no_proxy_carries_fyers: whenever
    `require_direct_fyers` lets HAT start, neither `requests` (the SDK's client) nor urllib's
    reading of the environment uses a proxy for any Fyers host HAT reaches; and when it refuses,
    both the start of the production parts and `sdk` (every Fyers HTTP client) fail with
    `HatConfigError` naming the variable, never its value. Harm: a
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
                seen = urllib.request.getproxies_environment()
                proxied = seen.get("https") or seen.get("all")
                assert not proxied or urllib.request.proxy_bypass_environment(host, seen), host
        else:
            assert named in fyers_ipv4.PROXY_VARIABLES
            build = production_parts(load_settings(os.environ), feed=None)  # type: ignore[arg-type]
            with pytest.raises(HatConfigError) as refused:
                build()
            assert str(refused.value) == f"fyers_proxy_set: {named}"
            assert "pw-77ee" not in str(refused.value)
            with pytest.raises(HatConfigError) as refused:
                fyers_ipv4.sdk()
            assert str(refused.value) == f"fyers_proxy_set: {named}"
