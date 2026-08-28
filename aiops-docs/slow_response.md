# MerchantFlow Redis 延迟 Runbook

服务：`merchantflow`
故障类别：`DEPENDENCY_LATENCY`

## 症状

- HTTP P95 超过 1 秒，但健康检查仍可能为 `UP`。
- Loki 出现 Redis/Lettuce timeout。
- Tempo 中 Redis Span 占请求总耗时的大部分。

## 必须验证的证据

1. 对齐告警前后 15 分钟的请求率、P95 和错误率。
2. 统计 Loki 中 Redis timeout、connection refused 与 MySQL 错误数量，区分慢与断开。
3. 检查至少一个慢 Trace，确认耗时集中于 Redis 客户端 Span。
4. 如果 Tempo 不可用，只能在指标和日志均支持时给出中等置信度结论。

## 恢复与验证

- 生产环境仅建议检查 Redis 延迟、连接池和网络。
- lab 环境可审批 `remove_redis_latency` 固定动作。
- 操作后必须重新检查 `/actuator/health` 和 HTTP P95，不能只相信工具返回成功。
