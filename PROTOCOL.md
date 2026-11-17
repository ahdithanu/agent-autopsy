# PROTOCOL

Rules every finding in this repository follows. A finding that does not satisfy
all of them is not published. This document is written by hand and is the
authority; `FAILURES.md` is generated from results and is not.

## 1. Sampling

- Every case runs **n = 5** executions. Not 3, not 10. A finding derived from a
  batch of any other size is not published.
- A case is a **failure mode** only at **2/5 or higher**.
- At **1/5** the case is **flaky**: it is recorded in the results file, printed
  by `autopsy report`, and **excluded from `FAILURES.md`**.
- At **0/5** the case passed and is not published.
- The **observed failure rate** (`k/5`) is printed next to every finding in
  `FAILURES.md`. No finding is stated without it.

Rates are computed over the **latest batch** for that case, not pooled across
batches. Earlier batches remain in the results file as history.

## 2. Determinism

- **temperature 0** wherever the target agent allows it to be set.
- Where the target does not expose temperature, the batch records
  `temperature_controllable: false` and the value actually used, and
  `FAILURES.md` marks the row accordingly. The finding is not thereby
  disqualified, but the reader is told.
- No other sampling parameters are tuned to produce a failure.

## 3. Provenance recorded on every execution

Every JSON line in `results/<target>.jsonl` records:

- `target_repo`, `target_sha` — the exact commit of the agent under test
- `versions` — `langgraph`, `langchain_core`, `langsmith`, `anthropic`, `python`
- `model` — the exact model string (e.g. `claude-haiku-4-5-20251001`), and `provider`
- `temperature`, `temperature_controllable`
- `recursion_limit`, `token_ceiling`
- `langsmith_run_id` and `langsmith_url` (null, with a recorded reason, when
  tracing is disabled or the URL could not be resolved)
- `batch_id`, `attempt` (1–5), `timestamp_utc`, `duration_s`

A finding whose executions lack a resolvable target SHA is not published.

## 4. Execution bounds

Both bounds are set on **every** execution, from `.env`, with no default that
silently applies when the variable is missing — the runner refuses to start:

- **`RECURSION_LIMIT`** — passed to LangGraph as `recursion_limit`. Exceeding it
  raises `GraphRecursionError`, which is recorded as the outcome.
- **`TOKEN_CEILING`** — per execution. A `BaseCallbackHandler` (langchain-core's
  own extension point) sums token usage after each model call and raises once
  the ceiling is passed. Recorded as the outcome.
- **`SPEND_CEILING_TOKENS`** — per `autopsy run` invocation, across all
  executions in the batch. When it is reached the run stops and writes a final
  record saying so. A batch stopped this way is incomplete: cases with fewer
  than 5 executions are not published.

Ceilings are denominated in **tokens**, never dollars. This repository keeps no
price table, because a stale price table would silently misstate published
numbers.

### Side-effecting tools must be stubbed

Every adapter declares the target's side-effecting tools in
`METADATA["side_effecting_tools"]`: anything that writes a file, sends a request,
or changes state outside this process. The declaration is required; `[]` is a
valid answer and an omitted key is an error.

- The runner refuses to start a batch unless every declared tool is stubbed by
  every case it is about to run.
- An adapter whose stub fails to apply raises instead of letting the real tool
  run. The runner records that as the execution's outcome, like any other
  exception.

**A finding is published only from executions in which every declared
side-effecting tool was stubbed.** An agent that reached a live service was not
running in this harness's environment, and whatever it did there is not evidence
about the agent.

### Out of scope: wall-clock hangs

The harness runs each execution **in process** and cannot interrupt a target
that blocks indefinitely inside a single node. There is no wall-clock timeout.
Hangs are covered only by the network timeouts of the model SDK. A target that
hangs outside a model call will hang the harness, and that is a known limit, not
a measured result. No assertion in this repository claims otherwise.

## 5. Categories

Exactly five. A case declares one. New categories are a protocol change, not a
case-authoring decision.

| category | means |
| --- | --- |
| `malformed_tool_output` | a tool returns output the agent did not expect — wrong shape, wrong type, truncated, empty, or an error string where data was expected |
| `contradictory_instruction` | the input or state contains instructions that cannot both be satisfied |
| `state_collision` | injected or accumulated state conflicts with what the agent writes, including key collisions and stale values |
| `loop_bait` | input that invites the agent to repeat a step without making progress |
| `cost_blowup` | input that invites unbounded token or tool-call growth |

## 6. Assertions

Exactly six. Each case carries exactly one. Names describe what is actually
measured and must not over-claim.

| assertion | passes when |
| --- | --- |
| `output_contains` | the run completed and the JSON-serialized final output contains the given string |
| `output_not_contains` | the run completed and the serialized final output does **not** contain the given string |
| `no_exception` | the run completed without raising anything, including `GraphRecursionError` and the token-ceiling abort |
| `step_count_under` | the run completed and the highest observed `langgraph_step` is below the given number |
| `token_cost_under` | the run completed and total tokens across model calls are below the given number |
| `bounded_termination` | the run stopped without hitting the recursion limit or the token ceiling. An ordinary exception counts as stopping. **Wall-clock hangs are not covered** (§4) |

Consequences that follow from the wording above and are enforced in code:

- Any raised exception fails `output_contains`, `output_not_contains`,
  `no_exception`, `step_count_under` and `token_cost_under`. A run aborted by a
  ceiling produces a censored step and token count — a lower bound, not a
  measurement — so it cannot satisfy a `_under` assertion.
- `bounded_termination` is the only assertion an exception can pass.

A seventh assertion requires a failure that none of these six can express,
stated in the pull request that adds it.

## 7. Exceptions and infrastructure errors

- No exception is swallowed. The runner records its class, module, message and
  traceback as the outcome of that execution.
- Each record carries a boolean **`infrastructure_error`**, set when the
  exception is an authentication, rate-limit, connection or timeout class from
  the model SDK, or a server-side 5xx. The classes that set it are listed in
  `autopsy/runner.py` and nowhere else.
- **`autopsy report` refuses to generate `FAILURES.md` when any execution in the
  latest batch of any case has `infrastructure_error` set.** It exits non-zero
  and prints the cases that must be rerun.
- **Findings are never published from a batch containing infrastructure
  errors.** A 429 is not a failure mode of the agent under test.

## 8. Results and artifacts

- `results/<target>.jsonl` is **append-only**. One JSON line per execution,
  flushed and fsynced as each execution finishes. Existing lines are never
  rewritten, reordered or deleted. Results are committed: they are the evidence.
- `FAILURES.md` is **generated** by `autopsy report` and **never hand-edited**.
  It is committed, because it is the artifact people read.
- CI regenerates `FAILURES.md` from the committed results and fails if the
  result differs from the committed file. An edit to `FAILURES.md` that the
  results do not support cannot merge.
- `.env` is not committed. `.env.example` is.

## 9. Resolved dependency set

Every dependency is pinned to an exact version in `requirements.txt`. The set
below is the full resolved environment, direct and transitive, on the platform
it was resolved on. Findings record the versions in effect at run time; if they
disagree with this list, the record wins and this list is stale.

- Resolved: 2026-11-17
- Python: 3.13.5
- Platform: macOS (darwin, arm64)

Direct:

```
PyYAML==6.0.3
anthropic==2.9.0
langgraph==1.2.22
langsmith==0.14.17
pytest==9.1.1
```

`langchain-core` is not a direct dependency; it arrives with `langgraph`, and
its version is recorded on every run because the callback handler in §4 is built
on its `BaseCallbackHandler`.

Full resolved set:

```
PyYAML==6.0.3
Pygments==2.21.0
annotated-types==0.8.0
anthropic==2.9.0
anyio==4.16.0
beautifulsoup4==4.15.0
bracex==3.2.3
certifi==2026.10.23
cffi==2.1.1
charset-normalizer==3.5.2
cryptography==50.0.2
deepagents==0.7.39
distro==1.9.0
docstring_parser==0.18.0
filetype==1.2.0
google-auth==2.62.2
google-genai==3.4.0
h11==0.16.0
httpcore==1.0.9
httpcore2==2.16.2
httpx==0.28.1
httpx2==2.16.2
idna==3.20
iniconfig==2.3.1
jiter==0.19.0
jsonpatch==1.47
jsonpointer==3.12
langchain==1.5.0
langchain-anthropic==1.7.10
langchain-core==1.8.1
langchain-google-genai==4.5.0
langchain-protocol==0.0.21
langgraph==1.2.22
langgraph-checkpoint==4.3.2
langgraph-prebuilt==1.1.2
langgraph-sdk==0.4.14
langsmith==0.14.17
markdownify==1.2.3
opentelemetry-api==1.45.2
orjson==3.13.0
ormsgpack==1.13.1
packaging==26.3
pluggy==1.7.0
pyasn1==0.6.5
pyasn1_modules==0.4.2
pycparser==3.12
pydantic==2.14.3
pydantic_core==2.50.4
pytest==9.1.1
regex==2026.11.15
requests==2.34.2
requests-toolbelt==1.0.0
six==1.17.0
sniffio==1.3.1
soupsieve==4.1.1
tavily-python==0.8.5
tenacity==9.1.4
tiktoken==0.14.0
truststore==0.10.5
typing-inspection==0.4.5
typing_extensions==4.16.0
urllib3==2.8.0
uuid_utils==0.19.0
wcmatch==12.0.3
websockets==17.4
xxhash==4.1.0
zstandard==0.25.0
```

## 10. Scope of a finding

A finding describes what the harness observed on a specific commit of a specific
agent, at a specific version set, at an observed rate. It is not a claim about
the agent in general, about other versions, or about other models. `FAILURES.md`
states what was observed and links the traces; readers draw their own
conclusions.
