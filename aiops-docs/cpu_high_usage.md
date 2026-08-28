# MerchantFlow CPU 饱和 Runbook

服务：`merchantflow`
故障类别：`CPU_SATURATION`
风险：只读诊断

## 症状

- `process_cpu_usage` 持续超过 0.8。
- HTTP P95 同期升高，请求率通常较高。
- Redis/MySQL 连接错误不应成为主要信号。

## 必须验证的证据

1. Prometheus 查询 JVM 进程 CPU、系统 CPU、请求率与 P95。
2. Loki 检查同期是否存在依赖超时、连接拒绝或 OOM；存在时不能直接下 CPU 根因。
3. Tempo 检查慢请求是否均匀耗时在应用 Span，还是集中在 Redis/MySQL Span。
4. 至少两个数据源可用后再给出确定结论。

## 建议

- 先定位热点接口和线程活动，再决定限流、扩容或优化代码。
- 生产环境只给建议，不允许 Agent 修改 CPU 配额或重启服务。
- lab 环境可停止 k6 流量，并验证 CPU 与 P95 是否恢复。
