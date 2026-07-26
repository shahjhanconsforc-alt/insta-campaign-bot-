"""Runtime settings, editable from the Config panel.

Defaults live here; overrides persist in the JSON store, so a change survives a
restart without touching .env. Everything is clamped on the way in — the panel
must not be able to store a value that breaks typing.
"""
from typing import Any

from backend import db

TYPING_DEFAULTS: dict[str, Any] = {
    # milliseconds between characters
    "speed_ms": 120,
    # extra 0..jitter ms added per character, so the pace isn't robotic
    "jitter_ms": 60,
    # type a bit too far, then backspace and correct
    "remove_enabled": True,
    # trigger a correction after every N characters
    "remove_every": 8,
    # how many characters to delete, picked randomly in [min, max]
    "remove_min": 1,
    "remove_max": 3,
}

# How Scrapping scrolls the home feed while collecting usernames. The pause and
# step are ranges, picked randomly per step, so the pace isn't metronomic.
SCROLL_DEFAULTS: dict[str, Any] = {
    # how long one scrape runs for
    "scroll_duration_s": 90,
    # pause between scroll steps — this is the speed knob. Longer = slower.
    "scroll_pause_min_ms": 2800,
    "scroll_pause_max_ms": 4200,
    # how far each step moves, as a percentage of the window height
    "scroll_step_min_pct": 45,
    "scroll_step_max_pct": 75,
    # Search-Scrapping: how many 2-random-letter searches to run per account,
    # harvesting each one's suggestions. Lives here so it rides the same ⚙️
    # Settings dialog as the scrape's other knobs.
    "search_rounds": 100,
    # "Both" technique batching: after collecting a random number of new
    # usernames in [min, max] with one technique, switch to the other
    # (scroll <-> search). A fresh random size is drawn for every batch.
    "scrape_batch_min": 10,
    "scrape_batch_max": 20,
    # …and after each batch, STOP for a random number of seconds in this range
    # before the next one starts, repeating until the run's time is up. 0 = no
    # pause (batches run back to back).
    "scrape_batch_delay_min": 3,
    "scrape_batch_delay_max": 8,
}

# How a written message is committed once the composer holds it. `press_enter`
# is the master send-switch used by the Campaign: ON = press Enter after writing
# the text and/or attaching the media, which SENDS it; OFF = leave everything
# sitting in the composer, unsent (the app's original never-send behaviour).
SEND_DEFAULTS: dict[str, Any] = {
    "press_enter": True,
}

# Campaign batching: instead of running an account's whole list in one go, split
# it into random-sized batches and wait a random number of minutes between them,
# so the sending isn't one long uninterrupted burst. Off by default — the whole
# list runs straight through, as before.
BATCH_DEFAULTS: dict[str, Any] = {
    "batch_enabled": False,
    # rows per batch, picked randomly in [min, max] for each batch
    "batch_size_min": 5,
    "batch_size_max": 15,
    # minutes to wait between batches, picked randomly in [min, max]
    "batch_delay_min": 1,
    "batch_delay_max": 3,
    # Campaign actions: interleave one random Actions-panel action every N rows,
    # N a fresh random pick in [min, max] after each action (e.g. 10-20 -> 13, act,
    # then 17, act, …).
    "campaign_action_min": 3,
    "campaign_action_max": 10,
}

DEFAULTS: dict[str, Any] = {
    **TYPING_DEFAULTS, **SCROLL_DEFAULTS, **SEND_DEFAULTS, **BATCH_DEFAULTS,
}

# Settings whose value is a plain on/off, stored and clamped as a bool rather
# than a clamped integer.
BOOL_KEYS = {"remove_enabled", "press_enter", "batch_enabled"}

LIMITS = {
    "speed_ms": (0, 2000),
    "jitter_ms": (0, 2000),
    "remove_every": (1, 100),
    "remove_min": (0, 20),
    "remove_max": (0, 20),
    # up to 5 minutes; the scrape clamps to the same range
    "scroll_duration_s": (60, 300),
    # a pause under ~0.5s outruns the feed's lazy loading and collects nothing
    "scroll_pause_min_ms": (200, 15000),
    "scroll_pause_max_ms": (200, 15000),
    # over 100% of the window would skip past posts without ever seeing them
    "scroll_step_min_pct": (5, 100),
    "scroll_step_max_pct": (5, 100),
    # 1 search up to 1000 — enough for a long harvest, capped so a typo can't
    # queue an all-day run
    "search_rounds": (1, 1000),
    # "Both" batch size: at least 1 username, up to 500 before switching technique
    "scrape_batch_min": (1, 500),
    "scrape_batch_max": (1, 500),
    # pause between "Both" batches: 0 (back-to-back) up to 1 hour, in seconds
    "scrape_batch_delay_min": (0, 3600),
    "scrape_batch_delay_max": (0, 3600),
    # batch size: at least 1 row, up to 500
    "batch_size_min": (1, 500),
    "batch_size_max": (1, 500),
    # inter-batch delay: 0 (back-to-back) up to 4 hours
    "batch_delay_min": (0, 240),
    "batch_delay_max": (0, 240),
    # rows between campaign actions: at least 1, up to 500
    "campaign_action_min": (1, 500),
    "campaign_action_max": (1, 500),
}

# Ranges that must stay ordered, whatever order the panel sent them in.
_PAIRS = (
    ("remove_min", "remove_max"),
    ("scroll_pause_min_ms", "scroll_pause_max_ms"),
    ("scroll_step_min_pct", "scroll_step_max_pct"),
    ("scrape_batch_min", "scrape_batch_max"),
    ("scrape_batch_delay_min", "scrape_batch_delay_max"),
    ("batch_size_min", "batch_size_max"),
    ("batch_delay_min", "batch_delay_max"),
    ("campaign_action_min", "campaign_action_max"),
)


def _order(cfg: dict) -> dict:
    for low, high in _PAIRS:
        if low in cfg and high in cfg and cfg[low] > cfg[high]:
            cfg[low], cfg[high] = cfg[high], cfg[low]
    return cfg


def _clamp(key: str, value: Any, fallback: Any) -> Any:
    if key in BOOL_KEYS:
        return bool(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    low, high = LIMITS[key]
    return max(low, min(high, number))


def all_settings() -> dict:
    """Every setting — defaults merged with any stored overrides, clamped."""
    stored = db.get_settings()
    cfg = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    return _order({key: _clamp(key, cfg[key], DEFAULTS[key]) for key in DEFAULTS})


def typing() -> dict:
    """Current typing settings only — this feeds typing.plan_keystrokes."""
    cfg = all_settings()
    return {key: cfg[key] for key in TYPING_DEFAULTS}


def scroll() -> dict:
    """Current feed-scrolling settings only — this feeds Visit insta."""
    cfg = all_settings()
    return {key: cfg[key] for key in SCROLL_DEFAULTS}


def send() -> dict:
    """Current send settings only — feeds the Campaign's compose/send step."""
    cfg = all_settings()
    return {key: cfg[key] for key in SEND_DEFAULTS}


def press_enter() -> bool:
    """Whether the Campaign presses Enter (sends) after composing a message."""
    return bool(all_settings()["press_enter"])


def batch() -> dict:
    """Current campaign batching settings only — feeds the batched send loop."""
    cfg = all_settings()
    return {key: cfg[key] for key in BATCH_DEFAULTS}


def update(changes: dict) -> dict:
    known = {k: v for k, v in changes.items() if k in DEFAULTS}
    if not known:
        raise ValueError(f"Nothing to update. Known keys: {', '.join(DEFAULTS)}")
    # Merge over ALL settings, not just the group being edited — save_settings
    # replaces the whole blob, so saving one group alone would wipe the other.
    merged = {**all_settings(), **known}
    cleaned = _order({key: _clamp(key, merged[key], DEFAULTS[key]) for key in DEFAULTS})
    db.save_settings(cleaned)
    return cleaned


def reset() -> dict:
    db.save_settings(dict(DEFAULTS))
    return dict(DEFAULTS)


def describe() -> dict:
    return {"settings": all_settings(), "defaults": DEFAULTS, "limits": LIMITS}
