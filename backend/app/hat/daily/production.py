"""The production `RuntimeParts`: the Fyers adapter per broker account, the NSE downloads, the MCP
client and the Telegram Bot API over HTTPS, around the one live feed `app/main.py` builds
(HAT-OVR-006: only main.py constructs a `LiveFeed`) (PR 17).

Nothing here is built by a check: the simulated day hands `HatRuntime` fixtures, `FakeBroker` and
a recording Telegram transport instead. Building the parts reads the Telegram variables, so a bad
configuration refuses the start (HAT-TG-040).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from ..data.announcements import HttpFeedFetcher
from ..data.bars_store import BarStore
from ..data.bhavcopy import http_fetcher
from ..data.mcp_client import client_from_env
from ..data.nse_sources import (
    EQUITY_LIST_URL,
    HOLIDAYS_URL,
    INDUSTRY_LIST_URL,
    price_band_url,
    url_source,
)
from ..data.reference_jobs import TextSource
from ..data.store import DataStore
from ..execution.accounts import ADAPTER_FACTORIES
from ..execution.broker import HatBroker
from ..execution.fyers_auth import get_fyers_auth
from ..models import HatBrokerAccount
from ..settings import HatSettings, process_env
from ..telegram.client import HttpsTransport
from ..telegram.config import telegram_settings
from .nightly import TextSources
from .runtime import Feed, RuntimeParts

__all__ = ["production_parts"]

logger = logging.getLogger(__name__)


class FyersLoginLinks:
    """The Fyers login link the Telegram login prompt carries; None when it cannot be made."""

    def link(self, account: HatBrokerAccount) -> str | None:
        if account.broker != "fyers":
            return None
        try:
            return get_fyers_auth().login_url()
        except Exception as error:  # missing credentials: the prompt goes out without a link
            logger.warning("Fyers login link unavailable (%s)", type(error).__name__)
            return None


def _broker_for(account: HatBrokerAccount) -> HatBroker | None:
    factory = ADAPTER_FACTORIES.get(account.broker)
    if factory is None:
        logger.warning("account %s: no adapter for its broker", account.id)
        return None
    return factory()


def production_parts(
    settings: HatSettings, feed: Feed, *, prevalidated_token: bool | None = None
) -> Callable[[], RuntimeParts]:
    """The parts factory `HatRuntime` calls once HAT is enabled and the database is Postgres."""

    def build() -> RuntimeParts:
        telegram = telegram_settings()
        env = process_env()
        fetch = http_fetcher()

        def price_bands(day: date) -> TextSource:
            return url_source(price_band_url(day), fetch)

        return RuntimeParts(
            store=DataStore(settings.data_root),
            feed=feed,
            telegram=telegram,
            transport=HttpsTransport(telegram.token),
            broker_for=_broker_for,
            mcp=lambda: client_from_env(env),  # a fresh client per run: its call log is the run's
            bhavcopy=lambda: fetch,
            sources=TextSources(
                equity_list=url_source(EQUITY_LIST_URL, fetch),
                holidays=url_source(HOLIDAYS_URL, fetch),
                price_bands=price_bands,
                industry_list=url_source(INDUSTRY_LIST_URL, fetch),
            ),
            bar_store=BarStore(settings.data_root),
            announcements=HttpFeedFetcher(),
            links=FyersLoginLinks(),
            prevalidated_token=prevalidated_token,
        )

    return build
