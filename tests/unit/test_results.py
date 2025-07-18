from unittest.mock import MagicMock

import pytest

import tmt.utils
from tmt.checks import CheckEvent
from tmt.cli import TmtExitCode
from tmt.result import (
    CheckResult,
    CheckResultInterpret,
    Result,
    ResultInterpret,
    ResultOutcome,
    results_to_exit_code,
)
from tmt.utils import Common, Path


@pytest.mark.parametrize(
    ('outcomes', 'expected_exit_code'),
    [
        # No test results found.
        ([], TmtExitCode.NO_RESULTS_FOUND),
        # Errors occurred during test execution.
        (
            [
                ResultOutcome.PASS,
                ResultOutcome.FAIL,
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
                ResultOutcome.WARN,
                ResultOutcome.ERROR,
                ResultOutcome.PENDING,
            ],
            TmtExitCode.ERROR,
        ),
        # There was a fail or warn identified, but no error.
        (
            [
                ResultOutcome.PASS,
                ResultOutcome.FAIL,
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
                ResultOutcome.WARN,
                ResultOutcome.PENDING,
            ],
            TmtExitCode.FAIL,
        ),
        # Tests were executed, and all reported the ``skip`` result.
        (
            [
                ResultOutcome.SKIP,
                ResultOutcome.SKIP,
            ],
            TmtExitCode.ALL_TESTS_SKIPPED,
        ),
        # At least one test passed, there was no fail, warn or error.
        (
            [
                ResultOutcome.PASS,
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
            ],
            TmtExitCode.SUCCESS,
        ),
        # An info is treated as a pass.
        (
            [
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
            ],
            TmtExitCode.SUCCESS,
        ),
        # A pending without any fail or error is treated as an error.
        (
            [
                ResultOutcome.PASS,
                ResultOutcome.PENDING,
            ],
            TmtExitCode.ERROR,
        ),
    ],
    # IDs copied from the specification:
    ids=(
        "No test results found",
        "Errors occurred during test execution",
        "There was a fail or warn identified, but no error",
        "Tests were executed, and all reported the ``skip`` result",
        "At least one test passed, there was no fail, warn or error",
        "An info is treated as a pass",
        "A pending without any fail or error is treated as an error",
    ),
)
def test_result_to_exit_code(outcomes: list[ResultOutcome], expected_exit_code: int) -> None:
    assert (
        results_to_exit_code([MagicMock(result=outcome) for outcome in outcomes])
        == expected_exit_code
    )


@pytest.mark.parametrize(
    (
        'result_outcome',
        'interpret',
        'interpret_checks',
        'expected_outcome',
        'expected_note_contains',
    ),
    [
        # Test RESPECT interpretation
        (
            ResultOutcome.PASS,
            ResultInterpret.RESPECT,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.PASS,
            [],
        ),
        (
            ResultOutcome.FAIL,
            ResultInterpret.RESPECT,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.FAIL,
            ["check 'check1' failed"],  # Note is set when check fails
        ),
        # Test XFAIL interpretation
        (
            ResultOutcome.FAIL,
            ResultInterpret.XFAIL,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.PASS,
            ["check 'check1' failed", "test failed as expected", "original test result: fail"],
        ),
        (
            ResultOutcome.PASS,
            ResultInterpret.XFAIL,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.FAIL,
            ["test was expected to fail", "original test result: pass"],
        ),
        # Test INFO interpretation
        (
            ResultOutcome.FAIL,
            ResultInterpret.INFO,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.INFO,
            [
                "check 'check1' failed",
                "test result overridden: info",
                "original test result: fail",
            ],
        ),
        (
            ResultOutcome.PASS,
            ResultInterpret.INFO,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.INFO,
            ["test result overridden: info", "original test result: pass"],
        ),
        # Test WARN interpretation
        (
            ResultOutcome.PASS,
            ResultInterpret.WARN,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.WARN,
            ["test result overridden: warn", "original test result: pass"],
        ),
        # Test ERROR interpretation
        (
            ResultOutcome.PASS,
            ResultInterpret.ERROR,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.ERROR,
            ["test result overridden: error", "original test result: pass"],
        ),
        # Test CUSTOM interpretation (should not modify result)
        (
            ResultOutcome.FAIL,
            ResultInterpret.CUSTOM,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.FAIL,
            [],
        ),
    ],
    ids=[
        "respect-pass",
        "respect-fail",
        "xfail-fail",
        "xfail-pass",
        "info-fail",
        "info-pass",
        "warn-pass",
        "error-pass",
        "custom-fail",
    ],
)
def test_result_interpret_all_cases(
    result_outcome: ResultOutcome,
    interpret: ResultInterpret,
    interpret_checks: dict[str, CheckResultInterpret],
    expected_outcome: ResultOutcome,
    expected_note_contains: list[str],
) -> None:
    """
    Test all possible combinations of result interpretations
    """

    result = Result(
        name="test-case",
        result=result_outcome,
        check=[CheckResult(name="check1", result=result_outcome, event=CheckEvent.BEFORE_TEST)],
    )

    interpreted = result.interpret_result(interpret, interpret_checks)
    assert interpreted.result == expected_outcome

    if expected_note_contains:
        assert interpreted.note
        for expected_note in expected_note_contains:
            assert expected_note in interpreted.note
    else:
        assert not interpreted.note


def test_result_interpret_check_phases() -> None:
    """
    Test the interpretation of check results with different phases
    """

    result = Result(
        name="test-case",
        check=[
            CheckResult(name="check1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
            CheckResult(name="check1", result=ResultOutcome.FAIL, event=CheckEvent.AFTER_TEST),
            CheckResult(name="check2", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
        ],
    )

    # Test with mixed interpretations
    interpret_checks = {
        "check1": CheckResultInterpret.RESPECT,
        "check2": CheckResultInterpret.INFO,
    }

    interpreted = result.interpret_result(ResultInterpret.RESPECT, interpret_checks)
    assert interpreted.note is not None
    assert "check 'check1' failed" in interpreted.note
    assert "check 'check2' is informational" in interpreted.note

    # Verify individual check results were interpreted
    assert interpreted.check[0].result == ResultOutcome.PASS  # check1 BEFORE_TEST
    assert interpreted.check[1].result == ResultOutcome.FAIL  # check1 AFTER_TEST
    assert interpreted.check[2].result == ResultOutcome.PASS  # check2 BEFORE_TEST (INFO)


def test_result_interpret_edge_cases() -> None:
    """
    Test edge cases in result interpretation
    """

    # Test with no checks
    result = Result(name="test-case", result=ResultOutcome.FAIL)
    interpreted = result.interpret_result(ResultInterpret.RESPECT, {})
    assert interpreted.result == ResultOutcome.FAIL
    assert not interpreted.note

    # Test with empty check list
    result = Result(name="test-case", result=ResultOutcome.FAIL, check=[])
    interpreted = result.interpret_result(ResultInterpret.RESPECT, {})
    assert interpreted.result == ResultOutcome.FAIL
    assert not interpreted.note


# Weird control characters in failures.yaml
#
# https://github.com/teemtee/tmt/issues/3805
_BAD_STRING = """
    :: [  \x1b[1;31mFAIL\x1b[0m  ] :: Command './test_progs -a atomics'
    bpf_testmod.ko is already unloaded."""

_GOOD_STRING = """
    :: [  #{1b}[1;31mFAIL#{1b}[0m  ] :: Command './test_progs -a atomics'
    bpf_testmod.ko is already unloaded."""


def test_save_failures(tmppath: Path, root_logger) -> None:
    from tmt.result import save_failures
    from tmt.steps.execute import TestInvocation

    phase = Common(workdir=tmppath, logger=root_logger)
    phase.step = MagicMock(workdir=tmppath)
    invocation = TestInvocation(root_logger, phase, None, None)

    (tmppath / 'data').mkdir()

    save_failures(invocation, Path('data'), ['foo', _BAD_STRING, 'bar'])

    read_yaml = (tmppath / 'data/failures.yaml').read_text()

    assert tmt.utils.yaml_to_python(read_yaml) == ['foo', _GOOD_STRING, 'bar']
    assert tmt.utils.yaml_to_python(read_yaml, yaml_type='safe') == ['foo', _GOOD_STRING, 'bar']
    assert tmt.utils.yaml_to_list(read_yaml) == ['foo', _GOOD_STRING, 'bar']
