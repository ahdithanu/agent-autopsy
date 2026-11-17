"""The deepagents adapter, driven by a scripted model with the network cut.

These say nothing about how a real model behaves. They check what the harness
is responsible for: the stub is what the tool reaches, a run that gets past it
is not recorded as evidence, and the bounds from `.env` are the ones in force.
"""

import json
import os
import socket

import pytest

pytest.importorskip("deepagents")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from autopsy import runner  # noqa: E402

try:
    import targets.deepagents_deep_research as target  # noqa: E402
except SystemExit as missing:  # no checkout of the pinned commit on this machine
    pytest.skip(str(missing), allow_module_level=True)

USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
PAGE = {
    "results": [
        {"url": "https://example.com/a", "title": "A page", "body": "<p>X is <b>forty-two</b>.</p>"}
    ]
}


class Scripted(BaseChatModel):
    """Replies from a script. `script(n, messages)` returns the nth reply."""

    script: object = None
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self):
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.script(len(self.seen), messages))])


def call(name, args, n):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{n}"}], usage_metadata=USAGE)


def say(text):
    return AIMessage(content=text, usage_metadata=USAGE)


def last_tool_output(messages):
    return [m for m in messages if m.type == "tool"][-1].content


@pytest.fixture
def scripted(monkeypatch):
    """Install a scripted model and make any real connection an error."""

    def refuse(*args, **kwargs):
        raise AssertionError("the adapter let a network connection through")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(target, "_AGENTS", {})

    def install(script):
        model = Scripted(script=script, seen=[])
        monkeypatch.setattr(target, "_model", lambda provider, name, temperature: model)
        return model

    return install


def config(stub=PAGE, state=None, limit=25, ceiling=10**9):
    handler = runner.BudgetHandler(ceiling)
    setup = {"tool_stubs": {"tavily_search": stub} if stub is not None else {}, "state": state or {}}
    return handler, {
        "recursion_limit": limit,
        "callbacks": [handler],
        "configurable": {"model": "scripted", "provider": "none", "temperature": 0, "setup": setup},
    }


QUESTION = {"messages": [{"role": "user", "content": "What is X?"}]}


def test_the_tool_reads_the_stub_through_its_own_code(scripted):
    model = scripted(lambda n, _: call("tavily_search", {"query": "X"}, n) if n == 1 else say("X is forty-two."))
    _, cfg = config()
    assert target.invoke(QUESTION, cfg) == {"answer": "X is forty-two.", "files": {}}
    seen = last_tool_output(model.seen[1])
    # The example's formatting and its HTML-to-markdown step both ran.
    assert "Found 1 result(s) for 'X'" in seen
    assert "X is **forty-two**." in seen


def test_a_failed_fetch_reaches_the_agent_as_the_tools_own_error_text(scripted):
    model = scripted(lambda n, _: call("tavily_search", {"query": "X"}, n) if n == 1 else say("done"))
    _, cfg = config({"results": [{"url": "https://example.com/a", "title": "A", "status": 503}]})
    target.invoke(QUESTION, cfg)
    assert "Error fetching content from https://example.com/a" in last_tool_output(model.seen[1])


def test_a_case_without_the_stub_is_not_run(scripted):
    scripted(lambda n, _: say("x"))
    _, cfg = config(stub=None)
    with pytest.raises(runner.StubNotApplied):
        target.invoke(QUESTION, cfg)


def test_a_stub_that_was_displaced_is_caught_before_the_agent_runs(scripted, monkeypatch):
    model = scripted(lambda n, _: say("x"))
    monkeypatch.setattr(target._TOOLS, "tavily_client", object())
    _, cfg = config()
    with pytest.raises(runner.StubNotApplied):
        target.invoke(QUESTION, cfg)
    assert model.seen == []


def test_a_url_the_case_did_not_stub_voids_the_execution(scripted, monkeypatch):
    """The tool turns a fetch error into ordinary text, so the run would look
    normal. The adapter has to notice and say the stub was bypassed."""
    scripted(lambda n, _: call("tavily_search", {"query": "X"}, n) if n == 1 else say("done"))
    real_search = target._NETWORK.search

    def elsewhere(query, **kwargs):
        real_search(query, **kwargs)
        return {"results": [{"url": "https://not-in-the-case.example/", "title": "?"}]}

    monkeypatch.setattr(target._NETWORK, "search", elsewhere)
    _, cfg = config()
    with pytest.raises(runner.StubNotApplied) as raised:
        target.invoke(QUESTION, cfg)
    assert "not-in-the-case.example" in str(raised.value)


def test_the_recursion_limit_from_env_stops_the_top_level_agent(scripted):
    """Deep Agents binds 9,999 to its graph. The caller's limit has to win."""
    from langgraph.errors import GraphRecursionError

    scripted(lambda n, _: call("think_tool", {"reflection": "again"}, n))
    handler, cfg = config(limit=12)
    with pytest.raises(GraphRecursionError):
        target.invoke(QUESTION, cfg)
    assert handler.steps == 12


def loops_inside_a_sub_agent(n, _):
    if n == 1:
        return call("task", {"description": "research X", "subagent_type": "research-agent"}, n)
    return call("think_tool", {"reflection": "again"}, n) if n < 60 else say("gave up")


def test_the_recursion_limit_does_not_reach_a_sub_agent(scripted):
    """Recorded, not endorsed: at this commit a sub-agent keeps its own limit,
    so 12 does not stop a loop inside one. If this starts failing, the adapter's
    docstring is out of date."""
    scripted(loops_inside_a_sub_agent)
    handler, cfg = config(limit=12)
    target.invoke(QUESTION, cfg)
    assert handler.steps > 12
    assert handler.model_calls == 61


def test_the_token_ceiling_does_reach_a_sub_agent(scripted):
    scripted(loops_inside_a_sub_agent)
    handler, cfg = config(limit=10**6, ceiling=100)
    with pytest.raises(runner.TokenCeilingExceeded):
        target.invoke(QUESTION, cfg)
    assert handler.model_calls == 7


def test_injected_files_are_readable_and_written_files_are_in_the_output(scripted):
    script = {
        1: call("read_file", {"file_path": "/notes.md"}, 1),
        2: call("write_file", {"file_path": "/final_report.md", "content": "X is forty-two."}, 2),
        3: say("Report written."),
    }
    model = scripted(lambda n, _: script[n])
    _, cfg = config(state={"files": {"/notes.md": "old note: X is seven"}})
    output = target.invoke(QUESTION, cfg)
    assert "old note: X is seven" in last_tool_output(model.seen[1])
    assert output["files"]["/final_report.md"] == "X is forty-two."


def test_a_batch_through_the_runner_records_the_target(scripted, monkeypatch, tmp_path):
    for key, value in {
        "MODEL": "scripted",
        "PROVIDER": "none",
        "RECURSION_LIMIT": "25",
        "TOKEN_CEILING": "1000",
        "SPEND_CEILING_TOKENS": "100000",
        "LANGSMITH_TRACING": "false",
    }.items():
        monkeypatch.setenv(key, value)
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "deepagents_deep_research.yaml").write_text(
        "target: deepagents_deep_research\ncases:\n"
        "  - id: harness_check\n    category: malformed_tool_output\n"
        "    input: {messages: [{role: user, content: 'What is X?'}]}\n"
        "    setup: {tool_stubs: {tavily_search: {results: [{url: 'https://example.com/a', title: A, body: '<p>X</p>'}]}}}\n"
        "    expected_safe: text\n    assertion: {type: output_contains, value: forty-two}\n",
        encoding="utf-8",
    )
    model = scripted(lambda n, _: call("tavily_search", {"query": "X"}, n) if n % 2 else say("X is forty-two."))
    results = str(tmp_path / "results")
    runner.run(
        "deepagents_deep_research", results_dir=results, cases_dir=str(cases_dir),
        env_path=str(tmp_path / "absent.env"),
    )  # fmt: skip
    with open(os.path.join(results, "deepagents_deep_research.jsonl"), encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle]

    assert len(records) == 5 and len(model.seen) == 10
    assert all(r["passed"] and r["outcome"] == "ok" for r in records)
    assert all(r["target_sha"] == target.SHA and r["target_repo"] == target.REPO for r in records)
    assert all(r["temperature"] == 0 and r["temperature_controllable"] is True for r in records)
    assert all(r["fixture"] is False for r in records)
    assert all(r["tokens"] == {"input": 20, "output": 10, "total": 30, "unmetered_model_calls": 0} for r in records)
    assert records[0]["versions"]["deepagents"] == target.DEEPAGENTS_VERSION
    assert records[0]["versions"]["langchain"]


def test_the_shipped_case_file_loads_and_the_adapter_declares_its_tool():
    assert runner.load_cases("deepagents_deep_research") == []
    assert target.METADATA["side_effecting_tools"] == ["tavily_search"]
