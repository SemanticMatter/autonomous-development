#!/usr/bin/env python3
"""Run the offline test suite with an isolated environment (the supported way to run it).

    python3 tests/run_isolated.py                        # unittest discover -s tests
    python3 tests/run_isolated.py discover -s tests -v   # any `python -m unittest` arguments
    python3 tests/run_isolated.py tests.test_accounting

Some tests start `controller.py` as a subprocess with the caller's environment; `doctor`,
for example, runs `codex --version`, `codex login status` and a `codex exec --strict-config`
probe, finding `codex` by name on PATH. Under a plain `python -m unittest` from a normal
shell, that lookup can find the real Codex CLI, which then reads the real configuration and
credentials. This runner:

* puts a stub `codex` (logs its arguments, exits 97) first on PATH, drops inherited PATH
  entries that hold another `codex`, and refuses to run unless `codex` resolves to the stub;
* passes an allowlisted environment (no provider keys, tokens or proxy settings), with HOME,
  TMPDIR, the XDG base directories and CODEX_HOME in a fresh temporary directory that is
  removed afterwards;
* runs the tests with this interpreter (also exposed as `python3` on PATH), so use one that
  has the project's requirements installed.

This is environment isolation, not a sandbox. The tests still run as the current user with
normal file-system and network access. The isolation holds only while the code under test
finds Codex by name on PATH, and finds configuration and state through the variables above.
An absolute path to a Codex binary, a rebuilt PATH, or a hard-coded file location would
bypass it. The tests are offline because they use fakes; the runner does not block the
network.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB_EXIT_CODE = 97
# Inherited only on Windows, where the interpreter and subprocesses need them.
_WINDOWS_PASSTHROUGH = ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC")


def write_codex_stub(bin_dir: Path, log: Path) -> Path:
    message = "codex stub: the Codex CLI is replaced by a stub during the offline tests"
    if os.name == "nt":
        stub = bin_dir / "codex.cmd"
        stub.write_text(
            f'@echo off\r\necho %*>> "{log}"\r\necho {message} 1>&2\r\n'
            f"exit /b {STUB_EXIT_CODE}\r\n",
            encoding="utf-8",
        )
        return stub
    stub = bin_dir / "codex"
    stub.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> {shlex.quote(str(log))}\necho "{message}" >&2\n'
        f"exit {STUB_EXIT_CODE}\n",
        encoding="utf-8",
    )
    stub.chmod(0o700)
    return stub


def write_python3_shim(bin_dir: Path) -> None:
    """Tests start `python3 scripts/controller.py ...`: make that this interpreter."""
    if os.name == "nt":
        return
    shim = bin_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    shim.chmod(0o700)


def isolated_path(bin_dir: Path, inherited: str) -> tuple[str, list[str]]:
    """Stub dir first, then this interpreter's dir, then inherited absolute entries
    that hold no `codex` executable. Returns (PATH, dropped entries)."""
    entries = [str(bin_dir), os.path.dirname(sys.executable)]
    dropped: list[str] = []
    for entry in inherited.split(os.pathsep):
        if not entry or not os.path.isabs(entry) or entry in entries or entry in dropped:
            continue
        if shutil.which("codex", path=entry):
            dropped.append(entry)
            continue
        entries.append(entry)
    return os.pathsep.join(entries), dropped


def main(argv: list[str]) -> int:
    unittest_args = argv or ["discover", "-s", "tests"]
    scratch = Path(tempfile.mkdtemp(prefix="autonomous-dev-tests-"))
    try:
        dirs = {
            name: scratch / name
            for name in ("bin", "home", "tmp", "state", "config", "cache", "data", "codex")
        }
        for path in dirs.values():
            path.mkdir(mode=0o700)
        log = scratch / "codex-stub-calls.log"
        stub = write_codex_stub(dirs["bin"], log)
        write_python3_shim(dirs["bin"])
        path, dropped = isolated_path(dirs["bin"], os.environ.get("PATH", ""))
        resolved = shutil.which("codex", path=path)
        if resolved is None or Path(resolved).resolve() != stub.resolve():
            print(f"[run_isolated] refusing to run: codex resolves to {resolved!r}", file=sys.stderr)
            return 2
        if shutil.which("git", path=path) is None:
            print(
                "[run_isolated] refusing to run: git is not on PATH once directories "
                f"containing codex are dropped ({os.pathsep.join(dropped)})",
                file=sys.stderr,
            )
            return 2
        env = {
            "PATH": path,
            "HOME": str(dirs["home"]),
            "TMPDIR": str(dirs["tmp"]),
            "XDG_STATE_HOME": str(dirs["state"]),
            "XDG_CONFIG_HOME": str(dirs["config"]),
            "XDG_CACHE_HOME": str(dirs["cache"]),
            "XDG_DATA_HOME": str(dirs["data"]),
            "CODEX_HOME": str(dirs["codex"]),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if os.name == "nt":
            env.update({k: os.environ[k] for k in _WINDOWS_PASSTHROUGH if k in os.environ})
            env.update(
                TEMP=env["TMPDIR"], TMP=env["TMPDIR"], USERPROFILE=env["HOME"],
                APPDATA=str(dirs["config"]), LOCALAPPDATA=str(dirs["data"]),
            )
        result = subprocess.run(
            [sys.executable, "-m", "unittest", *unittest_args], cwd=ROOT, env=env, check=False
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        summary = sorted({" ".join(call.split()[:2]) for call in calls})
        print(
            f"[run_isolated] codex stub invoked {len(calls)} time(s)"
            + (f" ({'; '.join(summary)})" if summary else "")
            + (f"; dropped PATH entries holding codex: {', '.join(dropped)}" if dropped else ""),
            file=sys.stderr,
        )
        return result.returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
