"""
AIOps 智能运维接口
"""

import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse
from loguru import logger

from app.agent.mcp_client import get_mcp_client_with_retry
from app.agent.aiops.tool_runtime import evaluate_tool_policy
from app.models.aiops import AIOpsRequest, RemediationExecuteRequest
from app.services.aiops_service import aiops_service

router = APIRouter()


@router.post("/aiops")
async def diagnose_stream(request: AIOpsRequest):
    """
    AIOps 故障诊断接口（流式 SSE）

    **功能说明：**
    - 自动获取当前系统的活动告警
    - 使用 Plan-Execute-Replan 模式进行智能诊断
    - 流式返回诊断过程和结果

    **SSE 事件类型：**

    1. `status` - 状态更新
       ```json
       {
         "type": "status",
         "stage": "fetching_alerts",
         "message": "正在获取系统告警信息..."
       }
       ```

    2. `plan` - 诊断计划制定完成
       ```json
       {
         "type": "plan",
         "stage": "plan_created",
         "message": "诊断计划已制定，共 6 个步骤",
         "target_alert": {...},
         "plan": ["步骤1: ...", "步骤2: ..."]
       }
       ```

    3. `step_complete` - 步骤执行完成
       ```json
       {
         "type": "step_complete",
         "stage": "step_executed",
         "message": "步骤执行完成 (2/6)",
         "current_step": "查询系统日志",
         "result_preview": "...",
         "remaining_steps": 4
       }
       ```

    4. `report` - 最终诊断报告
       ```json
       {
         "type": "report",
         "stage": "final_report",
         "message": "最终诊断报告已生成",
         "report": "# 故障诊断报告\\n...",
         "evidence": {...}
       }
       ```

    5. `complete` - 诊断完成
       ```json
       {
         "type": "complete",
         "stage": "diagnosis_complete",
         "message": "诊断流程完成",
         "diagnosis": {...}
       }
       ```

    6. `error` - 错误信息
       ```json
       {
         "type": "error",
         "stage": "error",
         "message": "诊断过程发生错误: ..."
       }
       ```

    **使用示例：**
    ```bash
    curl -X POST "http://localhost:9900/api/aiops" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123"}' \\
      --no-buffer
    ```

    **前端使用示例：**
    ```javascript
    const eventSource = new EventSource('/api/aiops');

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'plan') {
        console.log('诊断计划:', data.plan);
      } else if (data.type === 'step_complete') {
        console.log('步骤完成:', data.current_step);
      } else if (data.type === 'report') {
        console.log('最终报告:', data.report);
      } else if (data.type === 'complete') {
        console.log('诊断完成');
        eventSource.close();
      }
    };
    ```

    Args:
        request: AIOps 诊断请求

    Returns:
        SSE 事件流
    """
    # 1. 用户发送POST请求到 / api / aiops
    # 2.  FastAPI路由器匹配到diagnose_stream函数
    # 3. 函数获取session_id 并记录日志
    # 4. 创建event_generator生成器
    # 5.调用 aiops_service.diagnose() 开始诊断
    # 6.逐步产生事件（plan、step_complete、report 等）
    # 7.每个事件通过yield 发送给前端
    # 8. 收到complete 或error事件后结束
    # 9. 返回EventSourceResponse给前端
    # 10. 前端通过 EventSource接收流式数据
    session_id = request.session_id or "default"
    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")

    async def event_generator():
        try:
            async for event in aiops_service.diagnose(request=request, session_id=session_id):
                # 发送事件
                yield {
                    "event": "message",#event是从服务层获取的事件数据（字典格式）
                    "data": json.dumps(event, ensure_ascii=False)
                }

                # 如果是完成或错误事件，结束流
                if event.get("type") in ["complete", "error"]:
                    break

            logger.info(f"[会话 {session_id}] AIOps 诊断流式响应完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] AIOps 诊断流式响应异常: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "stage": "exception",
                    "message": f"诊断异常: {str(e)}"
                }, ensure_ascii=False)
            }

            #返回这个响应对象给 FastAPI，FastAPI 会把它转换成 HTTP 响应发送给前端
    return EventSourceResponse(event_generator())#把生成器包装成 SSE 响应对象


@router.post("/aiops/remediation/execute")
async def execute_remediation(request: RemediationExecuteRequest):
    """执行用户确认后的半自动修复动作。

    这里不直接操作宿主机，只通过 remediation MCP 服务执行白名单动作。
    """
    if not request.approved:
        raise HTTPException(status_code=400, detail="修复动作未获得用户确认，禁止执行")

    try:
        # 人工修复入口也复用 Agent 工具策略，保证自动链路和人工链路的风险判断一致。
        policy_decision = evaluate_tool_policy(
            "execute_approved_remediation",
            request.model_dump(),
            execution_mode="manual_remediation",
        )
        if not policy_decision.allowed:
            raise HTTPException(status_code=400, detail=policy_decision.reason)

        mcp_client = await get_mcp_client_with_retry()
        tools = await mcp_client.get_tools()
        execute_tool = next(
            (tool for tool in tools if getattr(tool, "name", "") == "execute_approved_remediation"),
            None,
        )
        if execute_tool is None:
            raise HTTPException(status_code=503, detail="remediation MCP 工具不可用")

        result = await execute_tool.ainvoke(request.model_dump())
        return {
            "code": 200,
            "message": "success",
            "data": {
                "type": "remediation_executed",
                "result": result,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"执行修复动作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"执行修复动作失败: {e}") from e
