# AIOps 收尾与企业级故障闭环 Implementation Plan

> **For Codex:** 后续执行可按 `@executing-plans` 逐批推进；每个批次必须先写测试、再实现、再验收，未通过不得进入下一批。

**Goal:** 将当前已经可以运行的 AIOps 核心链路收尾为可解释、可复现、可审计的真实故障诊断闭环。

**Architecture:** 保持现有 FastAPI + Worker + PostgreSQL + MCP + Prometheus/Loki/Tempo + MerchantFlow Docker Compose 架构，不引入 Kubernetes，不删除旧数据卷。生产诊断继续通过 MCP 获取观测证据；修复动作默认拒绝，只允许隔离 Lab 在审批后执行。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy/Alembic、LangGraph、Pydantic、PostgreSQL/pgvector、MCP Streamable HTTP、Prometheus、Loki、Tempo、Docker Compose、PowerShell、k6、Spring Boot Actuator。

---

## 现状与完成口径

已经完成的基础：

- API、Worker、Incident、AgentRun、证据持久化和 Runbook 草稿基础链路已存在。
- MCP 健康、指标、日志、Trace 搜索和 Trace 详情已经有真实协议验证。
- Redis 延迟/中断、MySQL 延迟、RocketMQ 中断已经做过真实实验。
- 两套 Compose 当前可运行，MerchantFlow 健康地址为 `http://merchantflow:8081/actuator/health`。

本计划只处理尚未形成最终验收证据的部分：

- 失败态、运行元数据和告警幂等必须与真实执行一致；
- 修复提案、审批、执行、恢复验证必须可审计；
- 剩余真实故障场景必须逐项闭环；
- 前端必须按最近一次 AgentRun 展示，不能混入历史 Run；
- 所有验证必须保留 Ground Truth、Run ID、告警时间和恢复结果。

## 执行规则

- 计划确认前不修改 Python/Java 业务源码。
- 每个批次遵循：写失败测试 → 运行确认失败 → 最小实现 → 单元/集成测试 → Compose/HTTP 验收 → 更新 `tasks/todo.md`。
- 每次真实故障只执行一个场景，等待旧告警 resolved 后再开始下一个场景。
- 不删除 MySQL、Redis、RocketMQ、AIOps PostgreSQL、Prometheus、Loki、Tempo 数据卷。
- `LAB_MODE=false` 或 `ALLOW_MUTATIONS=false` 时，不执行任何破坏性动作。
- 真实密钥不写入计划、测试输出或提交内容。

## Task 0：建立收尾基线

**Files:**

- Inspect: `D:\pyagent\app\config.py`
- Inspect: `D:\pyagent\app\worker.py`
- Inspect: `D:\pyagent\app\api\v1\alerts.py`
- Inspect: `D:\pyagent\app\agent\evidence_graph.py`
- Inspect: `D:\pyagent\app\observability\mcp_client.py`
- Inspect: `D:\pyagent\tests\`
- Modify: `D:\pyagent\tasks\todo.md`

**Step 1: 运行当前基线检查**

Run:

```powershell
python -m compileall app mcp_servers scripts tests
uv run ruff check app mcp_servers scripts tests
uv run mypy app --no-incremental
uv run pytest -q
docker compose -f docker-compose.incident.yml config --quiet
```

Expected: 所有命令通过；如果失败，先记录为基线问题，不把失败归因到后续改动。

**Step 2: 检查当前未提交变更和环境状态**

Run:

```powershell
git status --short
docker compose ls
docker ps --format "table {{.Names}}\t{{.Status}}"
```

Expected: 不覆盖用户已有修改；只记录当前状态，不清理文件或数据卷。

**Step 3: 建立收尾检查清单**

在 `tasks/todo.md` 记录测试基线、当前 Compose 状态、当前 Git 状态和待修复项。该文件只作为任务跟踪，不新增无关说明文档。

## Task 1：修复告警幂等与 AgentRun 运行真实性

**Files:**

- Modify: `D:\pyagent\app\api\v1\alerts.py`
- Modify: `D:\pyagent\app\services\incident_repository.py`
- Modify: `D:\pyagent\app\worker.py`
- Modify: `D:\pyagent\app\core\llm_factory.py`
- Modify: `D:\pyagent\app\domain.py`
- Test: `D:\pyagent\tests\test_alerts.py`
- Test: `D:\pyagent\tests\test_worker.py`
- Test: `D:\pyagent\tests\test_incident_repository.py`

**Step 1: 写失败测试**

覆盖以下行为：

- 同一 fingerprint 的持续 `firing` 只刷新 Incident，不新建 Run；
- `resolved -> firing` 创建新 Run；
- 人工重新诊断创建新 Run；
- 当前 Evidence Graph 是确定性证据流，不直接调用 LLM；只对实际存在的聊天/模型入口验证 401、403、429 和模型不存在，不为不存在的调用路径增加伪代码；
- 失败 Run 不进入 Replanner、不生成成功报告、不生成 Runbook Draft；
- provider/model/步骤数/工具调用数来自真实运行；无法获取 token 时保持 `null`。

**Step 2: 运行失败测试**

```powershell
uv run pytest tests/test_alerts.py tests/test_worker.py tests/test_incident_repository.py -q
```

Expected: 新增用例先失败，失败原因必须指向目标行为。

**Step 3: 最小实现**

- 以 fingerprint + 告警状态维护当前告警周期；
- 对实际模型调用入口统一处理致命模型异常，写入脱敏错误事件；当前诊断图没有模型调用时，不额外引入不使用的抽象；
- 让 LangGraph state 累计真实执行节点，Run 结束时从运行上下文汇总步骤数和工具调用数，不再写死节点数量；
- 保留数据库唯一约束，并覆盖并发重复告警下的幂等行为；
- 失败状态只允许进入 `FAILED`/人工处理状态；
- 在关键函数补充中文注释，说明状态转换和“下一步进入哪里”。

**Step 4: 验证**

```powershell
uv run pytest tests/test_alerts.py tests/test_worker.py tests/test_incident_repository.py -q
uv run ruff check app tests
uv run mypy app --no-incremental
```

Expected: 幂等、失败态和运行元数据测试全部通过。

## Task 2：收紧证据门槛和诊断报告安全性

**Files:**

- Modify: `D:\pyagent\app\agent\evidence_graph.py`
- Modify: `D:\pyagent\app\observability\client.py`
- Modify: `D:\pyagent\app\observability\tools.py`
- Modify: `D:\pyagent\app\services\runbook_draft_service.py`
- Test: `D:\pyagent\tests\test_evidence_graph.py`
- Test: `D:\pyagent\tests\test_root_cause_categories.py`
- Test: `D:\pyagent\tests\test_runbook_draft_service.py`

**Step 1: 写失败测试**

覆盖：

- 单一数据源只能 `INCONCLUSIVE`；
- 空日志、空 Trace、过期窗口不能算有效证据；
- “不能排除应用层问题”不能被误判成已排除；
- 失败 Run、低置信度和 `INCONCLUSIVE` 不生成正式知识；
- 诊断结果必须包含根因、置信度、证据来源、缺失来源和下一步建议。

**Step 2: 实现证据聚合**

- 证据按类型去重，不按工具调用次数计数；
- Loki/Tempo 查询窗口绑定 Incident `startsAt`，允许有限 lookback，但不得跨实验窗口；
- Trace 子 Span 聚合 Redis、MySQL、RocketMQ 和 HTTP 下游信息；
- 证据不足时报告明确缺失项，不强行给出根因。

**Step 3: 验证**

```powershell
uv run pytest tests/test_evidence_graph.py tests/test_root_cause_categories.py tests/test_runbook_draft_service.py -q
uv run ruff check app tests
```

## Task 3：完善修复审批、执行和恢复验证闭环

**Files:**

- Inspect/Modify: `D:\pyagent\app\api\v1\remediation.py`
- Inspect/Modify: `D:\pyagent\app\services\remediation_service.py`
- Modify: `D:\pyagent\app\db.py`
- Modify: `D:\pyagent\app\schemas.py`
- Create: `D:\pyagent\alembic\versions\0005_remediation_run_binding.py`
- Inspect/Modify: `D:\pyagent\app\services\remediation_service.py`
- Modify: `D:\pyagent\static\index.html`
- Modify: `D:\pyagent\static\js\app.js`
- Test: `D:\pyagent\tests\test_remediation.py`
- Test: `D:\pyagent\tests\test_api_v1.py`

**Step 1: 写失败测试**

- 非 Lab 环境拒绝执行；
- 非管理员、无审批、重复审批拒绝；
- 修复动作绑定 Incident ID 和 Run ID；
- 执行前后均写审计；
- 修复提案同时绑定 Incident ID 和 DiagnosisRun ID；
- 执行后健康、指标和告警恢复验证失败时进入人工处理；
- 失败 Run 不允许执行修复。

**Step 2: 实现最小闭环**

- 只允许白名单动作；
- 前端只能调用高层审批接口，不能直接调用底层命令；
- 明确动作状态机：提案、审批、执行、验证成功/失败、人工处理；
- 中文注释说明权限边界、不可回滚动作和下一步人工入口。

**Step 3: 验证**

```powershell
uv run pytest tests/test_remediation.py tests/test_api_v1.py -q
uv run ruff check app mcp_servers tests
```

## Task 4：完成 Runbook 草稿审核和前端 Run 隔离

**Files:**

- Modify: `D:\pyagent\app\api\v1\runbook_drafts.py`
- Modify: `D:\pyagent\app\services\runbook_draft_service.py`
- Modify: `D:\pyagent\static\index.html`
- Modify: `D:\pyagent\static\js\app.js`
- Test: `D:\pyagent\tests\test_runbook_draft_service.py`
- Test: `D:\pyagent\tests\test_api_v1.py`

**Step 1: 写失败测试**

- 草稿列表能按 `PENDING/APPROVED/REJECTED` 筛选；
- 批准写入正式 Runbook，重复批准返回冲突；
- 驳回必须保存理由；
- checksum 防止同一诊断事实重复入库；
- 追问内容、失败 Run 和低置信度诊断不能进入正式知识；
- Incident 页面默认只展示最近一次 Run，历史 Run 可展开查看。

**Step 2: 实现和注释**

- 审核接口明确管理员边界；
- 前端将 Incident 生命周期和 AgentRun 执行过程分栏展示；
- 草稿、正式知识、历史 Run 使用不同状态标签；
- 注释说明批准后进入 `RunbookService.create()` 的下一步。

**Step 3: 验证**

```powershell
uv run pytest tests/test_runbook_draft_service.py tests/test_api_v1.py -q
node --check static/js/app.js
```

## Task 5：完成六类真实故障实验的剩余验收

**Files:**

- Inspect/Modify only when needed: `D:\pyagent\lab\scenarios.ps1`
- Inspect/Modify only when needed: `D:\pyagent\lab\k6\dependency-probes.js`
- Inspect/Modify only when needed: `D:\pyagent\lab\k6\rocketmq-probe.js`
- Inspect: `D:\pyagent\lab\check-data-consistency.ps1`
- Modify: `D:\pyagent\tasks\development-problems.md`
- Modify: `D:\pyagent\tasks\todo.md`

**实验顺序：**

1. MySQL 中断；
2. Redis 中断分类复验；
3. HTTP 5xx/健康检查异常；
4. CPU 饱和；
5. 对六类场景做最终结果汇总。

每个场景执行以下固定步骤：

**Step 1: 记录 Ground Truth**

- 场景名、开始时间、预期故障、代理/容器状态、恢复方式；
- 实验只使用真实 Toxiproxy、容器状态和真实业务请求；
- 等待 MerchantFlow healthcheck 为 `healthy` 后再开始发流量。

**Step 2: 注入并观察**

- 注入单一故障；
- 运行 k6 或真实业务请求；
- 记录 Prometheus 告警、Loki 日志、Tempo Trace、Actuator 状态；
- 等待 Alertmanager 创建 Incident 和 Worker 生成 Run。

**Step 3: 验证诊断**

- 记录 Incident ID、Run ID、根因分类、置信度、证据来源、MCP 工具调用数；
- 核对没有历史 Trace/日志污染；
- 单证据时应为 `INCONCLUSIVE`，不能为了演示强行诊断。

**Step 4: 恢复并验证**

- 清除 toxic 或恢复依赖容器；
- 使用 `--force-recreate` 恢复 Compose 配置；
- 验证健康、指标、告警、日志和业务接口恢复；
- 运行数据一致性脚本，确认没有新增孤立秒杀预占。

**Step 5: 记录复盘**

在 `tasks/development-problems.md` 追加根因、解决办法、方案取舍和面试表达；在 `tasks/todo.md` 记录每个场景的实际证据。

## Task 6：最终验收与交付判断

**Files:**

- Inspect: `D:\pyagent\git status`
- Modify: `D:\pyagent\tasks\todo.md`

**Step 1: AIOps 静态与单元验收**

```powershell
python -m compileall app mcp_servers scripts tests
uv run ruff check app mcp_servers scripts tests
uv run mypy app --no-incremental
uv run pytest -q
node --check static/js/app.js
docker compose -f docker-compose.incident.yml config --quiet
```

**Step 2: MerchantFlow 回归验收**

```powershell
mvn -s settings.xml -DskipTests package
mvn -s settings.xml test
docker compose -f docker-compose.yml config --quiet
```

**Step 3: 运行态验收**

- AIOps `/health`、`/ready` 返回 200；
- MerchantFlow `/actuator/health`、`/actuator/prometheus` 返回 200；
- Prometheus `merchantflow` target 为 `UP`；
- MCP 工具真实协议调用成功；
- 正常环境修复动作被拒绝；
- 数据重启后仍存在；
- 两套 Compose 可重复启动；
- Git 状态中没有敏感配置和无关源码修改。

**Step 4: 完成标准**

只有在所有批次和验收命令通过后，才将“收尾计划”标记为完成。若任何真实故障场景只得到 `INCONCLUSIVE`，必须保留该结果并说明证据缺口，不得包装成成功。

## 风险与方案取舍

- 不清空数据卷：保留历史实验可追溯性，避免把数据问题伪装成环境重置后的“成功”。
- 不默认启用真实修复：个人项目演示优先保证安全边界，Lab 才允许白名单动作。
- 不先上 Kubernetes：当前瓶颈是证据闭环而不是编排规模，Compose 更适合重复实验。
- 不用单一大模型结论作为根因：以独立观测证据为准，避免模型幻觉污染 Runbook。
- 不让告警接收依赖诊断成功：告警和 Incident 必须先持久化，Agent 失败只影响诊断状态。
