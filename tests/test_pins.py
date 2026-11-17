"""PROTOCOL.md section 9 and requirements.txt describe the same environment.

Two hand-maintained copies of one list drift. This is the check that they have
not, in the same spirit as CI regenerating FAILURES.md.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def requirements():
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as handle:
        lines = [line.strip() for line in handle]
    return [line for line in lines if line and not line.startswith("#")]


def protocol_block(heading):
    with open(os.path.join(ROOT, "PROTOCOL.md"), encoding="utf-8") as handle:
        text = handle.read()
    return re.search(re.escape(heading) + r"\n\n```\n(.*?)```", text, re.S).group(1).split()


def test_every_requirement_is_an_exact_pin():
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=<>~!*\s]+", line) for line in requirements())


def test_the_protocol_lists_exactly_what_requirements_pins():
    assert sorted(protocol_block("Full resolved set:")) == sorted(requirements())


def test_the_direct_dependencies_are_part_of_the_full_set():
    assert set(protocol_block("Direct:")) <= set(requirements())
