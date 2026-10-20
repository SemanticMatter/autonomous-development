"""Codex attempt accounting: ledger unit tests and cmd_codex integration tests.

Every test uses temporary run-state and a temporary accounting home; Codex is
never executed (the existing `controller.run_process` fake-process seam is used).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import errno
import io
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import accounting  # noqa: E402
import controller  # noqa: E402
from state import CrossProcessLock  # noqa: E402

ENV = accounting.ACCOUNTING_HOME_ENV
BOOT = "11111111-2222-4333-8444-555555555555"
OTHER_BOOT = "99999999-8888-4777-8666-555555555555"
THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"
FULL_USAGE = {
    "input_tokens": 1000,
    "cached_input_tokens": 800,
    "output_tokens": 120,
    "reasoning_output_tokens": 64,
}
FULL_REVIEW = {
    "verdict": "pass",
    "summary": "ok",
    "findings": [],
    "verification_gaps": [],
    "acceptance_criteria_assessment": [],
    "confidence": 1.0,
}

# AT-37: the real default ledger must never be touched by this module.
_MODULE_HOME: tempfile.TemporaryDirectory[str] | None = None
_SAVED_ENV: str | None = None
_REAL_DEFAULT: Path | None = None
_REAL_DEFAULT_BEFORE: object = None


def _fingerprint(path: Path) -> object:
    if not path.exists():
        return None
    return sorted(
        (str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in [path, *path.rglob("*")]
    )


def setUpModule() -> None:
    global _MODULE_HOME, _SAVED_ENV, _REAL_DEFAULT, _REAL_DEFAULT_BEFORE
    _SAVED_ENV = os.environ.get(ENV)
    _REAL_DEFAULT = accounting.resolve_accounting_home(
        {k: v for k, v in os.environ.items() if k != ENV}
    )
    _REAL_DEFAULT_BEFORE = _fingerprint(_REAL_DEFAULT)
    _MODULE_HOME = tempfile.TemporaryDirectory()
    os.environ[ENV] = _MODULE_HOME.name


def tearDownModule() -> None:
    if _SAVED_ENV is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = _SAVED_ENV
    if _MODULE_HOME is not None:
        _MODULE_HOME.cleanup()
    assert _REAL_DEFAULT is not None
    if _fingerprint(_REAL_DEFAULT) != _REAL_DEFAULT_BEFORE:
        raise AssertionError(f"tests modified the real default ledger at {_REAL_DEFAULT}")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def ndjson(*usages: object, thread: str | None = THREAD, model: str = "gpt-x-test") -> str:
    events: list[object] = []
    if thread:
        events.append({"type": "thread.started", "thread_id": thread})
    events.append({"msg": {"type": "session_configured", "model": model}})
    events.extend({"type": "turn.completed", "usage": usage} for usage in usages)
    return "".join(json.dumps(event) + "\n" for event in events)


def make_reservation(
    *,
    run_id: str = "RUN1",
    slot: str = "review-01",
    phase: str = "review",
    boot_id: str | None = BOOT,
    pid: int = 4242,
    timeout: float | None = 3600.0,
    generation: int | None = None,
) -> dict:
    return {
        "tool": "codex-exec",
        "phase": phase,
        "slot": slot,
        "delta_review": False,
        "timeout_seconds": timeout,
        "launcher": {"pid": pid, "boot_id": boot_id, "host_label": None},
        "workflow": {
            "repo_id": "0123456789abcdef",
            "run_id": run_id,
            "workflow_kind": "feature",
            "state_home_overridden": False,
            "review_contract_generation": generation,
        },
        "requested": {
            "model": None,
            "reasoning_effort": "high",
            "verbosity": "low",
            "profile_overridden": False,
        },
        "engine": {"version": "0.3.0", "code_digest": "sha256:" + "0" * 64, "root_kind": "plain"},
        "python": {"implementation": "cpython", "version": "3.12.0"},
        "refs": {"work_package": None, "pr": None, "track": None, "round": 1, "context": None},
        "prompt_characters": 10,
    }


def settle_published(ledger: accounting.Ledger, res: accounting.Reservation, *usages: object) -> None:
    tracker = accounting.AttemptTracker(ledger, res)
    tracker.record_process(
        returncode=0,
        timed_out=False,
        duration_seconds=1.0,
        stdout_text=ndjson(*(usages or (FULL_USAGE,))),
        stdout_truncated=False,
        observed_model="gpt-x-test",
    )
    assert tracker.settle(tracker.success_settlement()) is None


def settle_rejected(ledger: accounting.Ledger, res: accounting.Reservation, reason: str) -> None:
    tracker = accounting.AttemptTracker(ledger, res)
    tracker.record_process(
        returncode=1,
        timed_out=False,
        duration_seconds=1.0,
        stdout_text=ndjson(FULL_USAGE),
        stdout_truncated=False,
        observed_model=None,
    )
    tracker.stage = reason
    assert tracker.settle(tracker.failure_settlement(controller.WorkflowError("x"))) is None


def inject_events(home: Path, *events: dict) -> None:
    """Append raw event lines as a corruption fixture, bypassing the writer."""
    with (home / "v1" / "codex-attempts.jsonl").open("ab") as fh:
        for event in events:
            fh.write((json.dumps(event, sort_keys=True) + "\n").encode("utf-8"))


def fail_fsync_on(kind: str, error: int, only: Path | None = None):
    """An os.fsync replacement that fails with `error` on directories or files
    (optionally only on the directory `only`)."""
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) == (kind == "dir") and (
            only is None or os.path.samestat(info, os.stat(only))
        ):
            raise OSError(error, os.strerror(error))
        real_fsync(fd)

    return fsync


def tree_snapshot(path: Path) -> list:
    if not path.exists():
        return []
    return sorted(
        (str(p.relative_to(path)), p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ino)
        for p in [path, *path.rglob("*")]
    )


def _process_gone(pid: int) -> bool:
    """True when `pid` no longer runs (absent, or a zombie awaiting its reaper)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[0] == "Z"


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not _process_gone(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _kill_owned_group(proc: subprocess.Popen, children: list[int]) -> None:
    """SIGKILL the process group this test started (with start_new_session), and
    nothing else: once its leader is reaped the group id is signalled only while a
    known child of the test is still a live member of it."""
    owned = proc.poll() is None
    for pid in children:
        with contextlib.suppress(OSError):
            owned = owned or (not _process_gone(pid) and os.getpgid(pid) == proc.pid)
    if owned:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=30)


class _TempDirs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "accounting-home"
        self.ledger = accounting.Ledger(self.home)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def summary(self, **filters) -> dict:
        return accounting.summary_query(
            accounting.read_ledger(self.home),
            accounting.QueryFilter(**filters),
            now=accounting.utc_now(),
            boot_id=BOOT,
            pid_alive=lambda pid: False,
        )


# ---------------------------------------------------------------------------
# Unit tests: location, parsing, ledger
# ---------------------------------------------------------------------------


class LocationTests(unittest.TestCase):
    def test_precedence_env_xdg_default(self) -> None:  # AT-28
        home = Path("/h")
        env = {ENV: "/acct", "XDG_STATE_HOME": "/xdg"}
        self.assertEqual(accounting.resolve_accounting_home(env, platform="linux", home=home), Path("/acct").resolve())
        self.assertEqual(
            accounting.resolve_accounting_home({"XDG_STATE_HOME": "/xdg"}, platform="linux", home=home),
            Path("/xdg").resolve() / "claude-autonomous-accounting",
        )
        self.assertEqual(
            accounting.resolve_accounting_home({}, platform="linux", home=home),
            home / ".local" / "state" / "claude-autonomous-accounting",
        )
        # Relative XDG values are ignored per the XDG spec.
        self.assertEqual(
            accounting.resolve_accounting_home({"XDG_STATE_HOME": "rel"}, platform="linux", home=home),
            home / ".local" / "state" / "claude-autonomous-accounting",
        )
        self.assertEqual(
            accounting.resolve_accounting_home({}, platform="darwin", home=home),
            home / "Library" / "Application Support" / "claude-autonomous-accounting",
        )
        self.assertEqual(
            accounting.resolve_accounting_home({"LOCALAPPDATA": "/lad"}, platform="win32", home=home),
            Path("/lad") / "claude-autonomous-accounting",
        )

    def test_run_state_overrides_do_not_move_the_ledger(self) -> None:  # AT-28
        home = Path("/h")
        base = accounting.resolve_accounting_home({}, platform="linux", home=home)
        moved = accounting.resolve_accounting_home(
            {"CLAUDE_AUTONOMOUS_STATE_HOME": "/tmp/disposable"}, platform="linux", home=home
        )
        self.assertEqual(base, moved)

    def test_empty_or_relative_env_refused(self) -> None:  # AT-17
        for value in ("", "   ", "relative/dir"):
            with self.assertRaises(accounting.AccountingError):
                accounting.resolve_accounting_home({ENV: value})

    def test_require_outside(self) -> None:  # AT-17
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(accounting.AccountingError):
                accounting.require_outside(root / "acct", root)
            with self.assertRaises(accounting.AccountingError):
                accounting.require_outside(root, root)
            accounting.require_outside(root.parent / "elsewhere-acct", root)


class UsageParsingTests(unittest.TestCase):
    def classify(self, *usages: object, truncated: bool = False) -> dict:
        return accounting.parse_exec_stdout(ndjson(*usages), truncated=truncated)["usage"]

    def test_complete(self) -> None:  # AT-01
        usage = self.classify(FULL_USAGE)
        self.assertEqual(usage["status"], "complete")
        self.assertEqual(usage["counters"], FULL_USAGE)
        self.assertEqual(usage["source"], accounting.USAGE_SOURCE)
        self.assertEqual(accounting.total_tokens(usage["counters"]), 1120)

    def test_partial_cases(self) -> None:  # AT-11, AT-14, AT-15
        missing = self.classify({"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 2})
        self.assertEqual((missing["status"], missing["flags"]), ("partial", ["missing_counters"]))
        self.assertIsNone(missing["counters"]["reasoning_output_tokens"])
        multiple = self.classify(FULL_USAGE, {**FULL_USAGE, "input_tokens": 2000})
        self.assertEqual(multiple["status"], "partial")
        self.assertIn("multiple_usage_events", multiple["flags"])
        self.assertEqual(multiple["counters"]["input_tokens"], 2000)
        truncated = self.classify(FULL_USAGE, truncated=True)
        self.assertEqual((truncated["status"], truncated["flags"]), ("partial", ["stdout_truncated"]))

    def test_unavailable(self) -> None:  # AT-03, AT-10
        usage = self.classify()
        self.assertEqual(usage["status"], "unavailable")
        self.assertIsNone(usage["source"])
        self.assertEqual(self.classify(truncated=True)["flags"], ["stdout_truncated"])

    def test_invalid_counters(self) -> None:  # AT-31
        cases = {
            "negative": {**FULL_USAGE, "output_tokens": -1, "reasoning_output_tokens": 0},
            "non_integer": {**FULL_USAGE, "input_tokens": 1.5},
            "cached_exceeds_input": {**FULL_USAGE, "cached_input_tokens": 1001},
            "reasoning_exceeds_output": {**FULL_USAGE, "reasoning_output_tokens": 121},
        }
        for reason, usage in cases.items():
            with self.subTest(reason=reason):
                result = self.classify(usage)
                self.assertEqual(result["status"], "invalid")
                self.assertIn(reason, result["invalid_reasons"])
        self.assertIn("non_integer", self.classify({**FULL_USAGE, "input_tokens": True})["invalid_reasons"])
        self.assertEqual(self.classify("not-an-object")["invalid_reasons"], ["usage_not_object"])

    def test_thread_id_and_garbage_lines(self) -> None:
        text = "garbage\n[1,2]\n" + ndjson(FULL_USAGE) + '{"type":"thread.started","thread_id":"x y"}\n'
        parsed = accounting.parse_exec_stdout(text, truncated=False)
        self.assertEqual(parsed["thread_id"], THREAD)
        self.assertEqual(parsed["usage"]["status"], "complete")


class LedgerTests(_TempDirs):
    def test_reserve_then_settle_is_fsynced(self) -> None:  # AT-01, G1/G2
        calls: list[int] = []
        real_fsync = os.fsync

        def counting_fsync(fd: int) -> None:
            calls.append(fd)
            real_fsync(fd)

        with mock.patch.object(accounting.os, "fsync", counting_fsync):
            res = self.ledger.reserve(make_reservation())
            after_reserve = len(calls)
            settle_published(self.ledger, res)
        self.assertGreaterEqual(after_reserve, 2)  # meta + event (+ dirs)
        self.assertGreater(len(calls), after_reserve)
        view = accounting.read_ledger(self.home)
        record = view.attempts[res.attempt_id]
        self.assertEqual(record.settled["settlement"]["result"]["status"], "published")
        self.assertTrue(view.integrity_ok())
        self.assertEqual(oct((self.home / "v1").stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.home / "v1" / "codex-attempts.jsonl").stat().st_mode & 0o777), "0o600")

    def test_duplicate_and_conflicting_settlement(self) -> None:  # AT-23, AT-24
        res = self.ledger.reserve(make_reservation())
        tracker = accounting.AttemptTracker(self.ledger, res)
        tracker.record_process(
            returncode=0, timed_out=False, duration_seconds=1.0,
            stdout_text=ndjson(FULL_USAGE), stdout_truncated=False, observed_model=None,
        )
        settlement = tracker.success_settlement()
        self.assertEqual(self.ledger.settle(res.attempt_id, settlement), "written")
        self.assertEqual(self.ledger.settle(res.attempt_id, settlement), "duplicate")
        other = json.loads(json.dumps(settlement))
        other["result"] = {"status": "rejected", "reason": "schema_invalid"}
        with self.assertRaises(accounting.SettlementConflict):
            self.ledger.settle(res.attempt_id, other)
        lines = (self.home / "v1" / "codex-attempts.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        with self.assertRaises(accounting.AccountingError):
            self.ledger.settle("cxa-20260101T000000Z-0000000000000000", settlement)

    def test_replayed_ledger_is_not_double_counted(self) -> None:  # AT-25
        for _ in range(3):
            settle_published(self.ledger, self.ledger.reserve(make_reservation(run_id="R")))
        before = self.summary()
        events = self.home / "v1" / "codex-attempts.jsonl"
        events.write_bytes(events.read_bytes() * 2)
        after = self.summary()
        self.assertEqual(after["tokens"], before["tokens"])
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["diagnostics"]["duplicate_events"], 6)
        self.assertTrue(after["completeness"]["tokens_complete"])

    def test_retry_linkage(self) -> None:  # AT-26
        first = self.ledger.reserve(make_reservation())
        settle_rejected(self.ledger, first, "schema_invalid")
        second = self.ledger.reserve(make_reservation())
        self.assertEqual((second.retry_of, second.slot_attempt), (first.attempt_id, 2))
        self.assertFalse(second.predecessor_unsettled)
        settle_published(self.ledger, second)
        third = self.ledger.reserve(make_reservation())  # same slot after a publish
        self.assertIsNone(third.retry_of)
        self.assertEqual(third.slot_attempt, 3)
        fourth = self.ledger.reserve(make_reservation())  # predecessor still unsettled
        self.assertEqual(fourth.retry_of, third.attempt_id)
        self.assertTrue(fourth.predecessor_unsettled)
        other_slot = self.ledger.reserve(make_reservation(slot="review-02"))
        other_run = self.ledger.reserve(make_reservation(run_id="RUN2"))
        self.assertIsNone(other_slot.retry_of)
        self.assertIsNone(other_run.retry_of)

    def test_concurrent_writers(self) -> None:  # AT-22
        workers, per_worker = 6, 10
        script = self.tmp / "worker.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import json, sys
                from pathlib import Path
                sys.path.insert(0, {str(SCRIPTS)!r})
                import accounting
                home, count, reservation = Path(sys.argv[1]), int(sys.argv[2]), json.loads(sys.argv[3])
                stdout = sys.argv[4]
                ledger = accounting.Ledger(home)
                for _ in range(count):
                    res = ledger.reserve(reservation)
                    tracker = accounting.AttemptTracker(ledger, res)
                    tracker.record_process(returncode=0, timed_out=False, duration_seconds=1.0,
                                           stdout_text=stdout, stdout_truncated=False, observed_model=None)
                    assert tracker.settle(tracker.success_settlement()) is None
                """
            ),
            encoding="utf-8",
        )
        procs = [
            subprocess.Popen(
                [
                    sys.executable, str(script), str(self.home), str(per_worker),
                    json.dumps(make_reservation(run_id=f"W{i}")), ndjson(FULL_USAGE),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(workers)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=300)
            self.assertEqual(proc.returncode, 0, err)
        result = self.summary()
        total = workers * per_worker
        self.assertEqual(result["attempts"], {"total": total, "settled": total, "unsettled": 0, "stale": 0})
        self.assertEqual(result["tokens"]["complete_only"]["input_tokens"], 1000 * total)
        self.assertTrue(result["completeness"]["tokens_complete"])
        lines = (self.home / "v1" / "codex-attempts.jsonl").read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual(len(lines) - 1, 2 * total)
        for i in range(workers):  # per-worker retry chains are untouched by interleaving
            attempts = accounting.attempts_query(
                accounting.read_ledger(self.home), accounting.QueryFilter(run_id=f"W{i}"),
                now=accounting.utc_now(), boot_id=None,
            )["attempts"]
            self.assertEqual(sorted(a["slot_attempt"] for a in attempts), list(range(1, per_worker + 1)))

    def test_queries_are_read_only(self) -> None:  # AT-29
        missing = self.summary()
        self.assertFalse(missing["ledger"]["present"])
        self.assertFalse(self.home.exists())
        res = self.ledger.reserve(make_reservation())
        settle_published(self.ledger, res)
        self.ledger.reserve(make_reservation(slot="review-02"))
        before = tree_snapshot(self.home)
        view = accounting.read_ledger(self.home)
        now = accounting.utc_now()
        accounting.summary_query(view, accounting.QueryFilter(), now=now, boot_id=BOOT)
        accounting.attempts_query(view, accounting.QueryFilter(), now=now, boot_id=BOOT, unsettled_only=True)
        accounting.attempt_query(view, res.attempt_id, now=now, boot_id=BOOT)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            controller.main(["accounting", "summary", "--ledger-dir", str(self.home)])
            controller.main(["accounting", "attempts", "--ledger-dir", str(self.home), "--unsettled"])
            controller.main(["accounting", "show", res.attempt_id, "--ledger-dir", str(self.home)])
        self.assertEqual(tree_snapshot(self.home), before)

    def test_query_formats(self) -> None:  # AT-30
        res = self.ledger.reserve(make_reservation())
        settle_published(self.ledger, res)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(controller.main(["accounting", "summary", "--ledger-dir", str(self.home)]), 0)
            controller.main(["accounting", "attempts", "--ledger-dir", str(self.home), "--limit", "1"])
            controller.main(["accounting", "show", res.attempt_id, "--ledger-dir", str(self.home)])
        summary, attempts, show = (json.loads(line) for line in out.getvalue().splitlines())
        self.assertEqual(summary["format"], "claude-autonomous.accounting-summary/1")
        self.assertEqual(
            set(summary),
            {"format", "generated_at", "ledger", "filters", "attempts", "execution", "result",
             "usage", "tokens", "completeness", "diagnostics"},
        )
        self.assertEqual(summary["tokens"]["complete_only"]["total_tokens"], 1120)
        self.assertEqual(summary["tokens"]["complete_only"]["cached_input_tokens"], 800)
        self.assertEqual(attempts["format"], "claude-autonomous.accounting-attempts/1")
        self.assertEqual(attempts["attempts"][0]["usage"]["total_tokens"], 1120)
        self.assertEqual(show["format"], "claude-autonomous.accounting-attempt/1")
        self.assertEqual(len(show["events"]), 2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(
                controller.main(["accounting", "show", "cxa-20260101T000000Z-0000000000000000",
                                 "--ledger-dir", str(self.home)]),
                2,
            )

    def test_malformed_lines_and_torn_tail(self) -> None:  # AT-32
        settle_published(self.ledger, self.ledger.reserve(make_reservation()))
        events = self.home / "v1" / "codex-attempts.jsonl"
        good = events.read_text().splitlines()[0]
        extra_key = json.loads(good)
        extra_key["prompt"] = "secret"
        with events.open("a") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"format": "other/9", "x": 1}) + "\n")
            fh.write(json.dumps(extra_key) + "\n")
            fh.write('{"format": "claude-autonomous.codex-attempt-event/1", "torn')
        result = self.summary()
        diag = result["diagnostics"]
        self.assertEqual(diag["malformed_lines"], 1)
        self.assertEqual(diag["unsupported_format"], 1)
        self.assertEqual(diag["invalid_events"], 1)
        self.assertEqual(diag["duplicate_events"], 0)
        self.assertTrue(diag["incomplete_tail"])
        self.assertEqual(result["attempts"]["settled"], 1)
        self.assertFalse(result["completeness"]["tokens_complete"])
        # The next append starts on a fresh line; the fragment becomes one malformed line.
        res = self.ledger.reserve(make_reservation(slot="review-02"))
        after = self.summary()
        self.assertFalse(after["diagnostics"]["incomplete_tail"])
        self.assertEqual(after["diagnostics"]["malformed_lines"], 2)
        self.assertIn(res.attempt_id, accounting.read_ledger(self.home).attempts)

    def test_writer_fold_tolerates_shape_broken_lines(self) -> None:  # AT-32
        first = self.ledger.reserve(make_reservation())
        events = self.home / "v1" / "codex-attempts.jsonl"
        with events.open("a") as fh:
            fh.write(json.dumps({"format": accounting.EVENT_FORMAT, "event_type": "attempt.reserved",
                                 "event_id": "x", "attempt_id": "y", "ledger_id": "z"}) + "\n")
            fh.write(json.dumps({"format": accounting.EVENT_FORMAT, "event_type": "attempt.settled",
                                 "event_id": "x2", "attempt_id": first.attempt_id, "ledger_id": "z",
                                 "settlement": {"result": None}}) + "\n")
        second = self.ledger.reserve(make_reservation())
        self.assertEqual(second.retry_of, first.attempt_id)
        self.assertTrue(second.predecessor_unsettled)
        self.assertEqual(accounting.read_ledger(self.home, validate=False).diagnostics["invalid_events"], 2)
        self.assertEqual(self.summary()["diagnostics"]["invalid_events"], 2)

    def test_semantically_invalid_and_orphan_events(self) -> None:  # AT-31, AT-33
        res = self.ledger.reserve(make_reservation())
        tracker = accounting.AttemptTracker(self.ledger, res)
        tracker.record_process(
            returncode=0, timed_out=False, duration_seconds=1.0,
            stdout_text=ndjson(FULL_USAGE), stdout_truncated=False, observed_model=None,
        )
        bogus = tracker.success_settlement()
        bogus["usage"]["counters"]["cached_input_tokens"] = 5000  # schema-valid, semantically not
        ledger_id = self.ledger._ensure_meta()
        # Reader fixtures: corrupt history is injected as raw bytes, because the
        # public writer refuses to append either of these events.
        inject_events(
            self.home,
            self.ledger._event("attempt.settled", res.attempt_id, ledger_id, accounting.utc_now(), settlement=bogus),
            self.ledger._event(
                "attempt.settled", "cxa-20260101T000000Z-00000000000000ff", ledger_id,
                accounting.utc_now(), settlement=tracker.success_settlement(),
            ),
        )
        result = self.summary()
        self.assertEqual(result["diagnostics"]["invalid_events"], 1)
        self.assertEqual(result["diagnostics"]["orphan_settlements"], 1)
        self.assertEqual(result["attempts"]["unsettled"], 1)
        self.assertEqual(result["tokens"]["complete_only"]["attempts"], 0)

    def test_invalid_usage_excluded_from_totals(self) -> None:  # AT-31
        settle_published(self.ledger, self.ledger.reserve(make_reservation()))
        settle_published(
            self.ledger,
            self.ledger.reserve(make_reservation(slot="review-02")),
            {**FULL_USAGE, "cached_input_tokens": 999999},
        )
        result = self.summary()
        self.assertEqual(result["usage"], {"complete": 1, "invalid": 1})
        self.assertEqual(result["tokens"]["complete_only"]["input_tokens"], 1000)
        self.assertEqual(result["tokens"]["including_partial"]["input_tokens"], 1000)
        self.assertFalse(result["completeness"]["tokens_complete"])

    def test_corrupt_meta_refuses_writes(self) -> None:  # AT-34
        (self.home / "v1").mkdir(parents=True)
        meta = self.home / "v1" / "ledger.json"
        meta.write_text("{broken", encoding="utf-8")
        with self.assertRaises(accounting.AccountingError):
            self.ledger.reserve(make_reservation())
        self.assertEqual(meta.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(self.summary()["diagnostics"]["ledger_meta"], "invalid")

    # -- durability fault injection (does not prove power-loss durability) ----

    def events_path(self) -> Path:
        return self.home / "v1" / "codex-attempts.jsonl"

    def meta_path(self) -> Path:
        return self.home / "v1" / "ledger.json"

    def test_directory_sync_io_errors_refuse(self) -> None:  # AT-40
        for error in (errno.EIO, errno.ENOSPC):
            for stage in ("ledger-dirs", "metadata", "events-file"):
                with self.subTest(error=errno.errorcode[error], stage=stage):
                    shutil.rmtree(self.home, ignore_errors=True)
                    if stage != "ledger-dirs":
                        self.ledger._ensure_dir()
                    if stage == "events-file":
                        ledger_id = self.ledger._ensure_meta()
                    # The directory whose sync completes this stage's creation.
                    target = self.home.parent if stage == "ledger-dirs" else self.home / "v1"
                    with mock.patch.object(accounting.os, "fsync", fail_fsync_on("dir", error, target)):
                        with self.assertRaises(accounting.AccountingError) as ctx:
                            self.ledger.reserve(make_reservation())
                    self.assertIn("Cannot sync directory", str(ctx.exception))
                    # Nothing half-created is left behind for later writers to trip on.
                    self.assertFalse(self.events_path().exists())
                    if stage == "events-file":
                        self.assertEqual(self.ledger._ensure_meta(), ledger_id)
                    elif stage == "metadata":
                        self.assertFalse(self.meta_path().exists())
                    else:
                        self.assertFalse(self.home.exists())
                    res = self.ledger.reserve(make_reservation())  # fault cleared: recovers
                    view = accounting.read_ledger(self.home)
                    self.assertEqual(list(view.attempts), [res.attempt_id])
                    self.assertTrue(view.integrity_ok())

    def test_directory_sync_unsupported_is_skipped(self) -> None:  # AT-40
        for error in (errno.EINVAL, errno.ENOTSUP):
            with self.subTest(error=errno.errorcode[error]):
                shutil.rmtree(self.home, ignore_errors=True)
                with mock.patch.object(accounting.os, "fsync", fail_fsync_on("dir", error)):
                    res = self.ledger.reserve(make_reservation())
                self.assertIn(res.attempt_id, accounting.read_ledger(self.home).attempts)
        shutil.rmtree(self.home)
        opened: list[str] = []
        real_open = os.open

        def tracking_open(path, flags, *args):
            opened.append(str(path))
            return real_open(path, flags, *args)

        with mock.patch.object(accounting, "_DIR_FSYNC_SUPPORTED", False), \
                mock.patch.object(accounting.os, "open", tracking_open):
            self.ledger.reserve(make_reservation())
        self.assertTrue(opened)
        self.assertFalse([p for p in opened if Path(p).is_dir()])  # no directory was opened

    def test_metadata_write_failures_leave_no_metadata(self) -> None:  # AT-41
        real_write = os.write

        def short_write(fd: int, data: bytes) -> int:
            return real_write(fd, data[: len(data) // 2])

        faults = {
            "short-write": mock.patch.object(accounting.os, "write", short_write),
            "write-enospc": mock.patch.object(
                accounting.os, "write", side_effect=OSError(errno.ENOSPC, "No space left on device")
            ),
            "fsync-eio": mock.patch.object(accounting.os, "fsync", fail_fsync_on("file", errno.EIO)),
        }
        for name, fault in faults.items():
            with self.subTest(fault=name):
                shutil.rmtree(self.home, ignore_errors=True)
                with fault, self.assertRaises(accounting.AccountingError) as ctx:
                    self.ledger.reserve(make_reservation())
                self.assertIn("Cannot durably create", str(ctx.exception))
                self.assertFalse(self.meta_path().exists())
                self.assertFalse(self.events_path().exists())
                res = self.ledger.reserve(make_reservation())
                self.assertTrue(accounting.read_ledger(self.home).integrity_ok())
                self.assertIn(res.attempt_id, accounting.read_ledger(self.home).attempts)

    def test_partial_metadata_from_a_crash_refuses_without_repair(self) -> None:  # AT-41, AT-34
        (self.home / "v1").mkdir(parents=True)
        self.meta_path().write_bytes(b'{"format":"claude-autonomous.acc')  # creation cut short
        with self.assertRaises(accounting.AccountingError) as ctx:
            self.ledger.reserve(make_reservation())
        self.assertIn("move ledger.json aside", str(ctx.exception))
        self.assertEqual(self.meta_path().read_bytes(), b'{"format":"claude-autonomous.acc')
        self.events_path().write_bytes(b'{"some":"recorded event"}\n')
        with self.assertRaises(accounting.AccountingError) as ctx:
            self.ledger.reserve(make_reservation())
        self.assertIn("do not delete or replace", str(ctx.exception))
        self.assertEqual(self.events_path().read_bytes(), b'{"some":"recorded event"}\n')

    def test_short_event_append_is_isolated_by_the_next_writer(self) -> None:  # AT-41, AT-32
        first = self.ledger.reserve(make_reservation())
        real_write = os.write

        def short_write(fd: int, data: bytes) -> int:
            return real_write(fd, data[:20])

        with mock.patch.object(accounting.os, "write", short_write):
            with self.assertRaises(accounting.AccountingError):
                self.ledger.reserve(make_reservation(slot="review-02"))
        second = self.ledger.reserve(make_reservation(slot="review-03"))
        view = accounting.read_ledger(self.home)
        self.assertEqual(sorted(view.attempts), sorted([first.attempt_id, second.attempt_id]))
        self.assertEqual(view.diagnostics["malformed_lines"], 1)
        self.assertFalse(view.diagnostics["incomplete_tail"])

    # -- every writer confirms the ledger's directory entries itself ---------

    def spy_dir_syncs(self, record: list, fault=None):
        """Record (thread name, directory) for each directory sync; `fault(path)`
        may block or raise to emulate another writer's progress or failure."""
        real_fsync_dir = accounting._fsync_dir

        def fsync_dir(path: Path) -> None:
            record.append((threading.current_thread().name, Path(path)))
            if fault is not None:
                fault(Path(path))
            real_fsync_dir(path)

        return mock.patch.object(accounting, "_fsync_dir", fsync_dir)

    def ledger_entries(self) -> set[Path]:
        return {self.home.parent, self.home, self.home / "v1"}

    def test_writer_confirms_directories_left_by_interrupted_initialization(self) -> None:  # AT-45
        def home_only() -> None:
            self.home.mkdir(mode=0o700)

        def all_but_syncs() -> None:  # dirs, valid metadata and empty events file, never synced
            (self.home / "v1").mkdir(parents=True, mode=0o700)
            self.meta_path().write_text(
                json.dumps({"format": accounting.LEDGER_FORMAT, "ledger_id": "0" * 32,
                            "created_at": "2026-10-19T00:00:00+00:00"}),
                encoding="utf-8",
            )
            self.events_path().write_bytes(b"")

        for name, leftover in (("home-only", home_only), ("all-but-syncs", all_but_syncs)):
            with self.subTest(leftover=name):
                shutil.rmtree(self.home, ignore_errors=True)
                leftover()
                synced: list = []
                with self.spy_dir_syncs(synced):
                    res = self.ledger.reserve(make_reservation())
                self.assertLessEqual(self.ledger_entries(), {path for _, path in synced})
                self.assertIn(res.attempt_id, accounting.read_ledger(self.home).attempts)
                # A writer that cannot confirm the entry refuses instead of trusting it.
                shutil.rmtree(self.home)
                leftover()
                before = self.events_path().read_bytes() if self.events_path().exists() else None

                def eio_on_home_entry(path: Path) -> None:
                    if path == self.home.parent:
                        raise accounting.AccountingError(f"Cannot sync directory {path}: EIO")

                with self.spy_dir_syncs([], eio_on_home_entry), self.assertRaises(accounting.AccountingError):
                    self.ledger.reserve(make_reservation())
                after = self.events_path().read_bytes() if self.events_path().exists() else None
                self.assertEqual(after, before)

    def test_writer_does_not_rely_on_a_concurrent_initializer(self) -> None:  # AT-45
        """Barrier interleaving: writer A creates the accounting home and pauses before
        syncing its entry; writer B initializes the rest and reserves; A then fails
        and cannot remove the now-populated home."""
        for persistent_fault in (False, True):
            with self.subTest(persistent_fault=persistent_fault):
                shutil.rmtree(self.home, ignore_errors=True)
                paused, release = threading.Event(), threading.Event()
                synced: list = []

                def fault(path: Path) -> None:
                    if path != self.home.parent:
                        return
                    if threading.current_thread().name == "writer-A":
                        paused.set()
                        if not release.wait(30):
                            raise AssertionError("barrier timeout")
                        raise accounting.AccountingError(f"Cannot sync directory {path}: EIO")
                    if persistent_fault:
                        raise accounting.AccountingError(f"Cannot sync directory {path}: EIO")

                a_errors: list = []

                def writer_a() -> None:
                    try:
                        accounting.Ledger(self.home).reserve(make_reservation(slot="review-01"))
                    except accounting.AccountingError as exc:
                        a_errors.append(exc)

                with self.spy_dir_syncs(synced, fault):
                    thread_a = threading.Thread(target=writer_a, name="writer-A")
                    thread_a.start()
                    self.addCleanup(release.set)
                    self.assertTrue(paused.wait(30), "writer A never reached its sync")
                    self.assertTrue(self.home.is_dir())  # created by A, entry not yet synced
                    if persistent_fault:
                        with self.assertRaises(accounting.AccountingError):
                            self.ledger.reserve(make_reservation(slot="review-02"))
                        b_attempt = None
                    else:
                        b_attempt = self.ledger.reserve(make_reservation(slot="review-02")).attempt_id
                    release.set()
                    thread_a.join(30)
                self.assertFalse(thread_a.is_alive())
                self.assertEqual(len(a_errors), 1)
                b_synced = {path for thread, path in synced if thread != "writer-A"}
                self.assertIn(self.home.parent, b_synced)  # B confirmed the entry itself
                view = accounting.read_ledger(self.home)
                if b_attempt is None:
                    self.assertEqual(view.attempts, {})
                else:
                    self.assertEqual(list(view.attempts), [b_attempt])
                    self.assertTrue(view.integrity_ok())

    # -- settlement consistency and historical-corruption policy ------------

    def test_public_settle_rejects_semantically_invalid_settlements(self) -> None:  # AT-42
        res = self.ledger.reserve(make_reservation())
        tracker = accounting.AttemptTracker(self.ledger, res)
        tracker.record_process(
            returncode=0, timed_out=False, duration_seconds=1.0,
            stdout_text=ndjson(FULL_USAGE), stdout_truncated=False, observed_model=None,
        )
        valid = tracker.success_settlement()

        def variant(**usage_changes) -> dict:
            settlement = copy.deepcopy(valid)
            for key, value in usage_changes.items():
                if key in accounting.COUNTER_NAMES:
                    settlement["usage"]["counters"][key] = value
                else:
                    settlement["usage"][key] = value
            return settlement

        cases = {
            "complete-cached-exceeds-input": variant(cached_input_tokens=1001),
            "complete-reasoning-exceeds-output": variant(reasoning_output_tokens=121),
            "complete-missing-counter": variant(output_tokens=None),
            "complete-with-invalid-reasons": variant(invalid_reasons=["negative"]),
            "invalid-without-reasons": variant(status="invalid"),
            "unavailable-with-counters": variant(status="unavailable", source=None),
        }
        before = self.events_path().read_bytes()
        for name, settlement in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(accounting.AccountingError) as ctx:
                    self.ledger.settle(res.attempt_id, settlement)
                self.assertNotIsInstance(ctx.exception, accounting.SettlementConflict)
                self.assertIn("semantically invalid", str(ctx.exception))
                self.assertEqual(self.events_path().read_bytes(), before)
        self.assertIsNone(accounting.read_ledger(self.home).attempts[res.attempt_id].settled)
        self.assertEqual(self.ledger.settle(res.attempt_id, valid), "written")
        self.assertTrue(accounting.read_ledger(self.home).integrity_ok())

    def test_missing_metadata_with_events_refuses_and_preserves_history(self) -> None:  # AT-43
        res = self.ledger.reserve(make_reservation())
        settle_published(self.ledger, self.ledger.reserve(make_reservation(slot="review-02")))
        self.meta_path().unlink()
        before = self.events_path().read_bytes()
        with self.assertRaises(accounting.AccountingError) as ctx:
            self.ledger.reserve(make_reservation(slot="review-03"))
        self.assertIn("would relabel", str(ctx.exception))
        with self.assertRaises(accounting.AccountingError):
            self.ledger.settle(res.attempt_id, accounting.operator_abandoned_settlement())
        self.assertFalse(self.meta_path().exists())
        self.assertEqual(self.events_path().read_bytes(), before)
        result = self.summary()
        self.assertEqual(result["diagnostics"]["ledger_meta"], "missing_with_events")
        self.assertEqual(result["attempts"]["total"], 2)
        self.assertFalse(result["completeness"]["tokens_complete"])

    def test_conflicting_duplicate_event_ids_are_diagnosed_not_repaired(self) -> None:  # AT-43
        first = self.ledger.reserve(make_reservation())
        original = self.events_path().read_bytes()
        forged = json.loads(original.splitlines()[0])
        forged["reservation"]["prompt_characters"] = 999  # same event_id, different content
        inject_events(self.home, forged)
        injected = self.events_path().read_bytes()
        # Append-with-diagnostics: a writer still records the next attempt.
        second = self.ledger.reserve(make_reservation(slot="review-02"))
        self.assertTrue(self.events_path().read_bytes().startswith(injected))
        result = self.summary()
        self.assertEqual(result["diagnostics"]["conflicting_duplicate_events"], 1)
        self.assertEqual(result["diagnostics"]["duplicate_events"], 0)
        self.assertFalse(result["completeness"]["tokens_complete"])
        view = accounting.read_ledger(self.home)
        self.assertEqual(view.attempts[first.attempt_id].reservation["prompt_characters"], 10)
        self.assertIn(second.attempt_id, view.attempts)

    def published_settlement(self, res: accounting.Reservation, **counters) -> dict:
        tracker = accounting.AttemptTracker(self.ledger, res)
        tracker.record_process(
            returncode=0, timed_out=False, duration_seconds=1.0,
            stdout_text=ndjson({**FULL_USAGE, **counters}), stdout_truncated=False,
            observed_model=None,
        )
        return tracker.success_settlement()

    def inject_settlement(self, res: accounting.Reservation, settlement: dict, **overrides) -> None:
        event = self.ledger._event(
            "attempt.settled", res.attempt_id, res.ledger_id, accounting.utc_now(),
            settlement=settlement,
        )
        inject_events(self.home, {**event, **overrides})

    def test_writers_ignore_invalid_earlier_settlements_like_queries(self) -> None:  # AT-31, AT-33
        semantic = self.ledger.reserve(make_reservation())
        schema = self.ledger.reserve(make_reservation(slot="review-02"))
        bad_counters = self.published_settlement(semantic)
        bad_counters["usage"]["counters"]["cached_input_tokens"] = 5000  # schema-valid only
        self.inject_settlement(semantic, bad_counters)
        self.inject_settlement(schema, self.published_settlement(schema), prompt="x")  # fold shape only
        before = self.summary()
        self.assertEqual(before["attempts"]["unsettled"], 2)
        self.assertEqual(before["diagnostics"]["invalid_events"], 2)
        # Both attempts stay settleable: the invalid lines are not authoritative.
        self.assertEqual(
            self.ledger.settle(semantic.attempt_id, self.published_settlement(semantic)), "written"
        )
        self.assertEqual(self.ledger.reconcile_abandoned(schema.attempt_id, boot_id=OTHER_BOOT), "written")
        after = self.summary()
        self.assertEqual(after["attempts"]["settled"], 2)
        self.assertEqual(after["result"], {"published": 1, "unknown": 1})
        self.assertEqual(after["diagnostics"]["invalid_events"], 2)
        self.assertEqual(after["diagnostics"]["conflicting_settlements"], 0)

    def test_retry_linkage_ignores_an_invalid_settlement_like_queries(self) -> None:  # AT-26, AT-33
        first = self.ledger.reserve(make_reservation())
        bogus = self.published_settlement(first)
        bogus["usage"]["counters"]["reasoning_output_tokens"] = 5000
        self.inject_settlement(first, bogus)
        second = self.ledger.reserve(make_reservation())
        self.assertEqual((second.retry_of, second.slot_attempt), (first.attempt_id, 2))
        self.assertTrue(second.predecessor_unsettled)
        self.assertIsNone(accounting.read_ledger(self.home).attempts[first.attempt_id].settled)

    def test_calendar_invalid_timestamps_are_invalid_events(self) -> None:  # AT-31, AT-30
        good = self.ledger.reserve(make_reservation())
        line = json.loads(self.events_path().read_text().splitlines()[0])
        bad = {
            **line,
            "event_id": "f" * 32,
            "attempt_id": "cxa-20260101T000000Z-00000000000000ee",
            "recorded_at": "2026-99-19T12:00:00+00:00",
        }
        inject_events(self.home, bad)
        result = self.summary()
        self.assertEqual(result["diagnostics"]["invalid_events"], 1)
        self.assertEqual(result["attempts"]["total"], 1)
        for argv in (
            ["summary"],
            ["summary", "--since", "2026-01-01T00:00:00Z"],
            ["attempts", "--unsettled"],
            ["show", good.attempt_id],
        ):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(
                        controller.main(["accounting", *argv, "--ledger-dir", str(self.home)]), 0
                    )
                self.assertEqual(json.loads(out.getvalue())["diagnostics"]["invalid_events"], 1)
        with self.assertRaises(accounting.AccountingError) as ctx:
            accounting._validate_new_event(bad)
        self.assertIn("invalid_recorded_at", str(ctx.exception))

    def test_timestamp_validity_does_not_depend_on_the_python_version(self) -> None:  # AT-31, AT-30
        # datetime.fromisoformat accepts hour 24 from Python 3.14 on; the schema
        # pattern alone decides it, so the same line is invalid on every version.
        self.ledger.reserve(make_reservation())
        line = json.loads(self.events_path().read_text().splitlines()[0])
        utc = dt.timezone.utc
        valid = [
            accounting._iso(moment)  # exactly what the writer emits
            for moment in (
                dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=utc),
                dt.datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=utc),
                dt.datetime(2028, 2, 29, 12, 0, 0, tzinfo=utc),
                dt.datetime(2027, 1, 1, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=2))),
            )
        ] + ["2026-01-01T23:59:59Z"]
        self.assertIn("2026-12-31T23:59:59+00:00", valid)
        invalid = [
            "2026-01-01T24:00:00+00:00",
            "2026-12-31T24:00:00Z",
            "2026-01-01T24:00:01+00:00",
            "2026-01-01T23:60:00+00:00",
            "2026-01-01T23:59:60+00:00",
            "2026-02-30T00:00:00+00:00",
        ]
        events = [
            {
                **line,
                "event_id": f"{index:032x}",
                "attempt_id": f"cxa-20260101T000000Z-{index:016x}",
                "recorded_at": recorded_at,
            }
            for index, recorded_at in enumerate(valid + invalid, start=1)
        ]
        for event in events:
            with self.subTest(recorded_at=event["recorded_at"]):
                if event["recorded_at"] in valid:
                    accounting._validate_new_event(event)
                else:
                    with self.assertRaises(accounting.AccountingError):
                        accounting._validate_new_event(event)
        inject_events(self.home, *events)
        result = self.summary()
        self.assertEqual(result["diagnostics"]["invalid_events"], len(invalid))
        self.assertEqual(result["attempts"]["total"], 1 + len(valid))

    def test_foreign_ledger_events_and_absent_ledger_are_not_complete(self) -> None:  # AT-30, AT-43
        self.assertFalse(self.summary()["ledger"]["present"])
        self.assertFalse(self.summary()["completeness"]["tokens_complete"])
        settle_published(self.ledger, self.ledger.reserve(make_reservation()))
        self.assertTrue(self.summary()["completeness"]["tokens_complete"])
        meta = json.loads(self.meta_path().read_text())
        self.meta_path().write_text(json.dumps({**meta, "ledger_id": "e" * 32}))
        result = self.summary()
        self.assertEqual(result["diagnostics"]["foreign_ledger_events"], 2)
        self.assertFalse(result["completeness"]["tokens_complete"])

    def test_existing_metadata_and_duplicate_settlements_are_synced(self) -> None:  # AT-41
        res = self.ledger.reserve(make_reservation())
        settlement = self.published_settlement(res)
        self.assertEqual(self.ledger.settle(res.attempt_id, settlement), "written")
        before = self.events_path().read_bytes()
        # Writers confirm metadata created by someone else before using its id ...
        with mock.patch.object(
            accounting.os, "fsync", fail_fsync_on("file", errno.EIO, only=self.meta_path())
        ), self.assertRaises(accounting.AccountingError) as ctx:
            self.ledger.reserve(make_reservation(slot="review-02"))
        self.assertIn("Cannot sync", str(ctx.exception))
        # ... and a duplicate settlement is only reported once its line is durable.
        with mock.patch.object(
            accounting.os, "fsync", fail_fsync_on("file", errno.EIO, only=self.events_path())
        ), self.assertRaises(accounting.AccountingError):
            self.ledger.settle(res.attempt_id, settlement)
        self.assertEqual(self.events_path().read_bytes(), before)
        self.assertEqual(self.ledger.settle(res.attempt_id, settlement), "duplicate")

    def test_retry_linkage_does_not_cross_contract_generations(self) -> None:  # AT-26, AT-44
        before_refresh = self.ledger.reserve(make_reservation(generation=1))
        settle_rejected(self.ledger, before_refresh, "schema_invalid")
        after_refresh = self.ledger.reserve(make_reservation(generation=2))
        self.assertIsNone(after_refresh.retry_of)
        self.assertEqual(after_refresh.slot_attempt, 1)
        retry = self.ledger.reserve(make_reservation(generation=2))
        self.assertEqual((retry.retry_of, retry.slot_attempt), (after_refresh.attempt_id, 2))

    def test_schema_rejects_unlisted_fields(self) -> None:  # AT-35
        reservation = make_reservation()
        reservation["workflow"]["cwd"] = "/home/someone/repo"
        with self.assertRaises(accounting.AccountingError):
            self.ledger.reserve(reservation)
        self.assertEqual(accounting.read_ledger(self.home).attempts, {})

    def test_owner_process_and_staleness(self) -> None:  # AT-18
        reservation = make_reservation(timeout=60.0)
        self.assertEqual(accounting.owner_process_state(reservation, boot_id=OTHER_BOOT), "gone")
        self.assertEqual(accounting.owner_process_state(reservation, boot_id=None), "unknown")
        self.assertEqual(
            accounting.owner_process_state(reservation, boot_id=BOOT, pid_alive=lambda pid: True), "alive"
        )
        self.assertEqual(
            accounting.owner_process_state(reservation, boot_id=BOOT, pid_alive=lambda pid: False), "gone"
        )
        self.assertFalse(accounting.is_stale(reservation, 300))
        self.assertTrue(accounting.is_stale(reservation, 361))
        self.assertFalse(accounting.is_stale(make_reservation(timeout=None), 3600))
        self.assertTrue(accounting.is_stale(make_reservation(timeout=None), 24 * 3600 + 1))
        res = self.ledger.reserve(reservation, now=accounting.utc_now() - dt.timedelta(hours=2))
        record = accounting.attempts_query(
            accounting.read_ledger(self.home), accounting.QueryFilter(),
            now=accounting.utc_now(), boot_id=OTHER_BOOT, unsettled_only=True,
        )["attempts"][0]
        self.assertEqual(record["attempt_id"], res.attempt_id)
        self.assertTrue(record["stale"])
        self.assertEqual(record["owner_process"], "gone")

    def test_operator_reconcile(self) -> None:  # AT-21
        stale = self.ledger.reserve(make_reservation())
        with self.assertRaises(accounting.AccountingError):
            self.ledger.reconcile_abandoned(stale.attempt_id, boot_id=BOOT, pid_alive=lambda pid: True)
        self.assertEqual(
            self.ledger.reconcile_abandoned(stale.attempt_id, boot_id=OTHER_BOOT), "written"
        )
        self.assertEqual(
            self.ledger.reconcile_abandoned(stale.attempt_id, boot_id=OTHER_BOOT), "duplicate"
        )
        settled = accounting.read_ledger(self.home).attempts[stale.attempt_id].settled["settlement"]
        self.assertEqual(settled["settled_by"], "operator")
        self.assertEqual(settled["result"], {"status": "unknown", "reason": "operator_abandoned"})
        self.assertEqual(settled["usage"]["status"], "unavailable")
        engine_settled = self.ledger.reserve(make_reservation(slot="review-02"))
        settle_published(self.ledger, engine_settled)
        with self.assertRaises(accounting.SettlementConflict):
            self.ledger.reconcile_abandoned(engine_settled.attempt_id, boot_id=OTHER_BOOT)

    def test_reconcile_refuses_an_unknown_owner_until_stale(self) -> None:  # AT-21, AT-18
        res = self.ledger.reserve(make_reservation(timeout=60.0))
        before = self.events_path().read_bytes()
        unknown_owner = {
            "no-boot-id": {"boot_id": None},  # every non-Linux host
            "pid-check-unavailable": {"boot_id": BOOT, "pid_alive": lambda pid: None},
        }
        for name, kwargs in unknown_owner.items():
            with self.subTest(case=name), self.assertRaises(accounting.AccountingError) as ctx:
                self.ledger.reconcile_abandoned(res.attempt_id, **kwargs)
            self.assertIn("not stale yet", str(ctx.exception))
        self.assertEqual(self.events_path().read_bytes(), before)
        later = accounting.utc_now() + dt.timedelta(seconds=60 + accounting.STALE_GRACE_SECONDS + 1)
        self.assertEqual(
            self.ledger.reconcile_abandoned(res.attempt_id, now=later, boot_id=None), "written"
        )

    def test_failed_reconcile_creates_nothing(self) -> None:  # AT-21, AT-29
        unknown = "cxa-20260101T000000Z-0000000000000000"
        with self.assertRaises(accounting.AccountingError) as ctx:
            self.ledger.reconcile_abandoned(unknown, boot_id=OTHER_BOOT)
        self.assertIn("nothing was written", str(ctx.exception))
        self.assertFalse(self.home.exists())
        self.ledger.reserve(make_reservation())
        before = tree_snapshot(self.home)
        with self.assertRaises(accounting.AccountingError):
            self.ledger.reconcile_abandoned(unknown, boot_id=OTHER_BOOT)
        self.assertEqual(tree_snapshot(self.home), before)

    def test_reservation_sync_failure_names_the_possibly_recorded_attempt(self) -> None:  # AT-41
        self.ledger.reserve(make_reservation())
        with mock.patch.object(
            accounting.os, "fsync", fail_fsync_on("file", errno.EIO, only=self.events_path())
        ), self.assertRaises(accounting.AccountingError) as ctx:
            self.ledger.reserve(make_reservation(slot="review-02"))
        [orphan] = [
            a for a in accounting.read_ledger(self.home).attempts.values()
            if a.reservation["slot"] == "review-02"
        ]
        self.assertIn(orphan.attempt_id, str(ctx.exception))
        self.assertIn("never launched", str(ctx.exception))
        self.assertEqual(
            self.ledger.reconcile_abandoned(orphan.attempt_id, boot_id=OTHER_BOOT), "written"
        )
        # A reservation refused by validation writes nothing and names no attempt.
        bad = make_reservation(slot="review-03")
        bad["workflow"]["cwd"] = "/somewhere"
        with self.assertRaises(accounting.AccountingError) as ctx:
            self.ledger.reserve(bad)
        self.assertNotIn("never launched", str(ctx.exception))

    def test_engine_provenance_digest(self) -> None:
        root = self.tmp / "plugin"
        for rel in ("scripts", "schemas", "prompts", ".claude-plugin"):
            shutil.copytree(ROOT / rel, root / rel, ignore=shutil.ignore_patterns("__pycache__"))
        accounting.engine_provenance.cache_clear()
        try:
            first = accounting.engine_provenance(root)
            self.assertRegex(first["code_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(first["root_kind"], "plain")
            self.assertEqual(first["version"], json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"])
            (root / "prompts" / "code-review.md").write_text("changed", encoding="utf-8")
            accounting.engine_provenance.cache_clear()
            self.assertNotEqual(accounting.engine_provenance(root)["code_digest"], first["code_digest"])
        finally:
            accounting.engine_provenance.cache_clear()


# ---------------------------------------------------------------------------
# Integration: cmd_codex through the fake-process seam
# ---------------------------------------------------------------------------


class CodexAttemptAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "acct"
        self._env = mock.patch.dict(os.environ, {ENV: str(self.home)})
        self._env.start()
        self.repo = self.tmp / "repo"
        self.state_home = self.tmp / "state"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for key, value in (("user.email", "test@example.com"), ("user.name", "Test User")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        (self.repo / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                controller.main([
                    "--project-root", str(self.repo), "--state-dir", str(self.state_home),
                    "init", "--feature", "SENTINEL-FEATURE-TEXT",
                ]),
                0,
            )
        [self.state_path] = list(self.state_home.rglob("run-state.json"))
        self.run_dir = self.state_path.parent
        (self.run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (self.run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = self.load_state()
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        self.save_state(state)
        self.calls: list[list[str]] = []
        self._original_run_process = controller.run_process

    def tearDown(self) -> None:
        controller.run_process = self._original_run_process
        self._env.stop()
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def load_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def fake(
        self,
        *,
        rc: int = 0,
        stdout: str | None = None,
        stderr: str = "",
        payload: object = FULL_REVIEW,
        output_text: str | None = None,
        write_output: bool = True,
        raise_exc: BaseException | None = None,
        on_call=None,
    ) -> None:
        original = self._original_run_process

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None, env=None):
            if cmd and Path(cmd[0]).name in ("git", "git.exe"):
                return original(cmd, cwd=cwd, input_text=input_text, check=check, timeout=timeout, env=env)
            self.calls.append(list(cmd))
            if on_call is not None:
                on_call()
            if raise_exc is not None:
                raise raise_exc
            if write_output:
                out_path = Path(cmd[cmd.index("--output-last-message") + 1])
                out_path.write_text(
                    output_text if output_text is not None else json.dumps(payload), encoding="utf-8"
                )
            return subprocess.CompletedProcess(
                cmd, rc, stdout=ndjson(FULL_USAGE) if stdout is None else stdout, stderr=stderr
            )

        controller.run_process = fake_run_process

    def run_codex(self, phase: str = "review", timeout: float | None = None) -> int:
        args = argparse.Namespace(
            project_root=str(self.repo), state_dir=str(self.state_home), run_id=None,
            phase=phase, timeout=timeout,
        )
        return controller.cmd_codex(args)

    def attempts(self) -> list[dict]:
        return accounting.attempts_query(
            accounting.read_ledger(self.home), accounting.QueryFilter(),
            now=accounting.utc_now(), boot_id=accounting.current_boot_id(),
        )["attempts"]

    def only_attempt(self) -> dict:
        [attempt] = self.attempts()
        return attempt

    def outcome(self, attempt: dict) -> tuple:
        return (
            attempt["execution"]["status"],
            attempt["result"]["status"],
            attempt["result"]["reason"],
            attempt["usage"]["status"],
        )

    def assert_rejected(self, reason: str, usage: str = "complete", **fake_kwargs) -> dict:
        self.fake(**fake_kwargs)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(controller.WorkflowError):
            self.run_codex()
        attempt = self.only_attempt()
        self.assertEqual(attempt["result"], {"status": "rejected", "reason": reason})
        self.assertEqual(attempt["usage"]["status"], usage)
        self.assertEqual(list(self.run_dir.glob(".staging-*")), [])
        return attempt

    # -- outcomes ----------------------------------------------------------

    def test_success(self) -> None:  # AT-01
        self.fake()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_codex(), 0)
        attempt = self.only_attempt()
        self.assertEqual(self.outcome(attempt), ("exited", "published", None, "complete"))
        self.assertEqual(attempt["usage"]["counters"], FULL_USAGE)
        self.assertEqual(attempt["usage"]["total_tokens"], 1120)
        self.assertEqual(attempt["observed"], {"model": "gpt-x-test", "thread_id": THREAD})
        self.assertEqual((attempt["slot"], attempt["refs"]["round"]), ("review-01", 1))
        self.assertEqual(attempt["workflow_kind"], "feature")
        state = self.load_state()
        self.assertEqual(state["codex_runs"][-1]["attempt_id"], attempt["attempt_id"])
        self.assertEqual(state["reviews"][0]["verdict"], "pass")
        reservation = accounting.read_ledger(self.home).attempts[attempt["attempt_id"]].reservation
        self.assertTrue(reservation["workflow"]["state_home_overridden"])
        self.assertEqual(reservation["engine"], accounting.engine_provenance(controller.PLUGIN_ROOT))
        self.assertGreater(reservation["prompt_characters"], 0)
        self.assertIsNone(reservation["workflow"]["review_contract_generation"])

    def test_contract_generation_for_retry_linkage(self) -> None:  # AT-44
        generation = controller._accounting_contract_generation
        self.assertIsNone(generation(self.load_state()))  # feature run
        imported = {"workflow_kind": "existing_pr_review", "review_target": {}}
        self.assertIsNone(generation(imported))
        for value, expected in ((1, 1), (3, 3), (0, None), (True, None), ("2", None)):
            with self.subTest(value=value):
                imported["review_target"]["review_contract_generation"] = value
                self.assertEqual(generation(imported), expected)

    def test_nonzero_exit_keeps_usage(self) -> None:  # AT-02
        attempt = self.assert_rejected("nonzero_exit", rc=1, stderr="boom")
        self.assertEqual(attempt["execution"]["exit_code"], 1)
        self.assertEqual(attempt["usage"]["counters"], FULL_USAGE)
        self.assertIn(attempt["attempt_id"], self.load_state()["notes"][-1])

    def test_nonzero_exit_without_usage(self) -> None:  # AT-03
        self.assert_rejected("nonzero_exit", usage="unavailable", rc=1, stdout="", stderr="boom")

    def test_output_rejections(self) -> None:  # AT-04
        cases = {
            "invalid_json": {"output_text": "{not json"},
            "not_object": {"output_text": "[1, 2]"},
            "missing_output": {"write_output": False},
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                shutil.rmtree(self.home, ignore_errors=True)
                self.assert_rejected(reason, **kwargs)

    def test_oversize_output(self) -> None:  # AT-04, AT-15
        with mock.patch.object(controller, "_CODEX_OUTPUT_MAX_BYTES", 64):
            attempt = self.assert_rejected(
                "oversize_output", usage="unavailable",
                payload={**FULL_REVIEW, "summary": "x" * 500},
            )
        self.assertTrue(attempt["execution"]["stdout_truncated"])

    def test_schema_and_semantic_rejection(self) -> None:  # AT-05, AT-06
        self.assert_rejected("schema_invalid", payload={"verdict": "pass"})
        shutil.rmtree(self.home)
        duplicate_ac = {
            **FULL_REVIEW,
            "acceptance_criteria_assessment": [
                {"id": "AC-1", "status": "satisfied", "evidence": "a"},
                {"id": "AC-1", "status": "not_satisfied", "evidence": "b"},
            ],
        }
        self.assert_rejected("semantic_invalid", payload=duplicate_ac)

    def test_cancelled_during_exec(self) -> None:  # AT-07
        def cancel() -> None:
            state = self.load_state()
            state["status"] = "cancelled"
            self.save_state(state)

        self.assert_rejected("run_inactive", on_call=cancel)
        self.assertEqual(self.load_state()["status"], "cancelled")

    def test_drift_before_publication(self) -> None:  # AT-08
        for target, reason in (
            ("require_review_contract_unchanged", "contract_drift"),
            ("reverify_imported_target_after_exec", "target_drift"),
        ):
            with self.subTest(reason=reason):
                shutil.rmtree(self.home, ignore_errors=True)
                with mock.patch.object(
                    controller, target, side_effect=controller.WorkflowError("drift")
                ):
                    self.assert_rejected(reason)
                self.assertEqual(self.load_state()["reviews"], [])

    def test_concurrent_round_changes(self) -> None:  # AT-09
        initial = self.state_path.read_bytes()

        def advance_to_delta() -> None:
            state = self.load_state()
            state["review_round"] = 1
            state["reviews"] = [{"round": 1, "path": "review-01.codex.json", "verdict": "pass", "delta": False}]
            self.save_state(state)

        self.assert_rejected("round_mode_mismatch", on_call=advance_to_delta)
        shutil.rmtree(self.home)
        self.state_path.write_bytes(initial)

        def exhaust_budget() -> None:
            state = self.load_state()
            state["review_round"] = int(state.get("max_review_rounds", 3))
            self.save_state(state)

        self.assert_rejected("review_budget_exhausted", on_call=exhaust_budget)

    def test_timeout(self) -> None:  # AT-10, AT-11
        marker = "\n[controller] command timed out after 42.0s and was terminated."
        self.fake(rc=controller.PROCESS_TIMEOUT_EXIT_CODE, stdout="", stderr=marker)
        with self.assertRaises(controller.WorkflowError):
            self.run_codex(timeout=42.0)
        attempt = self.only_attempt()
        self.assertEqual(self.outcome(attempt), ("timed_out", "rejected", "timeout", "unavailable"))
        reserved = accounting.read_ledger(self.home).attempts[attempt["attempt_id"]].reservation
        self.assertEqual(reserved["timeout_seconds"], 42.0)
        shutil.rmtree(self.home)
        partial = {k: v for k, v in FULL_USAGE.items() if k != "reasoning_output_tokens"}
        self.fake(rc=controller.PROCESS_TIMEOUT_EXIT_CODE, stdout=ndjson(partial), stderr=marker)
        with self.assertRaises(controller.WorkflowError):
            self.run_codex(timeout=42.0)
        attempt = self.only_attempt()
        self.assertEqual(self.outcome(attempt), ("timed_out", "rejected", "timeout", "partial"))
        self.assertEqual(attempt["usage"]["counters"]["input_tokens"], 1000)

    def test_interrupt_and_unexpected_exception(self) -> None:  # AT-12, AT-13
        self.fake(raise_exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_codex()
        self.assertEqual(
            self.outcome(self.only_attempt()), ("interrupted", "rejected", "interrupted", "unavailable")
        )
        shutil.rmtree(self.home)
        self.fake(raise_exc=RuntimeError("bug"))
        with self.assertRaises(RuntimeError):
            self.run_codex()
        self.assertEqual(
            self.outcome(self.only_attempt()), ("unknown", "not_evaluated", "internal_error", "unavailable")
        )

    def test_executable_missing(self) -> None:  # AT-16
        def missing():
            try:
                raise FileNotFoundError("codex")
            except FileNotFoundError as exc:
                raise controller.WorkflowError("Required executable not found: codex") from exc

        self.fake(on_call=missing)
        with self.assertRaises(controller.WorkflowError):
            self.run_codex()
        self.assertEqual(
            self.outcome(self.only_attempt()),
            ("launch_failed", "not_evaluated", "launch_failed", "not_applicable"),
        )

    # -- launch gating -----------------------------------------------------

    def test_prelaunch_failures_prevent_launch(self) -> None:  # AT-17
        not_a_dir = self.tmp / "file"
        not_a_dir.write_text("x", encoding="utf-8")
        corrupt = self.tmp / "corrupt"
        (corrupt / "v1").mkdir(parents=True)
        (corrupt / "v1" / "ledger.json").write_text("{", encoding="utf-8")
        cases = {
            "file": str(not_a_dir / "sub"),
            "empty": "",
            "relative": "relative/acct",
            "inside-repo": str(self.repo / ".acct"),
            "inside-state-home": str(self.state_home / "acct"),
            "inside-run-dir": str(self.run_dir / "acct"),
            "corrupt-meta": str(corrupt),
        }
        self.fake()
        before = self.state_path.read_bytes()
        for name, value in cases.items():
            with self.subTest(case=name), mock.patch.dict(os.environ, {ENV: value}):
                with self.assertRaises(controller.WorkflowError) as ctx:
                    self.run_codex()
                self.assertIn("NOT launched", str(ctx.exception))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse((self.repo / ".acct").exists())
        self.assertFalse((self.state_home / "acct").exists())
        self.assertFalse((self.run_dir / "acct").exists())

    def test_lock_timeout_prevents_launch(self) -> None:  # AT-17
        accounting.Ledger(self.home)._ensure_dir()
        self.fake()
        with mock.patch.object(accounting, "LOCK_TIMEOUT_SECONDS", 0.2):
            with CrossProcessLock(self.home / "v1" / ".ledger.lock"):
                with self.assertRaises(controller.WorkflowError):
                    self.run_codex()
        self.assertEqual(self.calls, [])

    def test_stale_predecessor_is_linked_not_relaunched(self) -> None:  # AT-18
        repo_id = controller.resolve_repository(self.repo).id
        run_id = self.run_dir.name
        crashed = accounting.Ledger(self.home).reserve(
            {**make_reservation(run_id=run_id, boot_id=OTHER_BOOT),
             "workflow": {**make_reservation()["workflow"], "repo_id": repo_id, "run_id": run_id}}
        )
        self.fake()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(self.run_codex(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(crashed.attempt_id, err.getvalue())
        newest, oldest = self.attempts()
        self.assertEqual(newest["retry_of"], crashed.attempt_id)
        self.assertEqual(newest["slot_attempt"], 2)
        self.assertEqual(oldest["state"], "unsettled")
        self.assertEqual(oldest["owner_process"], "gone" if accounting.current_boot_id() else "unknown")

    @unittest.skipUnless(os.name == "posix", "needs POSIX signals and process groups")
    def test_killed_controller_leaves_attempt_unsettled(self) -> None:  # AT-18
        """A real controller process dies abruptly while its (fake) Codex child runs.

        Production signal handling is unchanged (deferred D1): neither SIGTERM nor
        SIGKILL settles the attempt, and the child outlives the controller. The test
        owns the controller's process group and kills exactly that group on cleanup.
        """
        fake_bin = self.tmp / "fake-bin"
        fake_bin.mkdir()
        launches = self.tmp / "fake-codex-launches.log"
        # The controller hands Codex a minimized environment, so the log path is
        # baked into the stub. The child only sleeps (bounded) and never reads input.
        stub = fake_bin / "codex"
        stub.write_text(
            "#!/bin/sh\n"
            f"echo \"$$\" >> {shlex.quote(str(launches))}\n"
            f"exec {shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'\n",
            encoding="utf-8",
        )
        stub.chmod(0o700)
        env = {
            "PATH": os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]),
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.tmp / "xdg-config"),
            "CODEX_HOME": str(self.tmp / "codex-home"),
            "LANG": "C.UTF-8",
            ENV: str(self.home),
        }
        self.assertEqual(shutil.which("codex", path=env["PATH"]), str(stub))

        def launched_pids() -> list[int]:
            return [int(x) for x in launches.read_text().split()] if launches.exists() else []

        killed: list[str] = []
        for number, sig in enumerate((signal.SIGTERM, signal.SIGKILL), start=1):
            stderr_path = self.tmp / f"controller-{number}.stderr"
            with stderr_path.open("wb") as stderr_file:
                proc = subprocess.Popen(
                    [sys.executable, str(SCRIPTS / "controller.py"), "--project-root", str(self.repo),
                     "--state-dir", str(self.state_home), "codex", "--phase", "review", "--timeout", "600"],
                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr_file,
                    start_new_session=True,
                )
            children: list[int] = []
            self.addCleanup(_kill_owned_group, proc, children)
            deadline = time.monotonic() + 60
            while len(launched_pids()) < number:
                self.assertIsNone(proc.poll(), stderr_path.read_text(errors="replace"))
                self.assertLess(time.monotonic(), deadline, "fake codex was never launched")
                time.sleep(0.05)
            child = launched_pids()[-1]
            children.append(child)
            # Reservation precedes launch: the attempt is already recorded and open.
            new = [a["attempt_id"] for a in self.attempts()
                   if a["state"] == "unsettled" and a["attempt_id"] not in killed]
            self.assertEqual(len(new), 1)
            killed.append(new[0])
            os.kill(proc.pid, sig)
            self.assertEqual(proc.wait(timeout=30), -sig)
            self.assertFalse(_process_gone(child), "child should outlive the killed controller")
            _kill_owned_group(proc, children)
            self.assertTrue(_wait_gone(child, 30), f"owned fake codex {child} still running")
            if number == 2:  # the next invocation links its unsettled predecessor
                self.assertIn(killed[0], stderr_path.read_text(errors="replace"))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(controller.main(["accounting", "attempts", "--unsettled"]), 0)
            self.assertEqual(controller.main(["accounting", "summary"]), 0)
            for attempt_id in killed:
                self.assertEqual(controller.main(["accounting", "show", attempt_id]), 0)
        attempts_out, summary, *shows = (json.loads(line) for line in out.getvalue().splitlines())
        by_id = {a["attempt_id"]: a for a in attempts_out["attempts"]}
        self.assertEqual(sorted(by_id), sorted(killed))
        for attempt_id in killed:
            attempt = by_id[attempt_id]
            self.assertEqual((attempt["state"], attempt["result"], attempt["usage"]), ("unsettled", None, None))
            self.assertFalse(attempt["stale"])  # uncertain, not yet stale
            self.assertEqual(attempt["owner_process"], "gone" if accounting.current_boot_id() else "unknown")
        self.assertEqual(by_id[killed[1]]["retry_of"], killed[0])
        self.assertEqual([len(show["events"]) for show in shows], [1, 1])  # reservations only
        self.assertEqual(summary["attempts"]["unsettled"], 2)
        self.assertFalse(summary["completeness"]["tokens_complete"])
        self.assertEqual(len(launched_pids()), 2)  # queries never relaunch Codex
        self.assertEqual(self.load_state().get("codex_runs", []), [])

    def test_settlement_failure_after_publication(self) -> None:  # AT-19
        self.fake()
        err = io.StringIO()
        with mock.patch.object(
            accounting.Ledger, "settle", side_effect=accounting.AccountingError("disk full")
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(self.run_codex(), 0)
        attempt = self.only_attempt()
        self.assertEqual(attempt["state"], "unsettled")
        self.assertIn("[accounting]", err.getvalue())
        self.assertIn(attempt["attempt_id"], err.getvalue())
        self.assertIn("UNSETTLED, or settled without a durability guarantee", err.getvalue())
        self.assertEqual(self.load_state()["reviews"][0]["verdict"], "pass")

    def assert_published_despite(self, exc: BaseException) -> None:
        class FailingStdout(io.StringIO):
            def write(self, text: str) -> int:
                raise exc

        self.fake()
        with contextlib.redirect_stdout(FailingStdout()), self.assertRaises(type(exc)):
            self.run_codex()
        self.assertEqual(len(self.load_state()["reviews"]), 1)
        self.assertEqual(self.outcome(self.only_attempt()), ("exited", "published", None, "complete"))

    def test_broken_stdout_after_publication_settles_as_published(self) -> None:  # AT-19
        self.assert_published_despite(BrokenPipeError())

    def test_interrupt_after_publication_settles_as_published(self) -> None:  # AT-19, AT-12
        self.assert_published_despite(KeyboardInterrupt())

    def test_settlement_failure_on_failure_path(self) -> None:  # AT-20
        self.fake(rc=1, stderr="codex broke")
        err = io.StringIO()
        with mock.patch.object(
            accounting.Ledger, "settle", side_effect=OSError("disk full")
        ), contextlib.redirect_stderr(err):
            with self.assertRaises(controller.WorkflowError) as ctx:
                self.run_codex()
        self.assertIn("codex broke", str(ctx.exception))
        attempt = self.only_attempt()
        self.assertEqual(attempt["state"], "unsettled")
        self.assertIn(attempt["attempt_id"], err.getvalue())

    # -- persistence, privacy, compatibility --------------------------------

    def test_accounting_survives_run_directory_deletion(self) -> None:  # AT-27
        self.fake()
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_codex()
        self.fake(rc=1, stderr="boom")
        with self.assertRaises(controller.WorkflowError):
            self.run_codex()
        before = self.attempts()
        shutil.rmtree(self.state_home)
        after = self.attempts()
        self.assertEqual(len(after), 2)
        self.assertEqual(
            [(a["attempt_id"], a["usage"]) for a in after],
            [(a["attempt_id"], a["usage"]) for a in before],
        )

    def test_ledger_contains_no_sensitive_text(self) -> None:  # AT-35
        (self.run_dir / "accepted-spec.md").write_text("SENTINEL-SPEC-TEXT", encoding="utf-8")
        self.fake(rc=1, stderr="SENTINEL-STDERR-TEXT")
        with mock.patch.dict(os.environ, {"SENTINEL_SECRET": "SENTINEL-ENV-VALUE"}):
            with self.assertRaises(controller.WorkflowError):
                self.run_codex()
        raw = b"".join(p.read_bytes() for p in (self.home / "v1").iterdir() if p.is_file())
        for needle in ("SENTINEL-", str(self.tmp), str(self.repo), str(Path.home())):
            self.assertNotIn(needle.encode("utf-8"), raw, needle)

    def test_usage_report_unchanged_shape(self) -> None:  # AT-36
        self.fake()
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_codex()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            controller.main([
                "--project-root", str(self.repo), "--state-dir", str(self.state_home),
                "usage-report", "--json",
            ])
        [record] = json.loads(out.getvalue())
        self.assertEqual(record["phase"], "review-01")
        self.assertIn("attempt_id", record)
        self.assertEqual(record["tokens"], {"input_tokens": 1000, "output_tokens": 120})

    def test_doctor_writes_no_ledger_events(self) -> None:  # AT-38
        def fake_doctor(cmd, *, cwd, input_text=None, check=False, timeout=None, env=None):
            if cmd and Path(cmd[0]).name in ("git", "git.exe"):
                return self._original_run_process(cmd, cwd=cwd, input_text=input_text, check=check, timeout=timeout, env=env)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no prompt provided")

        controller.run_process = fake_doctor
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                controller.main(["--project-root", str(self.repo), "doctor"])
            except SystemExit:
                pass
        self.assertFalse((self.home / "v1" / "codex-attempts.jsonl").exists())


class ProjectValidationTests(unittest.TestCase):
    def test_validate_project_passes(self) -> None:  # AT-39
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_project.py")],
            text=True, capture_output=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
