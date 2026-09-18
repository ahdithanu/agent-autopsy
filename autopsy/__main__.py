"""python -m autopsy run --target <name> [--case <id>]
python -m autopsy report [--target <name>] [--check]
"""

import argparse
import sys

from autopsy import report as report_module
from autopsy import runner


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m autopsy",
        description="Run adversarial cases against a target agent and report what failed.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="execute a target's cases, 5 times each")
    run.add_argument("--target", required=True, help="adapter name under targets/")
    run.add_argument("--case", help="single case id; omit to run every case")

    report = commands.add_parser("report", help="regenerate FAILURES.md from results/")
    report.add_argument("--target", help="focus stdout on one target")
    report.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if FAILURES.md differs from what the results generate",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return runner.run(args.target, args.case)
    return report_module.report(args.target, check=args.check)


if __name__ == "__main__":
    sys.exit(main())
