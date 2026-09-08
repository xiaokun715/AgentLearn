"""观测 API 路由 —— 让你透过 HTTP 驱动/检查记忆系统的每一层与每个原语。

对齐姊妹项目惯例：路由不引 runtime 全局，而是从 ``request.app.state.runtime`` 取(``_rt``)，
这样 FastAPI 单测可以注入不同 runtime、examples 也可以直接 import 复用。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..factory import Runtime
from ..records import MemoryType
from .schemas import (
    ConsolidateIn,
    DecayRunOnceIn,
    MemoryCreate,
    RecallIn,
    SessionCommitIn,
    SessionObserveIn,
    SupersedeIn,
    TouchIn,
)

router = APIRouter(prefix="/v1", tags=["memory"])


def _rt(request: Request) -> Runtime:
    return request.app.state.runtime  # type: ignore[no-any-return]


def _rec(ctx) -> dict:
    return ctx.to_dict()


def _require(request: Request, memory_id: str) -> dict:
    rec = _rt(request).engine.get(memory_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"memory_id {memory_id!r} 不存在")
    return rec.to_dict()


# ---------------------------------------------------------------------------
# 记忆 CRUD + 8 原语的可观测端点
# ---------------------------------------------------------------------------
@router.post("/memories", summary="Insert：写入一条新记忆")
def create_memory(body: MemoryCreate, request: Request) -> dict:
    rec = _rt(request).engine.remember(
        user_id=body.user_id,
        subject=body.subject,
        predicate=body.predicate,
        object_value=body.object_value,
        memory_type=body.memory_type,  # type: ignore[arg-type]
        confidence=body.confidence,
    )
    return _rec(rec)


@router.get("/memories", summary="列出记忆(可按 user/type/状态过滤)")
def list_memories(
    request: Request,
    user_id: Optional[str] = None,
    memory_type: Optional[MemoryType] = None,
    active_only: bool = True,
) -> list[dict]:
    pool = _rt(request).engine.active_memories(user_id) if active_only else [
        r for r in _rt(request).engine.storage.values()
        if user_id is None or r.user_id == user_id
    ]
    if memory_type is not None:
        pool = [r for r in pool if r.memory_type == memory_type]
    return [r.to_dict() for r in pool]


@router.get("/memories/{memory_id}", summary="查一条记忆")
def get_memory(memory_id: str, request: Request) -> dict:
    return _require(request, memory_id)


@router.post("/memories/{memory_id}/supersede", summary="Update/Supersede：SCD2 换代")
def supersede(memory_id: str, body: SupersedeIn, request: Request) -> dict:
    rt = _rt(request)
    old = rt.engine.get(memory_id)
    if old is None:
        raise HTTPException(status_code=404, detail=f"memory_id {memory_id!r} 不存在")
    new_rec = rt.engine.update_supersede(
        old_mem_id=memory_id,
        new_value=body.new_value,
        confidence=body.confidence,
    )
    return {
        "archived": _rec(old),
        "new": _rec(new_rec),
        "lineage": [r.to_dict() for r in rt.engine.history(old.user_id, old.subject)],
    }


@router.delete("/memories/{memory_id}", summary="Delete/Evict：软删或硬删")
def delete_memory(memory_id: str, request: Request, hard: bool = False) -> dict:
    rt = _rt(request)
    if rt.engine.get(memory_id) is None:
        raise HTTPException(status_code=404, detail=f"memory_id {memory_id!r} 不存在")
    rt.engine.delete(memory_id, hard_delete=hard)
    return {"memory_id": memory_id, "deleted": True, "hard": hard}


@router.post("/memories/{memory_id}/touch", summary="Touch/Reinforce：召回后强化/证伪降权")
def touch(memory_id: str, body: TouchIn, request: Request) -> dict:
    rt = _rt(request)
    if rt.engine.get(memory_id) is None:
        raise HTTPException(status_code=404, detail=f"memory_id {memory_id!r} 不存在")
    rt.engine.touch_reinforce(memory_id, task_succeeded=body.succeeded)
    return _rec(rt.engine.get(memory_id))


@router.post("/memories/{memory_id}/wake", summary="Wake/Reactivate：唤醒沉睡冷记忆")
def wake(memory_id: str, request: Request) -> dict:
    rt = _rt(request)
    rec = rt.engine.wake_reactivate(memory_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"memory_id {memory_id!r} 不存在")
    return _rec(rec)


# ---------------------------------------------------------------------------
# 检索 / 合并
# ---------------------------------------------------------------------------
@router.post("/recall", summary="Retrieve/Recall：混合语义检索(只搜活跃)")
def recall(body: RecallIn, request: Request) -> dict:
    rt = _rt(request)
    q_vec = rt.engine.embedder.embed(body.query)
    hits = rt.engine.retrieve(
        user_id=body.user_id,
        query_embedding=q_vec,
        top_k=body.top_k or rt.config.recall_top_k,
        sim_threshold=(
            body.sim_threshold
            if body.sim_threshold is not None
            else rt.config.sim_threshold
        ),
    )
    return {
        "query": body.query,
        "hits": [
            {**r.to_dict(),
             "rank_score": round(score, 4),
             "sim_note": "rank = 0.6*sim + 0.2*confidence + 0.2*ebbinghaus"}
            for r, score in hits
        ],
    }


@router.post("/recall/sleeping", summary="深潜检索：连沉睡冷记忆一起捞出来(供 wake)")
def recall_sleeping(body: RecallIn, request: Request) -> dict:
    rt = _rt(request)
    q_vec = rt.engine.embedder.embed(body.query)
    hits = rt.engine.deep_retrieve(
        user_id=body.user_id,
        query_embedding=q_vec,
        top_k=body.top_k or rt.config.recall_top_k,
    )
    sleeping = [(r, s) for r, s in hits if not r.is_active]
    return {
        "query": body.query,
        "sleeping_hits": [
            {**r.to_dict(), "rank_score": round(s, 4)} for r, s in sleeping
        ],
        "hint": "对 memory_id 调 POST /v1/memories/{id}/wake 即可唤醒复活",
    }


@router.post("/consolidate", summary="Consolidate/Merge：碎片合并升阶为 SOP")
def consolidate(body: ConsolidateIn, request: Request) -> dict:
    rt = _rt(request)
    try:
        rec = rt.engine.consolidate(
            memory_ids=body.memory_ids,
            generalized_value=body.generalized_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "sop": _rec(rec),
        "note": "碎片已软下线，产出 predicate=consolidated_sop 的 SEMANTIC 记忆",
    }


# ---------------------------------------------------------------------------
# 后台 worker 手动触发
# ---------------------------------------------------------------------------
@router.post("/workers/decay-run-once", summary="Decay/Forget：手动跑一轮遗忘代谢")
def decay_run_once(body: DecayRunOnceIn, request: Request) -> dict:
    rt = _rt(request)
    if body.simulate_idle_days > 0:
        # 快进整个世界(教学演示：不必真等 30 天)
        rt.engine.simulate_elapsed(body.simulate_idle_days)
    slept = rt.decay.run_once(
        half_life_days=body.half_life_days,
        retention_floor=body.retention_floor,
    )
    return {
        "sweep": rt.decay.sweeps,
        "slept_count": len(slept),
        "slept": [r.to_dict() for r in slept],
        "stats": rt.engine.stats(),
    }


# ---------------------------------------------------------------------------
# 会话闭环演示 + 分层快照
# ---------------------------------------------------------------------------
@router.post("/sessions/observe", summary="热注水：看一次请求把哪些记忆装进工作记忆")
def session_observe(body: SessionObserveIn, request: Request) -> dict:
    return _rt(request).system.observe(body.user_id, body.query, body.system_prompt)


@router.post("/sessions/commit", summary="任务终态：把轨迹蒸馏沉淀为记忆")
def session_commit(body: SessionCommitIn, request: Request) -> dict:
    saved = _rt(request).system.commit_session(
        user_id=body.user_id,
        goal=body.goal,
        steps=[s.model_dump() for s in body.steps],
        outcome=body.outcome,
        also_reflect=body.also_reflect,
    )
    return {"saved": [r.to_dict() for r in saved]}


@router.post("/sessions/run-canned", summary="一键跑通：热注水→蒸馏→命中 三段式剧本")
def session_run_canned(request: Request) -> list[dict]:
    return _rt(request).system.run_canned_session()


@router.get("/tiers/{user_id}", summary="四层记忆总览(Working/ShortTerm/Episodic/Semantic)")
def tiers(user_id: str, request: Request) -> dict:
    return _rt(request).system.tiers_snapshot(user_id)


@router.get("/system/stats", summary="记忆库统计")
def stats(request: Request) -> dict:
    return _rt(request).engine.stats()


@router.get("/config", summary="当前参数")
def get_config(request: Request) -> dict:
    return _rt(request).config.as_dict()
