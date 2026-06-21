"""
Executor 节点：执行单个步骤
基于 LangGraph 官方教程实现
"""

import time
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import ToolNode
from loguru import logger

from app.core.llm_factory import llm_factory
from app.tools import get_current_time, retrieve_knowledge
from app.agent.mcp_client import get_mcp_client_with_retry
from .state import PlanExecuteState
from .tool_runtime import execute_planned_tool_step, precheck_tool_calls


async def executor(state: PlanExecuteState) -> Dict[str, Any]:
    """
    执行节点：执行计划中的下一个步骤
    
    使用 LangGraph 的 ToolNode 自动处理工具调用
    """
    logger.info("=== Executor：执行步骤 ===")

    plan = state.get("plan", [])
    input_text = state.get("input", "")

    # 如果计划为空，不执行
    if not plan:
        logger.info("计划为空，跳过执行")
        return {}

    # 取出第一个步骤
    task = plan[0]
    logger.info(f"当前任务: {task}")
    started_at = time.perf_counter()

    try:
        # 获取本地工具
        local_tools = [
            get_current_time,
            retrieve_knowledge
        ]

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 合并所有工具
        all_tools = local_tools + mcp_tools

        # Tool Runtime 优先执行 Planner 写明的工具调用。
        # 这一步让固定 Playbook 不再完全依赖模型二次选择工具，减少参数漂移和越权调用。
        runtime_result = await execute_planned_tool_step(task, all_tools)
        if runtime_result is not None:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            execution_event = {
                "step": task,
                "status": runtime_result.status,
                "duration_ms": duration_ms,
                "tool_calls": runtime_result.tool_calls,
                "tool_results": runtime_result.tool_results,
                "tool_runtime": {
                    "mode": "deterministic",
                    "policy_decision": (
                        runtime_result.policy_decision.to_dict()
                        if runtime_result.policy_decision
                        else None
                    ),
                },
                "result_preview": runtime_result.result_text[:800],
                "result_chars": len(runtime_result.result_text),
            }
            return {
                "plan": plan[1:],
                "past_steps": [(task, runtime_result.result_text)],
                "execution_events": [execution_event],
            }

        # 创建 LLM（绑定工具），模型由统一工厂管理，支持运行时切换。
        llm = llm_factory.create_chat_model(temperature=0, streaming=False)
        #：把工具的名称、参数、描述注入到 LLM 的 prompt 里。LLM看到这些信息后，知道有哪些工具可以用、参数怎么填。
        llm_with_tools = llm.bind_tools(all_tools)

        # 创建工具节点（自动执行工具调用）
        tool_node = ToolNode(all_tools)

        # 构建消息（只包含当前步骤，避免原始任务干扰）
        messages = [
            SystemMessage(content="""你是一个能力强大的助手，负责执行具体的任务步骤。

你可以使用各种工具来完成任务。对于每个步骤：
1. 理解步骤的目标
2. 选择合适的工具，如果已经指定了工具，则使用指定的工具
3. 调用工具获取信息
4. 返回执行结果

注意：
- 如果工具调用失败，请说明失败原因
- 不要编造数据，只返回实际获取的信息
- 工具参数必须来自“原始告警上下文”，禁止把“指定服务”“某指标”这类占位词当成真实参数
- 如果当前步骤缺少参数，请回到原始告警上下文中提取 service_name、metric_name、starts_at 等字段
- 执行结果要清晰、准确
- 专注于当前步骤，不要考虑其他任务"""),
            HumanMessage(content=f"原始告警上下文:\n{input_text}"),
            HumanMessage(content=f"请执行以下任务: {task}")#取出来的第一个任务
        ]
     #executor 不是调一次 LLM，而是调了两轮：
        # 两轮调用的原因：第一轮做决策（选工具），第二轮做翻译（数据 →人话）。
        # 第一步：LLM 决定是否调用工具
        llm_response = await llm_with_tools.ainvoke(messages)
        logger.info(f"LLM 响应类型: {type(llm_response)}")

        # 第二步：如果有工具调用，执行工具
        tool_calls = getattr(llm_response, "tool_calls", []) or []
        tool_results: List[Dict[str, Any]] = []
        if tool_calls:
            logger.info(f"检测到 {len(tool_calls)} 个工具调用")

            # 模型自主选择工具时也要先过策略预检，高风险修复和占位参数会被阻断。
            blocked_runtime_result = precheck_tool_calls(tool_calls)
            if blocked_runtime_result is not None:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                execution_event = {
                    "step": task,
                    "status": blocked_runtime_result.status,
                    "duration_ms": duration_ms,
                    "tool_calls": blocked_runtime_result.tool_calls,
                    "tool_results": blocked_runtime_result.tool_results,
                    "tool_runtime": {
                        "mode": "llm_precheck",
                        "policy_decision": (
                            blocked_runtime_result.policy_decision.to_dict()
                            if blocked_runtime_result.policy_decision
                            else None
                        ),
                    },
                    "result_preview": blocked_runtime_result.result_text[:800],
                    "result_chars": len(blocked_runtime_result.result_text),
                }
                return {
                    "plan": plan[1:],
                    "past_steps": [(task, blocked_runtime_result.result_text)],
                    "execution_events": [execution_event],
                }
            
            # 使用 ToolNode 自动执行工具
            messages.append(llm_response)
            #  ToolNode 就是个"工具执行器"——它收到 LLM 的 tool_calls 指令，真的去调函数，把结果包装成 ToolMessage 返回。
            tool_messages = await tool_node.ainvoke({"messages": messages})
            tool_results = _summarize_tool_messages(tool_messages.get("messages", []))
            
            # 第三步：将工具结果返回给 LLM 生成最终答案
            messages.extend(tool_messages["messages"])
            #第二次调用模型，这次有工具调用结果
            final_response = await llm_with_tools.ainvoke(messages)
            result = final_response.content if hasattr(final_response, 'content') else str(final_response)
        else:
            # 没有工具调用，直接使用 LLM 的输出
            logger.info("LLM 未调用工具，直接返回结果")
            result = llm_response.content if hasattr(llm_response, 'content') else str(llm_response)

        logger.info(f"步骤执行完成，结果长度: {len(result)}")
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        execution_event = {
            "step": task,
            "status": "success",
            "duration_ms": duration_ms,
            "tool_calls": _summarize_tool_calls(tool_calls),
            "tool_results": tool_results,
            "result_preview": result[:800],
            "result_chars": len(result),
        }

        # 返回更新：移除已执行的步骤，添加执行历史
        return {
            "plan": plan[1:],  # 移除第一个步骤
            "past_steps": [(task, result)],  # 使用 operator.add 追加
            "execution_events": [execution_event],
        }

    except Exception as e:
        logger.error(f"执行步骤失败: {e}", exc_info=True)
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "plan": plan[1:],
            "past_steps": [(task, f"执行失败: {str(e)}")],
            "execution_events": [
                {
                    "step": task,
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "tool_calls": [],
                    "tool_results": [],
                    "result_preview": f"执行失败: {str(e)}",
                    "result_chars": len(str(e)),
                    "error": str(e),
                }
            ],
        }


def _summarize_tool_calls(tool_calls: List[Any]) -> List[Dict[str, Any]]:
    """把 LLM 产生的工具调用指令压缩成前端可展示的结构。"""
    summaries = []
    for call in tool_calls:
        if isinstance(call, dict):
            summaries.append(
                {
                    "name": call.get("name") or call.get("function", {}).get("name") or "unknown_tool",
                    "args": call.get("args") or call.get("arguments") or {},
                    "id": call.get("id"),
                }
            )
        else:
            summaries.append(
                {
                    "name": getattr(call, "name", "unknown_tool"),
                    "args": getattr(call, "args", {}),
                    "id": getattr(call, "id", None),
                }
            )
    return summaries


def _summarize_tool_messages(tool_messages: List[Any]) -> List[Dict[str, Any]]:
    """把真实工具返回结果压缩成摘要，避免 incident 里塞入过长日志。"""
    summaries = []
    for message in tool_messages:
        content = getattr(message, "content", str(message))
        summaries.append(
            {
                "name": getattr(message, "name", "tool_result") or "tool_result",
                "tool_call_id": getattr(message, "tool_call_id", None),
                "content_preview": str(content)[:1000],
                "content_chars": len(str(content)),
            }
        )
    return summaries
