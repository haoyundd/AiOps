# 容器存储异常 Runbook

服务：`merchantflow`
故障类别：`STORAGE_PRESSURE`

当前首版没有注册宿主机或 Kubernetes 磁盘操作工具，因此不能根据缺失的磁盘指标给出确定根因。

如未来接入 node-exporter/cAdvisor，至少验证：文件系统使用率趋势、inode、容器可写层增长、数据库卷和日志写入错误。清理文件属于高风险动作，只能形成建议，不能由诊断 Agent 执行。
