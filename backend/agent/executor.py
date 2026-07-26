"""Executor — runs a plan's steps in order and records the run."""
import inspect
import time

from backend import activity, db
from backend.agent import registry


async def execute_plan(plan: dict) -> dict:
    results = []
    started = time.time()

    for index, step in enumerate(plan.get("steps", [])):
        tool = registry.get(step["tool"])
        if tool is None:
            results.append(
                {"step": index, "tool": step["tool"], "ok": False, "error": "unknown tool"}
            )
            continue

        step_started = time.time()
        try:
            args = _coerce_args(tool, step.get("args", {}))
            activity.log("agent", f"step {index + 1}: {tool.name}")
            output = await tool.handler(**args)
            results.append(
                {
                    "step": index,
                    "tool": tool.id,
                    "name": tool.name,
                    "ok": True,
                    "args": args,
                    "output": output,
                    "ms": round((time.time() - step_started) * 1000),
                }
            )
        except Exception as exc:  # noqa: BLE001 — surface per-step errors, keep going
            activity.log("agent", f"step {index + 1} {tool.name} failed: {exc}", "error")
            results.append(
                {
                    "step": index,
                    "tool": tool.id,
                    "name": tool.name,
                    "ok": False,
                    "args": step.get("args", {}),
                    "error": f"{type(exc).__name__}: {exc}",
                    "ms": round((time.time() - step_started) * 1000),
                }
            )

    run = {
        "goal": plan.get("goal"),
        "strategy": plan.get("strategy"),
        "steps": len(results),
        "ok": all(r["ok"] for r in results) if results else False,
        "ms": round((time.time() - started) * 1000),
        "results": results,
    }
    db.save_run(run)
    activity.log(
        "agent",
        f"run finished: {run['steps']} step(s) in {run['ms']}ms",
        "ok" if run["ok"] else "warn",
    )
    return run


def _coerce_args(tool, args: dict) -> dict:
    """Keep only kwargs the handler actually accepts — a plan may carry extras."""
    if not isinstance(args, dict):
        return {}
    accepted = inspect.signature(tool.handler).parameters
    return {k: v for k, v in args.items() if k in accepted}
