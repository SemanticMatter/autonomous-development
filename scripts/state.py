"""Shared state module for the autonomous-development plugin."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

STATE_SCHEMA_VERSION = 2
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"complete", "blocked", "cancelled", "archived"}
)
LEGACY_STATE_REL = Path(".ai/autonomous-development")
LEGACY_STATE_FILE_NAME = "run-state.json"


class StateError(RuntimeError):
    """User-actionable state error with a clear message."""


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------


@dataclass
class RepoInfo:
    """Snapshot of a git repository's identity and current state."""

    id: str
    canonical_root: Path
    git_common_dir: Path
    worktree_path: Path
    branch: str
    head_commit: str
    display_name: str
    remote_display: str


def _run_git(*args: str, cwd: Path) -> str:
    """Run a git command and return stripped stdout; return '' on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def _strip_credentials(url: str) -> str:
    """Remove userinfo (user:pass@ or token@) from a URL using url parsing."""
    try:
        parsed = urlsplit(url)
        if parsed.username:
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunsplit(
                (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
            )
    except Exception:
        pass
    return url


def _compute_repo_id(git_common_dir: Path, first_commit: str) -> str:
    """Compute a stable 16-char hex repo ID."""
    key = str(git_common_dir.resolve()) + "\n" + first_commit
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def resolve_repository(start: Path | None = None) -> RepoInfo:
    """Find git repository from start (or cwd). Raises StateError if not in a git repo."""
    cwd = (start or Path.cwd()).resolve()

    toplevel = _run_git("rev-parse", "--show-toplevel", cwd=cwd)
    if not toplevel:
        raise StateError(
            f"{cwd} is not inside a git repository. "
            "Run this command from within a git worktree."
        )
    canonical_root = Path(toplevel).resolve()

    raw_common = _run_git("rev-parse", "--git-common-dir", cwd=canonical_root)
    if raw_common:
        git_common_dir = (canonical_root / raw_common).resolve()
    else:
        git_common_dir = canonical_root

    worktree_path = Path(
        _run_git("rev-parse", "--show-toplevel", cwd=cwd) or toplevel
    ).resolve()

    branch = _run_git("branch", "--show-current", cwd=canonical_root)
    head_commit = _run_git("rev-parse", "HEAD", cwd=canonical_root)

    first_commit = _run_git("rev-list", "--max-parents=0", "HEAD", cwd=canonical_root)

    if raw_common:
        repo_id = _compute_repo_id(git_common_dir, first_commit)
    else:
        key = str(canonical_root) + "\n" + first_commit
        repo_id = hashlib.sha256(key.encode()).hexdigest()[:16]

    remote_raw = _run_git("remote", "get-url", "origin", cwd=canonical_root)
    if not remote_raw:
        remotes_v = _run_git("remote", "-v", cwd=canonical_root)
        first_line = remotes_v.splitlines()[0] if remotes_v else ""
        parts = first_line.split()
        remote_raw = parts[1] if len(parts) >= 2 else ""
    remote_display = _strip_credentials(remote_raw) if remote_raw else ""

    return RepoInfo(
        id=repo_id,
        canonical_root=canonical_root,
        git_common_dir=git_common_dir,
        worktree_path=worktree_path,
        branch=branch,
        head_commit=head_commit,
        display_name=canonical_root.name,
        remote_display=remote_display,
    )


# ---------------------------------------------------------------------------
# State home resolver
# ---------------------------------------------------------------------------


def resolve_state_home(state_dir_arg: str | None = None) -> Path:
    """Precedence: CLI arg > CLAUDE_AUTONOMOUS_STATE_HOME env > XDG > ~/.local/state/claude-autonomous"""
    if state_dir_arg:
        return Path(state_dir_arg).expanduser().resolve()

    env_val = os.environ.get("CLAUDE_AUTONOMOUS_STATE_HOME", "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-autonomous"

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            return Path(local_app) / "claude-autonomous"
        return Path.home() / "AppData" / "Local" / "claude-autonomous"

    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser().resolve() / "claude-autonomous"
    return Path.home() / ".local" / "state" / "claude-autonomous"


# ---------------------------------------------------------------------------
# Run ID
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """<YYYYMMDDTHHMMSSZ>-<secrets.token_hex(4)>"""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def validate_run_id(run_id: object) -> str:
    """Validate a run ID for safe use as a single filesystem path segment.

    A run ID is used directly as a directory name under the runs root. This is the
    single canonical validator: a crafted CLI arg, migrated/legacy state, or loaded
    run-state.json must not be able to escape the runs directory via absolute paths,
    separators, or `..` traversal. Mirror this with the resolved-path containment
    check in `run_dir_path`. Conforming v0.2 run IDs (produced by `new_run_id`)
    always pass, so this is backward compatible.
    """
    if not isinstance(run_id, str):
        raise StateError(
            f"Run ID must be a string, got {type(run_id).__name__}."
        )
    if not _RUN_ID_PATTERN.match(run_id):
        raise StateError(
            f"Invalid run ID {run_id!r}: must match {_RUN_ID_PATTERN.pattern} "
            "(one path segment of letters, digits, '.', '_', '-'; 1-80 chars; "
            "no path separators, no leading '.', '/', or '\\', no traversal)."
        )
    return run_id


# ---------------------------------------------------------------------------
# Legacy state detection
# ---------------------------------------------------------------------------


def detect_legacy_state(repo_root: Path) -> Path | None:
    """Return the legacy .ai/autonomous-development/ dir if run-state.json exists there, else None."""
    legacy_dir = repo_root / LEGACY_STATE_REL
    if (legacy_dir / LEGACY_STATE_FILE_NAME).exists():
        return legacy_dir
    return None


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------


def run_dir_path(state_home: Path, repo_id: str, run_id: str) -> Path:
    """<state_home>/repositories/<repo_id>/runs/<run_id>/

    The single canonical run-directory constructor. Validates the run ID
    lexically and confirms the resolved path stays within the runs root, so no
    caller can concatenate an unvalidated run ID into a filesystem path.
    """
    validate_run_id(run_id)
    runs_base = (state_home / "repositories" / repo_id / "runs").resolve()
    candidate = (runs_base / run_id).resolve()
    try:
        candidate.relative_to(runs_base)
    except ValueError:
        raise StateError(f"Run directory escapes runs root: {run_id!r}")
    return candidate


def repo_metadata_path(state_home: Path, repo_id: str) -> Path:
    """<state_home>/repositories/<repo_id>/metadata.json"""
    return state_home / "repositories" / repo_id / "metadata.json"


def make_relative_path(absolute: Path, run_dir: Path) -> str:
    """Return a relative path if absolute is inside run_dir; else return str(absolute)."""
    try:
        rel = absolute.resolve().relative_to(run_dir.resolve())
        parts = rel.parts
        if parts and parts[0] == "..":
            return str(absolute)
        return str(rel)
    except ValueError:
        return str(absolute)


def resolve_artifact_path(relative_or_abs: str, run_dir: Path) -> Path:
    """Resolve an artifact pointer to an absolute path confined to run_dir.

    Artifact pointers are controller-generated and always live inside the run
    directory. Reject both absolute paths and `..` traversal that escape run_dir
    so a crafted or legacy run-state cannot aim an artifact at an arbitrary local
    file and exfiltrate its contents into a Codex prompt.
    """
    p = Path(relative_or_abs)
    base = run_dir.resolve()
    resolved = p.resolve() if p.is_absolute() else (run_dir / p).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise StateError(f"Artifact path escapes run directory: {relative_or_abs!r}")
    return resolved


# ---------------------------------------------------------------------------
# Atomic write and locking
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, value: dict) -> None:
    """Write to an invocation-unique temp file then replace atomically.

    The temp file name is unique per call (PID + random token) rather than a
    fixed ``<name>.tmp``: two concurrent writers to the same target (e.g. two
    ``init --force`` processes updating one repository's metadata.json) would
    otherwise race on the same temp path, with one truncating or replacing the
    other's half-written file. A unique temp also lets a failed write be cleaned
    up without clobbering an unrelated in-flight writer's staging file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        temp.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temp.replace(path)
    except BaseException:
        # Never leave a stray staging file behind on failure. The replace is the
        # last step, so if it raised the temp may still exist.
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


try:  # POSIX advisory locking
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform-dependent
    _fcntl = None  # type: ignore[assignment]

try:  # Windows mandatory locking
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - platform-dependent
    _msvcrt = None  # type: ignore[assignment]


class LockTimeout(StateError):
    """Raised when an exclusive lock cannot be acquired within the timeout."""


class CrossProcessLock:
    """Bounded, real cross-platform exclusive file lock.

    Provides genuine mutual exclusion between independent processes (not a
    best-effort no-op) so the run-state and repository-initialization critical
    sections are safe on every supported platform. Backends, in priority order:

      * ``fcntl``    — POSIX advisory ``flock`` (auto-released on close/crash).
      * ``msvcrt``   — Windows mandatory byte-range lock (auto-released on
                       close/crash).
      * ``portable`` — atomic ``O_CREAT | O_EXCL`` lock-file. Works anywhere,
                       including when neither ``fcntl`` nor ``msvcrt`` is
                       available; this is the path a stripped-down or exotic
                       platform falls back to.

    The acquire is bounded by ``timeout`` seconds and raises ``LockTimeout``
    with an actionable message rather than blocking forever. ``force_backend``
    (class attribute) pins a backend so tests can exercise the portable /
    Windows-compatible path on a POSIX host.
    """

    force_backend: str | None = None

    def __init__(
        self,
        lock_path: Path,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.02,
    ) -> None:
        self._lock_path = Path(lock_path)
        self._timeout = timeout
        self._poll = poll_interval
        self._fd: int | None = None
        self._backend: str | None = None
        self._holds_exclusive_file = False
        self._owner_token = secrets.token_hex(8)

    def _select_backend(self) -> str:
        forced = type(self).force_backend
        if forced:
            return forced
        if _fcntl is not None:
            return "fcntl"
        if _msvcrt is not None:
            return "msvcrt"
        return "portable"

    def _owner_metadata(self) -> str:
        """Owner record stamped into a portable lock file for stale-lock recovery."""
        return json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "token": self._owner_token,
                "backend": "portable",
            },
            sort_keys=True,
        )

    def _read_portable_owner(self) -> str:
        """Best-effort description of the current portable lock-file owner."""
        try:
            info = json.loads(self._lock_path.read_text(encoding="utf-8"))
            return (
                f"The lock file records owner pid={info.get('pid')} "
                f"host={info.get('host')!r} started_at={info.get('started_at')!r}."
            )
        except (OSError, ValueError):
            return "The lock file records no readable owner metadata."

    def _timeout_error(self) -> LockTimeout:
        base = (
            f"Could not acquire lock {str(self._lock_path)!r} within "
            f"{self._timeout:g}s. "
        )
        if self._backend == "portable":
            # The portable backend is a plain O_EXCL marker file, NOT a kernel
            # lock, so a crashed holder can strand it. Removing it is the only
            # recovery — but only once the recorded owner is known to be gone.
            detail = (
                "This is the portable lock-file backend (an atomic O_EXCL marker "
                "file, not a kernel-held lock), so a crashed holder can leave it "
                "behind. " + self._read_portable_owner() + " If that process is "
                f"no longer running, remove the lock file ({str(self._lock_path)!r}) "
                "to recover. Never remove it while the owning process is still "
                "alive."
            )
        else:
            # fcntl/msvcrt locks live on the open file description in the kernel
            # and are released automatically when the holder exits or crashes.
            # Deleting the pathname does NOT release such a lock and is unsafe: a
            # new process could create a fresh file at the same path and acquire a
            # second, conflicting lock while the original holder still holds it.
            detail = (
                f"This is the {self._backend!r} OS lock backend; the kernel holds "
                "the lock on the open file and releases it automatically when the "
                "owning process exits or crashes. A live holder is therefore "
                "running now — wait for it to finish or stop that process. Do NOT "
                "delete the lock file: the holder keeps its lock on the original "
                "file even after the path is unlinked, so deleting the path lets "
                "another process create a new file there and take a second, "
                "conflicting lock."
            )
        return LockTimeout(base + detail)

    def __enter__(self) -> CrossProcessLock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._backend = self._select_backend()
        deadline = time.monotonic() + self._timeout
        if self._backend == "portable":
            self._acquire_portable(deadline)
        else:
            self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
            self._acquire_fd(deadline)
        return self

    def _acquire_fd(self, deadline: float) -> None:
        assert self._fd is not None
        while True:
            try:
                if self._backend == "fcntl":
                    _fcntl.flock(self._fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                else:  # msvcrt
                    _msvcrt.locking(self._fd, _msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise self._timeout_error()
                time.sleep(self._poll)

    def _acquire_portable(self, deadline: float) -> None:
        while True:
            try:
                self._fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                self._holds_exclusive_file = True
                # Stamp owner metadata so a stranded lock from a crashed holder
                # can be attributed (pid/host/start) during recovery. Best-effort:
                # a write failure must not defeat the acquired lock.
                try:
                    os.write(self._fd, self._owner_metadata().encode("utf-8"))
                except OSError:
                    pass
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise self._timeout_error()
                time.sleep(self._poll)

    def _portable_lock_is_ours(self, fd: int) -> bool:
        """Whether the file at the lock path is still the one we created.

        Guards the release race: if our lock file is removed and another
        process recreates the path, the replacement is a *different* inode (and
        carries a different owner token). Deleting it would evict a lock we do
        not hold. The fstat happens before the descriptor is closed so the inode
        we created stays pinned and cannot be reused underneath us.
        """
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.stat(self._lock_path)
        except OSError:
            # Path is gone or unreadable: nothing of ours to unlink.
            return False
        if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            # A different file now occupies the path; it belongs to someone else.
            return False
        try:
            info = json.loads(self._lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Metadata unreadable (our best-effort stamp may have failed). Inode
            # identity already proved this is the file we created, so honor it.
            return True
        # Same inode but rewritten owner token means the content was replaced in
        # place by another holder; do not unlink it.
        return info.get("token") == self._owner_token

    def __exit__(self, *args: object) -> None:
        safe_to_unlink = False
        if self._fd is not None:
            if self._backend == "portable" and self._holds_exclusive_file:
                safe_to_unlink = self._portable_lock_is_ours(self._fd)
            try:
                if self._backend == "fcntl":
                    _fcntl.flock(self._fd, _fcntl.LOCK_UN)
                elif self._backend == "msvcrt":
                    try:
                        _msvcrt.locking(self._fd, _msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                os.close(self._fd)
                self._fd = None
        if self._holds_exclusive_file:
            if safe_to_unlink:
                try:
                    self._lock_path.unlink()
                except FileNotFoundError:
                    pass
            self._holds_exclusive_file = False


class RunStateLock(CrossProcessLock):
    """Exclusive lock guarding a single run's state file."""

    def __init__(self, run_dir: Path, *, timeout: float = 30.0) -> None:
        super().__init__(run_dir / ".run-state.lock", timeout=timeout)


class RepoInitLock(CrossProcessLock):
    """Repository-level lock serializing run creation for a repository.

    Run-state locks are per-run-directory, so two concurrent ``init`` calls that
    mint *different* run IDs never contend on a shared lock and could both pass
    the "is another run already active?" check. This repository-scoped lock
    makes the active-run check and run creation a single critical section.
    """

    def __init__(self, state_home: Path, repo_id: str, *, timeout: float = 30.0) -> None:
        repo_dir = state_home / "repositories" / repo_id
        repo_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(repo_dir / ".init.lock", timeout=timeout)


# ---------------------------------------------------------------------------
# State schema validation
# ---------------------------------------------------------------------------


def validate_state(state: dict) -> None:
    """Validate loaded state dict. Raises StateError on schema problems."""
    if not isinstance(state, dict):
        raise StateError("State must be a JSON object.")

    if "status" not in state:
        raise StateError("State is missing required field 'status'.")
    if not isinstance(state["status"], str):
        raise StateError("State field 'status' must be a string.")

    if "run_id" not in state:
        raise StateError("State is missing required field 'run_id'.")
    if not isinstance(state["run_id"], str):
        raise StateError("State field 'run_id' must be a string.")
    validate_run_id(state["run_id"])

    schema_version = state.get("schema_version") or state.get("version")
    if schema_version is not None and schema_version not in (1, 2):
        raise StateError(
            f"Unsupported schema_version {schema_version!r}. "
            "Supported versions are 1 (legacy) and 2. "
            "Run `migrate-legacy-state` to upgrade."
        )


# ---------------------------------------------------------------------------
# Schema migration v1 → v2
# ---------------------------------------------------------------------------


def _remap_path(p: Path, legacy_dir: Path | None, run_dir: Path) -> str:
    """Convert a legacy absolute path to a run-dir-relative path.

    If the path is under legacy_dir, produce the relative path assuming the file
    was copied to the equivalent location under run_dir.  Fallback: try to relativize
    against run_dir directly.  Return the original absolute path string only as a last resort.
    """
    if legacy_dir is not None:
        try:
            rel_to_legacy = p.resolve().relative_to(legacy_dir.resolve())
            return str(rel_to_legacy)
        except ValueError:
            pass
    return make_relative_path(p, run_dir)


def migrate_v1_to_v2(
    legacy_state: dict,
    run_dir: Path,
    repo: RepoInfo,
    legacy_dir: Path | None = None,
) -> dict:
    """Convert a v1/legacy state dict to v2 format in-memory."""
    state = dict(legacy_state)

    state.pop("version", None)
    state["schema_version"] = 2

    if "run_id" not in state:
        state["run_id"] = new_run_id()

    state["repository"] = {
        "id": repo.id,
        "display_name": repo.display_name,
        "canonical_root": str(repo.canonical_root),
        "worktree_mode": "isolated",
        "remote_display": repo.remote_display,
    }

    baseline = state.get("baseline", {})
    if not isinstance(baseline, dict):
        baseline = {}
    if "branch" not in baseline:
        baseline["branch"] = repo.branch
    if "worktree_path" not in baseline:
        baseline["worktree_path"] = str(repo.worktree_path)
    state["baseline"] = baseline

    artifacts = state.get("artifacts", {})
    if isinstance(artifacts, dict):
        new_artifacts: dict[str, object] = {}
        for key, value in artifacts.items():
            if isinstance(value, str):
                p = Path(value)
                if p.is_absolute():
                    new_artifacts[key] = _remap_path(p, legacy_dir, run_dir)
                else:
                    new_artifacts[key] = value
            else:
                new_artifacts[key] = value
        state["artifacts"] = new_artifacts

    for list_key in ("reviews", "adversarial_reviews"):
        entries = state.get(list_key, [])
        if isinstance(entries, list):
            updated_entries = []
            for entry in entries:
                if isinstance(entry, dict) and "path" in entry:
                    p = Path(entry["path"])
                    if p.is_absolute():
                        entry = dict(entry)
                        entry["path"] = _remap_path(p, legacy_dir, run_dir)
                updated_entries.append(entry)
            state[list_key] = updated_entries

    checks = state.get("verification", {}).get("checks", [])
    if isinstance(checks, list):
        updated_checks = []
        for check in checks:
            if isinstance(check, dict) and "log" in check:
                p = Path(check["log"])
                if p.is_absolute():
                    check = dict(check)
                    check["log"] = _remap_path(p, legacy_dir, run_dir)
            updated_checks.append(check)
        if "verification" in state and isinstance(state["verification"], dict):
            state["verification"]["checks"] = updated_checks

    state.setdefault("migrated_from", "v1")

    return state


# ---------------------------------------------------------------------------
# Run state loading / saving
# ---------------------------------------------------------------------------

_STATE_FILE_NAME = "run-state.json"


def load_run_state(run_dir: Path, required: bool = True) -> dict:
    """Load and validate run-state.json from run_dir. Returns {} if not found and not required."""
    path = run_dir / _STATE_FILE_NAME
    if not path.exists():
        if required:
            raise StateError(f"No run-state.json found at {path}")
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Invalid run state at {path}: {exc}") from exc
    if not isinstance(state, dict):
        raise StateError(f"Run state must be a JSON object: {path}")
    validate_state(state)
    return state


def save_run_state(run_dir: Path, state: dict) -> None:
    """Add updated_at timestamp and atomically write run-state.json."""
    state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    atomic_write_json(run_dir / _STATE_FILE_NAME, state)


# ---------------------------------------------------------------------------
# Repo metadata
# ---------------------------------------------------------------------------


def load_repo_metadata(state_home: Path, repo_id: str) -> dict:
    """Load repositories/<repo_id>/metadata.json or return {}."""
    path = repo_metadata_path(state_home, repo_id)
    if not path.exists():
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def save_repo_metadata(state_home: Path, repo_id: str, meta: dict) -> None:
    """Save repositories/<repo_id>/metadata.json atomically."""
    atomic_write_json(repo_metadata_path(state_home, repo_id), meta)


# ---------------------------------------------------------------------------
# Run discovery and selection
# ---------------------------------------------------------------------------


@dataclass
class RunRef:
    """Reference to a discovered run with its loaded state."""

    run_id: str
    run_dir: Path
    state: dict


def find_active_runs(state_home: Path, repo_id: str) -> list[RunRef]:
    """Return all non-terminal runs for this repository."""
    return [
        r
        for r in find_all_runs(state_home, repo_id)
        if r.state.get("status") not in TERMINAL_STATUSES
    ]


def find_all_runs(state_home: Path, repo_id: str) -> list[RunRef]:
    """Return all runs (active + archived + terminal) for this repository."""
    runs_dir = state_home / "repositories" / repo_id / "runs"
    if not runs_dir.is_dir():
        return []
    refs: list[RunRef] = []
    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir():
            continue
        state = load_run_state(child, required=False)
        if not state:
            continue
        run_id = state.get("run_id", child.name)
        refs.append(RunRef(run_id=run_id, run_dir=child, state=state))
    return refs


def resolve_active_run(
    state_home: Path,
    repo_id: str,
    repo_root: Path,
    run_id: str | None = None,
    *,
    allow_multiple: bool = False,
) -> RunRef:
    """Resolve the run to operate on."""
    if run_id is not None:
        run_dir = run_dir_path(state_home, repo_id, run_id)
        state = load_run_state(run_dir, required=True)
        return RunRef(run_id=run_id, run_dir=run_dir, state=state)

    active = find_active_runs(state_home, repo_id)

    if len(active) == 1:
        return active[0]

    if len(active) > 1:
        if allow_multiple:
            return active[0]
        ids = ", ".join(r.run_id for r in active)
        raise StateError(
            f"Multiple active runs found: {ids}. "
            "Specify one with --run-id <run_id> or use `list-runs` to review them."
        )

    legacy_dir = detect_legacy_state(repo_root)
    if legacy_dir is not None:
        legacy_path = legacy_dir / LEGACY_STATE_FILE_NAME
        try:
            legacy_state = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Invalid legacy state at {legacy_path}: {exc}") from exc
        if not isinstance(legacy_state, dict):
            raise StateError(f"Legacy state must be a JSON object: {legacy_path}")
        run_id_val = legacy_state.get("run_id", "legacy")
        print(
            f"[autonomous-development] DEPRECATION: Using legacy state at {legacy_dir}. "
            "Run `controller.py migrate-legacy-state` to upgrade to the portable layout.",
            file=sys.stderr,
        )
        return RunRef(run_id=run_id_val, run_dir=legacy_dir, state=legacy_state)

    raise StateError(
        "No active workflow run found. "
        'Run `controller.py init --feature "..."` to start a new run, '
        "or `list-runs` to see all runs."
    )


# ---------------------------------------------------------------------------
# Terminal-state and mutation-integrity policy
# ---------------------------------------------------------------------------
#
# Three explicit run-access contracts replace the ambiguous historical use of
# `resolve_active_run` for both reads and writes:
#
#   * resolve_run_for_inspection      — read-only; may resolve terminal runs.
#   * resolve_run_for_active_mutation — refuses terminal runs (even with an
#                                       explicit --run-id) so a completed,
#                                       blocked, cancelled, or archived run can
#                                       never be mutated or resurrected.
#   * resolve_run_for_transition      — lifecycle commands that must read a
#                                       terminal run (e.g. archiving a complete
#                                       run); the caller enforces the transition
#                                       table via `assert_transition_allowed`.
#
# Mutating commands additionally re-assert the status under the run lock (see
# `require_active_run_state`) immediately before publishing, closing the TOCTOU
# window between resolution and the locked write.

# Lifecycle transition table: operation -> (allowed source statuses, target).
# Centralized so status checks are not duplicated (and silently diverge) across
# command handlers. No operation may move a terminal run back to "active".
TRANSITION_POLICY: dict[str, tuple[frozenset[str], str]] = {
    "cancel": (frozenset({"active"}), "cancelled"),
    "block": (frozenset({"active"}), "blocked"),
    "archive-run": (frozenset({"complete", "blocked", "cancelled"}), "archived"),
}


def _terminal_mutation_error(run_id: str, status: str, operation: str) -> StateError:
    return StateError(
        f"Cannot {operation} run {run_id!r}: its status is {status!r} (terminal). "
        f"Terminal runs are immutable and cannot be mutated or resurrected. "
        f"Inspect it with `status --run-id {run_id}` or start a new run with "
        f"`init --feature ...`."
    )


def _inactive_mutation_error(run_id: str, status: str, operation: str) -> StateError:
    return StateError(
        f"Cannot {operation} run {run_id!r}: its status is {status!r}, but "
        f"{operation} requires an active run. Inspect it with "
        f"`status --run-id {run_id}`."
    )


def _reject_non_active(run_id: str, status: object, operation: str) -> None:
    """Raise unless `status` is exactly the string "active".

    Active-only mutations must require the canonical active status rather than
    merely "not terminal": an unknown/garbage status (corruption, a future
    status this build does not understand, or a partial write) must fail closed,
    never be treated as mutable.
    """
    if status == "active":
        return
    if status in TERMINAL_STATUSES:
        raise _terminal_mutation_error(run_id, str(status), operation)
    raise _inactive_mutation_error(run_id, str(status), operation)


def require_active_run_state(state: dict, run_id: str, operation: str) -> None:
    """Raise unless `state` is exactly active. Call after reloading under the lock.

    This is the TOCTOU guard: a long-running operation (a verification check or a
    Codex exec) may have been cancelled/blocked while it ran, so the freshly
    reloaded status must be re-checked before publishing, or the write would
    resurrect a terminal (or otherwise non-active) run.
    """
    _reject_non_active(run_id, state.get("status"), operation)


def verify_loaded_run_identity(
    state: dict, *, run_dir: Path, expected_repo_id: str
) -> None:
    """Enforce run-identity invariants on a freshly loaded state dict.

    state.run_id must equal the run directory name and state.repository.id must
    match the selected repository, so a tampered or misfiled run-state cannot be
    mutated under the wrong identity. Operates on the exact state object being
    mutated so it can be re-asserted *under the lock* after every reload (defense
    in depth), not only at pre-lock resolution. The legacy in-repo layout
    (run_dir is the `.ai/autonomous-development` directory, not `runs/<run_id>`)
    is exempt.
    """
    if run_dir.parent.name != "runs":
        return  # legacy compatibility path
    recorded_id = state.get("run_id")
    if recorded_id != run_dir.name:
        raise StateError(
            f"State integrity error: run-state run_id {recorded_id!r} does not "
            f"match its run directory name {run_dir.name!r}."
        )
    recorded_repo = state.get("repository", {})
    recorded_repo_id = (
        recorded_repo.get("id") if isinstance(recorded_repo, dict) else None
    )
    if not recorded_repo_id:
        raise StateError(
            f"State integrity error: run {run_dir.name!r} does not record a "
            f"repository id; an external-layout run must be bound to its "
            f"repository before it can be mutated."
        )
    if recorded_repo_id != expected_repo_id:
        raise StateError(
            f"State integrity error: run {run_dir.name!r} records repository "
            f"{recorded_repo_id!r} but the current repository is {expected_repo_id!r}."
        )


def verify_run_identity(ref: RunRef, repo_id: str) -> None:
    """Enforce run-identity invariants for a resolved RunRef (pre-lock check)."""
    verify_loaded_run_identity(
        ref.state, run_dir=ref.run_dir, expected_repo_id=repo_id
    )


def assert_transition_allowed(
    current_status: str, operation: str, run_id: str
) -> None:
    """Enforce the lifecycle transition table. Call under the run lock."""
    allowed, _target = TRANSITION_POLICY[operation]
    if current_status not in allowed:
        raise StateError(
            f"Cannot {operation} run {run_id!r}: current status is "
            f"{current_status!r}; {operation} is only allowed from "
            f"{sorted(allowed)}. This prevents resurrecting or overwriting a "
            f"terminal run."
        )


def resolve_run_for_active_mutation(
    state_home: Path,
    repo_id: str,
    repo_root: Path,
    run_id: str | None = None,
    *,
    operation: str = "mutate",
    allow_multiple: bool = False,
) -> RunRef:
    """Resolve a run for a state-changing command, refusing terminal runs.

    Mirrors `resolve_active_run` but additionally (a) rejects a terminal run even
    when named by an explicit --run-id, and (b) enforces run-identity invariants.
    Handlers must still re-assert the status under the lock with
    `require_active_run_state` before publishing.
    """
    ref = resolve_active_run(
        state_home, repo_id, repo_root, run_id, allow_multiple=allow_multiple
    )
    verify_run_identity(ref, repo_id)
    _reject_non_active(ref.run_id, ref.state.get("status"), operation)
    return ref


def resolve_run_for_transition(
    state_home: Path,
    repo_id: str,
    repo_root: Path,
    run_id: str | None = None,
) -> RunRef:
    """Resolve a run for a lifecycle transition that may target a terminal run.

    Read-resolution semantics (terminal runs are reachable, e.g. to archive a
    completed run). The caller enforces the transition table under the lock.
    Lifecycle transitions (cancel/block/archive-run) mutate state, so the same
    run-identity invariants enforced for active mutations must hold here too: a
    tampered or misfiled run-state whose run_id or repository.id is missing or
    inconsistent must not be transitioned under the wrong identity.
    """
    ref = resolve_run_for_inspection(state_home, repo_id, repo_root, run_id)
    verify_run_identity(ref, repo_id)
    return ref


def resolve_run_for_inspection(
    state_home: Path,
    repo_id: str,
    repo_root: Path,
    run_id: str | None = None,
) -> RunRef:
    """Resolve a run for READ-ONLY inspection (status, usage-report).

    Like `resolve_active_run`, but when no active run exists it falls back to
    the most-recently-created run (terminal ones included) so inspection keeps
    working after a run completes/cancels. Mutating commands must keep using
    `resolve_active_run`, which intentionally refuses to operate on terminal runs
    without an explicit `--run-id`.
    """
    if run_id is not None:
        return resolve_active_run(state_home, repo_id, repo_root, run_id)

    active = find_active_runs(state_home, repo_id)
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        ids = ", ".join(r.run_id for r in active)
        raise StateError(
            f"Multiple active runs found: {ids}. "
            "Specify one with --run-id <run_id> or use `list-runs` to review them."
        )

    all_runs = find_all_runs(state_home, repo_id)
    if all_runs:
        # Order by the recorded creation timestamp (ISO-8601, sortable), not the
        # run_id: a legacy/custom id need not be chronological, so lexical id
        # ordering could pick a stale run. Fall back to run_id when created_at is
        # absent so ordering is still deterministic.
        return max(
            all_runs,
            key=lambda r: (str(r.state.get("created_at") or ""), r.run_id),
        )

    # No runs at all: defer to resolve_active_run for the canonical legacy
    # detection / "no run found" guidance.
    return resolve_active_run(state_home, repo_id, repo_root, None)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


class DriftKind(Enum):
    """Classification of repository drift relative to a recorded baseline."""

    NONE = "none"
    EXPECTED = "expected"
    UNSAFE = "unsafe"


@dataclass
class DriftResult:
    """Result of a drift detection check."""

    kind: DriftKind
    message: str
    recovery: str


def detect_drift(state: dict, repo: RepoInfo) -> DriftResult:
    """Check for drift between recorded baseline and current repo state."""
    repo_block = state.get("repository", {})
    baseline = state.get("baseline", {})

    if isinstance(repo_block, dict) and repo_block.get("id"):
        recorded_repo_id = repo_block["id"]
        if recorded_repo_id != repo.id:
            return DriftResult(
                kind=DriftKind.UNSAFE,
                message=(
                    f"Repository identity changed: recorded {recorded_repo_id!r}, "
                    f"current {repo.id!r}."
                ),
                recovery=(
                    "You appear to be in a different repository. "
                    "Switch to the correct repository or use `list-runs` to find the right run."
                ),
            )

    if isinstance(baseline, dict):
        recorded_worktree = baseline.get("worktree_path", "")
        if recorded_worktree and str(repo.worktree_path) != recorded_worktree:
            return DriftResult(
                kind=DriftKind.UNSAFE,
                message=(
                    f"Worktree changed: recorded {recorded_worktree!r}, "
                    f"current {str(repo.worktree_path)!r}."
                ),
                recovery=(
                    "Switch to the recorded worktree or run `accept-drift` "
                    "to record the new worktree as the baseline."
                ),
            )

        recorded_branch = baseline.get("branch", "")
        if recorded_branch and repo.branch != recorded_branch:
            return DriftResult(
                kind=DriftKind.UNSAFE,
                message=(
                    f"Branch changed: recorded {recorded_branch!r}, "
                    f"current {repo.branch!r}."
                ),
                recovery=(
                    f"Switch back to branch {recorded_branch!r} or run `accept-drift` "
                    "to record the new branch as the baseline."
                ),
            )

        recorded_commit = baseline.get("commit", "")
        if recorded_commit and repo.head_commit and repo.head_commit != recorded_commit:
            return DriftResult(
                kind=DriftKind.EXPECTED,
                message=(
                    f"HEAD advanced from {recorded_commit[:12]!r} "
                    f"to {repo.head_commit[:12]!r} on branch {repo.branch!r}."
                ),
                recovery="No action required; HEAD advancing on the same branch is expected.",
            )

    return DriftResult(
        kind=DriftKind.NONE,
        message="No drift detected.",
        recovery="",
    )


# ---------------------------------------------------------------------------
# Repository context string
# ---------------------------------------------------------------------------


_INSTRUCTION_NAMES = frozenset({"CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules"})
_BUILD_MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
        "package.json",
        "pnpm-workspace.yaml",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "composer.json",
        "Makefile",
        "CMakeLists.txt",
    }
)
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec"})
_CI_PREFIXES = (".github/workflows/", ".gitlab-ci", ".circleci/", "azure-pipelines")


def build_repository_manifest(tracked_files: list[str]) -> dict[str, list[str]]:
    """Derive a compact, relevance-oriented manifest from the tracked file set.

    Returns sections (instructions, build manifests, primary modules, test roots,
    CI workflows) instead of an arbitrary file dump. Pure function of the file list
    so it is deterministically testable.
    """
    instructions: list[str] = []
    build_manifests: list[str] = []
    test_roots: set[str] = set()
    ci: list[str] = []
    top_dirs: set[str] = set()

    for raw in tracked_files:
        path = raw.strip()
        if not path:
            continue
        parts = path.split("/")
        name = parts[-1]

        if name in _INSTRUCTION_NAMES:
            instructions.append(path)
        if name in _BUILD_MANIFEST_NAMES:
            build_manifests.append(path)
        if path.startswith(_CI_PREFIXES) or name in {
            ".gitlab-ci.yml",
            "azure-pipelines.yml",
        }:
            ci.append(path)

        for depth, segment in enumerate(parts[:-1]):
            if segment in _TEST_DIR_NAMES:
                test_roots.add("/".join(parts[: depth + 1]))
                break

        if len(parts) > 1 and not parts[0].startswith("."):
            top_dirs.add(parts[0])

    primary_modules = sorted(d for d in top_dirs if d not in _TEST_DIR_NAMES)
    return {
        "instructions": sorted(set(instructions)),
        "build_manifests": sorted(set(build_manifests)),
        "primary_modules": primary_modules,
        "test_roots": sorted(test_roots),
        "ci": sorted(set(ci)),
    }


def _format_manifest_section(title: str, items: list[str]) -> str:
    if not items:
        return f"{title}:\n- (none)\n"
    body = "\n".join(f"- {item}" for item in items)
    return f"{title}:\n{body}\n"


def repository_context(repo: RepoInfo) -> str:
    """Return a compact repository manifest for inclusion in Codex prompts.

    Replaces the former first-250-tracked-files dump with relevance-oriented
    sections so Codex learns where conventions and build boundaries live.
    """
    tracked = _run_git("ls-files", cwd=repo.canonical_root)
    manifest = build_repository_manifest(tracked.splitlines())
    status = _run_git("status", "--short", cwd=repo.canonical_root)
    sections = (
        _format_manifest_section("Instructions", manifest["instructions"])
        + "\n"
        + _format_manifest_section("Build manifests", manifest["build_manifests"])
        + "\n"
        + _format_manifest_section("Primary modules", manifest["primary_modules"])
        + "\n"
        + _format_manifest_section("Test roots", manifest["test_roots"])
        + "\n"
        + _format_manifest_section("CI", manifest["ci"])
    )
    return (
        f"Repository: {repo.display_name}\n"
        f'Branch: {repo.branch or "(detached)"}\n'
        f'HEAD: {repo.head_commit or "(unknown)"}\n'
        f'Working tree status:\n{status or "(clean)"}\n\n'
        f'Remote (credential-stripped, informational; workflow must not push): '
        f'{repo.remote_display or "(none)"}\n\n'
        f"{sections}"
    )
