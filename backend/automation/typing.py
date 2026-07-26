"""Human-paced typing: plan first, then replay.

`plan_keystrokes` is a pure function — text + settings in, keystroke list out.
That means the Config panel can preview exactly what will happen, and the whole
thing is testable without launching a browser.

The invariant that matters: however many characters get deleted and retyped,
replaying a plan must reproduce the original text exactly. `replay()` exists to
assert that.

This is not a cloaking device. Instagram fingerprints far more than key timing;
varying delays will not stop a challenge. It is here because the login form is
React-controlled and only enables its submit button after real key events.
"""
import asyncio
import random

BACKSPACE = "Backspace"


def plan_keystrokes(text: str, cfg: dict, rng: random.Random | None = None) -> list[dict]:
    """Expand `text` into {key, delay_ms} events, with occasional
    type-a-bit-too-far-then-correct behaviour."""
    rng = rng or random
    speed = max(0, int(cfg.get("speed_ms", 120)))
    jitter = max(0, int(cfg.get("jitter_ms", 0)))
    enabled = bool(cfg.get("remove_enabled", False))
    every = max(1, int(cfg.get("remove_every", 8)))
    lo = max(0, int(cfg.get("remove_min", 1)))
    hi = max(lo, int(cfg.get("remove_max", 3)))

    def delay() -> int:
        return speed + (rng.randint(0, jitter) if jitter else 0)

    events: list[dict] = []
    typed = ""          # what the field currently holds
    since_removal = 0

    for index, char in enumerate(text):
        events.append({"key": char, "delay_ms": delay()})
        typed += char
        since_removal += 1

        at_end = index == len(text) - 1
        if not enabled or at_end or since_removal < every:
            continue

        # Delete a few of the characters just typed, then put them back.
        since_removal = 0
        count = min(rng.randint(lo, hi), len(typed))
        if not count:
            continue
        for _ in range(count):
            events.append({"key": BACKSPACE, "delay_ms": delay()})
        restored, typed = typed[-count:], typed[:-count]
        for again in restored:
            events.append({"key": again, "delay_ms": delay()})
            typed += again

    return events


def replay(events: list[dict]) -> str:
    """What a plan actually leaves in the field. Used by tests and the preview."""
    out = ""
    for event in events:
        if event["key"] == BACKSPACE:
            out = out[:-1]
        else:
            out += event["key"]
    return out


def summarize(text: str, events: list[dict]) -> dict:
    total_ms = sum(e["delay_ms"] for e in events)
    corrections = sum(1 for e in events if e["key"] == BACKSPACE)
    result = replay(events)
    return {
        "chars": len(text),
        "keystrokes": len(events),
        "backspaces": corrections,
        "estimated_ms": total_ms,
        "estimated_s": round(total_ms / 1000, 1),
        "result": result,
        "correct": result == text,
    }


async def type_text(locator, text: str, cfg: dict) -> dict:
    """Execute a plan against a real input. Returns the summary."""
    events = plan_keystrokes(text, cfg)
    await locator.click()
    await asyncio.sleep(0.15)
    keyboard = locator.page.keyboard  # handles '@', '.', case — press() would not
    for event in events:
        if event["key"] == BACKSPACE:
            await keyboard.press(BACKSPACE)
        else:
            await keyboard.type(event["key"])
        await asyncio.sleep(event["delay_ms"] / 1000)
    return summarize(text, events)
