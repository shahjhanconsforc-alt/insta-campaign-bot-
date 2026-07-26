"""Global run controls: stop, pause/resume, reset.

Long jobs (a feed scrape, a campaign) call `await checkpoint()` once per step.
That's the only place they can be paused or stopped, which is deliberate: a job
is halted BETWEEN steps, never mid-keystroke, so it can't leave half a username
typed in a search box or half a message in a composer.

Chrome is never touched here. Stop ends the work and leaves every window open
and logged in — the sessions are the expensive thing, not the jobs.
"""
import asyncio
import time

_stopping = False
_paused = False
# Set = running. Cleared = paused, and checkpoint() blocks on it.
_gate = asyncio.Event()
_gate.set()


class Stopped(RuntimeError):
    """Raised inside a job when Stop is pressed. Ends the job; keeps the browser."""


async def checkpoint() -> None:
    """Called by jobs between steps. Returns normally, waits, or raises Stopped."""
    if _stopping:
        raise Stopped("Stopped by the Stop button — the browser is still open.")
    if not _gate.is_set():
        await _gate.wait()          # parked here until Resume
        if _stopping:               # stopped while parked
            raise Stopped("Stopped by the Stop button — the browser is still open.")


async def sleep(seconds: float, slice_s: float = 0.15) -> None:
    """A between-steps pause that stays responsive to Stop and Pause.

    Sleeps up to `seconds`, but wakes the instant Stop is pressed (so the job's
    next checkpoint() halts it right away instead of after the whole pause) and
    parks while Paused. Never raises — turning a Stop into the actual break is
    the caller's checkpoint()'s job, not this gap's.
    """
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if _stopping:
            return                  # don't burn the rest of the gap after a Stop
        if not _gate.is_set():
            await _gate.wait()      # park while paused; time parked doesn't count
            continue
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(slice_s, remaining))


def stopping() -> bool:
    return _stopping


def stop() -> dict:
    """Ask every running job to end at its next step."""
    global _stopping
    _stopping = True
    _gate.set()                     # wake anything parked so it can see the stop
    return state()


def pause() -> dict:
    global _paused
    _paused = True
    _gate.clear()
    return state()


def resume() -> dict:
    global _paused
    _paused = False
    _gate.set()
    return state()


def reset() -> dict:
    """Back to a clean slate: nothing stopping, nothing paused.

    Call after the stopped jobs have actually ended — a fresh job started while
    `_stopping` is still true would die at its first checkpoint.
    """
    global _stopping, _paused
    _stopping = False
    _paused = False
    _gate.set()
    return state()


def state() -> dict:
    return {"stopping": _stopping, "paused": _paused}
