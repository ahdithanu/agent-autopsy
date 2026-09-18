"""Configuration and version capture. Standard library only."""

import os
import platform
from importlib import metadata

REQUIRED_INTS = ("RECURSION_LIMIT", "TOKEN_CEILING", "SPEND_CEILING_TOKENS")
REQUIRED_STRS = ("MODEL", "PROVIDER")

RECORDED_PACKAGES = ("langgraph", "langchain-core", "langsmith", "anthropic")


def load_dotenv(path=".env"):
    """Read KEY=VALUE lines into os.environ without overriding what is already set.

    The process environment wins so CI can inject secrets. Returns the keys that
    were applied from the file.
    """
    applied = []
    if not os.path.exists(path):
        return applied
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"{path}:{lineno}: expected KEY=VALUE, got {raw!r}")
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value
                applied.append(key)
    return applied


def config(path=".env"):
    """Return the run configuration, or raise if a required value is missing.

    Missing bounds are an error rather than a default: PROTOCOL.md section 4
    forbids a ceiling that silently applies without being chosen.
    """
    load_dotenv(path)
    missing = [k for k in REQUIRED_INTS + REQUIRED_STRS if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "missing required configuration: "
            + ", ".join(missing)
            + f"\nset them in {path} (see .env.example) or in the environment"
        )

    cfg = {key.lower(): os.environ[key] for key in REQUIRED_STRS}
    for key in REQUIRED_INTS:
        raw = os.environ[key]
        try:
            cfg[key.lower()] = int(raw)
        except ValueError:
            raise SystemExit(f"{key} must be an integer number of tokens, got {raw!r}")
        if cfg[key.lower()] <= 0:
            raise SystemExit(f"{key} must be positive, got {raw!r}")

    cfg["tracing"] = os.environ.get("LANGSMITH_TRACING", "").lower() == "true"
    cfg["langsmith_project"] = os.environ.get("LANGSMITH_PROJECT") or None
    cfg["langsmith_api_key_present"] = bool(os.environ.get("LANGSMITH_API_KEY"))
    return cfg


def versions():
    """Installed versions of everything a finding is conditioned on."""
    found = {}
    for name in RECORDED_PACKAGES:
        try:
            found[name.replace("-", "_")] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name.replace("-", "_")] = None
    found["python"] = platform.python_version()
    return found
