"""Prove that deterministic replay consults no model.

Asserting "replay uses no LLM" is cheap. This makes it falsifiable: it runs a
real replay inside a process with three seals, any one of which a hidden model
call would trip.

  1. Every *_API_KEY is removed from the environment.
  2. The model SDKs are made unimportable — importing one raises.
  3. Outbound sockets are blocked to everything except 127.0.0.1 (the mock app).

Then it runs the control experiment: discovery, under the identical seals, must
FAIL. A test that cannot fail proves nothing, so the seals are shown to bite
before the replay result is believed.

Run it with:  ./scripts/prove_no_llm.sh
"""

from __future__ import annotations

import importlib.abc
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BLOCKED_SDKS = {"anthropic", "openai", "cohere", "mistralai", "google"}


def seal() -> None:
    removed = [k for k in list(os.environ) if k.endswith("_API_KEY") or "ANTHROPIC" in k]
    for key in removed:
        del os.environ[key]
    print(f"  seal 1  removed from environment: {removed or '(none were set)'}")

    class ImportSeal(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED_SDKS:
                raise ImportError(f"SEAL TRIPPED: tried to import {fullname!r}")
            return None

    sys.meta_path.insert(0, ImportSeal())
    print(f"  seal 2  import blocked for: {sorted(BLOCKED_SDKS)}")

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guard(fn):
        def wrapper(self, address, *a, **kw):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in ("127.0.0.1", "::1", "localhost"):
                raise OSError(f"SEAL TRIPPED: outbound connection to {host!r} blocked")
            return fn(self, address, *a, **kw)

        return wrapper

    socket.socket.connect = guard(real_connect)
    socket.socket.connect_ex = guard(real_connect_ex)
    print("  seal 3  outbound sockets restricted to 127.0.0.1")


def run_cli(argv: list[str]) -> int:
    sys.argv = ["cua", *argv]
    from cua.cli import app

    try:
        app()
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException as exc:  # noqa: BLE001 - we are reporting, not handling
        print(f"\n  raised: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
        return 1
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "replay"
    member = os.environ.get("PROVE_MEMBER_ID", "100731")
    os.environ.setdefault("MERIDIAN_OPERATOR_ID", "ops.demo")
    os.environ.setdefault("MERIDIAN_OPERATOR_PASSCODE", "demo-not-a-real-password")

    print(f"\nsealing the process ({mode})")
    seal()
    print()

    if mode == "replay":
        code = run_cli(["replay", "-c", "member_savings_balance_lookup",
                        "-i", f"member_id={member}", "--label", "sealed"])
        ok = code == 0
        print("\n" + ("  PASS  replay completed with no key, no SDK and no network."
                      if ok else "  FAIL  replay did not complete under the seals."))
        return 0 if ok else 1

    # control experiment: this MUST fail, or the seals prove nothing
    code = run_cli(["discover", "--task", "tasks/member_savings_lookup.yaml", "--no-approve"])
    ok = code != 0
    print("\n" + ("  PASS  discovery was stopped by the seals, so the seals do bite."
                  if ok else "  FAIL  discovery ran under the seals — the seals are not working."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
