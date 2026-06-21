"""AIOps Evaluation Harness API。"""

from fastapi import APIRouter

from app.agent.aiops.evaluation import list_builtin_eval_cases, run_builtin_eval_suite

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
