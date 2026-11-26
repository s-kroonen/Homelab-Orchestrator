"""mypy against a recorded baseline: fail on type errors that are not in it (OR-99).

The repository adopted dev-standard with existing mypy errors. They are recorded in
``tools/mypy-baseline.txt`` and fixed in a follow-up; until then this script fails only on errors
that the baseline does not list. Line numbers are left out of the comparison, so moving code does
not count as a new error. An error that is fixed is reported, so the baseline can shrink.

    uv run python tools/mypy_baseline.py            # the check (TYPECHECK_CMD in project.config)
    uv run python tools/mypy_baseline.py --update   # rewrite the baseline (a person reviews it)

The baseline is a guardrail file (.guardrails): agents never edit it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / "mypy-baseline.txt"
CMD = [
    sys.executable,
    "-m",
    "mypy",
    "--exclude",
    r"^(tests/(quality|security)-canary|spikes|\.specify|tools)/",
    ".",
]
LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+)(?::\d+)?: error: (?P<msg>.*)$")


def errors() -> tuple[Counter[str], list[str], int]:
    proc = subprocess.run(CMD, cwd=ROOT, capture_output=True, text=True, check=False)
    found: Counter[str] = Counter()
    lines: list[str] = []
    for raw in proc.stdout.splitlines():
        m = LINE.match(raw)
        if m:
            found[f"{m['path']}: {m['msg']}"] += 1
            lines.append(raw)
    return found, lines, proc.returncode


def main() -> int:
    found, lines, rc = errors()
    if rc not in (0, 1) and not found:
        sys.stdout.write("mypy did not run:\n")
        subprocess.run(CMD, cwd=ROOT, check=False)
        return 2
    if "--update" in sys.argv:
        BASELINE.write_text("".join(f"{k}\n" for k in sorted(found.elements())))
        sys.stdout.write(f"Baseline written: {sum(found.values())} errors.\n")
        return 0
    known: Counter[str] = Counter(
        line for line in BASELINE.read_text().splitlines() if line.strip()
    )
    new = found - known
    fixed = known - found
    if fixed:
        sys.stdout.write(
            f"{sum(fixed.values())} baseline error(s) no longer reported; "
            "shrink the baseline with --update.\n"
        )
    if new:
        sys.stdout.write(f"{sum(new.values())} new mypy error(s), not in the baseline:\n")
        for raw in lines:
            m = LINE.match(raw)
            if m and new[f"{m['path']}: {m['msg']}"] > 0:
                sys.stdout.write(raw + "\n")
        return 1
    sys.stdout.write(
        f"mypy: no new errors ({sum(found.values())} known, recorded in the baseline).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
