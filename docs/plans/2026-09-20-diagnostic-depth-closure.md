# Hot Endpoint and JVM Diagnosis Implementation Plan

> **For Codex:** Execute task-by-task with verification after every batch; do not claim closure from unit tests alone.

**Goal:** 让 AIOps 能从真实告警定位到受影响 URI、Trace 路由、RocketMQ 发送阶段和 JVM 热点线程，并消除 MerchantFlow Chat SSE 的无界资源风险。

**Architecture:** MerchantFlow 负责产生低基数业务指标并提供受 AK/SK 保护的只读线程快照；AIOps 的生产诊断统一通过 mcp-ops 读取 Prometheus、Tempo 和线程快照。CPU 故障注入只在 Spring `lab` Profile 注册，通过真实有界计算制造 CPU 与 P95 信号，脚本等待告警和诊断完成后才恢复。

**Tech Stack:** Spring Boot 2.3 / Java 8 / Micrometer / OpenTelemetry / FastAPI / LangGraph / MCP Streamable HTTP / Prometheus / Tempo / PowerShell / k6

---

### Task 0: 固化基线和测试边界

**Files:**
- Modify: `D:\pyagent\tasks\todo.md`
- Modify when an issue is found: `D:\pyagent\tasks\development-problems.md`

**Steps:**
1. 串行运行 AIOps `uv run pytest -q`，避免共享 SQLite 并发重置。
2. 运行 MerchantFlow `mvn -s settings.xml test`，确认 MySQL/Redis 测试基线。
3. 记录容器健康、Prometheus target 和当前告警状态。

### Task 1: 收紧 Chat SSE 资源边界

**Files:**
- Modify: `E:\code\hm-dianping\src\main\java\com\hmdp\config\ChatConfig.java`
- Modify: `E:\code\hm-dianping\src\main\java\com\hmdp\service\impl\ChatServiceImpl.java`
- Modify: `E:\code\hm-dianping\src\main\resources\application.yaml`
- Create: `E:\code\hm-dianping\src\test\java\com\hmdp\service\impl\ChatServiceImplTest.java`

**Steps:**
1. 先写测试：持续 SSE 超过字符/事件上限必须停止；任务队列满时返回明确错误；关闭 emitter 必须取消 Call。
2. `OkHttpClient` 增加总 `callTimeout`，保留连接/读取超时。
3. 将 `Executors.newFixedThreadPool` 改为固定大小、有界队列、拒绝策略明确的 `ThreadPoolExecutor`。
4. 给输出累计字符、SSE 事件数和整体生命周期增加硬上限；Response 使用 try-with-resources。
5. 运行目标测试、`mvn -DskipTests package` 和全量 Maven 测试。

### Task 2: RocketMQ 指标、告警和 Agent 证据闭环

运行时补充验收：Compose 必须等待 NameServer 端口和 Broker 注册均健康后再启动 backend；backend 因瞬时 MQ 初始化超时退出时应自动恢复，不能永久离线。

**Files:**
- Create: `E:\code\hm-dianping\src\main\java\com\hmdp\observability\MerchantFlowMetrics.java`
- Modify: `E:\code\hm-dianping\src\main\java\com\hmdp\service\impl\VoucherOrderServiceImpl.java`
- Modify: `E:\code\hm-dianping\src\main\java\com\hmdp\mq\CacheDeleteCompensationProducer.java`
- Create: `E:\code\hm-dianping\src\test\java\com\hmdp\observability\MerchantFlowMetricsTest.java`
- Modify: `D:\pyagent\observability\alert_rules.yml`
- Modify: `D:\pyagent\app\observability\client.py`
- Modify: `D:\pyagent\app\observability\tools.py`
- Modify: `D:\pyagent\mcp_servers\ops_server.py`
- Modify: `D:\pyagent\app\agent\evidence_graph.py`
- Modify: `D:\pyagent\tests\test_observability_tools.py`
- Modify: `D:\pyagent\tests\test_root_cause_categories.py`

**Steps:**
1. 先写 Java 测试，证明 MQ 发送异常只增加固定 `flow/topic` Counter，不包含订单号、用户 ID 或异常文本标签。
2. 在两个 Producer 的 catch 分支记录 `merchantflow_mq_publish_failures_total`；原有 Redis 回滚和业务响应保持不变。
3. 新增 `MerchantFlowRocketMQPublishFailure` 告警，基于短窗口 `increase`，severity 为 warning，避免与用户影响告警混为一谈。
4. 新增只读 MCP 工具 `get_messaging_metrics`；Agent 用消息指标 + Loki/Trace 两类证据判定 `ROCKETMQ_FAILURE`。
5. 运行 Java 目标测试、Prometheus 规则校验、Python 目标测试和全量测试。

### Task 3: Top URI 指标与 Trace 路由关联

**Files:**
- Modify: `D:\pyagent\app\observability\client.py`
- Modify: `D:\pyagent\app\observability\tools.py`
- Modify: `D:\pyagent\mcp_servers\ops_server.py`
- Modify: `D:\pyagent\app\agent\evidence_graph.py`
- Modify: `D:\pyagent\tests\test_observability_tools.py`
- Modify: `D:\pyagent\tests\test_evidence_graph.py`

**Steps:**
1. 先写测试：Prometheus 多向量结果必须按 `method, uri` 合并并限制最多 10 条；原始 URL/用户 ID 不进入结果。
2. 新增 `get_top_endpoint_metrics` MCP 工具，返回 request rate、P95 和 5xx rate。
3. 扩展 `_extract_trace_spans()`，兼容新旧 OTel HTTP 属性名并提取 route/method/status/error。
4. Evidence Graph 将 Top URI 和 server Span 关联，报告输出 `affected_endpoints`，证据不足时保持空数组。
5. 运行 MCP 协议、Evidence Graph、Ruff、Mypy 和全量 Pytest。

### Task 4: 受保护的 JVM 热点线程快照

**Files:**
- Create: `E:\code\hm-dianping\src\main\java\com\hmdp\controller\OpsDiagnosticsController.java`
- Create: `E:\code\hm-dianping\src\main\java\com\hmdp\service\ThreadSnapshotService.java`
- Create: `E:\code\hm-dianping\src\main\java\com\hmdp\dto\ThreadSnapshotVO.java`
- Create: `E:\code\hm-dianping\src\test\java\com\hmdp\service\ThreadSnapshotServiceTest.java`
- Modify: `D:\pyagent\app\config.py`
- Modify: `D:\pyagent\app\observability\client.py`
- Modify: `D:\pyagent\app\observability\tools.py`
- Modify: `D:\pyagent\mcp_servers\ops_server.py`
- Modify: `D:\pyagent\app\agent\evidence_graph.py`
- Modify: `D:\pyagent\docker-compose.incident.yml`
- Modify: `E:\code\hm-dianping\docker-compose.yml`

**Steps:**
1. 先写测试：快照限制 Top 10、每线程最多 20 个栈帧，输出无环境变量和请求参数。
2. Controller 复用现有 `@AkSkAuth`，并添加全局限流；未签名请求返回 401。
3. ThreadMXBean 两次短采样计算 CPU 增量，只返回结构化类名、方法、文件和行号。
4. 新增 `get_jvm_thread_snapshot` MCP 工具；mcp-ops 从 `.env` 注入 AK/SK 并生成时间戳、nonce、HMAC，不输出密钥。
5. Evidence Graph 只在 JVM CPU >= 0.8 时调用该工具，并将热点线程作为 `jvm_thread_snapshot` 证据持久化。
6. 运行鉴权、边界、MCP 和完整测试；确认未授权接口无法读取线程栈。

### Task 5: 可重复的 CPU Lab 闭环

**Files:**
- Create: `E:\code\hm-dianping\src\main\java\com\hmdp\controller\LabFaultController.java`
- Create: `E:\code\hm-dianping\src\test\java\com\hmdp\controller\LabFaultControllerTest.java`
- Modify: `E:\code\hm-dianping\docker-compose.lab.yml`
- Modify: `E:\code\hm-dianping\lab\k6\cpu-saturation.js`
- Modify: `E:\code\hm-dianping\lab\scenarios.ps1`

**Steps:**
1. Controller 仅 `@Profile("lab")` 注册，并校验 `X-Lab-Scenario-Token`；请求时长限制在 100-5000ms，循环不分配集合。
2. Lab Compose 激活 profile、注入本地令牌；基础 Compose 不注册接口。
3. k6 请求 CPU 负载接口，使 `process_cpu_usage` 和 URI P95 同时越阈。
4. 场景脚本等待 HighCPU firing，再等待新 Incident 和 Run 终态，最后无条件恢复基础 Compose。
5. 验证恢复后告警 resolved、业务接口 200、无数据卷删除。

### Task 6: 最终验收

**AIOps:**
```powershell
uv run python -m compileall app mcp_servers scripts tests
uv run ruff check app mcp_servers scripts tests alembic
uv run mypy app --no-incremental
uv run pytest -q
docker compose -f docker-compose.incident.yml config --quiet
```

**MerchantFlow:**
```powershell
mvn -s settings.xml -DskipTests package
mvn -s settings.xml test
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.lab.yml config --quiet
```

**Runtime acceptance:**
- RocketMQ Broker 中断产生真实 Counter、告警、Incident、MCP 消息证据、`ROCKETMQ_FAILURE` Run，并验证 Redis 预占已回滚。
- CPU 场景产生 Top URI、慢 Trace、热点线程、HighCPU 告警和新的 `CPU_SATURATION` Run。
- 未授权线程快照返回 401；正常环境不注册 CPU 注入接口。
- Chat 超长/超时流释放连接和线程，队列满时快速失败。
- 所有容器恢复健康，旧数据卷不删除，敏感配置不进入 Git。
