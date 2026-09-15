import sqlite3
import json
import operator
from typing import TypedDict, Annotated

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command


class State(TypedDict):
    task_id: str
    requirement: str
    current_agent: str
    completed_agents: Annotated[list[str], operator.add]
    messages: Annotated[list[dict], operator.add]
    requirement_analysis: dict
    test_strategy: dict
    test_cases: Annotated[list[dict], operator.add]
    environment: dict
    execution_result: dict
    human_decision: str | None
    defects: Annotated[list[dict], operator.add]
    metadata: dict


def requirement_agent(state: State):
    print("🤖 requirement_agent")
    return {
        "current_agent": "requirement_agent",
        "completed_agents": ["requirement_agent"],
        "messages": [{"agent": "requirement_agent", "content": "需求分析完成"}],
        "requirement_analysis": {
            "feature": "5G SRS",
            "modules": ["RRC", "MAC", "PHY"],
            "risk": "HIGH"
        }
    }


def design_agent(state: State):
    print("🤖 design_agent")
    return {
        "current_agent": "design_agent",
        "completed_agents": ["design_agent"],
        "messages": [{"agent": "design_agent", "content": "测试设计完成"}],
        "test_strategy": {
            "coverage": 0.9,
            "dimensions": ["normal", "boundary", "exception"]
        },
        "test_cases": [
            {"id": "TC001", "name": "单用户SRS", "priority": "P0"},
            {"id": "TC002", "name": "多用户SRS", "priority": "P1"}
        ]
    }


def environment_agent(state: State):
    print("🤖 environment_agent")
    return {
        "current_agent": "environment_agent",
        "completed_agents": ["environment_agent"],
        "messages": [{"agent": "environment_agent", "content": "环境准备完成"}],
        "environment": {
            "cluster": "5G-Test-Cluster",
            "gnb": "10.0.0.10",
            "ue_count": 2
        }
    }


def execution_agent(state: State):
    print("🤖 execution_agent")
    return {
        "current_agent": "execution_agent",
        "completed_agents": ["execution_agent"],
        "messages": [{"agent": "execution_agent", "content": "测试执行完成"}],
        "execution_result": {
            "total": 2,
            "passed": 1,
            "failed": 1,
            "failed_case": "TC002",
            "error": "SRS timeout"
        }
    }


def diagnosis_agent(state: State):
    print("🤖 diagnosis_agent")
    decision = interrupt({
        "message": "发现 TC002 执行失败",
        "error": state["execution_result"]["error"],
        "options": ["CREATE_DEFECT", "IGNORE", "RETRY"]
    })
    print(f"👤 Human Decision: {decision}")
    defects = []
    if decision == "CREATE_DEFECT":
        defects.append({
            "id": "BUG-001",
            "severity": "HIGH",
            "description": "SRS timeout"
        })
    return {
        "current_agent": "diagnosis_agent",
        "completed_agents": ["diagnosis_agent"],
        "human_decision": decision,
        "defects": defects,
        "messages": [{
            "agent": "diagnosis_agent",
            "content": f"人工决策: {decision}"
        }]
    }


def final_agent(state: State):
    print("🤖 final_agent")
    return {
        "current_agent": "final_agent",
        "completed_agents": ["final_agent"],
        "messages": [{"agent": "final_agent", "content": "Workflow 完成"}]
    }


def build_graph(checkpointer):
    builder = StateGraph(State)
    builder.add_node("requirement", requirement_agent)
    builder.add_node("design", design_agent)
    builder.add_node("environment", environment_agent)
    builder.add_node("execution", execution_agent)
    builder.add_node("diagnosis", diagnosis_agent)
    builder.add_node("final", final_agent)
    builder.add_edge(START, "requirement")
    builder.add_edge("requirement", "design")
    builder.add_edge("design", "environment")
    builder.add_edge("environment", "execution")
    builder.add_edge("execution", "diagnosis")
    builder.add_edge("diagnosis", "final")
    builder.add_edge("final", END)
    return builder.compile(checkpointer=checkpointer)


def inspect_db(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    tables = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    for (table,) in tables:
        print(f"\n{'=' * 60}\nTABLE: {table}")
        columns = cursor.execute(f"PRAGMA table_info({table})").fetchall()
        print("Columns:", [col[1] for col in columns])
        rows = cursor.execute(f"SELECT * FROM {table}").fetchall()
        print(f"Rows: {len(rows)}")
        for i, row in enumerate(rows):
            print(f"ROW {i}:")
            for value in row:
                print(f"  {value if not isinstance(value, bytes) else f'<BLOB {len(value)} bytes>'}")
    conn.close()


def main():
    db_path = "langgraph.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    graph = build_graph(SqliteSaver(conn))
    config = {"configurable": {"thread_id": "task-001"}}
    initial_state = {
        "task_id": "TASK-001",
        "requirement": "测试5G SRS测量优化",
        "current_agent": "system",
        "completed_agents": [],
        "messages": [],
        "requirement_analysis": {},
        "test_strategy": {},
        "test_cases": [],
        "environment": {},
        "execution_result": {},
        "human_decision": None,
        "defects": [],
        "metadata": {"project": "AI4Test", "version": "1.0"}
    }
    print("========== 第一次执行 ==========")
    result = graph.invoke(initial_state, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    inspect_db(db_path)
    print("\n========== Resume ==========")
    result = graph.invoke(Command(resume="CREATE_DEFECT"), config)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    inspect_db(db_path)
    conn.close()


if __name__ == "__main__":
    main()