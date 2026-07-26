from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import settings
from backend.automation import typing

router = APIRouter(prefix="/api/settings", tags=["settings"])


class TypingBody(BaseModel):
    speed_ms: int | None = None
    jitter_ms: int | None = None
    remove_enabled: bool | None = None
    remove_every: int | None = None
    remove_min: int | None = None
    remove_max: int | None = None


class ScrollBody(BaseModel):
    scroll_duration_s: int | None = None
    scroll_pause_min_ms: int | None = None
    scroll_pause_max_ms: int | None = None
    scroll_step_min_pct: int | None = None
    scroll_step_max_pct: int | None = None
    # Search-Scrapping rounds — saved from the same ⚙️ dialog as the scroll knobs.
    search_rounds: int | None = None
    # "Both" scraping: batch size range + the pause (seconds) between batches.
    scrape_batch_min: int | None = None
    scrape_batch_max: int | None = None
    scrape_batch_delay_min: int | None = None
    scrape_batch_delay_max: int | None = None


class SendBody(BaseModel):
    # Master send-switch: after the Campaign writes the text / attaches the media
    # it presses Enter (sends) when this is on, and leaves it unsent when off.
    press_enter: bool | None = None


class BatchBody(BaseModel):
    # Campaign batching: run the list in random-sized batches with a random
    # minute delay between them.
    batch_enabled: bool | None = None
    batch_size_min: int | None = None
    batch_size_max: int | None = None
    batch_delay_min: int | None = None
    batch_delay_max: int | None = None
    # Campaign actions: rows between each interleaved random action.
    campaign_action_min: int | None = None
    campaign_action_max: int | None = None


class SettingsBody(TypingBody, ScrollBody, SendBody, BatchBody):
    """The Config panel saves every group in one PUT."""


# Preview is typing-only on purpose: scroll settings have nothing to do with
# planning keystrokes, and would just show up as noise in the returned cfg.
class PreviewBody(TypingBody):
    text: str = "test.user@example.com"


@router.get("")
async def index():
    return settings.describe()


@router.put("")
async def update(body: SettingsBody):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return {"settings": settings.update(changes)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/reset")
async def reset():
    return {"settings": settings.reset()}


@router.post("/preview")
async def preview(body: PreviewBody):
    """Dry-run the typing plan — no browser involved. Uses the values sent in
    (falling back to saved ones), so the panel can preview before saving."""
    overrides = {
        k: v for k, v in body.model_dump().items() if v is not None and k != "text"
    }
    cfg = {**settings.typing(), **overrides}
    events = typing.plan_keystrokes(body.text, cfg)
    return {
        "cfg": cfg,
        **typing.summarize(body.text, events),
        # '«' marks a deletion. Not '⌫' (U+232B): Consolas has no glyph for it,
        # so the trace rendered as tofu boxes in the panel.
        "trace": "".join("«" if e["key"] == typing.BACKSPACE else e["key"] for e in events),
        "trace_legend": "« = backspace",
    }
