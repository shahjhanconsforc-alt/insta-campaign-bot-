import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend import activity

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
async def index(since: int = 0, limit: int = 300):
    return {"events": activity.history(since, limit), **activity.stats()}


@router.post("/clear")
async def clear():
    return {"cleared": activity.clear()}


@router.get("/stream")
async def stream():
    """Server-sent events. EventSource reconnects on its own, so a dropped
    connection heals without the panel doing anything."""

    async def events():
        queue = activity.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keep proxies from closing an idle stream
        finally:
            activity.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
