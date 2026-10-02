"""Load cases, execute them against a target, append one JSON line per execution.

Results are append-only. Nothing here rewrites, reorders or deletes a line that
was already written, and no exception is swallowed.
"""

import datetime
import importlib
import json
import os
import time
import traceback
import uuid

import anthropic
import yaml
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphRecursionError

from autopsy import assertions, env

# PROTOCOL.md section 1. Not a parameter.
RUNS_PER_CASE = 5

# PROTOCOL.md section 5.
CATEGORIES = (
    "malformed_tool_output",
    "contradictory_instruction",
    "state_collision",
    "loop_bait",
    "cost_blowup",
)

# PROTOCOL.md section 7. The one place infrastructure errors are defined.
# A batch containing any of these is never published.
INFRASTRUCTURE_ERRORS = (
    anthropic.APIConnectionError,  # covers APITimeoutError
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class TokenCeilingExceeded(Exception):
    """The per-execution token ceiling was passed mid-run."""


class StubNotApplied(Exception):
    """An adapter's stub did not take effect, so the real tool would have run.

    Adapters raise this instead of proceeding. PROTOCOL.md section 4: an
    execution that reached a live service is not evidence about the agent.
    """


def is_infrastructure_error(exc):
    if isinstance(exc, INFRASTRUCTURE_ERRORS):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500


class BudgetHandler(BaseCallbackHandler):
    """Counts tokens and steps, and aborts the run at the token ceiling.

    langchain-core's callback manager discards exceptions raised by a handler
    unless the handler sets raise_error, so the ceiling only bites with it set.
    """

    raise_error = True

    def __init__(self, token_ceiling):
        self.token_ceiling = token_ceiling
        self.input_tokens = 0
        self.output_tokens = 0
        self.steps = 0
        self.model_calls = 0

    @property
    def tokens(self):
        return self.input_tokens + self.output_tokens

    def on_chain_start(self, serialized, inputs, **kwargs):
        step = (kwargs.get("metadata") or {}).get("langgraph_step")
        if isinstance(step, int):
            self.steps = max(self.steps, step)

    def on_llm_end(self, response, **kwargs):
        self.model_calls += 1
        usage = _usage(response)
        self.input_tokens += usage[0]
        self.output_tokens += usage[1]
        if self.tokens > self.token_ceiling:
            raise TokenCeilingExceeded(
                f"{self.tokens} tokens used, ceiling is {self.token_ceiling}"
            )


def _usage(response):
    """(input, output) tokens from an LLMResult, whichever shape it arrives in."""
    for generations in getattr(response, "generations", None) or []:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if usage:
                return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    output = getattr(response, "llm_output", None) or {}
    usage = output.get("usage") or output.get("token_usage") or {}
    return (
        int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
        int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
    )


def load_cases(target, cases_dir="cases"):
    """Read cases/<target>.yaml and reject anything malformed before running."""
    path = os.path.join(cases_dir, f"{target}.yaml")
    if not os.path.exists(path):
        raise SystemExit(f"no case file at {path}")
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)

    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise SystemExit(f"{path}: expected a mapping with a 'cases' list")
    if document.get("target") != target:
        raise SystemExit(
            f"{path}: declares target {document.get('target')!r}, loaded as {target!r}"
        )

    cases = []
    seen = set()
    for index, case in enumerate(document["cases"]):
        where = f"{path}: case {index}"
        if not isinstance(case, dict):
            raise SystemExit(f"{where}: expected a mapping")
        for field in ("id", "category", "input", "expected_safe", "assertion"):
            if field not in case:
                raise SystemExit(f"{where}: missing {field!r}")
        if case["category"] not in CATEGORIES:
            raise SystemExit(
                f"{where}: unknown category {case['category']!r}; the five are "
                + ", ".join(CATEGORIES)
            )
        if case["id"] in seen:
            raise SystemExit(f"{where}: duplicate id {case['id']!r}")
        seen.add(case["id"])
        try:
            assertions.validate(case["assertion"], f"{path}: case {case['id']}")
        except ValueError as exc:
            raise SystemExit(str(exc))
        case.setdefault("setup", {})
        cases.append(case)
    return cases


def load_target(name):
    try:
        module = importlib.import_module(f"targets.{name}")
    except ModuleNotFoundError as exc:
        raise SystemExit(f"no adapter at targets/{name}.py ({exc})")
    for attribute in ("invoke", "METADATA"):
        if not hasattr(module, attribute):
            raise SystemExit(f"targets/{name}.py must expose {attribute}")
    return module


def check_stubs(target, cases, name):
    """Refuse the batch unless every declared side-effecting tool is stubbed.

    PROTOCOL.md section 4. This catches the case author who forgot; an adapter
    whose stub silently fails to apply is the adapter's job to catch, by raising
    StubNotApplied.
    """
    declared = target.METADATA.get("side_effecting_tools")
    if declared is None:
        raise SystemExit(
            f"targets/{name}.py: METADATA must declare side_effecting_tools "
            "(an empty list if the target has none)"
        )
    if not isinstance(declared, (list, tuple)):
        raise SystemExit(f"targets/{name}.py: side_effecting_tools must be a list")

    missing = []
    for case in cases:
        stubbed = set(((case.get("setup") or {}).get("tool_stubs") or {}))
        for tool in declared:
            if tool not in stubbed:
                missing.append((case["id"], tool))
    if missing:
        raise SystemExit(
            "refusing to run: side-effecting tools are not stubbed, so these "
            "executions could reach a live service (PROTOCOL.md section 4):\n"
            + "\n".join(f"  {case_id}: {tool}" for case_id, tool in missing)
        )


def trace_url(run_id, cfg):
    """(url, reason). A URL that cannot be resolved is recorded, not raised."""
    if not cfg["tracing"]:
        return None, "tracing disabled: LANGSMITH_TRACING is not true"
    if not cfg["langsmith_api_key_present"]:
        return None, "LANGSMITH_API_KEY not set"
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers
        from langsmith import Client

        wait_for_all_tracers()
        client = Client()
        for attempt in range(3):
            try:
                return client.read_run(str(run_id)).url, None
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1.0)
    except Exception as exc:  # recorded, never fatal: a missing link is not a result
        return None, f"{type(exc).__name__}: {exc}"


def execute_once(target, case, cfg, batch, attempt):
    """Run the target once. Returns the record that will be appended."""
    handler = BudgetHandler(cfg["token_ceiling"])
    run_id = uuid.uuid4()
    controllable = bool(target.METADATA.get("temperature_controllable"))
    config = {
        "recursion_limit": cfg["recursion_limit"],
        "callbacks": [handler],
        "run_id": run_id,
        "configurable": {
            "model": cfg["model"],
            "provider": cfg["provider"],
            "setup": case.get("setup") or {},
        },
    }
    if controllable:
        config["configurable"]["temperature"] = 0

    output, error, aborted_by = None, None, None
    started = time.monotonic()
    try:
        output = target.invoke(case["input"], config)
    except GraphRecursionError as exc:
        error, aborted_by = exc, "recursion_limit"
    except TokenCeilingExceeded as exc:
        error, aborted_by = exc, "token_ceiling"
    except Exception as exc:
        error = exc
    duration = time.monotonic() - started

    observation = {
        "completed": error is None,
        "output": output,
        "steps": handler.steps,
        "tokens": handler.tokens,
        "aborted_by": aborted_by,
    }
    url, url_error = trace_url(run_id, cfg)

    return {
        "record_type": "execution",
        "batch_id": batch["batch_id"],
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target": batch["target"],
        "case_id": case["id"],
        "category": case["category"],
        "attempt": attempt,
        "assertion": case["assertion"],
        "expected_safe": case["expected_safe"],
        "passed": assertions.check(case["assertion"], observation),
        "outcome": "ok" if error is None else (aborted_by or "exception"),
        "exception": None
        if error is None
        else {
            "class": type(error).__name__,
            "module": type(error).__module__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        },
        "infrastructure_error": error is not None and is_infrastructure_error(error),
        "output": assertions.serialize(output) if error is None else None,
        "steps": handler.steps,
        "model_calls": handler.model_calls,
        "tokens": {
            "input": handler.input_tokens,
            "output": handler.output_tokens,
            "total": handler.tokens,
        },
        "duration_s": round(duration, 3),
        "langsmith_run_id": str(run_id),
        "langsmith_url": url,
        "langsmith_url_error": url_error,
        "langsmith_project": cfg["langsmith_project"],
        "target_repo": target.METADATA.get("repo"),
        "target_sha": target.METADATA.get("sha"),
        "model": cfg["model"],
        "provider": cfg["provider"],
        "temperature": 0 if controllable else None,
        "temperature_controllable": controllable,
        "recursion_limit": cfg["recursion_limit"],
        "token_ceiling": cfg["token_ceiling"],
        "versions": batch["versions"],
    }


def append(path, record):
    """One line, flushed and fsynced, before the next execution starts."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run(target_name, case_id=None, results_dir="results", cases_dir="cases", env_path=".env"):
    cfg = env.config(env_path)
    target = load_target(target_name)
    cases = load_cases(target_name, cases_dir)
    if case_id is not None:
        cases = [case for case in cases if case["id"] == case_id]
        if not cases:
            raise SystemExit(f"no case {case_id!r} in cases/{target_name}.yaml")
    check_stubs(target, cases, target_name)

    batch = {
        "batch_id": str(uuid.uuid4()),
        "target": target_name,
        "versions": env.versions(),
    }
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, f"{target_name}.jsonl")

    print(f"batch {batch['batch_id']}  target {target_name}  model {cfg['model']}")
    print(f"sha {target.METADATA.get('sha')}  {len(cases)} case(s) x {RUNS_PER_CASE}")

    spent = 0
    for case in cases:
        for attempt in range(1, RUNS_PER_CASE + 1):
            if spent >= cfg["spend_ceiling_tokens"]:
                append(
                    path,
                    {
                        "record_type": "batch_stopped",
                        "batch_id": batch["batch_id"],
                        "target": target_name,
                        "timestamp_utc": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        "reason": "spend_ceiling_tokens",
                        "spend_ceiling_tokens": cfg["spend_ceiling_tokens"],
                        "tokens_spent": spent,
                        "stopped_at": {"case_id": case["id"], "attempt": attempt},
                    },
                )
                print(
                    f"\nstopped: SPEND_CEILING_TOKENS {cfg['spend_ceiling_tokens']} reached "
                    f"({spent} tokens). Incomplete cases are not publishable."
                )
                return 1
            record = execute_once(target, case, cfg, batch, attempt)
            append(path, record)
            spent += record["tokens"]["total"]
            mark = "pass" if record["passed"] else "FAIL"
            detail = record["outcome"]
            if record["infrastructure_error"]:
                detail += " (infrastructure)"
            print(f"  {case['id']} {attempt}/{RUNS_PER_CASE}  {mark}  {detail}")

    print(f"\nwrote {path}  tokens {spent}")
    return 0
