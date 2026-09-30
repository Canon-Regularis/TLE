"""Bot settings read from the environment (see .env.example and README.md).

TLE's older settings live in ``tle.constants``; new ones are read here, once, by
``Settings.from_env()`` at startup.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from tle import constants
from tle.kcpc.core import timeutil
from tle.kcpc.core.errors import ConfigError

DEFAULT_TIMEZONE = 'Europe/London'
DEFAULT_HTTP_USER_AGENT = 'KCPC-bot (+https://github.com/Canon-Regularis/TLE)'
DEFAULT_ICPC_CONTEST_CODES = ('UKIEPC', 'Northwestern-Europe-2027')


@dataclass(frozen=True)
class Settings:
    """Settings read at startup; ``from_env`` lists the environment variables."""

    kcpc_timezone: str = DEFAULT_TIMEZONE
    disabled_extensions: frozenset[str] = frozenset()
    http_user_agent: str = DEFAULT_HTTP_USER_AGENT
    luma_calendar_id: str | None = None
    icpc_contest_codes: tuple[str, ...] = DEFAULT_ICPC_CONTEST_CODES
    clist_username: str | None = None
    clist_api_key: str | None = field(default=None, repr=False)  # a secret
    kcpc_db_path: Path = constants.KCPC_DB_FILE_PATH

    def __post_init__(self) -> None:
        # Fail at startup rather than when a schedule first needs the zone.
        try:
            timeutil.zone(self.kcpc_timezone)
        except ConfigError as exc:
            raise ConfigError(f'KCPC_TIMEZONE: {exc}') from exc

    @property
    def tz(self) -> ZoneInfo:
        """The club's time zone, for schedules and times that admins type."""
        return timeutil.zone(self.kcpc_timezone)

    @property
    def clist_configured(self) -> bool:
        """Whether clist.by credentials are set, which the clist source needs."""
        return bool(self.clist_username and self.clist_api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> 'Settings':
        """Read the settings from ``env``, by default ``os.environ``.

        - ``KCPC_TIMEZONE``: an IANA zone name, e.g. ``Europe/London``.
        - ``DISABLED_EXTENSIONS``: comma-separated extension names or families,
          e.g. ``tle.duel,kcpc``; see ``tle.extensions``.
        - ``HTTP_USER_AGENT``: the User-Agent of KCPC's requests to other sites,
          except where a host policy sets its own (codeforces.com pages are
          fetched with a browser's User-Agent).
        - ``LUMA_CALENDAR_ID``: the Luma calendar of servers that set none.
        - ``ICPC_CONTEST_CODES``: comma-separated icpc.global contest codes.
        - ``CLIST_USERNAME`` and ``CLIST_API_KEY``: clist.by credentials.
        - ``KCPC_DB_PATH``: where the KCPC database file lives.

        Values and list items are stripped, empty list items are dropped, and
        extension names are lowercased. A variable that is unset or blank, or
        a list with no items, keeps its default (None if there is no other).
        Raises ``ConfigError`` for an unknown time zone.
        """
        env = os.environ if env is None else env
        db_path = _value(env, 'KCPC_DB_PATH')
        return cls(
            kcpc_timezone=_value(env, 'KCPC_TIMEZONE') or DEFAULT_TIMEZONE,
            disabled_extensions=frozenset(
                name.lower() for name in _items(env, 'DISABLED_EXTENSIONS')
            ),
            http_user_agent=_value(env, 'HTTP_USER_AGENT') or DEFAULT_HTTP_USER_AGENT,
            luma_calendar_id=_value(env, 'LUMA_CALENDAR_ID'),
            icpc_contest_codes=(
                _items(env, 'ICPC_CONTEST_CODES') or DEFAULT_ICPC_CONTEST_CODES
            ),
            clist_username=_value(env, 'CLIST_USERNAME'),
            clist_api_key=_value(env, 'CLIST_API_KEY'),
            kcpc_db_path=Path(db_path) if db_path else constants.KCPC_DB_FILE_PATH,
        )


def _value(env: Mapping[str, str], name: str) -> str | None:
    """The stripped value of ``name``, or None if it is unset or blank."""
    return env.get(name, '').strip() or None


def _items(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    """The comma-separated items of ``name``, stripped, without empty ones."""
    items = (item.strip() for item in env.get(name, '').split(','))
    return tuple(item for item in items if item)
