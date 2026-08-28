# 真实故障评测集

`cases.json` 包含 3 类、15 个参数变体。每个案例应独立执行 3 次，共 45 次在线评测。

这些案例不会让应用返回伪造指标或伪造错误：CPU 案例使用真实并发和容器 CPU 配额；延迟与中断案例使用 Toxiproxy 改变 MerchantFlow 到 Redis 的真实 TCP 链路。

每次执行流程：

1. 启动 AIOps Compose 与 MerchantFlow lab overlay。
2. 启动 k6 基础流量。
3. 执行案例的 `command`，记录 Alertmanager 告警出现时间。
4. 等待 Incident 进入 `DIAGNOSED`、`INCONCLUSIVE` 或 `FAILED`。
5. 将结果写入 `results.json`，字段格式见 `results.example.json`。
6. 每次案例完成后运行 `powershell -File lab/scenarios.ps1 restore`。
7. 使用 `uv run python scripts/evaluate_results.py` 计算指标。

简历中只填写脚本实际输出的数值，不使用计划目标替代测量结果。
