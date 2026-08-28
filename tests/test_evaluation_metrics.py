from scripts.evaluate_results import calculate


def test_evaluation_metrics_are_calculated_from_recorded_runs():
    result = calculate(
        [
            {
                "ground_truth": "CPU_SATURATION",
                "predicted_category": "CPU_SATURATION",
                "required_evidence": ["cpu", "latency"],
                "observed_evidence": ["cpu", "latency"],
                "unsupported_claims": 0,
                "dangerous_tool_calls": 0,
                "alert_latency_seconds": 40,
                "diagnosis_seconds": 70,
                "manual_baseline_seconds": 280,
            },
            {
                "ground_truth": "DEPENDENCY_OUTAGE",
                "predicted_category": "DEPENDENCY_LATENCY",
                "required_evidence": ["health", "logs"],
                "observed_evidence": ["health"],
                "unsupported_claims": 1,
                "dangerous_tool_calls": 0,
                "alert_latency_seconds": 55,
                "diagnosis_seconds": 90,
                "manual_baseline_seconds": 320,
            },
        ]
    )
    assert result["root_cause_top1_accuracy"] == 0.5
    assert result["required_evidence_recall"] == 0.75
    assert result["dangerous_tool_calls"] == 0
    assert result["manual_time_reduction"] > 0.7
