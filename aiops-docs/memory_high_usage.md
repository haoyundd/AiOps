# JVM 内存异常 Runbook

服务：`merchantflow`
故障类别：`JVM_MEMORY_PRESSURE`

## 证据

- 对比 `jvm_memory_used_bytes`、`jvm_memory_max_bytes` 与 GC 暂停时间。
- 检查 Loki 中 `OutOfMemoryError`、频繁 Full GC 和线程创建失败。
- 检查 Tempo 中是否出现全局长尾，而非单个依赖 Span 变慢。

单次内存峰值不足以证明泄漏。需要持续趋势、GC 行为和日志共同支持；证据不足时返回 `INCONCLUSIVE`。

生产环境不允许 Agent 修改 JVM 参数或重启进程。
