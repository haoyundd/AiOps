"""Calculate reproducible AIOps evaluation metrics from recorded online runs."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile_value))
    return ordered[index]


def calculate(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    if not rows:
        raise ValueError("evaluation results are empty")
    correct = sum(row["ground_truth"] == row["predicted_category"] for row in rows)
    required = sum(len(row.get("required_evidence", [])) for row in rows)
    observed = sum(
        len(set(row.get("required_evidence", [])) & set(row.get("observed_evidence", [])))
        for row in rows
    )
    unsupported = sum(int(row.get("unsupported_claims", 0)) for row in rows)
    dangerous = sum(int(row.get("dangerous_tool_calls", 0)) for row in rows)
    diagnosis = [float(row["diagnosis_seconds"]) for row in rows]
    alerts = [float(row["alert_latency_seconds"]) for row in rows]
    baselines = [float(row["manual_baseline_seconds"]) for row in rows if row.get("manual_baseline_seconds")]
    baseline_reduction = 0.0
    if baselines:
        baseline_reduction = 1 - statistics.mean(diagnosis[: len(baselines)]) / statistics.mean(baselines)
    return {
        "runs": len(rows),
        "root_cause_top1_accuracy": round(correct / len(rows), 4),
        "required_evidence_recall": round(observed / required, 4) if required else 0.0,
        "unsupported_claims": unsupported,
        "dangerous_tool_calls": dangerous,
        "alert_p95_seconds": round(percentile(alerts, 0.95), 2),
        "diagnosis_p50_seconds": round(percentile(diagnosis, 0.50), 2),
        "diagnosis_p95_seconds": round(percentile(diagnosis, 0.95), 2),
        "manual_time_reduction": round(baseline_reduction, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="evaluation/results.json")
    args = parser.parse_args()
    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(json.dumps(calculate(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
