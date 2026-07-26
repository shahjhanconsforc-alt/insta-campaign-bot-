"""Planner — turns a natural-language goal into an ordered list of tool steps.

Keyword routing: the goal's words are matched against the tools they imply. This
used to ask a local LLM to pick the tools and fall back to keywords when the model
was unreachable; the model is gone, so what was the fallback is now the whole
planner — deterministic, instant, and offline by construction.

A goal that matches nothing yields an empty plan rather than a guess, so the run
reports "nothing to do" instead of firing an unrelated tool.
"""
import re

from backend.agent import registry


async def create_plan(goal: str) -> dict:
    steps = _keyword_plan(goal)
    return {
        "goal": goal,
        "strategy": "keyword",
        "steps": steps,
        **({} if steps else {"note": "No tool keywords matched this goal."}),
    }


def _keyword_plan(goal: str) -> list[dict]:
    text = goal.lower()
    url_match = re.search(r"https?://\S+", goal)
    steps: list[dict] = []

    def add(tool_id: str, args: dict, why: str) -> None:
        tool = registry.require(tool_id)
        steps.append({"tool": tool.id, "name": tool.name, "args": args, "why": why})

    if any(word in text for word in ("open", "browser", "chrome", "navigate", "visit")):
        add(
            "browser.open",
            {"url": url_match.group(0)} if url_match else {},
            "Goal mentions opening a browser.",
        )
    if any(word in text for word in ("screenshot", "capture", "shot")):
        add("browser.screenshot", {}, "Goal mentions a screenshot.")
    if any(word in text for word in ("save", "store", "record", "remember")):
        add("db.save", {"title": goal[:60], "body": goal}, "Goal mentions saving.")
    if any(word in text for word in ("list", "show", "history", "records")):
        add("db.query", {"limit": 20}, "Goal mentions listing records.")

    return steps[:4]
