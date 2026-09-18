import pytest

from autopsy import assertions


def observation(**overrides):
    base = {
        "completed": True,
        "output": {"answer": "ok"},
        "steps": 2,
        "tokens": 100,
        "aborted_by": None,
    }
    base.update(overrides)
    return base


def test_contains_reads_the_serialized_output():
    assert assertions.check({"type": "output_contains", "value": "ok"}, observation())
    assert not assertions.check({"type": "output_contains", "value": "nope"}, observation())


def test_not_contains_is_not_the_negation_when_the_run_raised():
    """A crashed run produced no output, so it cannot demonstrate safe output."""
    crashed = observation(completed=False, output=None)
    assert not assertions.check({"type": "output_contains", "value": "ok"}, crashed)
    assert not assertions.check({"type": "output_not_contains", "value": "ok"}, crashed)


def test_no_exception_fails_on_ceiling_aborts():
    for aborted in ("recursion_limit", "token_ceiling"):
        aborted_run = observation(completed=False, output=None, aborted_by=aborted)
        assert not assertions.check({"type": "no_exception", "value": None}, aborted_run)


def test_under_assertions_reject_censored_counts():
    """A run the ceiling stopped has a lower bound, not a measurement."""
    aborted = observation(completed=False, output=None, steps=1, tokens=1, aborted_by="token_ceiling")
    assert not assertions.check({"type": "step_count_under", "value": 10}, aborted)
    assert not assertions.check({"type": "token_cost_under", "value": 10}, aborted)
    assert assertions.check({"type": "step_count_under", "value": 10}, observation())
    assert not assertions.check({"type": "step_count_under", "value": 2}, observation(steps=2))


def test_bounded_termination_passes_on_an_ordinary_exception():
    crashed = observation(completed=False, output=None)
    assert assertions.check({"type": "bounded_termination", "value": None}, crashed)


def test_bounded_termination_fails_only_on_ceilings():
    for aborted in ("recursion_limit", "token_ceiling"):
        run = observation(completed=False, output=None, aborted_by=aborted)
        assert not assertions.check({"type": "bounded_termination", "value": None}, run)


def test_there_are_exactly_six():
    assert len(assertions.ASSERTIONS) == 6
    assert set(assertions.ASSERTIONS) == set(assertions.VALUE_TYPES)


@pytest.mark.parametrize(
    "assertion",
    [
        {"type": "terminates"},
        {"type": "output_contains", "value": 3},
        {"type": "step_count_under", "value": "10"},
        {"type": "step_count_under", "value": True},
        {"type": "no_exception", "value": "something"},
        {"value": "no type"},
    ],
)
def test_validate_rejects(assertion):
    with pytest.raises(ValueError):
        assertions.validate(assertion, "test")


def test_validate_accepts_the_six():
    for assertion in (
        {"type": "output_contains", "value": "x"},
        {"type": "output_not_contains", "value": "x"},
        {"type": "no_exception"},
        {"type": "step_count_under", "value": 10},
        {"type": "token_cost_under", "value": 10},
        {"type": "bounded_termination"},
    ):
        assertions.validate(assertion, "test")
