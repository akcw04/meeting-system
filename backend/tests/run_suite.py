"""Run the WHOLE automated smoke-test suite in one console - the "final
all-pass run" evidence for Chapter 5 (Section 5.2).

Executes every smoke_phase*.py in order, each in its own interpreter, and
prints a per-phase PASS/FAIL line plus a final summary block that is made to
be screenshotted.

USAGE (from backend/, venv active; have Ollama running for phase 7):
    python tests\\run_suite.py

Notes:
  * Phases 4-5 load the GPU models - the full run takes a while; let it finish.
  * The live uvicorn server does NOT need to be running (tests use an
    in-process TestClient); leaving it running does not interfere either.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent

SUITE = sorted(p.name for p in TESTS.glob("smoke_phase*.py"))

def main() -> int:
    print("Meeting System - automated test suite")
    print("=" * 60)
    results: list[tuple[str, bool, float]] = []
    for name in SUITE:
        print(f"\n>>> {name}")
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, str(TESTS / name)], cwd=TESTS.parent)
        dt = time.monotonic() - t0
        ok = proc.returncode == 0
        results.append((name, ok, dt))
        print(f"<<< {name}: {'PASS' if ok else 'FAIL'} ({dt:.1f}s)")

    print("\n" + "=" * 60)
    print("SUITE SUMMARY")
    print("=" * 60)
    passed = 0
    for name, ok, dt in results:
        print(f"  {'PASS' if ok else 'FAIL'}   {name:<34} {dt:7.1f}s")
        passed += ok
    total = len(results)
    print("-" * 60)
    verdict = "ALL TESTS PASSED" if passed == total else "FAILURES PRESENT"
    print(f"  {passed}/{total} suites passed - {verdict}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
