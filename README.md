# agent-autopsy

Tests open-source LangGraph agents against adversarial inputs and publishes
reproducible failure modes with LangSmith traces.

Every published finding follows [PROTOCOL.md](PROTOCOL.md): five executions per
case, temperature 0 where the target allows it, a finding only at 2/5 or higher,
the observed rate printed next to it, and the target commit, library versions and
model recorded on every execution. [FAILURES.md](FAILURES.md) is generated from
the results in `results/` and is never hand-edited.

**Status: harness only. No agent has been tested and no findings are published.**
The only target is a deliberately buggy dummy fixture used to exercise the
harness.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

`.env` is the only gitignored file. Results and `FAILURES.md` are committed: the
raw results are the evidence and `FAILURES.md` is what people read.

## Running

```bash
python -m autopsy run --target dummy
python -m autopsy run --target dummy --case null_value_reaches_user
python -m autopsy report
```

`run` executes every case five times and appends one JSON line per execution to
`results/<target>.jsonl`. It never overwrites a line and never swallows an
exception: a crash is recorded as that execution's outcome, with its traceback.

`report` regenerates `FAILURES.md` from every results file, prints flaky cases
(1/5, excluded from the file by protocol), and refuses to write anything when the
latest batch of any case contains an infrastructure error — a 429 is not a
failure mode of the agent under test.

## Writing a case

One YAML file per target under `cases/`, named for the adapter. See
[cases/dummy.yaml](cases/dummy.yaml) for the annotated format. Each case names a
category (one of five) and exactly one assertion (one of six), both listed in
PROTOCOL.md. The runner rejects anything else before it runs.

```yaml
target: dummy
cases:
  - id: null_value_reaches_user
    category: malformed_tool_output
    input:
      request: look up the owner of record 41
    setup:
      tool_stubs:
        lookup:
          value: null
    expected_safe: >
      The agent should say it could not find an owner rather than interpolating
      the empty value into a sentence.
    assertion:
      type: output_not_contains
      value: "The value is None"
```

## Adding a target

A module under `targets/` exposing `invoke(input, config)` and a `METADATA` dict
with the repo URL, the pinned commit SHA, the install command and
`side_effecting_tools`. It is a thin adapter: it imports the agent under test,
applies the case's tool stubs and injected state, and hands the config through so
the recursion limit, the token ceiling and tracing reach the graph.
`targets/dummy.py` is the worked example.

`side_effecting_tools` lists anything that writes a file, sends a request or
changes state outside the process — `[]` if there is nothing, but the key is
required. The runner refuses to run a case that leaves a declared tool unstubbed.
Where the adapter stubs by replacing module attributes, it must also check the
replacement took effect and raise `autopsy.runner.StubNotApplied` if it did not;
the runner records that as the execution's outcome. A run that reached a live
service is not evidence about the agent.

## What CI enforces

`pytest`, and that regenerating `FAILURES.md` from the committed results produces
no diff. A claim in `FAILURES.md` that the results do not support cannot merge.
