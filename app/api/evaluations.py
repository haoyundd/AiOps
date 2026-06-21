"""AIOps Evaluation Harness API。"""

from fastapi import APIRouter, HTTPException

from app.agent.aiops.evaluation import (
    evaluate_agent_run,
    list_builtin_eval_cases,
    run_builtin_eval_suite,
)
from app.services.incident_service import incident_service

router = APIRouter()


@router.get("/evaluations/cases")
async def list_evaluation_cases():
    """列出内置回放评测场景。"""
    return {
        "code": 200,
        "message": "success",
        "data": list_builtin_eval_cases(),
    }


@router.post("/evaluations/replay")
async def replay_builtin_evaluations():
    """执行内置回放评测，不调用大模型、不修改 incident。"""
    return {
        "code": 200,
        "message": "success",
        "data": run_builtin_eval_suite(),
    }


@router.post("/evaluations/agent-runs/{run_id}")
async def evaluate_persisted_agent_run(run_id: str):
    """对一次真实 AgentRun 做确定性质量审计，并保存审计结果。"""
    repository = incident_service.repository
    run = repository.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="agent run 不存在")
    if run.get("status") != "completed":
        raise HTTPException(status_code=400, detail="AgentRun 尚未完成，不能执行最终质量审计")
    if not str(run.get("final_report") or "").strip():
        raise HTTPException(status_code=400, detail="AgentRun 缺少最终报告，不能执行报告安全审计")

    timeline = repository.list_timeline_for_run(run_id)
    result = evaluate_agent_run(run, timeline).model_dump()
    evaluation = repository.save_run_evaluation(run_id, result)
    repository.append_timeline(
        incident_id=run["incident_id"],
        event_type="evaluation_completed",
        message="真实 AgentRun 质量审计完成",
        payload={
            "evaluation_id": evaluation["id"],
            "passed": evaluation["passed"],
            "score": evaluation["score"],
        },
        run_id=run_id,
    )

    return {
        "code": 200,
        "message": "success",
        "data": {
            "run": run,
            "evaluation": evaluation,
        },
    }
