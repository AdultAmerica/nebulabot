"""Run every test module in this directory.

Each one is a plain script with asserts — no pytest needed, so it runs the same
on a bare VPS as it does locally:

    python tests/run_all.py
"""

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent


def main() -> int:
    failures = []
    for path in sorted(HERE.glob("test_*.py")):
        print(f"\n\033[1m── {path.name} ──\033[0m")
        result = subprocess.run([sys.executable, str(path)])
        if result.returncode != 0:
            failures.append(path.name)

    print()
    if failures:
        print(f"\033[31mFAILED: {', '.join(failures)}\033[0m")
        return 1
    print("\033[32mAll test modules passed.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
