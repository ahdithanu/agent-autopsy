"""Target: the deep_research example from langchain-ai/deepagents.

The agent under test is `examples/deep_research/agent.py` at the commit pinned
below, which is the commit tagged `deepagents==0.7.39`. The harness installs
that release of the library from PyPI and reads the example from a checkout of
the same commit, so one SHA names both.

What this adapter changes, and nothing else:

  * The model. `agent.py` hardcodes one. The adapter repeats its
    `create_deep_agent(...)` call with the example's own prompt, tools and
    sub-agent, and the model named in `.env`, at temperature 0. To test the
    example exactly as shipped, set MODEL to the model in its `agent.py`.
  * The network. `tavily_search` asks Tavily for URLs and then fetches each
    page over HTTP. Both clients are replaced where the tool looks them up, in
    `research_agent.tools`. The tool's own code still runs: its formatting, and
    its handling of a failed fetch, are part of what is under test.

Stub format, under `setup.tool_stubs.tavily_search` in a case:

    results:              # what Tavily returns for every query, in order
      - url: https://example.com/a
        title: A page
        body: "<p>what a GET of that URL returns</p>"   # default ""
        status: 200                                     # default 200
    search_error: "..."   # optional: the Tavily client raises this instead

The tool asks Tavily for `max_results` results, which the model cannot set and
which defaults to 1, so only the first result is normally used. A `status` of
400 or above makes the fetch fail the way httpx would.

`setup.state` is merged into the input. A `files` entry may map a path to plain
text; it is converted to the file record the agent's filesystem expects.

Every other tool the agent has (`ls`, `read_file`, `write_file`, `edit_file`,
`delete`, `glob`, `grep`, `task`, `think_tool`) works on LangGraph state inside
this process, and `execute` refuses without a sandbox backend. The adapter
checks that tool list each time it builds the agent, so a tool it has not
accounted for stops the run.

RECURSION_LIMIT does not reach sub-agents at this commit. Deep Agents binds
its own limit of 9,999 to each sub-agent graph and that binding wins over the
caller's, so the limit from `.env` applies to the top-level agent only. A loop
inside a sub-agent is stopped by TOKEN_CEILING, which does reach it. Both
behaviors are pinned by tests/test_deepagents_deep_research.py.

Install the checkout with `python -m targets.deepagents_deep_research`.
"""

import importlib.util
import os
import subprocess
import sys

import httpx

from autopsy.runner import StubNotApplied

REPO = "https://github.com/langchain-ai/deepagents"
SHA = "53e090968118f72f6dd3847d097607461a0fb5a8"  # tag deepagents==0.7.39
DEEPAGENTS_VERSION = "0.7.39"
EXAMPLE = "examples/deep_research"

# Where the pinned checkout lives. Outside this repository on purpose: it is a
# copy of someone else's code, not evidence, and is rebuilt from the SHA.
CHECKOUT = os.path.join(
    os.environ.get("AUTOPSY_TARGETS_DIR") or os.path.expanduser("~/.cache/agent-autopsy-targets"),
    "deepagents",
)

# Every tool the built agent may have. Anything else stops the run.
EXPECTED_TOOLS = frozenset(
    {
        "ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep",
        "execute", "task", "tavily_search", "think_tool",
    }
)  # fmt: skip

METADATA = {
    "repo": REPO,
    "sha": SHA,
    "path": EXAMPLE,
    "install": "pip install -r requirements.txt && python -m targets.deepagents_deep_research",
    "temperature_controllable": True,
    # PROTOCOL.md section 4. tavily_search calls Tavily and then fetches pages.
    "side_effecting_tools": ["tavily_search"],
    # Recorded with every execution, next to the harness's own versions.
    "packages": ["deepagents", "langchain", "langchain-anthropic", "tavily-python", "markdownify"],
}


# ------------------------------------------------------------------ checkout

def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=CHECKOUT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _checkout_sha():
    if not os.path.isdir(os.path.join(CHECKOUT, ".git")):
        return None
    try:
        return _git("rev-parse", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def fetch():
    """Put the pinned commit at CHECKOUT. Safe to run again."""
    if _checkout_sha() == SHA:
        print(f"{CHECKOUT} is already at {SHA}")
        return
    os.makedirs(CHECKOUT, exist_ok=True)
    if not os.path.isdir(os.path.join(CHECKOUT, ".git")):
        _git("init", "-q")
        _git("remote", "add", "origin", f"{REPO}.git")
    _git("fetch", "-q", "--depth", "1", "origin", SHA)
    _git("checkout", "-q", "--detach", "FETCH_HEAD")
    print(f"{CHECKOUT} is at {_checkout_sha()}")


# --------------------------------------------------------------------- stubs

class _Network:
    """Stands in for both network clients `tavily_search` uses.

    `search` is the one method the tool calls on the Tavily client, and `get`
    the one function it calls on httpx. A URL the case did not stub is recorded
    in `unstubbed`, because the tool turns any fetch error into ordinary text
    and raising here would be swallowed.
    """

    def __init__(self):
        self.spec = None
        self.unstubbed = []

    def arm(self, spec):
        self.spec = spec
        self.unstubbed = []

    def _results(self):
        return (self.spec or {}).get("results") or []

    def search(self, query, max_results=1, **_ignored):
        if self.spec is None:
            self.unstubbed.append(f"search: {query}")
            raise RuntimeError("tavily_search was called with no stub armed")
        if self.spec.get("search_error") is not None:
            raise RuntimeError(str(self.spec["search_error"]))
        return {
            "query": query,
            "results": [
                {"url": r["url"], "title": r.get("title", "")} for r in self._results()
            ][:max_results],
        }

    def get(self, url, **_ignored):
        for result in self._results():
            if result["url"] == url:
                return httpx.Response(
                    int(result.get("status", 200)),
                    text=str(result.get("body", "")),
                    request=httpx.Request("GET", url),
                )
        self.unstubbed.append(url)
        raise RuntimeError(f"no stub for {url}")


_NETWORK = _Network()


# ------------------------------------------------------------------- example

def _load_example():
    """Import the example at the pinned commit and put the stubs in place."""
    if _checkout_sha() != SHA:
        raise SystemExit(
            f"targets/deepagents_deep_research.py: no checkout of {SHA} at {CHECKOUT}\n"
            "run: python -m targets.deepagents_deep_research"
        )
    from importlib import metadata

    try:
        installed = metadata.version("deepagents")
    except metadata.PackageNotFoundError:
        installed = None
    if installed != DEEPAGENTS_VERSION:
        raise SystemExit(
            f"targets/deepagents_deep_research.py: deepagents {installed} is installed, the "
            f"pinned commit is {DEEPAGENTS_VERSION}\nrun: pip install -r requirements.txt"
        )

    example_dir = os.path.join(CHECKOUT, EXAMPLE)
    # The example builds a Tavily client at import and that needs a key to be
    # set. It gets a value that is not one: the client is replaced below, and a
    # call that slipped past the stub could not authenticate.
    real_key = os.environ.get("TAVILY_API_KEY")
    os.environ["TAVILY_API_KEY"] = "not-a-key-stubbed-by-agent-autopsy"
    sys.path.insert(0, example_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            "autopsy_deep_research_example", os.path.join(example_dir, "agent.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import research_agent.tools as tools
    finally:
        sys.path.remove(example_dir)
        if real_key is None:
            del os.environ["TAVILY_API_KEY"]
        else:
            os.environ["TAVILY_API_KEY"] = real_key

    tools.tavily_client = _NETWORK
    tools.httpx = _NETWORK
    return module, tools


def _check_stubbed():
    """Raise unless the tool will reach the stub rather than the network."""
    seen = _EXAMPLE.tavily_search.func.__globals__
    for name in ("tavily_client", "httpx"):
        if seen.get(name) is not _NETWORK or getattr(_TOOLS, name, None) is not _NETWORK:
            raise StubNotApplied(f"research_agent.tools.{name} is not the stub")


def _model(provider, model, temperature):
    from langchain.chat_models import init_chat_model

    return init_chat_model(f"{provider}:{model}", temperature=temperature)


_AGENTS = {}


def _agent(provider, model, temperature):
    """The example's own `create_deep_agent(...)` call, with the model swapped."""
    key = (provider, model, temperature)
    if key not in _AGENTS:
        from deepagents import create_deep_agent

        agent = create_deep_agent(
            model=_model(provider, model, temperature),
            tools=[_EXAMPLE.tavily_search, _EXAMPLE.think_tool],
            system_prompt=_EXAMPLE.INSTRUCTIONS,
            subagents=[_EXAMPLE.research_sub_agent],
        )
        tools = set(agent.nodes["tools"].bound.tools_by_name)
        if not tools <= EXPECTED_TOOLS:
            raise StubNotApplied(
                f"the agent has tools this adapter has not accounted for: {sorted(tools - EXPECTED_TOOLS)}"
            )
        _AGENTS[key] = agent
    return _AGENTS[key]


# -------------------------------------------------------------------- invoke

def _text(message):
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
    )


def _output(result):
    """What a user of the agent would see: its last reply and the files it left.

    The transcript is left out on purpose. It contains the stubbed tool output,
    so a substring assertion over it would match the case's own stub.
    """
    replies = [m for m in result.get("messages") or [] if getattr(m, "type", None) == "ai"]
    files = {}
    for path, data in (result.get("files") or {}).items():
        content = data.get("content") if isinstance(data, dict) else data
        files[path] = "\n".join(content) if isinstance(content, list) else content
    return {"answer": _text(replies[-1]) if replies else "", "files": files}


def invoke(payload, config):
    """Entry point the runner calls."""
    configurable = config.get("configurable") or {}
    setup = configurable.get("setup") or {}
    stub = (setup.get("tool_stubs") or {}).get("tavily_search")
    if not isinstance(stub, dict):
        raise StubNotApplied("the case has no mapping at setup.tool_stubs.tavily_search")
    _check_stubbed()
    _NETWORK.arm(stub)

    state = dict(payload)
    injected = dict(setup.get("state") or {})
    if isinstance(injected.get("files"), dict):
        from deepagents.backends.utils import create_file_data

        injected["files"] = {
            path: create_file_data(data) if isinstance(data, str) else data
            for path, data in injected["files"].items()
        }
    state.update(injected)

    agent = _agent(configurable["provider"], configurable["model"], configurable.get("temperature", 0))
    try:
        result = agent.invoke(state, config)
    except Exception as exc:
        if _NETWORK.unstubbed:
            raise StubNotApplied(f"the agent reached past the stub: {_NETWORK.unstubbed}") from exc
        raise
    finally:
        leaked = list(_NETWORK.unstubbed)
        _NETWORK.arm(None)
    if leaked:
        raise StubNotApplied(f"the agent reached past the stub: {leaked}")
    return _output(result)


if __name__ == "__main__":
    fetch()
else:
    _EXAMPLE, _TOOLS = _load_example()
