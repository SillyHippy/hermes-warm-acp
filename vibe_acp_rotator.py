#!/usr/bin/env python3
"""vibe-acp key rotator — one Vibe allowance key per ACP session.

Mistral's vibe-acp harness authenticates from a single MISTRAL_API_KEY in its
environment. Hermes holds six paid Vibe keys in the mistral-vibe credential
pool (fill_first). This wrapper sits in front of vibe-acp:

  1. select the next live key from the pool,
  2. spawn vibe-acp with that key as MISTRAL_API_KEY,
  3. on a billing/capacity failure (402 vibe budget, 429 capacity, 401 dead key)
     bench that key via the pool and retry the SAME session with the next key.

Stdio is a transparent pipe either way, so Hermes' ACP client sees one process.
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import threading

os.environ.setdefault("HERMES_HOME", "/root/.hermes")
sys.path.insert(0, "/usr/local/lib/hermes-agent")

POOL = "mistral-vibe"
VIBE_ACP = os.path.expanduser("~/.local/bin/vibe-acp")
MAX_ROTATIONS = 6
# Mistral error codes: 2304 = vibe budget exhausted (402), 3505 = backend out of capacity (429).
_FAIL = re.compile(r"billing_vibe_budget_exhausted|backend_out_of_capacity|\"code\":\s*\"?(2304|3505|401)\"?|HTTP 40[12]|status.?code.?(401|402|429)", re.I)


def _pool():
    from agent.credential_pool import load_pool

    return load_pool(POOL)


def _pick():
    entry = _pool().select()
    if entry is None or not entry.runtime_api_key:
        return None
    return entry


def _bench(key: str, code: int) -> None:
    try:
        _pool().mark_exhausted_and_rotate(status_code=code, api_key_hint=key, failure_reason="vibe-acp-rotator")
    except Exception as exc:  # rotation bookkeeping must never kill the session
        print(f"[vibe-rotator] bench failed: {exc}", file=sys.stderr)


def _relay(src, dst, bucket: list[str] | None = None) -> None:
    try:
        for line in src:
            if bucket is not None:
                bucket.append(line)
            dst.write(line)
            dst.flush()
    except Exception:
        pass


def _spawn(key: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["MISTRAL_API_KEY"] = key
    env["HOME"] = os.path.expanduser("~")
    return subprocess.Popen(
        [VIBE_ACP],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )


def main() -> int:
    tried: set[str] = set()
    proc: subprocess.Popen[str] | None = None
    for _ in range(MAX_ROTATIONS):
        entry = _pick()
        if entry is None or entry.runtime_api_key in tried:
            print("[vibe-rotator] no live Vibe keys left in the pool", file=sys.stderr)
            return 3
        key = entry.runtime_api_key
        tried.add(key)
        print(f"[vibe-rotator] using {entry.label or entry.id[:8]}", file=sys.stderr)
        proc = _spawn(key)
        stderr_lines: list[str] = []
        threading.Thread(target=_relay, args=(sys.stdin, proc.stdin), daemon=True).start()
        threading.Thread(target=_relay, args=(proc.stderr, sys.stderr, stderr_lines), daemon=True).start()

        failed_code: int | None = None
        assert proc.stdout is not None
        for line in proc.stdout:
            if failed_code is None and _FAIL.search(line):
                failed_code = 402 if "402" in line or "2304" in line else 429 if "429" in line or "3505" in line else 401
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.wait(timeout=30)
        if failed_code is None and proc.returncode not in (0, None):
            blob = "".join(stderr_lines)
            if _FAIL.search(blob):
                failed_code = 402
        if failed_code is None:
            return proc.returncode or 0
        print(f"[vibe-rotator] key failed ({failed_code}); rotating", file=sys.stderr)
        _bench(key, failed_code)
        proc = None
    return proc.returncode if proc else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
