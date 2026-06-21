"""
Replanner 节点：重新规划或生成最终响应
基于 LangGraph 官方教程实现
"""

import json
from textwrap import dedent
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from loguru import logger

from app.core.llm_factory import llm_factory
from app.tools import get_current_time, retrieve_knowledge
from app.agent.mcp_client import get_mcp_client_with_retry
from .evidence_gate import check_evidence_sufficiency
from .state import PlanExecuteState
from .utils import format_tools_description


class Response(BaseModel):
    """最终响应的格式"""
    response: str = Field(description="对用户的最终响应")


class Act(BaseModel):
    """重新规划的输出格式"""
    action: str = Field(
        description="""下一步的行动，必须是以下三种之一：
        - 'continue': 当前计划合理，继续执行下一个步骤
        - 'replan': 当前计划需要调整，提供新的步骤列表
        - 'respond': 计划已完成且信息充足，生成最终响应"""
    )
    # action 为 'replan' 时，新的步骤列表（会替换当前剩余计划）
    new_steps: List[str] = Field(
        default_factory=list,
        description="新的步骤列表（如果 action 是 'replan'，这些步骤会替换剩余计划）"
    )
    reason: str = Field(
        default="",
        description="做出该决策的简短原因，用于展示给用户理解 Agent 为什么继续、重排或生成报告"
    )


# Replanner 提示词
replanner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个重新规划专家，你需要根据已执行的步骤决定下一步行动。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定或调整计划，实际的工具调用由 Executor 负责执行。

                你有三个选择（按优先级排序）：

                **1. 'respond' - 信息充足，生成最终响应**
                   - 使用场景：当前信息已经足够回答用户问题，并且核心证据已经收集完成
                   - 决策标准：
                     * AIOps 告警必须至少完成指标查询、日志查询、服务健康检查这三类核心证据中的前两类
                     * 如果剩余步骤里还明确包含 query_metric_range、query_cpu_metrics、query_service_logs、find_error_patterns、get_service_health，则不要 respond
                     * 或者当前信息完全满足任务需求，且剩余步骤只是在写最终报告
                   - 不要过早结束，避免报告缺失关键证据

                **2. 'continue' - 当前计划合理，继续执行** 【优先级最高】
                   - 使用场景：剩余计划合理且必要                                                                                                            
                   - 决策标准：剩余步骤仍包含指标、日志、健康检查、修复建议等关键动作
                   - 如果不确定是否信息足够，优先 continue

                **3. 'replan' - 当前计划有严重问题** 【最低优先级，谨慎使用】
                   - 使用场景：原计划明显错误或遗漏关键步骤
                   - ⚠️ **严格限制**：
                     * 新步骤数量必须 <= 当前剩余步骤数
                     * 优先简化计划，不要添加不必要的步骤
                     * 总步骤数已执行 >= 5 次时，禁止 replan，只能 respond

                评估标准：
                - 当前信息是否已经足够解决用户问题？【最关键】
                - 已执行步骤是否成功获取了核心信息？
                - 剩余步骤是否真的"必需"？
                - 已执行步骤数是否过多（>= 5）？如果是，立即 respond

                **决策优先级口诀：** 
                "证据不足先继续 > 信息足够再响应 > 严重错误才调整计划"
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)

# 最终响应生成提示词
response_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                根据原始任务和已执行步骤的结果，生成一个全面的最终响应。

                响应要求：
                - 清晰、结构化
                - 基于实际数据，不要编造
                 - 如果某些步骤失败，要诚实说明
                 - 使用 Markdown 格式
                 - 所有根因判断必须基于已执行步骤中的证据，不得编造未查询到的数据
                 - 必须区分“已证实”“未发现”“不能排除”
                 - “未发现错误日志”只能说明没有 error/exception/timeout/failed 证据，不能写成“排除应用层问题”
                 - 必须固定包含以下小节：
                  1. 告警摘要
                  2. 证据链
                  3. 根因判断
                  4. 置信度
                  5. 修复方案
                  6. 待人工确认动作
                  7. 回滚建议
                - 修复动作只能提出建议，不能声称已经执行
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


async def replanner(state: PlanExecuteState) -> Dict[str, Any]:
    """
    重新规划节点：决定是继续、调整计划还是生成最终响应

    三种决策：
    1. continue - 继续执行当前计划
    2. replan - 调整计划（替换剩余步骤）
    3. respond - 生成最终响应
    """
    logger.info("=== Replanner：重新规划 ===")

    input_text = state.get("input", "")
    plan = state.get("plan", [])
    past_steps = state.get("past_steps", [])

    logger.info(f"剩余计划步骤: {len(plan)}")
    logger.info(f"已执行步骤: {len(past_steps)}")

    # ⚠️ 强制限制：如果已执行步骤过多，直接生成响应
    MAX_STEPS = 8
    if len(past_steps) >= MAX_STEPS:
        logger.warning(f"已执行 {len(past_steps)} 个步骤，超过最大限制 {MAX_STEPS}，强制生成最终响应")
        llm = llm_factory.create_chat_model(temperature=0, streaming=False)
        result = await _generate_response(state, llm)
        result["replan_events"] = [
            _build_replan_event(
                action="respond",
                reason=f"已执行 {len(past_steps)} 个步骤，达到最大步骤限制，生成最终报告。",
                plan=plan,
                past_steps=past_steps,
            )
        ]
        return result

    # 获取可用工具列表
    try:
        # 获取本地工具
        local_tools = [
            get_current_time,
            retrieve_knowledge
        ]

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()

        # 合并所有工具
        all_tools = local_tools + mcp_tools
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 格式化工具描述
        tools_description = format_tools_description(all_tools)
    except Exception as e:
        logger.warning(f"获取工具列表失败: {e}")
        tools_description = "无法获取工具列表"

    # 创建 LLM
    llm = llm_factory.create_chat_model(temperature=0, streaming=False)

    # 格式化已执行的步骤
    steps_summary = "\n".join([
        f"步骤: {step}\n结果: {result[:300]}..."
        for step, result in past_steps
    ])

    # 如果还有剩余计划，进行决策
    if plan:
        logger.info("还有剩余计划，评估下一步行动")

        replanner_chain = replanner_prompt | llm.with_structured_output(Act)

        try:
            messages = [
                ("user", f"原始任务: {input_text}"),
                ("user", f"已执行的步骤:\n{steps_summary}"),
                ("user", f"剩余计划: {', '.join(plan)}"),
                ("user", f"⚠️ 重要提示：已执行 {len(past_steps)} 个步骤。如果剩余计划仍包含指标、日志、健康检查或修复建议，请优先 continue；只有核心证据足够且剩余步骤只是报告整理时才 respond。")
            ]

            act = await _generate_act_with_fallback(
                llm=llm,
                messages=messages,
                tools_description=tools_description,
            )

            # 处理返回结果
            if isinstance(act, Act):
                action = act.action
                new_steps = act.new_steps
                reason = act.reason
            else:
                # 如果返回的是字典
                action = act.get("action", "continue")  # type: ignore
                new_steps = act.get("new_steps", [])  # type: ignore
                reason = act.get("reason", "")  # type: ignore

            logger.info(f"Replanner 决策: {action}")

            evidence_gate = check_evidence_sufficiency(state)
            if action == "respond" and (not evidence_gate.passed) and plan:
                logger.info(f"Evidence Gate 未通过，覆盖 respond 为 continue: {evidence_gate.reason}")
                return {
                    "replan_events": [
                        _build_replan_event(
                            action="continue",
                            reason=evidence_gate.reason,
                            plan=plan,
                            past_steps=past_steps,
                        )
                    ]
                }

            if action == "respond" and _has_required_evidence_step(plan):
                logger.info("剩余计划仍包含核心证据步骤，覆盖 respond 为 continue")
                return {
                    "replan_events": [
                        _build_replan_event(
                            action="continue",
                            reason="剩余计划仍包含指标、日志、健康检查或修复建议等核心步骤，继续执行以补齐证据。",
                            plan=plan,
                            past_steps=past_steps,
                        )
                    ]
                }

            if action == "respond":
                logger.info("决定生成最终响应")
                result = await _generate_response(state, llm)
                result["replan_events"] = [
                    _build_replan_event(
                        action="respond",
                        reason=reason or "当前证据已足够生成最终报告。",
                        plan=plan,
                        past_steps=past_steps,
                    )
                ]
                return result

            elif action == "replan":
                # ⚠️ 强制限制：新步骤数不能超过当前剩余步骤数
                if len(new_steps) > len(plan):
                    logger.warning(
                        f"新步骤数 {len(new_steps)} > 剩余步骤数 {len(plan)}，"
                        f"强制截断为 {len(plan)} 个步骤"
                    )
                    new_steps = new_steps[:len(plan)]
                
                # ⚠️ 二次检查：如果已执行步骤 >= 5，禁止 replan
                if len(past_steps) >= 5:
                    logger.warning(f"已执行 {len(past_steps)} 个步骤，禁止重新规划，强制生成响应")
                    result = await _generate_response(state, llm)
                    result["replan_events"] = [
                        _build_replan_event(
                            action="respond",
                            reason=f"已执行 {len(past_steps)} 个步骤，禁止继续重新规划，生成最终报告。",
                            plan=plan,
                            past_steps=past_steps,
                        )
                    ]
                    return result
                
                logger.info(f"决定调整计划，新步骤数量: {len(new_steps)}")
                if new_steps:
                    # 替换剩余计划
                    return {
                        "plan": new_steps,
                        "replan_events": [
                            _build_replan_event(
                                action="replan",
                                reason=reason or "当前剩余计划需要调整。",
                                plan=new_steps,
                                past_steps=past_steps,
                                new_steps=new_steps,
                            )
                        ],
                    }
                else:
                    logger.warning("replan 但未提供新步骤，继续执行原计划")
                    return {
                        "replan_events": [
                            _build_replan_event(
                                action="continue",
                                reason="模型选择重新规划但未给出新步骤，因此继续原计划。",
                                plan=plan,
                                past_steps=past_steps,
                            )
                        ]
                    }

            else:  # action == "continue"
                logger.info("决定继续执行当前计划")
                return {
                    "replan_events": [
                        _build_replan_event(
                            action="continue",
                            reason=reason or "剩余步骤仍有必要，继续执行当前计划。",
                            plan=plan,
                            past_steps=past_steps,
                        )
                    ]
                }

        except Exception as e:
            logger.error(f"重新规划失败: {e}, 继续执行剩余计划")
            return {
                "replan_events": [
                    _build_replan_event(
                        action="continue",
                        reason=f"重新规划失败，保守继续执行原计划: {e}",
                        plan=plan,
                        past_steps=past_steps,
                    )
                ]
            }

    else:
        # 没有剩余计划，生成最终响应
        logger.info("计划已执行完毕，生成最终响应")
        evidence_gate = check_evidence_sufficiency(state)
        if not evidence_gate.passed:
            logger.warning(f"计划结束但 Evidence Gate 未通过: {evidence_gate.reason}")
        result = await _generate_response(state, llm)
        result["replan_events"] = [
            _build_replan_event(
                action="respond",
                reason=(
                    "计划已执行完毕，生成最终报告。"
                    if evidence_gate.passed
                    else f"计划已执行完毕但证据不足，生成低置信度报告：{evidence_gate.reason}"
                ),
                plan=plan,
                past_steps=past_steps,
            )
        ]
        return result


async def _generate_response(state: PlanExecuteState, llm: Any) -> Dict[str, Any]:
    """生成最终响应"""
    logger.info("生成最终响应...")

    input_text = state.get("input", "")
    past_steps = state.get("past_steps", [])

    # 格式化执行历史
    execution_history = "\n\n".join([
        f"### 步骤: {step}\n**结果:**\n{result}"
        for step, result in past_steps
    ])

    try:
        messages = [
            ("user", f"原始任务: {input_text}"),
            ("user", f"执行历史:\n{execution_history}"),
            ("user", "请基于以上信息生成全面的最终响应")
        ]

        response_obj = await _generate_response_with_fallback(llm, messages)

        # 处理返回结果
        if isinstance(response_obj, Response):
            final_response = response_obj.response
        else:
            # 如果返回的是字典
            final_response = response_obj.get("response", "")  # type: ignore

        logger.info(f"最终响应生成完成，长度: {len(final_response)}")

        return {"response": final_response}

    except Exception as e:
        logger.error(f"生成响应失败: {e}")
        # 生成简单的后备响应
        fallback_response = f"""# 任务执行结果

## 原始任务
{input_text}

## 执行的步骤
{_format_simple_steps(past_steps)}

## 说明
由于系统异常，无法生成完整响应。以上是已收集的信息。
"""
        return {"response": fallback_response}


async def _generate_act_with_fallback(
    llm: Any,
    messages: list,
    tools_description: str,
) -> Act:
    """生成 Replanner 决策，兼容不稳定的 structured output。"""
    try:
        replanner_chain = replanner_prompt | llm.with_structured_output(Act)
        return await replanner_chain.ainvoke({
            "messages": messages,
            "tools_description": tools_description,
        })
    except Exception as structured_error:
        logger.warning(f"结构化 Replanner 输出失败，改用 JSON 兜底解析: {structured_error}")

    plain_messages = [
        SystemMessage(content=dedent(f"""
            你是 AIOps Replanner，请输出严格 JSON，不要输出 Markdown。
            JSON 格式必须是：
            {{"action": "continue|replan|respond", "new_steps": [], "reason": "原因"}}

            可用工具：
            {tools_description}

            决策规则：
            - 如果剩余步骤还有指标、日志、健康检查、修复建议，优先 continue。
            - 只有证据充足时才 respond。
            - replan 时 new_steps 数量不要超过当前剩余步骤。
        """).strip())
    ]
    plain_messages.extend(HumanMessage(content=item[1]) for item in messages)
    response = await llm.ainvoke(plain_messages)
    content = getattr(response, "content", str(response))
    return Act.model_validate(_parse_json_object(content))


async def _generate_response_with_fallback(llm: Any, messages: list) -> Response:
    """生成最终报告，兼容不稳定的 structured output。"""
    try:
        response_gen = response_prompt | llm.with_structured_output(Response)
        return await response_gen.ainvoke({"messages": messages})
    except Exception as structured_error:
        logger.warning(f"结构化最终报告输出失败，改用普通文本兜底: {structured_error}")

    plain_messages = [
        SystemMessage(content=dedent("""
            你是 AIOps Incident Copilot，请直接输出 Markdown 诊断报告。
            报告必须包含：告警摘要、证据链、根因判断、置信度、修复方案、待人工确认动作、回滚建议。
            所有结论都必须绑定执行历史中的工具证据。
            必须区分“已证实”“未发现”“不能排除”。
            “未发现错误日志”不能写成“排除应用层问题”，只能写成“未发现 error/exception/timeout/failed 日志证据”。
            不要声称已经执行修复，只能提出待确认动作。
        """).strip())
    ]
    plain_messages.extend(HumanMessage(content=item[1]) for item in messages)
    response = await llm.ainvoke(plain_messages)
    content = getattr(response, "content", str(response))
    return Response(response=str(content))


def _parse_json_object(content: str) -> Dict[str, Any]:
    """从模型文本中解析 JSON 对象，兼容带代码块的输出。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start:end + 1]
    return json.loads(text)


def _format_simple_steps(past_steps: list) -> str:
    """格式化步骤列表（简单版）"""
    if not past_steps:
        return "无"

    formatted = []
    for i, (step, result) in enumerate(past_steps, 1):
        result_preview = result[:200] + "..." if len(result) > 200 else result
        formatted.append(f"{i}. **{step}**\n   {result_preview}\n")

    return "\n".join(formatted)


def _build_replan_event(
    action: str,
    reason: str,
    plan: list,
    past_steps: list,
    new_steps: list | None = None,
) -> Dict[str, Any]:
    """构造 Replanner 决策事件，给前端解释 Agent 的下一步判断。"""
    return {
        "action": action,
        "reason": reason,
        "remaining_steps": len(plan),
        "executed_steps": len(past_steps),
        "new_steps": new_steps or [],
    }


def _has_required_evidence_step(plan: list) -> bool:
    """判断剩余计划里是否还有必须执行的核心证据步骤。"""
    required_keywords = (
        "query_metric_range",
        "query_metric_summary",
        "query_cpu_metrics",
        "query_memory_metrics",
        "query_service_logs",
        "find_error_patterns",
        "find_fault_signals",
        "get_service_health",
        "propose_remediation",
    )
    return any(
        keyword in str(step)
        for step in plan
        for keyword in required_keywords
    )
