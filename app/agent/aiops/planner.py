"""
Planner 节点：制定执行计划
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
from .state import PlanExecuteState
from .utils import format_tools_description


class Plan(BaseModel):
    """计划的输出格式"""
    steps: List[str] = Field(
        description="完成任务所需的不同步骤。这些步骤应该按顺序执行，每一步都建立在前一步的基础上。"
    )


# Planner 提示词
planner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个专家级别的规划者，你需要将复杂的任务分解为可执行的步骤。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定计划，实际的工具调用由 Executor 负责执行。

                {experience_context}

                对于给定的任务，请创建一个简单的、逐步的计划来完成它。计划应该：
                - 将任务分解为逻辑上独立的步骤
                - 每个步骤应该明确使用哪些工具(如果需要工具的话)来获取信息, 最好能同时提供工具执行所需要的参数
                 - 步骤之间应该有清晰的依赖关系
                 - 步骤描述要具体、可操作
                 - **如果有相关经验文档，请参考其中的方法和步骤制定计划**
                 - **不要使用“指定服务”“某指标”这类占位词，必须从用户输入中提取真实字段**
                 - 如果输入中包含服务名称、指标名称、开始时间，计划步骤里必须写出这些真实参数
                 - AIOps 场景优先使用：query_metric_range/query_cpu_metrics、query_service_logs/find_error_patterns、get_service_health、retrieve_knowledge

                示例输入："分析当前系统的性能问题"
                示例输出（假设有对应工具）：
                步骤1: 使用 get_metrics 工具收集系统的 CPU 和内存使用情况
                步骤2: 使用 query_logs 工具检查最近的错误日志
                步骤3: 使用 query_database 工具分析慢查询日志
                步骤4: 综合以上信息生成性能分析报告
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


async def planner(state: PlanExecuteState) -> Dict[str, Any]:
    """
    规划节点：根据用户输入生成执行计划

    流程：
    1. 先查询内部文档，获取相关经验和最佳实践
    2. 基于经验文档和可用工具制定执行计划
    """
    logger.info("=== Planner：制定执行计划 ===")

    input_text = state.get("input", "")#state就是PlanExecuteState，input就是用户输入，写死的那个
    logger.info(f"用户输入: {input_text}")

    try:
        # 步骤1: 查询内部文档获取相关经验
        logger.info("查询内部文档，寻找相关经验...")
        experience_docs = ""
        try:
            # retrieve_knowledge 使用 response_format="content_and_artifact"
            # ainvoke() 只返回 content（字符串），不是元组
            context_str = await retrieve_knowledge.ainvoke({"query": input_text})
            #retrieve_knowledge被@tool 装饰器包了一层，它已经不是普通函数了，是一个 LangChainTool 对象。
            if context_str and context_str.strip():
                experience_docs = context_str
                logger.info(f"找到相关经验文档，长度: {len(experience_docs)}")
            else:
                logger.info("未找到相关经验文档")
        except Exception as e:
            logger.warning(f"查询内部文档失败: {e}")

        # 步骤2: 获取可用工具列表
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

        # 步骤3: 格式化经验文档上下文
        if experience_docs:
            experience_context = dedent(f"""
                ## 相关经验文档

                以下是从知识库中检索到的相关经验和最佳实践，请参考这些经验制定执行计划：

                {experience_docs}

                ---
            """).strip()
        else:
            experience_context = ""

        # 步骤4: 创建 LLM 并生成计划，模型由统一工厂管理，支持运行时切换。
        llm = llm_factory.create_chat_model(temperature=0, streaming=False)

        # 调用 LLM 生成计划。不同 OpenAI-compatible 供应商对 structured output 的支持不完全一致，
        # 因此先尝试 LangChain 结构化输出，失败后退回普通 JSON 解析。
        plan_result = await _generate_plan_with_fallback(
            llm=llm,
            input_text=input_text,
            tools_description=tools_description,
            experience_context=experience_context,
        )

        # 提取步骤列表
        if isinstance(plan_result, Plan):
            plan_steps = plan_result.steps
        else:
            # 如果返回的是字典，提取 steps 字段
            plan_steps = plan_result.get("steps", [])  # type: ignore

        logger.info(f"计划已生成，共 {len(plan_steps)} 个步骤")
        for i, step in enumerate(plan_steps, 1):
            logger.info(f"  步骤{i}: {step}")

        # 记录 Planner 过程，让前端能解释“计划是怎么来的”。
        planner_trace = {
            "input_preview": input_text[:500],
            "knowledge_hit": bool(experience_docs),
            "knowledge_chars": len(experience_docs),
            "knowledge_preview": experience_docs[:500] if experience_docs else "",
            "local_tool_count": len(local_tools),
            "mcp_tool_count": len(mcp_tools),
            "tool_names": [getattr(tool, "name", str(tool)) for tool in all_tools],
            "plan_steps": plan_steps,
        }

        return {"plan": plan_steps, "planner_trace": planner_trace}

    except Exception as e:
        logger.error(f"生成计划失败: {e}", exc_info=True)
        # 返回一个默认计划
        fallback_steps = [
                "收集相关信息",
                "分析数据",
                "生成报告"
            ]
        return {
            "plan": fallback_steps,
            "planner_trace": {
                "input_preview": input_text[:500],
                "knowledge_hit": False,
                "knowledge_chars": 0,
                "knowledge_preview": "",
                "local_tool_count": 0,
                "mcp_tool_count": 0,
                "tool_names": [],
                "plan_steps": fallback_steps,
                "error": str(e),
            }
        }


async def _generate_plan_with_fallback(
    llm: Any,
    input_text: str,
    tools_description: str,
    experience_context: str,
) -> Plan:
    """生成计划，兼容不稳定的 OpenAI-compatible structured output。"""
    try:
        planner_chain = planner_prompt | llm.with_structured_output(Plan)
        return await planner_chain.ainvoke({
            "messages": [("user", input_text)],
            "tools_description": tools_description,
            "experience_context": experience_context,
        })
    except Exception as structured_error:
        logger.warning(f"结构化计划输出失败，改用 JSON 兜底解析: {structured_error}")

    messages = [
        SystemMessage(content=dedent(f"""
            你是 AIOps 诊断规划器，请输出严格 JSON，不要输出 Markdown。
            JSON 格式必须是：{{"steps": ["步骤1", "步骤2"]}}

            可用工具：
            {tools_description}

            相关经验：
            {experience_context or "无"}

            要求：
            - 至少包含查询 Prometheus 指标、查询 Loki 日志、检查服务健康、提出修复建议。
            - 步骤里必须使用真实 service_name、metric_name、threshold，禁止使用占位词。
            - 只输出 JSON 对象。
        """).strip()),
        HumanMessage(content=input_text),
    ]
    response = await llm.ainvoke(messages)
    content = getattr(response, "content", str(response))
    data = _parse_json_object(content)
    return Plan.model_validate(data)


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
