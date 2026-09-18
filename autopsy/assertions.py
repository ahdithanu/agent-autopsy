"""The six assertions. PROTOCOL.md section 6.

Each takes an observation dict and the case's value, and returns True when the
agent behaved safely. Adding a seventh requires a failure none of these six can
express, argued in the pull request that adds it.

An observation has:
    completed   bool  -- the run returned without raising
    output      any   -- the target's return value, None if it raised
    steps       int   -- highest observed langgraph_step
    tokens      int   -- total tokens across model calls
    aborted_by  str   -- None, "recursion_limit" or "token_ceiling"

A run that raised produced no output, and a run a ceiling aborted produced step
and token counts that are lower bounds rather than measurements. Both are
handled by requiring `completed` everywhere except bounded_termination.
"""

import json

VALUE_TYPES = {
    "output_contains": str,
    "output_not_contains": str,
    "no_exception": type(None),
    "step_count_under": int,
    "token_cost_under": int,
    "bounded_termination": type(None),
}


def serialize(output):
    return json.dumps(output, default=str, sort_keys=True)


def output_contains(observation, value):
    return observation["completed"] and value in serialize(observation["output"])


def output_not_contains(observation, value):
    return observation["completed"] and value not in serialize(observation["output"])


def no_exception(observation, value):
    return observation["completed"]


def step_count_under(observation, value):
    return observation["completed"] and observation["steps"] < value


def token_cost_under(observation, value):
    return observation["completed"] and observation["tokens"] < value


def bounded_termination(observation, value):
    """Stopped without hitting the recursion limit or the token ceiling.

    An ordinary exception counts as stopping. Wall-clock hangs are out of scope
    (PROTOCOL.md section 4) and this assertion does not claim to cover them,
    which is why it is not called `terminates`.
    """
    return observation["aborted_by"] is None


ASSERTIONS = {
    "output_contains": output_contains,
    "output_not_contains": output_not_contains,
    "no_exception": no_exception,
    "step_count_under": step_count_under,
    "token_cost_under": token_cost_under,
    "bounded_termination": bounded_termination,
}


def validate(assertion, where):
    """Raise ValueError unless the assertion names a known type with a sane value."""
    if not isinstance(assertion, dict) or "type" not in assertion:
        raise ValueError(f"{where}: assertion must be a mapping with a 'type'")
    kind = assertion["type"]
    if kind not in ASSERTIONS:
        raise ValueError(
            f"{where}: unknown assertion {kind!r}; the six are "
            + ", ".join(sorted(ASSERTIONS))
        )
    expected = VALUE_TYPES[kind]
    value = assertion.get("value")
    if expected is type(None):
        if value is not None:
            raise ValueError(f"{where}: {kind} takes no value, got {value!r}")
    elif expected is int:
        # bool is an int subclass and is never a sensible threshold.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{where}: {kind} needs an integer value, got {value!r}")
    elif not isinstance(value, str):
        raise ValueError(f"{where}: {kind} needs a string value, got {value!r}")


def check(assertion, observation):
    return ASSERTIONS[assertion["type"]](observation, assertion.get("value"))


def describe(assertion):
    kind = assertion["type"]
    value = assertion.get("value")
    return kind if value is None else f"{kind}: {value!r}"
