"""Tool definitions. A Tool couples metadata (shown to the planner and the UI)
with the async handler the executor calls."""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend import db
from backend.automation import instagram as ig
from backend.automation import playwright_runner as browser


@dataclass
class Tool:
    id: str
    name: str
    description: str
    category: str
    args_schema: dict[str, str]
    handler: Callable[..., Awaitable[Any]] = field(repr=False)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "args": self.args_schema,
        }


# ─── Handlers ────────────────────────────────────────────────────────────


async def _db_save(title: str = "untitled", body: str = "", type: str = "note") -> dict:
    return db.insert({"title": title, "body": body, "type": type})


async def _db_query(limit: int = 20) -> dict:
    return {"records": db.get_all(limit=limit), "count": db.count()}


async def _browser_open(url: str | None = None) -> dict:
    return await browser.open_browser(url)


async def _browser_goto(url: str) -> dict:
    return await browser.goto(url)


async def _browser_screenshot() -> dict:
    return await browser.screenshot()


async def _browser_close() -> dict:
    return await browser.close_browser()


async def _ig_login(username: str | None = None, password: str | None = None) -> dict:
    page = await browser.require_page()
    return await ig.login(page, username, password)


async def _ig_status() -> dict:
    page = await browser.require_page()
    return await ig.status(page)


# ─── Registry contents ───────────────────────────────────────────────────

TOOLS: list[Tool] = [
    Tool(
        id="db.save",
        name="DB Save",
        description="Persist a record to the JSON store.",
        category="data",
        args_schema={"title": "string", "body": "string", "type": "string?"},
        handler=_db_save,
    ),
    Tool(
        id="db.query",
        name="DB Query",
        description="Read the most recent records from the store.",
        category="data",
        args_schema={"limit": "int?"},
        handler=_db_query,
    ),
    Tool(
        id="browser.open",
        name="Open Chrome",
        description="Launch a visible Chrome window via Playwright.",
        category="automation",
        args_schema={"url": "string?"},
        handler=_browser_open,
    ),
    Tool(
        id="browser.goto",
        name="Navigate",
        description="Navigate the open Chrome window to a URL.",
        category="automation",
        args_schema={"url": "string"},
        handler=_browser_goto,
    ),
    Tool(
        id="browser.screenshot",
        name="Screenshot",
        description="Capture the current Chrome page as a PNG.",
        category="automation",
        args_schema={},
        handler=_browser_screenshot,
    ),
    Tool(
        id="browser.close",
        name="Close Chrome",
        description="Close the Playwright Chrome window.",
        category="automation",
        args_schema={},
        handler=_browser_close,
    ),
    Tool(
        id="instagram.status",
        name="Instagram Status",
        description="Check whether the saved Chrome profile is still logged into Instagram.",
        category="instagram",
        args_schema={},
        handler=_ig_status,
    ),
    Tool(
        id="instagram.login",
        name="Instagram Login",
        description="Log into Instagram if the saved session has expired. No-op when already logged in.",
        category="instagram",
        args_schema={"username": "string?", "password": "string?"},
        handler=_ig_login,
    ),
]
