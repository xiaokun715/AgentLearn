"""/v1/guardrails/* API（设计说明书 §30~§32）。

三个核心 Check：Input / Tool / Output，另补 Context / ToolResult / Approvals / Audit。
Guardrails 实例挂在 ``app.state.guardrails``（见 app/main.py）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..service import Guardrails
from .schemas import (
    ApprovalDecisionRequest,
    ContextCheckRequest,
    InputCheckRequest,
    OutputCheckRequest,
    ToolCheckRequest,
    ToolResultCheckRequest,
)

router = APIRouter(prefix="/v1/guardrails", tags=["guardrails"])


def get_guardrails(request: Request) -> Guardrails:
    return request.app.state.guardrails


# ---- Input Guardrail（§30） -------------------------------------------------
@router.post("/input")
async def check_input(
    body: InputCheckRequest,
    g: Guardrails = Depends(get_guardrails),
) -> dict:
    result = await g.check_input(
        body.content,
        agent=body.agent,
        user_id=body.user_id,
        metadata=body.metadata,
    )
    return result.to_dict()


@router.post("/context")
async def check_context(
    body: ContextCheckRequest,
    g: Guardrails = Depends(get_guardrails),
) -> dict:
    result = await g.check_context(
        body.content,
        agent=body.agent,
        user_id=body.user_id,
        metadata=body.metadata,
    )
    return result.to_dict()


# ---- Tool Check（§31） ------------------------------------------------------
@router.post("/tool")
async def check_tool(
    body: ToolCheckRequest,
    g: Guardrails = Depends(get_guardrails),
) -> dict:
    result = await g.check_tool(
        body.agent, body.tool, body.arguments, user_id=body.user_id,
    )
    return result.to_dict()


# ---- Tool Result Guardrail（§22 / Demo 6）-----------------------------------
@router.post("/tool_result")
async def check_tool_result(
    body: ToolResultCheckRequest,
    g: Guardrails = Depends(get_guardrails),
) -> dict:
    result = await g.check_tool_result(
        body.content,
        tool_name=body.tool_name,
        agent=body.agent,
        user_id=body.user_id,
        metadata=body.metadata,
    )
    return result.to_dict()


# ---- Output Guardrail（§32 / Demo 3, 8）-------------------------------------
@router.post("/output")
async def check_output(
    body: OutputCheckRequest,
    g: Guardrails = Depends(get_guardrails),
) -> dict:
    result = await g.check_output(
        body.content,
        agent=body.agent,
        user_id=body.user_id,
        schema=body.json_schema,
        metadata=body.metadata,
    )
    return result.to_dict()


# ---- Human Approval（§28）--------------------------------------------------
@router.get("/approvals")
async def list_approvals(
    status: str | None = Query(default=None, pattern="^(PENDING|APPROVED|REJECTED|EXPIRED)$"),
    g: Guardrails = Depends(get_guardrails),
) -> list[dict]:
    return g.list_approvals(status)


@router.post("/approvals/{approval_id}/approve")
async def approve(
    approval_id: str,
    body: ApprovalDecisionRequest,
    g: Guardrails = Depends(get_guardrails),
) -> dict:
    return g.decide_approval(
        approval_id, approved=True, decided_by=body.decided_by, note=body.note
    )


@router.post("/approvals/{approval_id}/reject")
async def reject(
    approval_id: str,
    body: ApprovalDecisionRequest,
    g: Guardrails = Depends(get_guardrails),
) -> dict:
    return g.decide_approval(
        approval_id, approved=False, decided_by=body.decided_by, note=body.note
    )


# ---- Audit / Registry 只读（§29、§5）---------------------------------------
@router.get("/events")
async def audit_events(
    stage: str | None = None,
    action: str | None = None,
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    g: Guardrails = Depends(get_guardrails),
) -> list[dict]:
    return g.list_audit_events(limit=limit, stage=stage, action=action, category=category)


@router.get("/tools")
async def list_tools(g: Guardrails = Depends(get_guardrails)) -> dict:
    return {"tools": g.list_tools()}


@router.get("/policy")
async def policy_report(g: Guardrails = Depends(get_guardrails)) -> dict:
    return g.policy_report()


__all__ = ["router"]
