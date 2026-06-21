from mcp_servers import cls_server, monitor_server


class _DummyResponse:
    """模拟 httpx 响应对象。"""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        """测试中默认 HTTP 请求成功。"""

    def json(self):
        """返回预设响应。"""
        return self._payload


class _DummyClient:
    """记录 HTTP 查询参数的测试客户端。"""

    last_url = ""
    last_params = {}

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params):
        _DummyClient.last_url = url
        _DummyClient.last_params = params
        return _DummyResponse(
            {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "demo-service"},
                            "values": [["1", "error mode injected"]],
                        }
                    ]
                },
            }
        )


class _MetricDummyClient:
    """返回 Prometheus matrix 数据，用于测试指标摘要。"""

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params):
        return _DummyResponse(
            {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"service": "demo-service"},
                            "values": [["1", "0.15"], ["2", "0.95"], ["3", "0.85"]],
                        }
                    ]
                },
            }
        )


class _FaultLogDummyClient(_DummyClient):
    """返回 CPU warning 日志，用于测试非 error 故障线索。"""

    def get(self, url, params):
        _DummyClient.last_url = url
        _DummyClient.last_params = params
        return _DummyResponse(
            {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "demo-service"},
                            "values": [
                                ["1", "WARNING service=demo-service message=cpu spike injected cpu_load=high"],
                            ],
                        }
                    ]
                },
            }
        )


def test_prometheus_metric_range_query_builds_service_selector(monkeypatch):
    """Prometheus 工具应按服务名构造真实 query_range 查询。"""
    monkeypatch.setattr(monitor_server.httpx, "Client", _DummyClient)

    result = monitor_server._query_metric_range_impl(
        metric_name="demo_cpu_load",
        service_name="demo-service",
        start_time="2026-06-02 10:00:00",
        end_time="2026-06-02 10:10:00",
        step="1m",
    )

    assert result["status"] == "success"
    assert result["query"] == 'demo_cpu_load{service="demo-service"}'
    assert _DummyClient.last_url.endswith("/api/v1/query_range")
    assert _DummyClient.last_params["step"] == "1m"


def test_prometheus_metric_summary_calculates_threshold(monkeypatch):
    """指标摘要工具应计算 last/max/avg，并判断是否超过阈值。"""
    monkeypatch.setattr(monitor_server.httpx, "Client", _MetricDummyClient)

    result = monitor_server._query_metric_summary_impl(
        metric_name="demo_cpu_load",
        service_name="demo-service",
        threshold=0.8,
    )

    assert result["evidence_type"] == "metric_summary"
    assert result["summary"]["sample_count"] == 3
    assert result["summary"]["max"] == 0.95
    assert result["summary"]["last"] == 0.85
    assert result["summary"]["exceeded"] is True


def test_loki_log_query_builds_logql_and_samples(monkeypatch):
    """Loki 工具应构造 LogQL，并抽取可引用日志样本。"""
    monkeypatch.setattr(cls_server.httpx, "Client", _DummyClient)

    result = cls_server._query_service_logs_impl(
        service_name="demo-service",
        keyword="injected",
        log_level="error",
        limit=5,
    )

    assert result["status"] == "success"
    assert '{service="demo-service"}' in result["query"]
    assert "error" in result["query"]
    assert "injected" in result["query"]
    assert result["samples"][0]["line"] == "error mode injected"


def test_loki_fault_signals_find_cpu_warning(monkeypatch):
    """故障线索工具应能抓到 CPU warning，而不是只依赖错误日志。"""
    monkeypatch.setattr(cls_server.httpx, "Client", _FaultLogDummyClient)

    result = cls_server._find_fault_signals_impl(service_name="demo-service", limit=5)

    assert result["evidence_type"] == "fault_signals"
    assert "cpu|slow|fault|injected|latency|warning" in result["query"]
    assert result["signal_counts"]["cpu"] == 1
    assert result["signal_counts"]["fault_injection"] == 1
    assert result["total_signal_samples"] == 1
