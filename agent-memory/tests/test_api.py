"""观测 API 端到端：记忆 CRUD + 8 原语 + worker + 会话闭环(经 TestClient)。"""
from __future__ import annotations


def _post(client, path: str, body: dict | None = None):
    return client.post(path, json=body or {})


# ---------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------
def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_seed_stats_present(client):
    stats = client.get("/v1/system/stats").json()
    assert stats["total"] == 5
    assert stats["by_type"]["EPISODIC"] == 4


# ---------------------------------------------------------------------
# Memory CRUD + 原语端点
# ---------------------------------------------------------------------
def test_create_and_get_memory(client):
    created = _post(client, "/v1/memories", {
        "user_id": "alice", "subject": "告警X", "predicate": "原因",
        "object_value": "查 MME 话统的上行干扰指标", "memory_type": "EPISODIC",
    }).json()
    assert created["memory_id"].startswith("mem_")
    assert created["is_active"] is True and created["version"] == 1
    got = client.get(f"/v1/memories/{created['memory_id']}").json()
    assert got["subject"] == "告警X"
    assert client.get("/v1/memories/mem_ghost").status_code == 404


def test_list_filter_memory_type(client):
    items = client.get("/v1/memories", params={"user_id": "alice",
                                               "memory_type": "EPISODIC"}).json()
    assert len(items) == 4
    assert all(r["memory_type"] == "EPISODIC" for r in items)


def test_supersede_returns_archived_and_new(client):
    rec = _post(client, "/v1/memories", {
        "user_id": "alice", "subject": "邻区优化", "predicate": "方案",
        "object_value": "第一代方案: 手动补邻区", "memory_type": "EPISODIC",
    }).json()
    out = _post(client, f"/v1/memories/{rec['memory_id']}/supersede",
                {"new_value": "第二代方案: 基于测量报告自动补邻区"}).json()
    assert out["archived"]["is_active"] is False
    assert out["new"]["version"] == 2 and out["new"]["is_active"] is True
    assert [r["version"] for r in out["lineage"]] == [1, 2]


def test_touch_wake_and_delete(client):
    rec = _post(client, "/v1/memories", {
        "user_id": "alice", "subject": "低频经验", "predicate": "经验",
        "object_value": "一条临时经验", "memory_type": "EPISODIC",
    }).json()
    mid = rec["memory_id"]
    touched = _post(client, f"/v1/memories/{mid}/touch", {"succeeded": True}).json()
    assert touched["access_count"] == 1 and touched["confidence"] == 1.0

    client.delete(f"/v1/memories/{mid}")            # 软删
    assert client.get(f"/v1/memories/{mid}").json()["is_active"] is False
    woken = _post(client, f"/v1/memories/{mid}/wake").json()
    assert woken["is_active"] is True and woken["confidence"] == 0.6

    client.delete(f"/v1/memories/{mid}", params={"hard": "true"})
    assert client.get(f"/v1/memories/{mid}").status_code == 404


# ---------------------------------------------------------------------
# Recall / Consolidate
# ---------------------------------------------------------------------
def test_recall_returns_seeded_experience(client):
    out = _post(client, "/v1/recall", {
        "user_id": "alice", "query": "另一个 5G 基站闪断怎么处理", "top_k": 3,
    }).json()
    assert out["hits"] and out["hits"][0]["subject"] == "5G 基站闪断"
    assert "rank_score" in out["hits"][0]


def test_consolidate_promotes_sop(client):
    ids = []
    for text in ["退服计数高", "切换失败率飙升", "邻区漏配"]:
        ids.append(_post(client, "/v1/memories", {
            "user_id": "alice", "subject": "链路拥塞", "predicate": "经验",
            "object_value": text, "memory_type": "EPISODIC",
        }).json()["memory_id"])
    out = _post(client, "/v1/consolidate", {
        "memory_ids": ids, "generalized_value": "链路拥塞三步处理法",
    }).json()
    assert out["sop"]["predicate"] == "consolidated_sop"
    assert out["sop"]["memory_type"] == "SEMANTIC"


def test_consolidate_bad_request_400(client):
    resp = _post(client, "/v1/consolidate", {"memory_ids": ["mem_nope"],
                                             "generalized_value": "x"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------
# Decay worker (观测 API 手动触发)
# ---------------------------------------------------------------------
def test_decay_run_once_simulates_and_sleeps(client):
    out = _post(client, "/v1/workers/decay-run-once",
                {"simulate_idle_days": 200}).json()
    assert out["slept_count"] > 0
    assert out["stats"]["sleeping"] > 0
    assert all(r["is_active"] is False for r in out["slept"])


def test_recall_sleeping_after_decay(client):
    _post(client, "/v1/workers/decay-run-once", {"simulate_idle_days": 300})
    out = _post(client, "/v1/recall/sleeping",
                {"user_id": "alice", "query": "5G 基站闪断 排查", "top_k": 10}).json()
    assert out["sleeping_hits"]  # 深潜能捞到刚沉睡的记忆
    assert all(not h["is_active"] for h in out["sleeping_hits"])


# ---------------------------------------------------------------------
# 会话闭环 + 分层
# ---------------------------------------------------------------------
def test_session_observe_hydrates(client):
    out = _post(client, "/v1/sessions/observe", {
        "user_id": "alice", "query": "排查 NR 切换失败怎么处理",
        "system_prompt": "你是网优工程师",
    }).json()
    assert "你是网优工程师" in out["context"]
    assert out["recalled"] and out["recalled"][0]["subject"] == "NR 切换失败"


def test_session_commit_distills(client):
    out = _post(client, "/v1/sessions/commit", {
        "user_id": "alice", "goal": "排查 5G 基站闪断",
        "steps": [{"action": "查退服计数", "ok": True},
                  {"action": "看切换失败率", "ok": True}],
        "outcome": True,
    }).json()
    saved = out["saved"]
    assert saved and saved[0]["memory_type"] == "EPISODIC"


def test_run_canned_session_returns_three_stages(client):
    stages = _post(client, "/v1/sessions/run-canned").json()
    assert [s["title"] for s in stages] == [
        "新任务到达 · 热注水", "任务终态 · 蒸馏沉淀", "下一次同类请求 · 命中新经验",
    ]
    assert stages[1]["data"]["saved"]  # 有蒸馏落库


def test_tiers_and_config(client):
    tiers = client.get("/v1/tiers/alice").json()
    assert "engine_stats" in tiers and "working" in tiers
    cfg = client.get("/v1/config").json()
    assert cfg["sim_threshold"] == 0.3
