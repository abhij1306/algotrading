"""The production `RuntimeParts`: the active broker's adapter per broker account, the NSE
downloads, the MCP client and the Telegram Bot API over HTTPS, around the one live feed
`app/main.py` builds (HAT-OVR-006: only main.py constructs a `LiveFeed`) (PR 17).

The active broker (`settings.broker`) is the only one with an adapter. With Kite active, the start
refuses without the Kite credentials or with a proxy that would carry Kite traffic, and the weekly
reference refresh also fills the zerodha broker symbols. Market data stays on Fyers either way, so
the Fyers proxy check always runs.

Nothing here is built by a check: the simulated day hands `HatRuntime` fixtures, `FakeBroker` and
a recording Telegram transport instead. Building the parts reads the Telegram variables, so a bad
configuration refuses the start (HAT-TG-040).

The NSE files the reference jobs read are text sources over the bhavcopy `Fetcher` (browser-like
headers, one request per call); a failed download raises and the job turns it into its stable
error code (`reference_jobs._read`). [A] The locations are NSE's public archive paths as of 2026;
the holiday list is the JSON `parse_nse_holidays` reads. Each is validated on the first live run.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Callable, Mapping
from datetime import date

from ..data.announcements import HttpFeedFetcher
from ..data.bars_store import BarStore
from ..data.bhavcopy import Fetcher, http_fetcher
from ..data.lineage import NAME_CHANGES_URL, SYMBOL_CHANGES_URL
from ..data.mcp_client import client_from_env
from ..data.reference_jobs import TextSource
from ..data.store import DataStore
from ..errors import HatConfigError
from ..execution.accounts import active_adapters
from ..execution.broker import HatBroker
from ..execution.fyers_auth import MissingCredentialsError, get_fyers_auth
from ..execution.fyers_ipv4 import require_direct_fyers
from ..execution.kite_adapter import kite_symbol_source
from ..execution.kite_auth import get_kite_auth, kite_credentials_from_env
from ..execution.kite_ipv4 import require_direct_kite
from ..models import HatBrokerAccount
from ..models.strategy import BROKER_FYERS, BROKER_ZERODHA
from ..settings import HatSettings, process_env
from ..telegram.client import HttpsTransport
from ..telegram.config import telegram_settings
from .assembly import Feed, RuntimeParts
from .nightly import TextSources

__all__ = ["BrokerLoginLinks", "one_login_each", "production_parts", "require_broker_ready"]

logger = logging.getLogger(__name__)

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
HOLIDAYS_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
INDUSTRY_LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
PRICE_BANDS_URL = "https://nsearchives.nseindia.com/content/equities/sec_list_{stamp}.csv"
# The daily PR archive (published about 17:30 IST); its `bc` CSV lists the corporate actions
# of the coming sessions, the dividends with their cash (CA4).
PR_ARCHIVE_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{stamp}.zip"
PR_MEMBER_MAX = 5_000_000  # the bc CSV is about 15 kB: a larger member is refused, not read


def url_source(url: str, fetch: Fetcher) -> TextSource:
    """A text source downloading `url` on each call (no response text is kept on a failure)."""

    def read() -> str:
        result = fetch(url)
        if result.status != "ok" or not result.data:
            raise OSError(result.status)
        return result.data.decode("utf-8-sig")

    return read


def pr_member(data: bytes, name: str) -> str:
    """The text of the member `name` of a PR archive; `OSError` for an archive that is not a
    zip, has no such member or holds one larger than `PR_MEMBER_MAX` (checked before reading)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            found = [i for i in archive.infolist() if i.filename.lower() == name.lower()]
            if len(found) != 1 or found[0].file_size > PR_MEMBER_MAX:
                raise OSError("pr_member_missing")
            with archive.open(found[0]) as member:
                raw = member.read(PR_MEMBER_MAX + 1)
    except zipfile.BadZipFile as exc:
        raise OSError("pr_bad_archive") from exc
    if len(raw) > PR_MEMBER_MAX:
        raise OSError("pr_member_missing")
    return raw.decode("utf-8-sig", errors="replace")


def corporate_action_file(day: date, fetch: Fetcher) -> TextSource:
    """The `bc` CSV of NSE's PR archive of `day`, downloaded on each call."""

    def read() -> str:
        result = fetch(PR_ARCHIVE_URL.format(stamp=day.strftime("%d%m%y")))
        if result.status != "ok" or not result.data:
            raise OSError(result.status)
        return pr_member(result.data, f"bc{day:%d%m%Y}.csv")

    return read


class BrokerLoginLinks:
    """The login link of an account's broker (Fyers' or Kite's) that the Telegram login prompt
    carries; None for another broker or when one cannot be made."""

    def link(self, account: HatBrokerAccount) -> str | None:
        if account.broker == BROKER_FYERS:
            login_url = get_fyers_auth().login_url
        elif account.broker == BROKER_ZERODHA:
            login_url = get_kite_auth().login_url
        else:
            return None
        try:
            return login_url()
        except Exception as error:  # missing credentials: the prompt goes out without a link
            logger.warning("%s login link unavailable (%s)", account.broker, type(error).__name__)
            return None


def require_broker_ready(broker: str, env: Mapping[str, str]) -> None:
    """Refuse the start (`HatConfigError`) when Kite is the active broker without its three app
    credentials (`kite_auth.kite_credentials_from_env`) or with a proxy that would carry Kite
    traffic past the IPv4 pin. Fyers needs nothing here: its login is checked
    by the token check, and its proxy check runs at every start (market data)."""
    if broker != BROKER_ZERODHA:
        return
    require_direct_kite(env)
    try:
        kite_credentials_from_env()
    except MissingCredentialsError:
        raise HatConfigError("kite_credentials_missing") from None


def one_login_each(
    factories: Mapping[str, Callable[[], HatBroker]],
) -> Callable[[HatBrokerAccount], HatBroker | None]:
    """The adapter of each active account. An adapter logs in once per broker (one token store,
    trading-safety rule 9), so a second active account of a broker refuses the start: it would
    place and reconcile through the first account's login, and the allocation would count the
    same funds twice (HAT-OPS-002)."""
    built: set[str] = set()

    def broker_for(account: HatBrokerAccount) -> HatBroker | None:
        factory = factories.get(account.broker)
        if factory is None:
            logger.warning("account %s: no adapter for its broker", account.id)
            return None
        if account.broker in built:
            raise HatConfigError("two active accounts share one broker login")
        built.add(account.broker)
        return factory()

    return broker_for


def production_parts(
    settings: HatSettings, feed: Feed, *, prevalidated_token: bool | None = None
) -> Callable[[], RuntimeParts]:
    """The parts factory `HatRuntime` calls once HAT is enabled and the database is Postgres."""

    def build() -> RuntimeParts:
        env = process_env()
        require_direct_fyers(env)  # no proxy carries Fyers traffic past the IPv4 pin
        require_broker_ready(settings.broker, env)
        telegram = telegram_settings()
        fetch = http_fetcher()
        store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)

        def price_bands(day: date) -> TextSource:
            return url_source(PRICE_BANDS_URL.format(stamp=day.strftime("%d%m%Y")), fetch)

        return RuntimeParts(
            store=store,
            feed=feed,
            telegram=telegram,
            transport=HttpsTransport(telegram.token),
            broker_for=one_login_each(active_adapters(settings.broker)),
            mcp=lambda: client_from_env(env),  # a fresh client per run: its call log is the run's
            bhavcopy=lambda: fetch,
            sources=TextSources(
                equity_list=url_source(EQUITY_LIST_URL, fetch),
                holidays=url_source(HOLIDAYS_URL, fetch),
                price_bands=price_bands,
                industry_list=url_source(INDUSTRY_LIST_URL, fetch),
                symbol_changes=url_source(SYMBOL_CHANGES_URL, fetch),
                name_changes=url_source(NAME_CHANGES_URL, fetch),
                corporate_actions=lambda day: corporate_action_file(day, fetch),
                kite_symbols=(
                    kite_symbol_source(store, fetch) if settings.broker == BROKER_ZERODHA else None
                ),
            ),
            bar_store=BarStore(settings.data_root),
            announcements=HttpFeedFetcher(),
            links=BrokerLoginLinks(),
            prevalidated_token=prevalidated_token,
        )

    return build
