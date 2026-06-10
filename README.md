# 智能运维 Agent 系统

面向告警诊断与半自动修复的 AIOps Agent 系统。项目基于 FastAPI、LangGraph、MCP、Milvus 构建，接入 Prometheus、Loki、Alertmanager 等真实观测数据源，形成从告警接入、证据收集、诊断分析到白名单修复的闭环。

## 核心能力

- 真实告警驱动：接收 Alertmanager webhook，自动创建 incident 并触发诊断流程。
- 多阶段 Agent：基于 LangGraph 组织 Planner / Executor / Replanner 流程，避免单轮模型直接下结论。
- MCP 工具化：将指标查询、日志检索、健康检查和修复动作封装为统一工具层。
- RAG 知识检索：基于 Milvus 构建运维知识库，支持故障案例、处理手册和排障经验召回。
- 模型可切换：支持运行时切换 Xiaomi MiMo、DashScope 以及自定义 OpenAI-compatible 模型。
- 安全修复：支持用户确认后的白名单修复动作，避免 Agent 直接执行高风险操作。
- 容器化部署：通过 Docker Compose 编排多服务，提升环境一致性与本地复现效率。

## 技术栈

`FastAPI / LangGraph / LangChain / MCP / Milvus / Prometheus / Loki / Alertmanager / Docker / Pydantic / httpx / SSE`

## 系统架构

```text
前端 / Web
   -> FastAPI API
      -> AIOps Agent (Planner -> Executor -> Replanner)
         -> MCP Client
            -> mcp-monitor   (Prometheus 指标 / 服务健康)
            -> mcp-log       (Loki 日志 / 错误模式)
            -> mcp-remediation (白名单修复)
         -> Milvus (运维知识库)
         -> Alertmanager / Prometheus / Loki / demo-service
```

## 项目结构

```text
app/                # API、Agent、服务层、模型定义
mcp_servers/        # MCP Server：monitor / log / remediation
demo_service/       # 可注入故障的演示服务
observability/      # Prometheus、Loki、Alertmanager、Grafana 配置
aiops-docs/         # 运维知识文档
tests/              # 单元测试与集成测试
docker-compose.incident.yml
vector-database.yml
```

## 快速开始

### 1. 准备环境

- Python 3.10+
- Docker Desktop
- 一个可用的大模型 API Key（推荐 Xiaomi MiMo 或 DashScope）
- Milvus、Prometheus、Loki、Alertmanager 等容器环境

### 2. 配置环境变量

复制一份 `.env.example` 为 `.env`，根据本地环境补充 Key 和地址。

### 3. 启动容器化环境

```bash
docker compose -f docker-compose.incident.yml up -d
```

Windows 可直接使用：

```powershell
.\start-windows.bat
```

### 4. 访问服务

- Web 页面: `http://localhost:9900`
- API 文档: `http://localhost:9900/docs`

## 常用接口

| 功能 | 方法 | 路径 |
|------|------|------|
| 普通对话 | POST | `/api/chat` |
| 流式对话 | POST | `/api/chat_stream` |
| AIOps 诊断 | POST | `/api/aiops` |
| 告警接入 | POST | `/api/alerts/webhook` |
| incident 列表 | GET | `/api/incidents` |
| incident 详情 | GET | `/api/incidents/{incident_id}` |
| 模型切换 | POST | `/api/model/switch` |
| 修复执行 | POST | `/api/aiops/remediation/execute` |
| 文件上传 | POST | `/api/upload` |

## MCP 工具

### mcp-monitor

- `query_metric_range`
- `query_cpu_metrics`
- `query_memory_metrics`
- `get_service_health`

### mcp-log

- `query_service_logs`
- `find_error_patterns`
- `search_log`

### mcp-remediation

- `propose_remediation`
- `execute_approved_remediation`

## 本地开发

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900 --reload
```

## 测试

```bash
pytest
```

## 项目亮点

- 告警进入系统后不直接给结论，而是先收集指标、日志和知识库证据。
- 通过 Planner / Executor / Replanner 拆分诊断职责，增强可解释性。
- 通过 MCP 把 Prometheus、Loki、修复动作统一成工具层，便于扩展。
- 通过 Docker Compose 做服务隔离和配置解耦，方便本地开发和 GitHub 展示。
