#!/usr/bin/env python3
"""Fail if total coverage dropped compared with the base commit.

usage: coverage-gate.py HEAD_JSON [BASE_JSON]

Both files are `coverage json` reports. Without a usable base report (first pull request, or the base could not be
measured) the head coverage is only reported. A markdown summary is appended to $GITHUB_STEP_SUMMARY when set.
"""
import json
import os
import sys
from typing import Optional


def total(report_path: Optional[str]) -> Optional[float]:
    if not report_path or not os.path.isfile(report_path):
        return None
    try:
        with open(report_path, encoding='utf-8') as fp:
            return round(float(json.load(fp)['totals']['percent_covered']), 2)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    head = total(argv[1])
    if head is None:
        print(f"No usable coverage report at {argv[1]}", file=sys.stderr)
        return 2
    base = total(argv[2]) if len(argv) == 3 else None

    if base is None:
        verdict, ok = f"Coverage is {head:.2f}% (no base measurement to compare with).", True
    elif head < base:
        verdict, ok = f"Coverage dropped from {base:.2f}% to {head:.2f}% ({head - base:+.2f}).", False
    else:
        verdict, ok = f"Coverage is {head:.2f}% ({head - base:+.2f} against {base:.2f}% on the base).", True

    print(("PASS: " if ok else "FAIL: ") + verdict)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as fp:
            fp.write(f"### Coverage {'✅' if ok else '❌'}\n\n{verdict}\n")
            if not ok:
                fp.write("\nAdd tests for the new or changed code, or explain in the pull request why coverage dropped.\n")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
