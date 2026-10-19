"""Durable accounting ledger for engine-managed Codex attempts.

Every `controller.py codex` invocation reserves an attempt in a per-host,
append-only JSON Lines ledger *before* `codex exec` is launched, and settles it
exactly once afterwards with three independent outcomes:

* ``execution`` -- what the process did (exited / timed_out / interrupted /
  launch_failed / unknown);
* ``result``    -- whether the engine accepted and published the Codex result
  (published / rejected / not_evaluated / unknown) and why not;
* ``usage``     -- how complete the token counters are (complete / partial /
  unavailable / invalid / not_applicable).

Design invariants:

* **Outside disposable state.** The ledger location never follows `--state-dir`
  or `CLAUDE_AUTONOMOUS_STATE_HOME`; deleting run directories never deletes
  accounting.
* **Fail closed before launch.** If the reservation cannot be durably written,
  the caller must not launch Codex.
* **Append-only, fsynced.** Each event is one line written with a single
  `os.write` on an `O_APPEND` descriptor under a `CrossProcessLock`, then
  `fsync`ed. Settled facts are never rewritten; a crash can damage at most the
  trailing line, which readers report and skip.
* **Idempotent.** Reads fold events by attempt id; the first valid settlement is
  authoritative. Re-settling with an identical outcome is a no-op, a different
  outcome is refused, and duplicated/replayed lines are counted as diagnostics,
  never as additional attempts.
* **Strict allowlist.** Events are validated against
  `schemas/codex-attempt-event.schema.json` (`additionalProperties: false`)
  before writing. No prompt/output/stderr text, environment, absolute paths,
  hostnames or credentials are recorded.
* **Read-only queries.** Query helpers take no lock and never create or modify
  files.

Counter semantics: cached input is a subset of input and reasoning output is a
subset of output; they are never added to their parent counters. `total_tokens`
is derived as input + output only when both are known.

Durability is limited to what the storage stack honors for `fsync`; see the
README "Codex attempt accounting" section for the precise guarantees.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import errno
import functools
import hashlib
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_validation import SchemaValidationError, validate_payload  # noqa: E402
from state import CrossProcessLock, StateError  # noqa: E402

EVENT_FORMAT = "claude-autonomous.codex-attempt-event/1"
LEDGER_FORMAT = "claude-autonomous.accounting-ledger/1"
SUMMARY_FORMAT = "claude-autonomous.accounting-summary/1"
ATTEMPTS_FORMAT = "claude-autonomous.accounting-attempts/1"
ATTEMPT_FORMAT = "claude-autonomous.accounting-attempt/1"
RECONCILE_FORMAT = "claude-autonomous.accounting-reconcile/1"
EVENT_SCHEMA = "schemas/codex-attempt-event.schema.json"

ACCOUNTING_HOME_ENV = "CLAUDE_AUTONOMOUS_ACCOUNTING_HOME"
HOST_LABEL_ENV = "CLAUDE_AUTONOMOUS_HOST_LABEL"
DEFAULT_DIR_NAME = "claude-autonomous-accounting"
LEDGER_VERSION_DIR = "v1"
EVENTS_FILE_NAME = "codex-attempts.jsonl"
META_FILE_NAME = "ledger.json"
LOCK_FILE_NAME = ".ledger.lock"

USAGE_SOURCE = "codex-exec-json-stdout"
COUNTER_NAMES = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
STALE_GRACE_SECONDS = 300
STALE_WITHOUT_TIMEOUT_SECONDS = 24 * 3600
LOCK_TIMEOUT_SECONDS = 30.0

# Result reasons that may be applied when an exception escapes a given stage of
# the engine's post-launch processing (mirrors the schema enum).
RESULT_REASONS = frozenset(
    {
        "nonzero_exit",
        "timeout",
        "missing_output",
        "oversize_output",
        "invalid_json",
        "not_object",
        "schema_invalid",
        "semantic_invalid",
        "run_inactive",
        "target_drift",
        "contract_drift",
        "round_mode_mismatch",
        "review_budget_exhausted",
        "interrupted",
        "internal_error",
        "launch_failed",
        "operator_abandoned",
    }
)

_HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_BOOT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_LEDGER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


class AccountingError(StateError):
    """The accounting ledger could not be resolved, read for writing, or written."""


class SettlementConflict(AccountingError):
    """An attempt is already settled with a different outcome."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_attempt_id(now: dt.datetime | None = None) -> str:
    moment = (now or utc_now()).astimezone(dt.timezone.utc)
    return f"cxa-{moment:%Y%m%dT%H%M%SZ}-{secrets.token_hex(8)}"


def short_text(value: object) -> str | None:
    """A bounded, control-character-free string, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 200 or _CONTROL_CHARS.search(value):
        return None
    return value


def current_boot_id() -> str | None:
    """Linux boot id (changes on every boot, incl. each WSL2 VM start)."""
    try:
        raw = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    raw = raw.strip()
    return raw if _BOOT_ID_PATTERN.match(raw) else None


def host_label(environ: Mapping[str, str] | None = None) -> str | None:
    """Operator-chosen host label from the environment; never inferred."""
    env = os.environ if environ is None else environ
    raw = env.get(HOST_LABEL_ENV, "").strip()
    return raw if _HOST_LABEL_PATTERN.match(raw) else None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


def resolve_accounting_home(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the accounting home, independent of run-state overrides.

    Precedence: ``CLAUDE_AUTONOMOUS_ACCOUNTING_HOME`` (absolute; set-but-empty or
    relative is refused) > ``$XDG_STATE_HOME/claude-autonomous-accounting``
    (absolute XDG values only, per the XDG spec) > platform default
    (``~/.local/state/claude-autonomous-accounting`` on Linux/WSL). `--state-dir`
    and ``CLAUDE_AUTONOMOUS_STATE_HOME`` deliberately do not participate.
    """
    env = os.environ if environ is None else environ
    if ACCOUNTING_HOME_ENV in env:
        raw = env[ACCOUNTING_HOME_ENV]
        if not raw.strip():
            raise AccountingError(
                f"${ACCOUNTING_HOME_ENV} is set but empty. Unset it to use the "
                "default accounting location, or set it to an absolute path."
            )
        candidate = Path(raw.strip()).expanduser()
        if not candidate.is_absolute():
            raise AccountingError(
                f"${ACCOUNTING_HOME_ENV}={raw!r} is a relative path; set it to an "
                "absolute path outside any reviewed repository."
            )
        return candidate.resolve()
    plat = sys.platform if platform is None else platform
    base_home = Path.home() if home is None else home
    if plat == "darwin":
        return base_home / "Library" / "Application Support" / DEFAULT_DIR_NAME
    if plat == "win32":
        local_app = env.get("LOCALAPPDATA", "").strip()
        root = Path(local_app) if local_app else base_home / "AppData" / "Local"
        return root / DEFAULT_DIR_NAME
    xdg = env.get("XDG_STATE_HOME", "").strip()
    if xdg and Path(xdg).is_absolute():
        return Path(xdg).resolve() / DEFAULT_DIR_NAME
    return base_home / ".local" / "state" / DEFAULT_DIR_NAME


def require_outside(home: Path, root: Path) -> None:
    """Refuse a ledger that would live inside the reviewed worktree."""
    try:
        resolved_home = home.resolve()
        resolved_root = root.resolve()
    except OSError as exc:
        raise AccountingError(f"Cannot resolve accounting home {home}: {exc}") from exc
    if resolved_home == resolved_root or resolved_root in resolved_home.parents:
        raise AccountingError(
            f"The accounting home {str(resolved_home)!r} resolves inside the target "
            f"worktree {str(resolved_root)!r}. Set ${ACCOUNTING_HOME_ENV} to an "
            "absolute path outside the repository."
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def engine_provenance(plugin_root: Path) -> dict[str, Any]:
    """Identify the engine code that actually runs, for git checkouts and copies.

    ``code_digest`` covers the relative path and bytes of every top-level
    ``scripts/*.py``, ``schemas/*.json`` and ``prompts/*.md`` file.
    """
    digest = hashlib.sha256()
    for rel_dir, pattern in (("scripts", "*.py"), ("schemas", "*.json"), ("prompts", "*.md")):
        for path in sorted((plugin_root / rel_dir).glob(pattern)):
            data = path.read_bytes()
            digest.update(path.relative_to(plugin_root).as_posix().encode("utf-8") + b"\0")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    version: str | None = None
    try:
        manifest = json.loads(
            (plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        version = short_text(manifest.get("version")) if isinstance(manifest, dict) else None
    except (OSError, ValueError):
        version = None
    return {
        "version": version,
        "code_digest": "sha256:" + digest.hexdigest(),
        "root_kind": "git-worktree" if (plugin_root / ".git").exists() else "plain",
    }


def python_provenance() -> dict[str, str]:
    info = sys.version_info
    return {
        "implementation": sys.implementation.name[:40],
        "version": f"{info.major}.{info.minor}.{info.micro}",
    }


# ---------------------------------------------------------------------------
# Usage parsing
# ---------------------------------------------------------------------------


def _empty_counters() -> dict[str, int | None]:
    return {name: None for name in COUNTER_NAMES}


def unavailable_usage(*, truncated: bool = False) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "source": None,
        "counters": _empty_counters(),
        "flags": ["stdout_truncated"] if truncated else [],
        "invalid_reasons": [],
    }


def not_applicable_usage() -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "source": None,
        "counters": _empty_counters(),
        "flags": [],
        "invalid_reasons": [],
    }


def classify_usage(usage_events: list[object], *, truncated: bool) -> dict[str, Any]:
    """Classify the ``turn.completed.usage`` objects seen in one exec."""
    if not usage_events:
        return unavailable_usage(truncated=truncated)
    flags: set[str] = set()
    reasons: set[str] = set()
    if len(usage_events) > 1:
        # Cumulative-vs-per-turn semantics across turns are unverified: keep the
        # last one and mark the result partial rather than summing or guessing.
        flags.add("multiple_usage_events")
    if truncated:
        flags.add("stdout_truncated")
    raw = usage_events[-1]
    counters = _empty_counters()
    if not isinstance(raw, dict):
        reasons.add("usage_not_object")
    else:
        for name in COUNTER_NAMES:
            value = raw.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                reasons.add("non_integer")
                continue
            if value < 0:
                reasons.add("negative")
            counters[name] = value
        if any(counters[name] is None for name in COUNTER_NAMES) and not reasons:
            flags.add("missing_counters")
        reasons.update(_subset_violations(counters))
    if reasons:
        status = "invalid"
    elif flags:
        status = "partial"
    else:
        status = "complete"
    return {
        "status": status,
        "source": USAGE_SOURCE,
        "counters": counters,
        "flags": sorted(flags),
        "invalid_reasons": sorted(reasons),
    }


def _subset_violations(counters: Mapping[str, int | None]) -> set[str]:
    out: set[str] = set()
    inp, cached = counters.get("input_tokens"), counters.get("cached_input_tokens")
    outp, reasoning = counters.get("output_tokens"), counters.get("reasoning_output_tokens")
    if isinstance(inp, int) and isinstance(cached, int) and cached > inp:
        out.add("cached_exceeds_input")
    if isinstance(outp, int) and isinstance(reasoning, int) and reasoning > outp:
        out.add("reasoning_exceeds_output")
    return out


def parse_exec_stdout(stdout_text: str, *, truncated: bool) -> dict[str, Any]:
    """Extract usage and the thread id from `codex exec --json` NDJSON output."""
    usage_events: list[object] = []
    thread_id: str | None = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "thread.started" and thread_id is None:
            candidate = event.get("thread_id")
            if isinstance(candidate, str) and _THREAD_ID_PATTERN.match(candidate):
                thread_id = candidate
        elif event_type == "turn.completed":
            usage_events.append(event.get("usage"))
    return {
        "usage": classify_usage(usage_events, truncated=truncated),
        "thread_id": thread_id,
    }


def total_tokens(counters: Mapping[str, Any]) -> int | None:
    inp, outp = counters.get("input_tokens"), counters.get("output_tokens")
    if isinstance(inp, int) and isinstance(outp, int):
        return inp + outp
    return None


# ---------------------------------------------------------------------------
# Reading and folding (read-only)
# ---------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    reserved: dict[str, Any]
    settled: dict[str, Any] | None = None

    @property
    def attempt_id(self) -> str:
        return str(self.reserved["attempt_id"])

    @property
    def reservation(self) -> dict[str, Any]:
        return self.reserved["reservation"]


def _empty_diagnostics() -> dict[str, Any]:
    return {
        "ledger_meta": "missing",
        "malformed_lines": 0,
        "incomplete_tail": False,
        "unsupported_format": 0,
        "invalid_events": 0,
        "duplicate_events": 0,
        "conflicting_duplicate_events": 0,
        "duplicate_reservations": 0,
        "orphan_settlements": 0,
        "duplicate_settlements": 0,
        "conflicting_settlements": 0,
        "foreign_ledger_events": 0,
    }


# Diagnostics that mean the ledger cannot be trusted to be complete.
INTEGRITY_DIAGNOSTICS = (
    "malformed_lines",
    "unsupported_format",
    "invalid_events",
    "conflicting_duplicate_events",
    "duplicate_reservations",
    "orphan_settlements",
    "conflicting_settlements",
)


@dataclass
class LedgerView:
    present: bool
    ledger_id: str | None
    attempts: dict[str, AttemptRecord] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=_empty_diagnostics)
    events_read: int = 0

    def integrity_ok(self) -> bool:
        return (
            self.diagnostics["ledger_meta"] not in {"invalid", "missing_with_events"}
            and not self.diagnostics["incomplete_tail"]
            and all(self.diagnostics[key] == 0 for key in INTEGRITY_DIAGNOSTICS)
        )


def _settlement_digest(event: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(event["settlement"]).encode("utf-8")).hexdigest()


def _semantic_problems(event: Mapping[str, Any]) -> list[str]:
    """Checks the schema cannot express: counter consistency per usage status.

    Expects a schema-valid event. Used by both the writer (refuse before append)
    and validating readers (count as ``invalid_events``).
    """
    if event.get("event_type") != "attempt.settled":
        return []
    usage = event["settlement"]["usage"]
    counters = usage["counters"]
    status = usage["status"]
    problems: list[str] = []
    if status in {"complete", "partial"}:
        values = [counters[name] for name in COUNTER_NAMES]
        if any(isinstance(v, int) and v < 0 for v in values):
            problems.append("negative_counter")
        problems.extend(sorted(_subset_violations(counters)))
        if status == "complete" and any(v is None for v in values):
            problems.append("complete_with_missing_counter")
        if usage["source"] != USAGE_SOURCE:
            problems.append("unexpected_usage_source")
        if usage["invalid_reasons"]:
            problems.append("invalid_reasons_on_valid_usage")
    elif status == "invalid":
        if not usage["invalid_reasons"]:
            problems.append("invalid_usage_without_reasons")
    elif any(counters[name] is not None for name in COUNTER_NAMES):
        # unavailable / not_applicable carry no counters.
        problems.append(f"counters_on_{status}_usage")
    return problems


def _semantically_valid(event: Mapping[str, Any]) -> bool:
    return not _semantic_problems(event)


def _validate_new_event(event: Mapping[str, Any]) -> None:
    """Schema + semantic validation of an event about to be written (fail closed)."""
    try:
        validate_payload(event, EVENT_SCHEMA, label="accounting ledger event")
    except SchemaValidationError as exc:
        raise AccountingError(f"Refusing to write an invalid ledger event: {exc}") from exc
    problems = _semantic_problems(event)
    if problems:
        raise AccountingError(
            "Refusing to write a semantically invalid ledger event "
            f"({', '.join(problems)}); nothing was appended."
        )


def _read_meta(path: Path) -> tuple[str, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeDecodeError):
        return "invalid", None
    try:
        meta = json.loads(raw)
    except ValueError:
        return "invalid", None
    if (
        not isinstance(meta, dict)
        or meta.get("format") != LEDGER_FORMAT
        or not isinstance(meta.get("ledger_id"), str)
        or not _LEDGER_ID_PATTERN.match(meta["ledger_id"])
    ):
        return "invalid", None
    return "ok", meta["ledger_id"]


def _has_fold_shape(event: Mapping[str, Any]) -> bool:
    """The fields the write-side fold relies on, without full schema validation."""
    try:
        if not all(isinstance(event[k], str) for k in ("event_id", "attempt_id", "ledger_id")):
            return False
        if event["event_type"] == "attempt.reserved":
            reservation = event["reservation"]
            return isinstance(reservation["slot"], str) and all(
                isinstance(reservation["workflow"][k], str) for k in ("repo_id", "run_id")
            )
        if event["event_type"] == "attempt.settled":
            return isinstance(event["settlement"]["result"]["status"], str)
    except (KeyError, TypeError):
        return False
    return False


def read_ledger(home: Path, *, validate: bool = True) -> LedgerView:
    """Fold the ledger at `home` without locking, creating or modifying anything.

    Queries use full schema + semantic validation (``validate=True``). Writers fold
    with ``validate=False``, which only checks the fields that retry linkage and
    duplicate detection use: every line was schema-validated when it was written,
    and full validation of the whole history under the lock would make each
    launch O(history) in jsonschema calls (~0.2 ms per event).
    """
    ledger_dir = home / LEDGER_VERSION_DIR
    meta_status, ledger_id = _read_meta(ledger_dir / META_FILE_NAME)
    try:
        data = (ledger_dir / EVENTS_FILE_NAME).read_bytes()
    except FileNotFoundError:
        data = None
    view = LedgerView(present=meta_status != "missing" or data is not None, ledger_id=ledger_id)
    view.diagnostics["ledger_meta"] = meta_status
    if not data:
        return view
    if meta_status == "missing":
        # Events without metadata: writers refuse to create new metadata here.
        view.diagnostics["ledger_meta"] = "missing_with_events"
    lines = data.split(b"\n")
    tail = lines.pop()  # bytes after the final newline: a torn, incomplete append
    if tail.strip():
        view.diagnostics["incomplete_tail"] = True
    # event_id -> digest of its first occurrence; a later line with the same id is
    # a benign replay when identical, and an integrity problem when it differs.
    seen_events: dict[str, str] = {}
    for raw in lines:
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            view.diagnostics["malformed_lines"] += 1
            continue
        if not isinstance(event, dict):
            view.diagnostics["malformed_lines"] += 1
            continue
        if event.get("format") != EVENT_FORMAT:
            view.diagnostics["unsupported_format"] += 1
            continue
        if validate:
            try:
                validate_payload(event, EVENT_SCHEMA, label="accounting ledger event")
            except SchemaValidationError:
                view.diagnostics["invalid_events"] += 1
                continue
            if not _semantically_valid(event):
                view.diagnostics["invalid_events"] += 1
                continue
        elif not _has_fold_shape(event):
            view.diagnostics["invalid_events"] += 1
            continue
        digest = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
        first_digest = seen_events.get(event["event_id"])
        if first_digest is not None:
            if first_digest == digest:
                view.diagnostics["duplicate_events"] += 1
            else:
                view.diagnostics["conflicting_duplicate_events"] += 1
            continue
        seen_events[event["event_id"]] = digest
        view.events_read += 1
        if ledger_id is not None and event["ledger_id"] != ledger_id:
            view.diagnostics["foreign_ledger_events"] += 1
        attempt_id = event["attempt_id"]
        record = view.attempts.get(attempt_id)
        if event["event_type"] == "attempt.reserved":
            if record is not None:
                view.diagnostics["duplicate_reservations"] += 1
                continue
            view.attempts[attempt_id] = AttemptRecord(reserved=event)
        elif record is None:
            view.diagnostics["orphan_settlements"] += 1
        elif record.settled is None:
            record.settled = event
        elif _settlement_digest(record.settled) == _settlement_digest(event):
            view.diagnostics["duplicate_settlements"] += 1
        else:
            view.diagnostics["conflicting_settlements"] += 1
    return view


# ---------------------------------------------------------------------------
# Liveness / staleness
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool | None:
    if os.name == "nt":
        return None  # os.kill(pid, 0) would terminate the process on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def owner_process_state(
    reservation: Mapping[str, Any],
    *,
    boot_id: str | None,
    pid_alive: Callable[[int], bool | None] = _pid_alive,
) -> str:
    """alive / gone / unknown for the launching controller (PID reuse => advisory)."""
    launcher = reservation["launcher"]
    recorded_boot = launcher.get("boot_id")
    if recorded_boot is None or boot_id is None:
        return "unknown"
    if recorded_boot != boot_id:
        return "gone"
    alive = pid_alive(int(launcher["pid"]))
    if alive is None:
        return "unknown"
    return "alive" if alive else "gone"


def is_stale(reservation: Mapping[str, Any], age_seconds: float) -> bool:
    timeout = reservation.get("timeout_seconds")
    limit = (
        float(timeout) + STALE_GRACE_SECONDS
        if isinstance(timeout, (int, float))
        else STALE_WITHOUT_TIMEOUT_SECONDS
    )
    return age_seconds > limit


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    ledger_id: str
    retry_of: str | None
    slot_attempt: int
    predecessor_unsettled: bool


# Directory fsync makes a newly created entry durable. It is *unsupported* (and
# skipped) on Windows, which cannot open a directory with `os.open`, and on file
# systems that reject fsync on a directory descriptor with EINVAL/ENOTSUP. Every
# other error (EIO, ENOSPC, EROFS, EACCES, ...) is a real failure and is raised.
_DIR_FSYNC_SUPPORTED = os.name != "nt"
_DIR_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    code
    for code in (getattr(errno, name, None) for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP"))
    if code is not None
)


def _fsync_dir(path: Path) -> None:
    """Sync directory `path`; raises AccountingError on a real I/O failure."""
    if not _DIR_FSYNC_SUPPORTED:
        return
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise AccountingError(f"Cannot open directory {path} to sync it: {exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _DIR_FSYNC_UNSUPPORTED_ERRNOS:
            raise AccountingError(f"Cannot sync directory {path}: {exc}") from exc
    finally:
        os.close(fd)


def _remove_partial(path: Path) -> str:
    """Remove a file this writer just created and failed to complete (under the lock)."""
    try:
        path.unlink()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"; the incomplete {path.name} could not be removed ({exc})"
    return ""


class Ledger:
    """Writer for one accounting home. Every mutation runs under the ledger lock."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.dir = home / LEDGER_VERSION_DIR
        self.events_path = self.dir / EVENTS_FILE_NAME
        self.meta_path = self.dir / META_FILE_NAME
        self.lock_path = self.dir / LOCK_FILE_NAME

    # -- infrastructure ----------------------------------------------------

    def _ensure_dir(self) -> None:
        for directory in (self.home, self.dir):
            missing: list[Path] = []
            probe = directory
            while not probe.exists() and probe != probe.parent:
                missing.append(probe)
                probe = probe.parent
            # One level at a time, syncing each new entry into its parent. A failed
            # sync removes the new (empty) directory, so the next writer creates and
            # syncs it again instead of trusting an entry that may not be durable.
            for level in reversed(missing):
                try:
                    level.mkdir(mode=0o700 if level in (self.home, self.dir) else 0o777)
                except FileExistsError:
                    continue  # created concurrently; its entry is confirmed below
                except OSError as exc:
                    raise AccountingError(
                        f"Cannot create accounting directory {level}: {exc}"
                    ) from exc
                try:
                    _fsync_dir(level.parent)
                except AccountingError:
                    with contextlib.suppress(OSError):
                        level.rmdir()
                    raise
            # Every writer confirms the ledger's own directory entries itself, also
            # when another writer created them: that writer may still be paused
            # before its sync, may fail it, or may have been interrupted.
            _fsync_dir(directory.parent)

    def _lock(self) -> CrossProcessLock:
        return CrossProcessLock(self.lock_path, timeout=LOCK_TIMEOUT_SECONDS)

    def _has_recorded_events(self) -> bool:
        try:
            return self.events_path.stat().st_size > 0
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AccountingError(f"Cannot inspect {self.events_path}: {exc}") from exc

    def _ensure_meta(self) -> str:
        """Return the ledger id, creating `ledger.json` only for a ledger without events.

        Never repairs or replaces existing metadata: invalid metadata, or missing
        metadata next to recorded events, refuses the write (new metadata would
        relabel that history with a different ledger id).
        """
        status, ledger_id = _read_meta(self.meta_path)
        if status == "ok":
            assert ledger_id is not None
            return ledger_id
        if status == "invalid":
            if self._has_recorded_events():
                advice = (
                    f"{self.events_path.name} holds recorded events: do not delete or "
                    "replace either file; restore ledger.json from a backup."
                )
            else:
                advice = (
                    f"{self.events_path.name} is absent or empty, so no attempt was "
                    "recorded under it (likely an interrupted ledger creation); move "
                    "ledger.json aside to let the next launch create a new ledger."
                )
            raise AccountingError(
                f"Accounting ledger metadata {self.meta_path} is unreadable or "
                f"invalid; refusing to write. Inspect it with `controller.py accounting "
                f"summary --ledger-dir {self.home}`. {advice}"
            )
        if self._has_recorded_events():
            raise AccountingError(
                f"Accounting ledger metadata {self.meta_path} is missing but "
                f"{self.events_path} already holds events; refusing to create new "
                "metadata, which would relabel that history under a new ledger id. "
                "Restore ledger.json from a backup; do not delete the events file."
            )
        ledger_id = secrets.token_hex(16)
        payload = (
            _canonical(
                {"format": LEDGER_FORMAT, "ledger_id": ledger_id, "created_at": _iso(utc_now())}
            )
            + "\n"
        ).encode("utf-8")
        try:
            fd = os.open(str(self.meta_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise AccountingError(f"Cannot create {self.meta_path}: {exc}") from exc
        # This writer created the file under the lock and no event references the
        # new ledger id yet, so on any failure it removes the incomplete file rather
        # than leaving metadata that every later writer would have to refuse.
        try:
            try:
                written = os.write(fd, payload)
                if written != len(payload):
                    raise OSError(f"short write ({written} of {len(payload)} bytes)")
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(self.dir)
        except (OSError, AccountingError) as exc:
            raise AccountingError(
                f"Cannot durably create {self.meta_path}: {exc}"
                + _remove_partial(self.meta_path)
            ) from exc
        return ledger_id

    def _ensure_events_file(self) -> None:
        """Create the (empty) events file and sync its directory entry before the
        first append, so a directory-sync failure refuses before any event exists."""
        if self.events_path.exists():
            return
        try:
            fd = os.open(str(self.events_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except OSError as exc:
            raise AccountingError(f"Cannot create {self.events_path}: {exc}") from exc
        try:
            _fsync_dir(self.dir)
        except AccountingError as exc:
            raise AccountingError(f"{exc}{_remove_partial(self.events_path)}") from exc

    def _append(self, event: dict[str, Any]) -> None:
        _validate_new_event(event)
        line = (_canonical(event) + "\n").encode("utf-8")
        try:
            with self.events_path.open("rb") as existing:
                existing.seek(0, os.SEEK_END)
                if existing.tell():
                    existing.seek(-1, os.SEEK_END)
                    if existing.read(1) != b"\n":
                        # A previous append was torn: start on a fresh line so
                        # the fragment stays one isolated malformed line.
                        line = b"\n" + line
            # No O_CREAT: `_ensure_events_file` created and synced the file.
            fd = os.open(str(self.events_path), os.O_WRONLY | os.O_APPEND)
            try:
                written = os.write(fd, line)
                if written != len(line):
                    raise OSError(f"short write ({written} of {len(line)} bytes)")
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise AccountingError(
                f"Cannot durably append to {self.events_path}: {exc}"
            ) from exc

    def _event(
        self, event_type: str, attempt_id: str, ledger_id: str, now: dt.datetime, **body: Any
    ) -> dict[str, Any]:
        return {
            "format": EVENT_FORMAT,
            "event_id": secrets.token_hex(16),
            "event_type": event_type,
            "attempt_id": attempt_id,
            "ledger_id": ledger_id,
            "recorded_at": _iso(now),
            **body,
        }

    # -- operations --------------------------------------------------------

    def reserve(
        self, reservation: dict[str, Any], *, now: dt.datetime | None = None
    ) -> Reservation:
        """Durably record an attempt before launch; raises AccountingError on failure.

        `reservation` holds every schema field except ``retry_of`` and
        ``slot_attempt``, which are derived here under the lock from earlier
        attempts in the same (repo_id, run_id, slot).
        """
        moment = now or utc_now()
        self._ensure_dir()
        with self._lock():
            ledger_id = self._ensure_meta()
            self._ensure_events_file()
            _fsync_dir(self.dir)  # confirm the file entries, whoever created them
            view = read_ledger(self.home, validate=False)
            key = _slot_key(reservation)
            same_slot = [rec for rec in view.attempts.values() if _slot_key(rec.reservation) == key]
            # A retry follows the immediately preceding attempt in the slot when that
            # attempt did not publish (rejected, failed or still unsettled).
            predecessor = (
                same_slot[-1] if same_slot and not _is_published(same_slot[-1]) else None
            )
            attempt_id = new_attempt_id(moment)
            body = dict(reservation)
            body["retry_of"] = predecessor.attempt_id if predecessor else None
            body["slot_attempt"] = len(same_slot) + 1
            self._append(
                self._event("attempt.reserved", attempt_id, ledger_id, moment, reservation=body)
            )
        return Reservation(
            attempt_id=attempt_id,
            ledger_id=ledger_id,
            retry_of=body["retry_of"],
            slot_attempt=body["slot_attempt"],
            predecessor_unsettled=bool(predecessor and predecessor.settled is None),
        )

    def settle(
        self,
        attempt_id: str,
        settlement: dict[str, Any],
        *,
        now: dt.datetime | None = None,
        _guard: Callable[[AttemptRecord], None] | None = None,
    ) -> str:
        """Record the authoritative outcome once. Returns "written" or "duplicate"."""
        moment = now or utc_now()
        self._ensure_dir()
        with self._lock():
            ledger_id = self._ensure_meta()
            _fsync_dir(self.dir)  # confirm the file entries, whoever created them
            event = self._event(
                "attempt.settled", attempt_id, ledger_id, moment, settlement=settlement
            )
            # Reject an invalid new settlement before looking at history, so the
            # outcome does not depend on what is already recorded.
            _validate_new_event(event)
            view = read_ledger(self.home, validate=False)
            record = view.attempts.get(attempt_id)
            if record is None:
                raise AccountingError(f"Unknown attempt {attempt_id!r}; nothing to settle.")
            if record.settled is not None:
                if _settlement_digest(record.settled) == _settlement_digest(event):
                    return "duplicate"
                raise SettlementConflict(
                    f"Attempt {attempt_id} is already settled with a different "
                    "outcome; the first settlement is authoritative."
                )
            if _guard is not None:
                _guard(record)
            self._append(event)
        return "written"

    def reconcile_abandoned(
        self,
        attempt_id: str,
        *,
        now: dt.datetime | None = None,
        boot_id: str | None = None,
        pid_alive: Callable[[int], bool | None] = _pid_alive,
    ) -> str:
        """Operator-confirmed abandonment of an unsettled attempt (never relaunches)."""

        def guard(record: AttemptRecord) -> None:
            state = owner_process_state(record.reservation, boot_id=boot_id, pid_alive=pid_alive)
            if state == "alive":
                raise AccountingError(
                    f"Attempt {attempt_id} may still be running (launcher pid "
                    f"{record.reservation['launcher']['pid']} is alive on this boot); "
                    "refusing to mark it abandoned."
                )

        return self.settle(attempt_id, operator_abandoned_settlement(), now=now, _guard=guard)


def _slot_key(reservation: Mapping[str, Any]) -> tuple[str, str, object, str]:
    """Retry-linkage identity. `import-pr --refresh` that bumps the review-contract
    generation restarts review/adversarial numbering within the same run, so the
    generation is part of the key: a reused slot name for a different reviewed
    candidate is not linked as a retry."""
    workflow = reservation["workflow"]
    return (
        str(workflow["repo_id"]),
        str(workflow["run_id"]),
        workflow.get("review_contract_generation"),
        str(reservation["slot"]),
    )


def _is_published(record: AttemptRecord) -> bool:
    return (
        record.settled is not None
        and record.settled["settlement"]["result"]["status"] == "published"
    )


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------


def _settlement(
    *,
    settled_by: str,
    execution: dict[str, Any],
    result_status: str,
    reason: str | None,
    usage: dict[str, Any],
    observed: dict[str, Any] | None = None,
    output_characters: int | None = None,
) -> dict[str, Any]:
    # Deep copy: a settlement must not alias the tracker's mutable state.
    return copy.deepcopy(
        {
            "settled_by": settled_by,
            "execution": execution,
            "result": {"status": result_status, "reason": reason},
            "usage": usage,
            "observed": observed or {"model": None, "thread_id": None},
            "output_characters": output_characters,
        }
    )


def _execution(
    status: str,
    *,
    exit_code: int | None = None,
    duration_seconds: float | None = None,
    stdout_truncated: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "stdout_truncated": stdout_truncated,
    }


def operator_abandoned_settlement() -> dict[str, Any]:
    return _settlement(
        settled_by="operator",
        execution=_execution("unknown"),
        result_status="unknown",
        reason="operator_abandoned",
        usage=unavailable_usage(),
    )


class AttemptTracker:
    """Engine-side state for one reserved attempt; settles it exactly once.

    ``stage`` names the result reason that applies if an exception escapes the
    engine's post-launch processing at that point.
    """

    def __init__(self, ledger: Ledger, reservation: Reservation) -> None:
        self.ledger = ledger
        self.reservation = reservation
        self.stage = "internal_error"
        self.execution: dict[str, Any] | None = None
        self.usage: dict[str, Any] | None = None
        self.observed: dict[str, Any] = {"model": None, "thread_id": None}
        self.output_characters: int | None = None
        self.settled = False

    @property
    def attempt_id(self) -> str:
        return self.reservation.attempt_id

    def record_process(
        self,
        *,
        returncode: int,
        timed_out: bool,
        duration_seconds: float,
        stdout_text: str,
        stdout_truncated: bool,
        observed_model: str | None,
    ) -> None:
        parsed = parse_exec_stdout(stdout_text, truncated=stdout_truncated)
        self.execution = _execution(
            "timed_out" if timed_out else "exited",
            exit_code=returncode,
            duration_seconds=max(0.0, float(duration_seconds)),
            stdout_truncated=stdout_truncated,
        )
        self.usage = parsed["usage"]
        self.observed = {"model": short_text(observed_model), "thread_id": parsed["thread_id"]}

    def success_settlement(self) -> dict[str, Any]:
        assert self.execution is not None and self.usage is not None
        return _settlement(
            settled_by="engine",
            execution=self.execution,
            result_status="published",
            reason=None,
            usage=self.usage,
            observed=self.observed,
            output_characters=self.output_characters,
        )

    def failure_settlement(self, exc: BaseException) -> dict[str, Any]:
        interrupted = isinstance(exc, KeyboardInterrupt)
        if self.execution is None:
            # The process never returned a result to the engine.
            if interrupted:
                return _settlement(
                    settled_by="engine",
                    execution=_execution("interrupted"),
                    result_status="rejected",
                    reason="interrupted",
                    usage=unavailable_usage(),
                )
            if isinstance(exc.__cause__, FileNotFoundError):
                return _settlement(
                    settled_by="engine",
                    execution=_execution("launch_failed"),
                    result_status="not_evaluated",
                    reason="launch_failed",
                    usage=not_applicable_usage(),
                )
            return _settlement(
                settled_by="engine",
                execution=_execution("unknown"),
                result_status="not_evaluated",
                reason="internal_error",
                usage=unavailable_usage(),
            )
        reason = "interrupted" if interrupted else self.stage
        if reason not in RESULT_REASONS:
            reason = "internal_error"
        return _settlement(
            settled_by="engine",
            execution=self.execution,
            result_status="rejected",
            reason=reason,
            usage=self.usage or unavailable_usage(),
            observed=self.observed,
            output_characters=self.output_characters,
        )

    def settle(self, settlement: dict[str, Any]) -> str | None:
        """Settle once. Returns None on success, else a diagnostic message.

        Never raises for ledger problems: a settlement failure after launch must
        not change the engine's own outcome; the attempt stays visibly unsettled.
        """
        if self.settled:
            return None
        try:
            self.ledger.settle(self.attempt_id, settlement)
        except (AccountingError, OSError) as exc:
            return f"{type(exc).__name__}: {exc}"
        self.settled = True
        return None


# ---------------------------------------------------------------------------
# Queries (read-only, versioned JSON)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryFilter:
    repo_id: str | None = None
    run_id: str | None = None
    since: dt.datetime | None = None

    def matches(self, record: AttemptRecord) -> bool:
        workflow = record.reservation["workflow"]
        if self.repo_id is not None and workflow["repo_id"] != self.repo_id:
            return False
        if self.run_id is not None and workflow["run_id"] != self.run_id:
            return False
        if self.since is not None and _parse_iso(record.reserved["recorded_at"]) < self.since:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "run_id": self.run_id,
            "since": _iso(self.since) if self.since else None,
        }


def compact_attempt(
    record: AttemptRecord,
    *,
    now: dt.datetime,
    boot_id: str | None,
    pid_alive: Callable[[int], bool | None] = _pid_alive,
) -> dict[str, Any]:
    reservation = record.reservation
    workflow = reservation["workflow"]
    out: dict[str, Any] = {
        "attempt_id": record.attempt_id,
        "state": "settled" if record.settled else "unsettled",
        "reserved_at": record.reserved["recorded_at"],
        "settled_at": record.settled["recorded_at"] if record.settled else None,
        "retry_of": reservation["retry_of"],
        "slot_attempt": reservation["slot_attempt"],
        "phase": reservation["phase"],
        "slot": reservation["slot"],
        "repo_id": workflow["repo_id"],
        "run_id": workflow["run_id"],
        "workflow_kind": workflow["workflow_kind"],
        "host_label": reservation["launcher"]["host_label"],
        "refs": reservation["refs"],
        "requested": {
            "model": reservation["requested"]["model"],
            "reasoning_effort": reservation["requested"]["reasoning_effort"],
        },
        "engine": {
            "version": reservation["engine"]["version"],
            "code_digest": reservation["engine"]["code_digest"],
        },
    }
    if record.settled:
        settlement = record.settled["settlement"]
        usage = settlement["usage"]
        out.update(
            {
                "settled_by": settlement["settled_by"],
                "execution": settlement["execution"],
                "result": settlement["result"],
                "usage": {
                    "status": usage["status"],
                    "counters": usage["counters"],
                    "total_tokens": (
                        total_tokens(usage["counters"])
                        if usage["status"] in {"complete", "partial"}
                        else None
                    ),
                    "flags": usage["flags"],
                    "invalid_reasons": usage["invalid_reasons"],
                },
                "observed": settlement["observed"],
            }
        )
    else:
        age = max(0.0, (now - _parse_iso(record.reserved["recorded_at"])).total_seconds())
        out.update(
            {
                "settled_by": None,
                "execution": None,
                "result": None,
                "usage": None,
                "observed": None,
                "age_seconds": int(age),
                "stale": is_stale(reservation, age),
                "owner_process": owner_process_state(
                    reservation, boot_id=boot_id, pid_alive=pid_alive
                ),
            }
        )
    return out


def _ledger_block(view: LedgerView) -> dict[str, Any]:
    return {"present": view.present, "ledger_id": view.ledger_id, "events_read": view.events_read}


def _token_totals(records: list[dict[str, Any]], statuses: set[str]) -> dict[str, int]:
    totals = {name: 0 for name in COUNTER_NAMES}
    totals["total_tokens"] = 0
    totals["attempts"] = 0
    for rec in records:
        usage = rec["usage"]
        if not usage or usage["status"] not in statuses:
            continue
        totals["attempts"] += 1
        for name in COUNTER_NAMES:
            value = usage["counters"][name]
            if isinstance(value, int):
                totals[name] += value
        if isinstance(usage["total_tokens"], int):
            totals["total_tokens"] += usage["total_tokens"]
    return totals


def summary_query(
    view: LedgerView,
    query: QueryFilter,
    *,
    now: dt.datetime,
    boot_id: str | None,
    pid_alive: Callable[[int], bool | None] = _pid_alive,
) -> dict[str, Any]:
    records = [
        compact_attempt(rec, now=now, boot_id=boot_id, pid_alive=pid_alive)
        for rec in view.attempts.values()
        if query.matches(rec)
    ]
    settled = [r for r in records if r["state"] == "settled"]
    unsettled = [r for r in records if r["state"] == "unsettled"]

    def count(field_name: str, key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in settled:
            value = rec[field_name][key]
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

    without_complete = sum(
        1 for r in settled if r["usage"]["status"] not in {"complete", "not_applicable"}
    )
    return {
        "format": SUMMARY_FORMAT,
        "generated_at": _iso(now),
        "ledger": _ledger_block(view),
        "filters": query.as_dict(),
        "attempts": {
            "total": len(records),
            "settled": len(settled),
            "unsettled": len(unsettled),
            "stale": sum(1 for r in unsettled if r["stale"]),
        },
        "execution": count("execution", "status"),
        "result": count("result", "status"),
        "usage": count("usage", "status"),
        "tokens": {
            "complete_only": _token_totals(settled, {"complete"}),
            "including_partial": _token_totals(settled, {"complete", "partial"}),
        },
        "completeness": {
            "tokens_complete": not unsettled and without_complete == 0 and view.integrity_ok(),
            "attempts_without_complete_usage": without_complete + len(unsettled),
        },
        "diagnostics": dict(view.diagnostics),
    }


def attempts_query(
    view: LedgerView,
    query: QueryFilter,
    *,
    now: dt.datetime,
    boot_id: str | None,
    unsettled_only: bool = False,
    limit: int | None = None,
    pid_alive: Callable[[int], bool | None] = _pid_alive,
) -> dict[str, Any]:
    records = [
        compact_attempt(rec, now=now, boot_id=boot_id, pid_alive=pid_alive)
        for rec in reversed(list(view.attempts.values()))
        if query.matches(rec)
    ]
    if unsettled_only:
        records = [r for r in records if r["state"] == "unsettled"]
    total = len(records)
    if limit is not None:
        records = records[:limit]
    return {
        "format": ATTEMPTS_FORMAT,
        "generated_at": _iso(now),
        "ledger": _ledger_block(view),
        "filters": {**query.as_dict(), "unsettled_only": unsettled_only, "limit": limit},
        "matched": total,
        "attempts": records,
        "diagnostics": dict(view.diagnostics),
    }


def attempt_query(
    view: LedgerView,
    attempt_id: str,
    *,
    now: dt.datetime,
    boot_id: str | None,
    pid_alive: Callable[[int], bool | None] = _pid_alive,
) -> dict[str, Any] | None:
    record = view.attempts.get(attempt_id)
    if record is None:
        return None
    return {
        "format": ATTEMPT_FORMAT,
        "generated_at": _iso(now),
        "ledger": _ledger_block(view),
        "attempt": compact_attempt(record, now=now, boot_id=boot_id, pid_alive=pid_alive),
        "events": [record.reserved] + ([record.settled] if record.settled else []),
        "diagnostics": dict(view.diagnostics),
    }
