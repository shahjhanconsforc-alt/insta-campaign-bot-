"""Tool registry — lookup and catalogue over backend.agent.tools."""
from backend.agent.tools import TOOLS, Tool

_BY_ID: dict[str, Tool] = {tool.id: tool for tool in TOOLS}


def get(tool_id: str) -> Tool | None:
    return _BY_ID.get(tool_id)


def require(tool_id: str) -> Tool:
    tool = _BY_ID.get(tool_id)
    if tool is None:
        raise KeyError(f"Unknown tool '{tool_id}'. Known: {', '.join(_BY_ID)}")
    return tool


def list_tools() -> list[dict]:
    return [tool.as_dict() for tool in TOOLS]


def categories() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for tool in TOOLS:
        grouped.setdefault(tool.category, []).append(tool.as_dict())
    return grouped
