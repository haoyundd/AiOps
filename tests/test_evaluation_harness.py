from app.agent.aiops.evaluation import (
    build_builtin_eval_cases,
    evaluate_case,
    run_builtin_eval_suite,
)


def test_builtin_eval_cases_match_expectation():
    """内置评测场景应覆盖通过和失败样本，并匹配预期结果。"""
    cases = build_builtin_eval_cases()
    results = [evaluate_case(case) for case in cases]

    assert {case.id for case in cases} == {
        "high_cpu_complete",
        "high_cpu_missing_fault_signal",
        "high_cpu_bad_report_excludes_app",
    }
    assert all(result.passed == result.expected_pass for result in results)


def test_eval_detects_missing_fault_signal():
    """缺少故障线索日志时，Evidence Gate 检查必须失败。"""
    case = next(case for case in build_builtin_eval_cases() if case.id == "high_cpu_missing_fault_signal")
    result = evaluate_case(case)
    evidence_check = next(check for check in result.checks if check.name == "evidence_sufficiency")

    assert result.passed is False
    assert "fault_signal_log" in evidence_check.details["missing_evidence_types"]


def test_eval_detects_dangerous_report_phrase():
    """报告把无错误日志写成排除应用层时，评测必须失败。"""
    case = next(case for case in build_builtin_eval_cases() if case.id == "high_cpu_bad_report_excludes_app")
    result = evaluate_case(case)
    report_check = next(check for check in result.checks if check.name == "report_safety")

    assert result.passed is False
    assert "排除应用层问题" in report_check.details["matched_phrases"]


def test_builtin_eval_suite_passes_by_matching_expectations():
    """评测套件整体通过的含义是每个场景结果都符合预期。"""
    suite = run_builtin_eval_suite()

    assert suite["passed"] is True
    assert suite["case_count"] == 3
    assert suite["matched_expectation"] == 3
