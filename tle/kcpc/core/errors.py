"""Exception hierarchy for KCPC code.

Services raise these instead of discord.py exceptions. The Discord layer
(``tle.kcpc.bot``) turns them into replies: ``KcpcUserError`` messages are shown
to the user as-is, anything else gets a generic reply and a logged traceback.
"""


class KcpcError(Exception):
    """Base class for every KCPC error."""


class KcpcUserError(KcpcError):
    """An error whose message is safe and useful to show to the user."""


class KcpcDisabledError(KcpcUserError):
    """KCPC services are not running (``--nodb``, or they failed to start)."""

    def __init__(self, message: str = 'KCPC features are not available right now.'):
        super().__init__(message)


class ConfigError(KcpcError):
    """Invalid configuration: an environment variable or a stored setting."""


class MigrationError(KcpcError):
    """The KCPC database schema could not be brought up to date."""


class ExternalServiceError(KcpcUserError):
    """A request to an external site failed, after any retries.

    The message is user-facing (e.g. "AtCoder is not responding right now");
    ``service`` and ``status`` are kept for logs and tests.
    """

    def __init__(self, service: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.service = service
        self.status = status
