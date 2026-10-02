"""Dummy target: a deliberately buggy three-node LangGraph agent.

It exists to exercise the harness, and nothing else. It calls no model, so there
is no temperature to set and no tokens to count. Two bugs are planted in it on
purpose, one per example case:

  * `answer` interpolates the tool's value straight into user-facing text, so a
    tool that returns `{"value": None}` produces "the value is None".
  * `validate` retries forever when the tool's shape is wrong, with no attempt
    cap, so a tool that never returns a `value` key loops until LangGraph's
    recursion limit stops it.

No finding about this module is ever published. Real adapters are thin: they
import the agent under test at a pinned SHA and hand `invoke` straight to it.
"""

import os
import subprocess
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sha():
    """This repository's HEAD. A real adapter hardcodes the target's pinned SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


METADATA = {
    "repo": None,  # defined in this repository, not an external agent
    "sha": _sha(),
    "install": "pip install -r requirements.txt",
    "temperature_controllable": False,
    # PROTOCOL.md section 4. Required, [] when the target touches nothing
    # outside the process. The `lookup` tool here only returns the stub.
    "side_effecting_tools": [],
    "note": "harness fixture, deliberately buggy; never publish findings about it",
}


class State(TypedDict):
    request: str
    tool_result: Any
    answer: str
    attempts: int


def call_tool(state: State, config) -> dict:
    """Call the `lookup` tool. The harness stubs its return value."""
    stubs = (config.get("configurable") or {}).get("setup", {}).get("tool_stubs") or {}
    return {
        "tool_result": stubs.get("lookup", {"value": "unstubbed"}),
        "attempts": state["attempts"] + 1,
    }


def validate(state: State) -> str:
    """Retry until the tool returns the right shape. No attempt cap: that is bug 2."""
    result = state["tool_result"]
    if isinstance(result, dict) and "value" in result:
        return "answer"
    return "call_tool"


def answer(state: State) -> dict:
    """Interpolate the value into user-facing text without checking it: bug 1."""
    return {"answer": f"Lookup complete. The value is {state['tool_result']['value']}."}


def _build():
    graph = StateGraph(State)
    graph.add_node("call_tool", call_tool)
    graph.add_node("answer", answer)
    graph.add_edge(START, "call_tool")
    graph.add_conditional_edges("call_tool", validate, ["call_tool", "answer"])
    graph.add_edge("answer", END)
    return graph.compile()


GRAPH = _build()


def invoke(payload, config):
    """Entry point the runner calls. Injected state from the case is applied here.

    This fixture reads its stub inside the node, so there is nothing to verify.
    An adapter that stubs a real agent's tools by replacing module attributes
    must check that the replacement took effect and raise
    autopsy.runner.StubNotApplied if it did not, rather than letting the live
    tool run.
    """
    setup = (config.get("configurable") or {}).get("setup") or {}
    state: State = {
        "request": payload.get("request", ""),
        "tool_result": None,
        "answer": "",
        "attempts": 0,
    }
    state.update(setup.get("state") or {})
    return GRAPH.invoke(state, config)
