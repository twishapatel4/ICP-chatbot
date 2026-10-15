"""PYTHONPATH=src python -m icp_chatbot.cli.launch_check [eval_cases]

Exits non-zero until the launch set meets the spec: at least 30 cases covering every persona."""

import sys

from icp_chatbot.eval.cases import check_launch_set, load_cases


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cases = load_cases(args[0] if args else "eval_cases")
    problems = check_launch_set(cases)
    if not problems:
        print(f"OK: {len(cases)} cases cover every required persona")
        return 0
    print(f"NOT READY ({len(cases)} cases):")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
