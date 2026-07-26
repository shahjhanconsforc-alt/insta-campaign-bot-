"""One job per account at a time.

Different accounts run concurrently on purpose — that's the point of having
several logged in: scroll account 2's feed while account 1 works through its
campaign. What must never happen is two jobs on the SAME account, because they
share one Chrome window and would type over each other.

The check-then-set below is safe without a lock: asyncio runs it on a single
thread and there is no await between the read and the write.
"""
from typing import Any

_running: dict[int, str] = {}


class Busy(RuntimeError):
    """Raised when an account already has a job running."""


class claim:
    """Hold an account for the length of a job.

        with tasks.claim(account_id, "Campaign"):
            ...
    """

    def __init__(self, account_id: int | None, name: str) -> None:
        self.account_id = account_id
        self.name = name

    def __enter__(self) -> "claim":
        if self.account_id is None:          # no account picked — nothing to guard
            return self
        busy = _running.get(self.account_id)
        if busy:
            raise Busy(
                f"That account is already running {busy!r}. Wait for it to finish, "
                "or pick a different account."
            )
        _running[self.account_id] = self.name
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.account_id is not None:
            _running.pop(self.account_id, None)


def running() -> dict[str, str]:
    """What's running right now, keyed by account id — for the UI."""
    return {str(k): v for k, v in _running.items()}


def is_busy(account_id: int | None) -> str | None:
    return _running.get(account_id) if account_id is not None else None
