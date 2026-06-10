from app.models.aiops import AIOpsRequest


def test_aiops_request_builds_dynamic_diagnosis_task():
    """结构化告警应转换成可执行诊断任务，而不是依赖写死提示词。"""
    request = AIOpsRequest(
        session_id="case-001",
        service_name="demo-service",
        alert_name="HighCPUUsage",
        severity="critical",
        metric_name="demo_cpu_load",
        threshold=0.8,
        environment="local",
        description="CPU 持续高于阈值",
    )

    task = request.to_diagnosis_task()

    assert "demo-service" in task
    assert "HighCPUUsage" in task
    assert "demo_cpu_load" in task
    assert "Prometheus" in task
    assert "Loki" in task
    assert "不允许直接执行" in task
