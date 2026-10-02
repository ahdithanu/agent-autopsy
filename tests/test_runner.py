import json
import os

import pytest

from autopsy import runner


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """Bounds set, tracing off, results written somewhere disposable."""
    monkeypatch.setenv("MODEL", "dummy-model")
    monkeypatch.setenv("PROVIDER", "none")
    monkeypatch.setenv("RECURSION_LIMIT", "12")
    monkeypatch.setenv("TOKEN_CEILING", "1000")
    monkeypatch.setenv("SPEND_CEILING_TOKENS", "10000")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    return {"results_dir": str(tmp_path / "results"), "env_path": str(tmp_path / "absent.env")}


def read(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_missing_bounds_refuse_to_start(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL", "dummy-model")
    monkeypatch.setenv("PROVIDER", "none")
    monkeypatch.delenv("RECURSION_LIMIT", raising=False)
    monkeypatch.delenv("TOKEN_CEILING", raising=False)
    monkeypatch.delenv("SPEND_CEILING_TOKENS", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        runner.run("dummy", results_dir=str(tmp_path), env_path=str(tmp_path / "absent.env"))
    assert "RECURSION_LIMIT" in str(exit_info.value)


def test_malformed_output_case_records_five_executions(configured):
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    records = read(os.path.join(configured["results_dir"], "dummy.jsonl"))
    assert len(records) == 5
    assert [r["attempt"] for r in records] == [1, 2, 3, 4, 5]
    assert all(r["outcome"] == "ok" for r in records)
    # The planted bug: the empty value reaches user-facing text every time.
    assert all(r["passed"] is False for r in records)
    assert all("The value is None" in r["output"] for r in records)


def test_loop_bait_case_is_stopped_by_the_recursion_limit(configured):
    runner.run("dummy", case_id="reshaped_tool_output_retries_forever", **configured)
    records = read(os.path.join(configured["results_dir"], "dummy.jsonl"))
    assert all(r["outcome"] == "recursion_limit" for r in records)
    assert all(r["passed"] is False for r in records)
    assert all(r["exception"]["class"] == "GraphRecursionError" for r in records)
    assert all(r["exception"]["traceback"] for r in records)
    assert all(r["infrastructure_error"] is False for r in records)


def test_every_execution_records_its_provenance(configured):
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    record = read(os.path.join(configured["results_dir"], "dummy.jsonl"))[0]
    assert record["target_sha"]
    assert record["model"] == "dummy-model"
    assert record["recursion_limit"] == 12
    assert record["token_ceiling"] == 1000
    assert record["temperature_controllable"] is False
    assert record["temperature"] is None
    assert record["versions"]["langgraph"]
    assert record["versions"]["langchain_core"]
    assert record["langsmith_url"] is None
    assert "tracing disabled" in record["langsmith_url_error"]


def test_results_are_appended_never_overwritten(configured):
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    records = read(os.path.join(configured["results_dir"], "dummy.jsonl"))
    assert len(records) == 10
    assert len({r["batch_id"] for r in records}) == 2


def test_an_unexpected_exception_is_recorded_as_the_outcome(configured, monkeypatch):
    def explode(payload, config):
        raise ValueError("adapter blew up")

    module = runner.load_target("dummy")
    monkeypatch.setattr(module, "invoke", explode)
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    records = read(os.path.join(configured["results_dir"], "dummy.jsonl"))
    assert all(r["outcome"] == "exception" for r in records)
    assert all(r["exception"]["class"] == "ValueError" for r in records)
    assert all(r["exception"]["message"] == "adapter blew up" for r in records)


def test_infrastructure_errors_are_flagged(configured, monkeypatch):
    import httpx
    import anthropic

    def rate_limited(payload, config):
        raise anthropic.RateLimitError(
            "429",
            response=httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com")),
            body=None,
        )

    module = runner.load_target("dummy")
    monkeypatch.setattr(module, "invoke", rate_limited)
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    records = read(os.path.join(configured["results_dir"], "dummy.jsonl"))
    assert all(r["infrastructure_error"] is True for r in records)


def test_spend_ceiling_stops_the_batch(configured, monkeypatch):
    monkeypatch.setenv("SPEND_CEILING_TOKENS", "150")

    def burns_tokens(payload, config):
        """Stand-in for a target that actually spends, since the dummy spends nothing."""
        for handler in config["callbacks"]:
            handler.input_tokens += 100
        return {"answer": "ok"}

    module = runner.load_target("dummy")
    monkeypatch.setattr(module, "invoke", burns_tokens)
    exit_code = runner.run("dummy", **configured)
    records = read(os.path.join(configured["results_dir"], "dummy.jsonl"))
    assert exit_code == 1
    assert records[-1]["record_type"] == "batch_stopped"
    assert records[-1]["reason"] == "spend_ceiling_tokens"
    assert len([r for r in records if r["record_type"] == "execution"]) < 10


def test_case_validation_rejects_unknown_categories(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "dummy.yaml").write_text(
        "target: dummy\ncases:\n"
        "  - id: x\n    category: made_up\n    input: {}\n"
        "    expected_safe: text\n    assertion: {type: no_exception}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exit_info:
        runner.load_cases("dummy", str(cases_dir))
    assert "unknown category" in str(exit_info.value)


def test_case_validation_rejects_duplicate_ids(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    case = (
        "  - id: x\n    category: loop_bait\n    input: {}\n"
        "    expected_safe: text\n    assertion: {type: bounded_termination}\n"
    )
    (cases_dir / "dummy.yaml").write_text("target: dummy\ncases:\n" + case + case, encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        runner.load_cases("dummy", str(cases_dir))
    assert "duplicate id" in str(exit_info.value)


def test_an_unstubbed_side_effecting_tool_refuses_the_batch(configured, monkeypatch):
    module = runner.load_target("dummy")
    monkeypatch.setattr(
        module, "METADATA", dict(module.METADATA, side_effecting_tools=["send_email"])
    )
    with pytest.raises(SystemExit) as exit_info:
        runner.run("dummy", **configured)
    message = str(exit_info.value)
    assert "send_email" in message
    assert "live service" in message
    assert not os.path.exists(os.path.join(configured["results_dir"], "dummy.jsonl"))


def test_a_stubbed_side_effecting_tool_is_allowed(configured, monkeypatch):
    module = runner.load_target("dummy")
    monkeypatch.setattr(
        module, "METADATA", dict(module.METADATA, side_effecting_tools=["lookup"])
    )
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    assert len(read(os.path.join(configured["results_dir"], "dummy.jsonl"))) == 5


def test_an_undeclared_target_refuses_the_batch(configured, monkeypatch):
    module = runner.load_target("dummy")
    stripped = {k: v for k, v in module.METADATA.items() if k != "side_effecting_tools"}
    monkeypatch.setattr(module, "METADATA", stripped)
    with pytest.raises(SystemExit) as exit_info:
        runner.run("dummy", **configured)
    assert "must declare side_effecting_tools" in str(exit_info.value)


def test_a_stub_that_did_not_apply_is_recorded_not_published(configured, monkeypatch):
    """The pattern a real adapter follows when its monkeypatch misses."""

    def stub_missed(payload, config):
        raise runner.StubNotApplied("targets.agent.send_email is still the real tool")

    module = runner.load_target("dummy")
    monkeypatch.setattr(module, "invoke", stub_missed)
    runner.run("dummy", case_id="null_value_reaches_user", **configured)
    records = read(os.path.join(configured["results_dir"], "dummy.jsonl"))
    assert all(r["outcome"] == "exception" for r in records)
    assert all(r["exception"]["class"] == "StubNotApplied" for r in records)
    assert all(r["passed"] is False for r in records)


def test_the_shipped_dummy_declares_its_side_effects():
    assert runner.load_target("dummy").METADATA["side_effecting_tools"] == []


def test_shipped_cases_load():
    cases = runner.load_cases("dummy")
    assert {case["id"] for case in cases} == {
        "null_value_reaches_user",
        "reshaped_tool_output_retries_forever",
    }
    assert all(case["category"] in runner.CATEGORIES for case in cases)
