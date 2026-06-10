"""
通用 Plan-Execute-Replan 状态定义
基于 LangGraph 官方教程实现
"""

from typing import Any, Dict, List, TypedDict, Annotated
import operator

#TypedDict 是一个通用的类型，用来定义字典的键和值的类型，不可改变
class PlanExecuteState(TypedDict):
    """Plan-Execute-Replan 状态"""
    
    # 用户输入（任务描述）
    input: str
    
    # 执行计划（步骤列表）
    plan: List[str]
    
    # 已执行的步骤历史
    # 使用 operator.add 实现追加式更新（而非覆盖）
    #Annotated：类型注解，用来给类型添加额外的元数据（如更新方式）
    #每个节点返回 {"past_steps": [(step, result)]}，不会覆盖之前的数据，自动累加。
    past_steps: Annotated[List[tuple], operator.add]
    #tuple 是元组，类似于列表，但不可修改。

    # Planner 结构化过程信息，供 incident 时间线和前端展示使用。
    planner_trace: Dict[str, Any]

    # Executor 每一步的结构化执行记录，使用追加式更新保存完整证据链。
    execution_events: Annotated[List[Dict[str, Any]], operator.add]

    # Replanner 每次评估的结构化决策记录，方便解释为什么继续、重排或生成报告。
    replan_events: Annotated[List[Dict[str, Any]], operator.add]
    
    # 最终响应/报告
    response: str
