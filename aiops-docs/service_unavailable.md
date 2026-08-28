# MerchantFlow 依赖中断 Runbook

服务：`merchantflow`
故障类别：`DEPENDENCY_OUTAGE`

## 症状

- Blackbox `probe_success=0` 或 Actuator 整体状态为 `DOWN`。
- HTTP 5xx 比例升高。
- Loki 出现 Redis/MySQL/RocketMQ connection refused、unavailable 或连接超时。
- Tempo 下游 Span 标记错误。

## 调查顺序

1. 查询健康详情，识别失败的依赖组件。
2. 用 Loki 日志确认具体客户端和异常类型。
3. 用 Tempo 检查失败 Span 的 peer/service 属性，避免只根据告警名猜测。
4. 检查 CPU 与内存，排除应用自身资源耗尽导致的假性依赖错误。

## 安全边界

- MerchantFlow 正常环境禁止 Agent 启停 Redis/MySQL/RocketMQ。
- lab 中仅允许审批 `restore_redis_connection`，不接受任意容器名或 Shell 命令。
- 恢复后必须等待健康状态为 `UP` 并观察错误率下降。
