"""
通用 Plan-Execute-Replan 服务
基于 LangGraph 官方教程实现
"""

import uuid
from typing import AsyncGenerator, Dict, Any
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from loguru import logger

from app.agent.aiops import PlanExecuteState, planner, executor, replanner
from app.models.aiops import AIOpsRequest


# 节点名称常量
NODE_PLANNER = "planner"
NODE_EXECUTOR = "executor"
NODE_REPLANNER = "replanner"


class AIOpsService:
    """通用 Plan-Execute-Replan 服务"""

    def __init__(self):
        """初始化服务"""
        self.checkpointer = MemorySaver()
        self.graph = self._build_graph()
        logger.info("Plan-Execute-Replan Service 初始化完成")

    def _build_graph(self):
        """构建 Plan-Execute-Replan 工作流"""
        logger.info("构建工作流图...")

        # 创建状态图
        workflow = StateGraph(PlanExecuteState)

        # 添加节点
        # 节点名叫 "planner"，干活的是 planner 函数
        #第一个参数是字符串名字（标签），第二个参数是函数（干活的）
        workflow.add_node(NODE_PLANNER, planner)      # 制定计划
        workflow.add_node(NODE_EXECUTOR, executor)  # 执行步骤
        workflow.add_node(NODE_REPLANNER, replanner)  # 重新规划

        # 设置入口点
        workflow.set_entry_point(NODE_PLANNER)

        # 定义边(死边)固定路线
        workflow.add_edge(NODE_PLANNER, NODE_EXECUTOR)     # planner -> executor
        workflow.add_edge(NODE_EXECUTOR, NODE_REPLANNER)   # executor -> replanner

        # replanner 的条件边(根据状态判断是否继续执行)--红绿灯
        def should_continue(state: PlanExecuteState) -> str:
            """判断是否继续执行"""
            # 如果已经生成了最终响应，结束
            if state.get("response"):
                logger.info("已生成最终响应，结束流程")
                return END

            # 如果还有计划步骤，继续执行
            plan = state.get("plan", [])
            if plan:
                logger.info(f"继续执行，剩余 {len(plan)} 个步骤")
                return NODE_EXECUTOR

            # 计划为空但没有响应，返回 replanner 生成响应
            logger.info("计划执行完毕，生成最终响应")
            return END

        workflow.add_conditional_edges( # 添加replanner 的条件边(根据状态判断是否继续执行)--红绿灯
            NODE_REPLANNER,
            should_continue,#用哪个函数判断是否继续执行
            {
                NODE_EXECUTOR: NODE_EXECUTOR,
                END: END
            }
        )

        # 编译工作流，把checkpointer 绑定进去，用于状态保存
        compiled_graph = workflow.compile(checkpointer=self.checkpointer)

        logger.info("工作流图构建完成")
        return compiled_graph

    async def execute(
        self,
        user_input: str,# 第 21 行：用户任务描述
        session_id: str = "default"
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        执行 Plan-Execute-Replan 流程

        Args:
            user_input: 用户的任务描述
            session_id: 会话ID

        Yields:
            Dict[str, Any]: 流式事件
        """
        logger.info(f"[会话 {session_id}] 开始执行任务: {user_input}")

        try:
            # 初始化状态
            initial_state: PlanExecuteState = {
                "input": user_input,
                "plan": [],
                "past_steps": [],
                "planner_trace": {},
                "execution_events": [],
                "replan_events": [],
                "response": ""
            }

            # 流式执行工作流
            # 每次诊断都使用独立 thread_id，避免同一个 incident 重新触发时复用旧 checkpoint。
            # 否则 LangGraph 会把上一次的 past_steps 带进来，导致步骤数异常膨胀。
            run_thread_id = f"{session_id}:{uuid.uuid4().hex}"
            config_dict = {
                "configurable": {
                    "thread_id": run_thread_id
                }
            }

            async for event in self.graph.astream(
                input=initial_state,#初始状态，塞进入口节点
                config=config_dict,# 配置，主要是 thread_id
                stream_mode="updates"#什么时候往外吐数据 ，这里是每次节点输出事件时都吐
            ): #stream_mode="updates" 模式下，每次吐的 event 结构固定：
        # event 永远是只含一个键值对的字典，event.items() 遍历出来就一个
          # (node_name, node_output)。这是 LangGraph updates
         # 模式的约定——每跑完一个节点，吐那个节点的名字和输出。

                # 解析事件
                for node_name, node_output in event.items():
                    logger.info(f"节点 '{node_name}' 输出事件")

                    # 根据节点类型生成不同的事件
                    if node_name == NODE_PLANNER:
                        yield self._format_planner_event(node_output)

                    elif node_name == NODE_EXECUTOR:
                        yield self._format_executor_event(node_output)

                    elif node_name == NODE_REPLANNER:
                        yield self._format_replanner_event(node_output)

            # 获取最终状态
            final_state = self.graph.get_state(config_dict)
            final_response = ""

            # 安全地获取响应（处理 values 可能为 None 的情况）
            if final_state and final_state.values:
                final_response = final_state.values.get("response", "")

            # 发送完成事件
            yield {
                "type": "complete",
                "stage": "complete",
                "message": "任务执行完成",
                "response": final_response
            }

            logger.info(f"[会话 {session_id}] 任务执行完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] 任务执行失败: {e}", exc_info=True)
            yield {
                "type": "error",
                "stage": "error",
                "message": f"任务执行出错: {str(e)}"
            }

    async def diagnose(
        self,
        request: AIOpsRequest | None = None,
        session_id: str = "default"
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        AIOps 诊断接口。

        优先使用结构化告警生成动态诊断任务；如果没有传入告警，则使用本地 demo-service
        的默认 CPU 告警，保证开发环境可以直接演示。

        Args:
            request: 结构化告警请求
            session_id: 会话ID

        Yields:
            Dict[str, Any]: 诊断过程的流式事件
        """
        if request is None:
            request = AIOpsRequest(session_id=session_id)

        aiops_task = request.to_diagnosis_task()

        async for event in self.execute(aiops_task, session_id):
            # 转换事件格式以兼容旧的 API
            if event.get("type") == "complete":
                # 将 response 包装为 diagnosis 格式
                yield {
                    "type": "complete",
                    "stage": "diagnosis_complete",
                    "message": "诊断流程完成",
                    "diagnosis": {
                        "status": "completed",
                        "report": event.get("response", "")
                    }
                }
            else:
                normalized_event = self._normalize_event_type(event)
                normalized_event["alert"] = request.model_dump()
                yield normalized_event

    def _normalize_event_type(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """把内部事件转换为前端和测试稳定依赖的事件名。"""
        event_type = event.get("type")
        stage = event.get("stage")
        normalized = dict(event)

        if event_type == "plan":
            normalized["type"] = "plan_created"
        elif event_type == "step_complete":
            normalized["type"] = "tool_executed"
        elif event_type == "report" or stage == "final_report":
            normalized["type"] = "diagnosis_report"
        elif event_type == "status" and stage == "replanner":
            normalized["type"] = "evidence_collected"

        return normalized

    def _format_planner_event(self, state: Dict | None) -> Dict:
        """格式化 Planner 节点事件"""
        if not state:
            return {
                "type": "status",
                "stage": "planner",
                "message": "规划节点执行中"
            }

        plan = state.get("plan", [])
        planner_trace = state.get("planner_trace", {})

        return {
            "type": "plan",
            "stage": "plan_created",
            "message": f"执行计划已制定，共 {len(plan)} 个步骤",
            "plan": plan,
            "planner_trace": planner_trace,
        }

    def _format_executor_event(self, state: Dict | None) -> Dict:
        """格式化 Executor 节点事件"""
        if not state:
            return {
                "type": "status",
                "stage": "executor",
                "message": "执行节点运行中"
            }

        plan = state.get("plan", [])
        past_steps = state.get("past_steps", [])
        execution_events = state.get("execution_events", [])
        execution_event = execution_events[-1] if execution_events else {}

        if past_steps:
            last_step, _ = past_steps[-1]
            tool_calls = execution_event.get("tool_calls", [])
            return {
                "type": "step_complete",
                "stage": "step_executed",
                "message": f"步骤执行完成 ({len(past_steps)}/{len(past_steps) + len(plan)})",
                "current_step": last_step,
                "remaining_steps": len(plan),
                "execution_event": execution_event,
                "tool_call_count": len(tool_calls),
            }
        else:
            return {
                "type": "status",
                "stage": "executor",
                "message": "开始执行步骤"
            }

    def _format_replanner_event(self, state: Dict | None) -> Dict:
        """格式化 Replanner 节点事件"""
        if not state:
            return {
                "type": "status",
                "stage": "replanner",
                "message": "评估节点运行中"
            }

        response = state.get("response", "")
        plan = state.get("plan", [])
        replan_events = state.get("replan_events", [])
        replan_event = replan_events[-1] if replan_events else {}

        if response:
            # 已生成最终响应
            return {
                "type": "report",
                "stage": "final_report",
                "message": "最终报告已生成",
                "report": response,
                "replan_event": replan_event,
            }
        else:
            # 重新规划
            action = replan_event.get("action", "continue")
            reason = replan_event.get("reason", "")
            return {
                "type": "status",
                "stage": "replanner",
                "message": f"Replanner 决策: {action}。{reason or ('继续执行剩余步骤' if plan else '准备生成最终响应')}",
                "remaining_steps": len(plan),
                "replan_event": replan_event,
            }


# 全局单例
aiops_service = AIOpsService()
