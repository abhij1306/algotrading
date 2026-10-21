"""HAT exception types. Messages never carry raw broker, provider or database text."""


class HatError(Exception):
    """Base class for HAT errors."""


class HatConfigError(HatError):
    """HAT is configured in a way it must not run with (fail closed)."""
