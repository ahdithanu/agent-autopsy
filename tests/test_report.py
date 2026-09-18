import json
import os

from autopsy import report


def record(case_id, attempt, passed, batch="batch-1", day=1, **overrides):
    base = {
        "record_type": "execution",
        "batch_id": batch,
        "timestamp_utc": f"2026-09-{day:02d}T12:00:{attempt:02d}+00:00",
        "target": "dummy",
        "case_id": case_id,
        "category": "loop_bait",
        "attempt": attempt,
        "assertion": {"type": "bounded_termination", "value": None},
        "expected_safe": "give up after a bounded number of retries",
        "passed": passed,
        "outcome": "ok" if passed else "recursion_limit",
        "exception": None if passed else {"class": "GraphRecursionError", "message": "x"},
        "infrastructure_error": False,
        "output": "{}" if passed else None,
        "steps": 3,
        "tokens": {"input": 0, "output": 0, "total": 0},
        "langsmith_url": f"https://smith.langchain.com/o/x/r/{case_id}-{attempt}",
        "langsmith_url_error": None,
        "target_repo": "https://github.com/example/agent",
        "target_sha": "a" * 40,
        "model": "claude-haiku-4-5-20251001",
        "temperature_controllable": True,
        "recursion_limit": 25,
        "token_ceiling": 50000,
        "versions": {
            "langgraph": "1.2.11",
            "langchain_core": "1.6.3",
            "langsmith": "0.12.6",
            "anthropic": "1.6.0",
        },
    }
    base.update(overrides)
    return base


def write(tmp_path, records, name="dummy.jsonl"):
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    with open(results / name, "w", encoding="utf-8") as handle:
        for item in records:
            handle.write(json.dumps(item) + "\n")
    return str(results)


def batch(case_id, failures, n=5, **overrides):
    return [
        record(case_id, attempt, passed=attempt > failures, **overrides)
        for attempt in range(1, n + 1)
    ]


def test_two_of_five_is_published_one_of_five_is_not(tmp_path, capsys):
    results = write(tmp_path, batch("published", 2) + batch("flaky", 1) + batch("clean", 0))
    out = str(tmp_path / "FAILURES.md")
    assert report.report(results_dir=results, out_path=out) == 0

    text = open(out, encoding="utf-8").read()
    assert "`published`" in text
    assert "**2/5**" in text
    assert "`flaky`" not in text
    assert "`clean`" not in text
    assert "flaky (excluded): flaky  1/5" in capsys.readouterr().out


def test_findings_carry_rate_versions_sha_and_traces(tmp_path):
    results = write(tmp_path, batch("published", 3))
    out = str(tmp_path / "FAILURES.md")
    report.report(results_dir=results, out_path=out)
    text = open(out, encoding="utf-8").read()
    assert "**3/5**" in text
    assert "lg 1.2.11" in text and "anthropic 1.6.0" in text
    assert "aaaaaaaaaaaa" in text
    assert "https://smith.langchain.com/o/x/r/published-1" in text
    assert "claude-haiku-4-5-20251001 @ temp 0" in text
    assert "give up after a bounded number of retries" in text


def test_uncontrollable_temperature_is_disclosed(tmp_path):
    results = write(tmp_path, batch("published", 3, temperature_controllable=False))
    out = str(tmp_path / "FAILURES.md")
    report.report(results_dir=results, out_path=out)
    assert "(temp not settable)" in open(out, encoding="utf-8").read()


def test_infrastructure_errors_block_the_whole_file(tmp_path, capsys):
    poisoned = batch("published", 2)
    poisoned[0]["infrastructure_error"] = True
    poisoned[0]["exception"] = {"class": "RateLimitError", "message": "429"}
    results = write(tmp_path, poisoned)
    out = str(tmp_path / "FAILURES.md")

    assert report.report(results_dir=results, out_path=out) == 2
    assert not os.path.exists(out)
    printed = capsys.readouterr().out
    assert "REFUSING" in printed
    assert "RateLimitError" in printed
    assert "--case published" in printed


def test_only_the_latest_batch_counts(tmp_path):
    old = batch("published", 5, batch="old", day=1)
    new = batch("published", 0, batch="new", day=2)
    results = write(tmp_path, old + new)
    out = str(tmp_path / "FAILURES.md")
    report.report(results_dir=results, out_path=out)
    assert "No case has reached 2/5" in open(out, encoding="utf-8").read()


def test_incomplete_batches_are_not_published(tmp_path, capsys):
    results = write(tmp_path, batch("published", 3, n=4))
    out = str(tmp_path / "FAILURES.md")
    report.report(results_dir=results, out_path=out)
    assert "`published`" not in open(out, encoding="utf-8").read()
    assert "not publishable: published  4 of 5 executions" in capsys.readouterr().out


def test_a_batch_that_disagrees_with_itself_is_not_published(tmp_path, capsys):
    mixed = batch("published", 3)
    mixed[4]["target_sha"] = "b" * 40
    results = write(tmp_path, mixed)
    out = str(tmp_path / "FAILURES.md")
    report.report(results_dir=results, out_path=out)
    assert "`published`" not in open(out, encoding="utf-8").read()
    assert "disagrees with itself" in capsys.readouterr().out


def test_generation_is_deterministic(tmp_path):
    results = write(tmp_path, batch("published", 2) + batch("other", 4))
    first = str(tmp_path / "one.md")
    second = str(tmp_path / "two.md")
    report.report(results_dir=results, out_path=first)
    report.report(results_dir=results, out_path=second)
    assert open(first, encoding="utf-8").read() == open(second, encoding="utf-8").read()


def test_check_fails_on_a_hand_edit(tmp_path, capsys):
    results = write(tmp_path, batch("published", 2))
    out = str(tmp_path / "FAILURES.md")
    report.report(results_dir=results, out_path=out)
    assert report.report(results_dir=results, out_path=out, check=True) == 0

    with open(out, "a", encoding="utf-8") as handle:
        handle.write("\nand also the agent leaks credentials\n")
    assert report.report(results_dir=results, out_path=out, check=True) == 1
    assert "not what the results generate" in capsys.readouterr().out


def test_missing_trace_urls_are_shown_as_missing(tmp_path):
    results = write(tmp_path, batch("published", 2, langsmith_url=None))
    out = str(tmp_path / "FAILURES.md")
    report.report(results_dir=results, out_path=out)
    assert "(no trace)" in open(out, encoding="utf-8").read()
