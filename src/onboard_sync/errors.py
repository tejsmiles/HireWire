"""Error taxonomy shared by adapters and the executor.

Real HTTP adapters map status codes to these classes, and the fakes raise them directly.
The executor only cares about the class: retry, adopt, or stop.
"""
from __future__ import annotations


class TargetError(Exception):
    retryable = False

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


class TransientError(TargetError):
    """429 / 5xx / timeouts. Safe to retry with the same idempotency key."""
    retryable = True


class PermanentError(TargetError):
    """4xx other than 409/429. Retrying cannot help; a human or a data fix is needed."""


class AlreadyExists(TargetError):
    """409. Either our own earlier attempt succeeded (adopt it) or it is a real collision."""


def http_error(status: int, code: str, message: str) -> TargetError:
    if status == 409:
        return AlreadyExists(status, code, message)
    if status == 429 or status >= 500:
        return TransientError(status, code, message)
    return PermanentError(status, code, message)
