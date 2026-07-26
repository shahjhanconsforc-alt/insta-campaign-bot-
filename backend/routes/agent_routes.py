from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import db
from backend.agent import executor, planner, registry

router = APIRouter(prefix="/api/agent", tags=["agent"])


class RunBody(BaseModel):
    goal: str = ""


@router.get("/tools")
async def tools():
    return {"tools": registry.list_tools(), "categories": registry.categories()}


@router.post("/plan")
async def plan(body: RunBody):
    return await planner.create_plan(body.goal)


@router.post("/run")
async def run(body: RunBody):
    if not body.goal.strip():
        raise HTTPException(400, "goal is required")
    created = await planner.create_plan(body.goal)
    result = await executor.execute_plan(created)
    return {"plan": created, "run": result}


@router.get("/runs")
async def runs(limit: int = 25):
    return {"runs": db.get_runs(limit=limit)}
