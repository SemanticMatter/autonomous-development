"""Comprehensive tests for the state module (scripts/state.py)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
STOP_GATE = SCRIPTS / "stop_gate.py"

# Make sure scripts/ is importable
sys.path.insert(0, str(SCRIPTS))
import state as state_module  # noqa: E402
from state import (  # noqa: E402
    CrossProcessLock,
    DriftKind,
    LockTimeout,
    RunStateLock,
    StateError,
    detect_drift,
    detect_legacy_state,
    find_active_runs,
    load_run_state,
    new_run_id,
    resolve_artifact_path,
    resolve_repository,
    resolve_state_home,
    resolve_active_run,
    resolve_run_for_inspection,
    run_dir_path,
    save_run_state,
    validate_run_id,
    validate_state,
    atomic_write_json,
    LEGACY_STATE_FILE_NAME,
    LEGACY_STATE_REL,
    STATE_SCHEMA_VERSION,
)

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _make_repo_with_config(path: Path | None = None) -> Path:
    """Create a minimal git repo with local user config (no global config dependency)."""
    temp = Path(tempfile.mkdtemp()) if path is None else path
    temp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(temp)}
    subprocess.run(["git", "init", "-q", str(temp)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(temp), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(
        ["git", "-C", str(temp), "config", "user.name", "Test User"], check=True
    )
    (temp / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(temp), "commit", "-qm", "initial"], check=True)
    return temp


def _minimal_state(run_id: str = "test-run", status: str = "active") -> dict:
    """Return a minimal valid run state dict."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "phase": "initialized",
        "feature": "test feature",
        "baseline": {"commit": "abc123", "branch": "main"},
        "repository": {"id": "repoid00000001"},
        "verification": {"checks": [], "passed": False},
        "reviews": [],
        "adversarial_reviews": [],
        "stop_gate_blocks": 0,
    }


def _pinned_feature_state(repo_info, run_id: str) -> dict:
    """A minimal active feature run bound to `repo_info`'s repository and worktree,
    as `init` records it."""
    state = _minimal_state(run_id=run_id, status="active")
    state["workflow_kind"] = "feature"
    state["repository"] = {
        "id": repo_info.id,
        "worktree_path": str(repo_info.worktree_path),
    }
    state["baseline"]["worktree_path"] = str(repo_info.worktree_path)
    return state


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class StateModuleTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                # Fix permissions before removing (for test 24)
                try:
                    shutil.rmtree(str(d))
                except PermissionError:
                    subprocess.run(["chmod", "-R", "755", str(d)])
                    shutil.rmtree(str(d))

    def _tmpdir(self, path: Path | None = None) -> Path:
        d = Path(tempfile.mkdtemp()) if path is None else path
        d.mkdir(parents=True, exist_ok=True)
        self._tmpdirs.append(d)
        return d

    def make_repo_with_config(self, path: Path | None = None) -> Path:
        """Create a minimal git repo with local user config (no global config dependency)."""
        if path is None:
            path = Path(tempfile.mkdtemp())
            self._tmpdirs.append(path)
        else:
            self._tmpdirs.append(path)
        return _make_repo_with_config(path)

    # -----------------------------------------------------------------------
    # 1. repo discovery from nested directory
    # -----------------------------------------------------------------------

    def test_repo_discovery_from_nested_directory(self) -> None:
        """resolve_repository called from a subdir returns the git toplevel."""
        repo = self.make_repo_with_config()
        subdir = repo / "sub" / "deep"
        subdir.mkdir(parents=True)

        info = resolve_repository(subdir)

        self.assertEqual(info.canonical_root, repo.resolve())

    # -----------------------------------------------------------------------
    # 2. repo discovery from root
    # -----------------------------------------------------------------------

    def test_repo_discovery_from_root(self) -> None:
        """resolve_repository called from the repo root itself works."""
        repo = self.make_repo_with_config()

        info = resolve_repository(repo)

        self.assertEqual(info.canonical_root, repo.resolve())

    # -----------------------------------------------------------------------
    # 3. repo discovery in linked worktree
    # -----------------------------------------------------------------------

    @unittest.skipIf(
        subprocess.run(
            ["git", "worktree", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0,
        "git worktree not available",
    )
    def test_repo_discovery_in_linked_worktree(self) -> None:
        """resolve_repository in a linked worktree shares git_common_dir with main tree."""
        repo = self.make_repo_with_config()
        branch_name = "worktree-branch"
        worktree_path = Path(tempfile.mkdtemp())
        self._tmpdirs.append(worktree_path)
        worktree_path.rmdir()  # git worktree add creates it

        # Create branch then linked worktree
        subprocess.run(
            ["git", "-C", str(repo), "branch", branch_name],
            check=True,
        )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                str(worktree_path),
                branch_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            self.skipTest(f"git worktree add failed: {result.stderr.decode()}")

        main_info = resolve_repository(repo)
        wt_info = resolve_repository(worktree_path)

        self.assertEqual(main_info.git_common_dir, wt_info.git_common_dir)

    # -----------------------------------------------------------------------
    # 4. repo without remote
    # -----------------------------------------------------------------------

    def test_repo_without_remote(self) -> None:
        """A repo with no remote still produces a valid repo_id (non-empty string)."""
        repo = self.make_repo_with_config()

        info = resolve_repository(repo)

        self.assertIsInstance(info.id, str)
        self.assertTrue(len(info.id) > 0)
        self.assertEqual(info.remote_display, "")

    # -----------------------------------------------------------------------
    # 5. remote credentials stripped
    # -----------------------------------------------------------------------

    def test_remote_credentials_stripped(self) -> None:
        """Remote URL with user:pass@ is sanitised in remote_display."""
        repo = self.make_repo_with_config()
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://user:secret@github.com/org/repo.git",
            ],
            check=True,
        )

        info = resolve_repository(repo)

        self.assertNotIn("user", info.remote_display)
        self.assertNotIn("secret", info.remote_display)
        self.assertIn("github.com", info.remote_display)

    # -----------------------------------------------------------------------
    # 6. XDG state resolution default (Linux/non-macOS/non-Windows)
    # -----------------------------------------------------------------------

    @unittest.skipIf(sys.platform in ("darwin", "win32"), "Linux-only test")
    def test_xdg_state_resolution_default(self) -> None:
        """Without env vars, state home resolves to ~/.local/state/claude-autonomous."""
        env_backup_xdg = os.environ.pop("XDG_STATE_HOME", None)
        env_backup_state = os.environ.pop("CLAUDE_AUTONOMOUS_STATE_HOME", None)
        try:
            result = resolve_state_home(None)
            expected = Path.home() / ".local" / "state" / "claude-autonomous"
            self.assertEqual(result, expected)
        finally:
            if env_backup_xdg is not None:
                os.environ["XDG_STATE_HOME"] = env_backup_xdg
            if env_backup_state is not None:
                os.environ["CLAUDE_AUTONOMOUS_STATE_HOME"] = env_backup_state

    # -----------------------------------------------------------------------
    # 7. state_dir CLI arg takes precedence
    # -----------------------------------------------------------------------

    def test_state_dir_cli_precedence(self) -> None:
        """CLI arg beats both env var and XDG default."""
        cli_dir = self._tmpdir()
        old_env = os.environ.get("CLAUDE_AUTONOMOUS_STATE_HOME")
        old_xdg = os.environ.get("XDG_STATE_HOME")
        try:
            os.environ["CLAUDE_AUTONOMOUS_STATE_HOME"] = "/some/other/path"
            os.environ["XDG_STATE_HOME"] = "/yet/another/path"

            result = resolve_state_home(str(cli_dir))

            self.assertEqual(result, cli_dir.resolve())
        finally:
            if old_env is None:
                os.environ.pop("CLAUDE_AUTONOMOUS_STATE_HOME", None)
            else:
                os.environ["CLAUDE_AUTONOMOUS_STATE_HOME"] = old_env
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg

    # -----------------------------------------------------------------------
    # 8. CLAUDE_AUTONOMOUS_STATE_HOME env var resolution
    # -----------------------------------------------------------------------

    def test_env_var_state_resolution(self) -> None:
        """CLAUDE_AUTONOMOUS_STATE_HOME env var is used when set."""
        custom_dir = self._tmpdir()
        old_env = os.environ.get("CLAUDE_AUTONOMOUS_STATE_HOME")
        try:
            os.environ["CLAUDE_AUTONOMOUS_STATE_HOME"] = str(custom_dir)

            result = resolve_state_home(None)

            self.assertEqual(result, custom_dir.resolve())
        finally:
            if old_env is None:
                os.environ.pop("CLAUDE_AUTONOMOUS_STATE_HOME", None)
            else:
                os.environ["CLAUDE_AUTONOMOUS_STATE_HOME"] = old_env

    # -----------------------------------------------------------------------
    # 9. two simultaneous active runs
    # -----------------------------------------------------------------------

    def test_two_simultaneous_active_runs(self) -> None:
        """Two init calls for the same repo produce two distinct active runs."""
        repo = self.make_repo_with_config()
        state_home = self._tmpdir()

        repo_info = resolve_repository(repo)

        run_id_a = "run-a-00000001"
        run_id_b = "run-b-00000002"

        for run_id in (run_id_a, run_id_b):
            rdir = run_dir_path(state_home, repo_info.id, run_id)
            rdir.mkdir(parents=True, exist_ok=True)
            save_run_state(rdir, _minimal_state(run_id=run_id, status="active"))

        active = find_active_runs(state_home, repo_info.id)

        self.assertEqual(len(active), 2)
        ids = {r.run_id for r in active}
        self.assertIn(run_id_a, ids)
        self.assertIn(run_id_b, ids)

    # -----------------------------------------------------------------------
    # 10. ambiguous run selection raises StateError
    # -----------------------------------------------------------------------

    def test_ambiguous_run_selection(self) -> None:
        """With two active runs and no run_id, resolve_active_run raises StateError."""
        repo = self.make_repo_with_config()
        state_home = self._tmpdir()

        repo_info = resolve_repository(repo)

        run_id_a = "run-a-00000001"
        run_id_b = "run-b-00000002"

        for run_id in (run_id_a, run_id_b):
            rdir = run_dir_path(state_home, repo_info.id, run_id)
            rdir.mkdir(parents=True, exist_ok=True)
            save_run_state(rdir, _minimal_state(run_id=run_id, status="active"))

        with self.assertRaises(StateError) as ctx:
            resolve_active_run(state_home, repo_info.id, repo.resolve(), run_id=None)

        err_msg = str(ctx.exception)
        self.assertIn(run_id_a, err_msg)
        self.assertIn(run_id_b, err_msg)

    def test_inspection_resolves_newest_by_created_at_not_run_id(self) -> None:
        """With no active run, read-only inspection must select the run with the
        newest created_at, not the lexically-largest run_id (legacy/custom ids
        need not be chronological)."""
        repo = self.make_repo_with_config()
        state_home = self._tmpdir()
        repo_info = resolve_repository(repo)

        # The lexically-LARGER id ("zzz-old") is the OLDER run; the lexically-
        # smaller id ("aaa-new") is the NEWER run by created_at.
        older = _minimal_state(run_id="zzz-old", status="complete")
        older["created_at"] = "2026-01-01T00:00:00Z"
        newer = _minimal_state(run_id="aaa-new", status="complete")
        newer["created_at"] = "2026-06-01T00:00:00Z"
        for st in (older, newer):
            rdir = run_dir_path(state_home, repo_info.id, st["run_id"])
            rdir.mkdir(parents=True, exist_ok=True)
            save_run_state(rdir, st)

        ref = resolve_run_for_inspection(
            state_home, repo_info.id, repo.resolve(), run_id=None
        )
        self.assertEqual(ref.run_id, "aaa-new")

    # -----------------------------------------------------------------------
    # 11. relative artifact resolution after state move
    # -----------------------------------------------------------------------

    def test_relative_artifact_resolution_after_state_move(self) -> None:
        """Moving the run dir to a new location, resolve_artifact_path still resolves correctly."""
        state_home = self._tmpdir()
        run_id = "test-run-0001"
        repo_id = "fakerepo0000001"

        orig_run_dir = run_dir_path(state_home, repo_id, run_id)
        orig_run_dir.mkdir(parents=True, exist_ok=True)
        artifact_file = orig_run_dir / "output.txt"
        artifact_file.write_text("hello", encoding="utf-8")

        relative_ref = "output.txt"

        # Move run dir to a new location
        new_state_home = self._tmpdir()
        new_run_dir = run_dir_path(new_state_home, repo_id, run_id)
        shutil.copytree(str(orig_run_dir), str(new_run_dir))

        resolved = resolve_artifact_path(relative_ref, new_run_dir)

        self.assertEqual(resolved, (new_run_dir / "output.txt").resolve())
        self.assertTrue(resolved.exists())

    def test_artifact_path_rejects_absolute_outside_run_dir(self) -> None:
        """An absolute artifact pointer outside the run dir must be rejected so a
        crafted/legacy run-state cannot exfiltrate arbitrary local files."""
        state_home = self._tmpdir()
        run_dir = run_dir_path(state_home, "fakerepo0000002", "test-run-0002")
        run_dir.mkdir(parents=True, exist_ok=True)

        secret = self._tmpdir() / "secret.txt"
        secret.write_text("top secret", encoding="utf-8")

        with self.assertRaises(StateError):
            resolve_artifact_path(str(secret), run_dir)
        with self.assertRaises(StateError):
            resolve_artifact_path("../../../../etc/passwd", run_dir)

        # An absolute path that genuinely lives inside the run dir still resolves.
        inside = run_dir / "output.txt"
        inside.write_text("ok", encoding="utf-8")
        self.assertEqual(
            resolve_artifact_path(str(inside), run_dir), inside.resolve()
        )

    # -----------------------------------------------------------------------
    # 12. branch drift detection
    # -----------------------------------------------------------------------

    def test_branch_drift_detection(self) -> None:
        """Switching branches produces DriftKind.UNSAFE."""
        repo = self.make_repo_with_config()
        repo_info = resolve_repository(repo)

        # Record baseline on 'main' branch
        state = _minimal_state()
        state["repository"] = {"id": repo_info.id}
        state["baseline"] = {
            "branch": repo_info.branch or "main",
            "commit": repo_info.head_commit,
            "worktree_path": str(repo_info.worktree_path),
        }

        # Create and switch to a new branch
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "feature-branch"], check=True
        )
        new_repo_info = resolve_repository(repo)

        result = detect_drift(state, new_repo_info)

        self.assertEqual(result.kind, DriftKind.UNSAFE)

    # -----------------------------------------------------------------------
    # 13. HEAD drift is EXPECTED
    # -----------------------------------------------------------------------

    def test_head_drift_expected(self) -> None:
        """Advancing HEAD on the same branch produces DriftKind.EXPECTED."""
        repo = self.make_repo_with_config()
        repo_info = resolve_repository(repo)

        # Build state with current HEAD as baseline
        original_commit = repo_info.head_commit
        state = _minimal_state()
        state["repository"] = {"id": repo_info.id}
        state["baseline"] = {
            "branch": repo_info.branch,
            "commit": original_commit,
            "worktree_path": str(repo_info.worktree_path),
        }

        # Make a new commit
        (repo / "file2.txt").write_text("new content\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "file2.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "second commit"], check=True
        )

        new_repo_info = resolve_repository(repo)

        result = detect_drift(state, new_repo_info)

        self.assertEqual(result.kind, DriftKind.EXPECTED)

    # -----------------------------------------------------------------------
    # 14. accept-drift clears drift
    # -----------------------------------------------------------------------

    def test_accept_drift_clears_drift(self) -> None:
        """After updating baseline to current branch/commit, detect_drift returns NONE."""
        repo = self.make_repo_with_config()
        repo_info = resolve_repository(repo)

        # State that references an old branch (simulating drift)
        state = _minimal_state()
        state["repository"] = {"id": repo_info.id}
        state["baseline"] = {
            "branch": "old-branch",
            "commit": repo_info.head_commit,
            "worktree_path": str(repo_info.worktree_path),
        }

        # Accept drift: update baseline to current state
        state["baseline"]["branch"] = repo_info.branch
        state["baseline"]["commit"] = repo_info.head_commit
        state["baseline"]["worktree_path"] = str(repo_info.worktree_path)
        state["repository"]["id"] = repo_info.id

        result = detect_drift(state, repo_info)

        self.assertEqual(result.kind, DriftKind.NONE)

    # -----------------------------------------------------------------------
    # 15. corrupt JSON state raises StateError
    # -----------------------------------------------------------------------

    def test_corrupt_json_state(self) -> None:
        """Garbage JSON in run-state.json causes load_run_state to raise StateError."""
        run_dir = self._tmpdir()
        state_file = run_dir / "run-state.json"
        state_file.write_text("{not valid json!!!", encoding="utf-8")

        with self.assertRaises(StateError):
            load_run_state(run_dir, required=True)

    # -----------------------------------------------------------------------
    # 16. unsupported schema version raises StateError
    # -----------------------------------------------------------------------

    def test_unsupported_schema_version(self) -> None:
        """schema_version=99 causes validate_state to raise StateError."""
        bad_state = {
            "schema_version": 99,
            "run_id": "run-001",
            "status": "active",
        }

        with self.assertRaises(StateError) as ctx:
            validate_state(bad_state)

        self.assertIn("99", str(ctx.exception))

    # -----------------------------------------------------------------------
    # 17. atomic state update — temp file does not persist
    # -----------------------------------------------------------------------

    def test_atomic_state_update(self) -> None:
        """After atomic_write_json succeeds, no .tmp file is left behind."""
        run_dir = self._tmpdir()
        target = run_dir / "run-state.json"

        atomic_write_json(target, {"key": "value"})

        # No .tmp file should remain
        tmp_file = Path(str(target) + ".tmp")
        self.assertFalse(
            tmp_file.exists(), "Temp file was not cleaned up after atomic write"
        )
        self.assertTrue(target.exists())
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(loaded["key"], "value")

    # -----------------------------------------------------------------------
    # 18. concurrent state mutation — no lost updates
    # -----------------------------------------------------------------------

    def test_concurrent_state_mutation(self) -> None:
        """Two threads incrementing stop_gate_blocks concurrently produce correct final value."""
        run_dir = self._tmpdir()
        run_dir.mkdir(parents=True, exist_ok=True)

        initial = _minimal_state()
        initial["stop_gate_blocks"] = 0
        save_run_state(run_dir, initial)

        errors: list[Exception] = []
        NUM_INCREMENTS = 5
        NUM_THREADS = 2

        def increment_blocks() -> None:
            try:
                for _ in range(NUM_INCREMENTS):
                    with RunStateLock(run_dir):
                        s = load_run_state(run_dir, required=True)
                        s["stop_gate_blocks"] = int(s.get("stop_gate_blocks", 0)) + 1
                        save_run_state(run_dir, s)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=increment_blocks) for _ in range(NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")

        final_state = load_run_state(run_dir, required=True)
        self.assertEqual(
            final_state["stop_gate_blocks"],
            NUM_THREADS * NUM_INCREMENTS,
            "Lost updates detected in concurrent mutation",
        )

    def test_portable_lock_timeout_guidance_is_recoverable(self) -> None:
        """The portable (O_EXCL marker file) backend may strand a lock if a
        holder crashes, so its timeout message must attribute the recorded owner
        and tell the operator to remove the file once that process is gone."""
        lock_dir = self._tmpdir()
        lock_path = lock_dir / ".x.lock"
        original = CrossProcessLock.force_backend
        CrossProcessLock.force_backend = "portable"
        held = CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01)
        try:
            held.__enter__()
            with self.assertRaises(LockTimeout) as ctx:
                with CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01):
                    pass
            msg = str(ctx.exception)
            self.assertIn("portable lock-file backend", msg)
            # The live holder is this process; its pid must be attributed.
            self.assertIn(f"pid={os.getpid()}", msg)
            self.assertIn("remove the lock file", msg)
        finally:
            held.__exit__()
            CrossProcessLock.force_backend = original

    @unittest.skipUnless(
        state_module._fcntl is not None, "fcntl backend not available on this platform"
    )
    def test_fcntl_lock_timeout_guidance_forbids_deletion(self) -> None:
        """The fcntl OS backend holds the lock on the open file in the kernel and
        releases it automatically on exit/crash. Deleting the path does not
        release it and is unsafe, so the timeout message must never recommend
        removing the lock file."""
        lock_dir = self._tmpdir()
        lock_path = lock_dir / ".x.lock"
        original = CrossProcessLock.force_backend
        CrossProcessLock.force_backend = "fcntl"
        held = CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01)
        try:
            held.__enter__()
            with self.assertRaises(LockTimeout) as ctx:
                with CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01):
                    pass
            msg = str(ctx.exception)
            self.assertIn("'fcntl' OS lock backend", msg)
            self.assertIn("Do NOT delete the lock file", msg)
            self.assertNotIn("remove the lock file", msg)
        finally:
            held.__exit__()
            CrossProcessLock.force_backend = original

    def test_portable_release_does_not_delete_a_replacement_lock(self) -> None:
        """If a portable holder's lock file is removed and another holder creates
        a fresh lock at the same path, the original holder's release must not
        unlink the replacement (a different inode it does not own). Otherwise a
        third process could enter while the replacement holder believes it still
        owns the lock."""
        lock_dir = self._tmpdir()
        lock_path = lock_dir / ".x.lock"
        original = CrossProcessLock.force_backend
        CrossProcessLock.force_backend = "portable"
        held = CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01)
        other = CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01)
        try:
            held.__enter__()
            # The original lock file is removed out-of-band, then a different
            # holder acquires a brand-new lock at the same path.
            lock_path.unlink()
            other.__enter__()
            replacement_token = json.loads(
                lock_path.read_text(encoding="utf-8")
            )["token"]
            self.assertNotEqual(replacement_token, held._owner_token)

            # Releasing the stale original must leave the replacement intact.
            held.__exit__()
            self.assertTrue(lock_path.exists())
            self.assertEqual(
                json.loads(lock_path.read_text(encoding="utf-8"))["token"],
                replacement_token,
            )
        finally:
            other.__exit__()
            CrossProcessLock.force_backend = original

    def test_portable_release_does_not_delete_in_place_token_rewrite(self) -> None:
        """If the lock file keeps its inode but its owner token is rewritten in
        place by another holder, release must still refuse to unlink it: the
        recorded owner no longer matches this instance."""
        lock_dir = self._tmpdir()
        lock_path = lock_dir / ".x.lock"
        original = CrossProcessLock.force_backend
        CrossProcessLock.force_backend = "portable"
        held = CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01)
        try:
            held.__enter__()
            # Same file (same inode), but the owner metadata is replaced.
            lock_path.write_text(
                json.dumps({"token": "someone-elses-token"}), encoding="utf-8"
            )
            held.__exit__()
            self.assertTrue(lock_path.exists())
            self.assertEqual(
                json.loads(lock_path.read_text(encoding="utf-8"))["token"],
                "someone-elses-token",
            )
        finally:
            if lock_path.exists():
                lock_path.unlink()
            CrossProcessLock.force_backend = original

    def test_portable_release_unlinks_own_unmodified_lock(self) -> None:
        """The normal path: a portable holder that still owns its untouched lock
        file removes it on release so the next acquirer can proceed."""
        lock_dir = self._tmpdir()
        lock_path = lock_dir / ".x.lock"
        original = CrossProcessLock.force_backend
        CrossProcessLock.force_backend = "portable"
        try:
            with CrossProcessLock(lock_path, timeout=0.05, poll_interval=0.01):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())
        finally:
            CrossProcessLock.force_backend = original

    # -----------------------------------------------------------------------
    # 19. legacy state detection
    # -----------------------------------------------------------------------

    def test_legacy_state_detection(self) -> None:
        """detect_legacy_state returns the legacy dir when run-state.json exists there."""
        repo = self.make_repo_with_config()
        legacy_dir = repo / LEGACY_STATE_REL
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / LEGACY_STATE_FILE_NAME).write_text(
            json.dumps({"run_id": "legacy-run", "status": "active", "version": 1}),
            encoding="utf-8",
        )

        result = detect_legacy_state(repo)

        self.assertIsNotNone(result)
        self.assertEqual(result, legacy_dir)

    # -----------------------------------------------------------------------
    # 20. non-destructive migration
    # -----------------------------------------------------------------------

    def test_non_destructive_migration(self) -> None:
        """migrate_v1_to_v2 leaves original legacy files untouched."""
        repo = self.make_repo_with_config()
        repo_info = resolve_repository(repo)

        legacy_dir = repo / LEGACY_STATE_REL
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_state = {
            "version": 1,
            "run_id": "legacy-migration-run",
            "status": "active",
            "feature": "old feature",
            "baseline": {"commit": "abc123", "branch": "main"},
            "verification": {"checks": [], "passed": False},
        }
        original_content = json.dumps(legacy_state)
        (legacy_dir / LEGACY_STATE_FILE_NAME).write_text(
            original_content, encoding="utf-8"
        )

        state_home = self._tmpdir()
        run_id = legacy_state["run_id"]
        new_run_dir = run_dir_path(state_home, repo_info.id, run_id)
        new_run_dir.mkdir(parents=True, exist_ok=True)

        migrated = state_module.migrate_v1_to_v2(legacy_state, new_run_dir, repo_info)
        save_run_state(new_run_dir, migrated)

        # Original file must be unchanged
        actual_original = (legacy_dir / LEGACY_STATE_FILE_NAME).read_text(
            encoding="utf-8"
        )
        self.assertEqual(actual_original, original_content)

        # New run state exists and has v2 format
        new_state = load_run_state(new_run_dir, required=True)
        self.assertEqual(new_state.get("schema_version"), 2)
        self.assertEqual(new_state.get("run_id"), run_id)

    # -----------------------------------------------------------------------
    # 21. idempotent migration
    # -----------------------------------------------------------------------

    def test_idempotent_migration(self) -> None:
        """Running migrate twice: second call recognises already-migrated state."""
        repo = self.make_repo_with_config()
        repo_info = resolve_repository(repo)

        state_home = self._tmpdir()
        run_id = "20240101T000000Z-aabbccdd"

        legacy_state = {
            "version": 1,
            "run_id": run_id,
            "status": "active",
            "feature": "idempotent test",
            "baseline": {"commit": "abc123", "branch": "main"},
            "verification": {"checks": [], "passed": False},
        }

        run_dir = run_dir_path(state_home, repo_info.id, run_id)

        # First migration
        run_dir.mkdir(parents=True, exist_ok=True)
        migrated = state_module.migrate_v1_to_v2(legacy_state, run_dir, repo_info)
        migrated["migrated_from"] = "v1"
        save_run_state(run_dir, migrated)

        first_mtime = (run_dir / "run-state.json").stat().st_mtime

        # Second migration attempt: detect already-migrated condition
        existing = load_run_state(run_dir, required=True)
        already_migrated = (
            existing.get("run_id") == run_id
            and existing.get("migrated_from") is not None
        )
        self.assertTrue(
            already_migrated, "Second migration should detect already-migrated state"
        )

        # State file should not change if idempotency is respected
        second_mtime = (run_dir / "run-state.json").stat().st_mtime
        self.assertEqual(
            first_mtime, second_mtime, "State file was rewritten on second migration"
        )

    # -----------------------------------------------------------------------
    # 22. stop_gate.py finds run state from nested cwd
    # -----------------------------------------------------------------------

    def test_stop_gate_resolution_from_nested_dir(self) -> None:
        """stop_gate.py invoked with cwd=subdir payload finds and processes the run."""
        repo = self.make_repo_with_config()
        subdir = repo / "src" / "lib"
        subdir.mkdir(parents=True)

        state_home = self._tmpdir()
        repo_info = resolve_repository(repo)

        run_id = new_run_id()
        run_dir = run_dir_path(state_home, repo_info.id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        save_run_state(run_dir, _pinned_feature_state(repo_info, run_id))

        payload = json.dumps({"cwd": str(subdir), "hook_event_name": "Stop"})
        env = {**os.environ, "CLAUDE_AUTONOMOUS_STATE_HOME": str(state_home)}

        result = subprocess.run(
            ["python3", str(STOP_GATE)],
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0)
        # Should block (decision=block) since no artifacts are complete
        self.assertIn('"decision": "block"', result.stdout)

    # -----------------------------------------------------------------------
    # 23. stop_gate with multiple active runs returns 0 without blocking
    # -----------------------------------------------------------------------

    def test_stop_gate_multiple_active_runs(self) -> None:
        """stop_gate returns 0 and no block JSON when multiple active runs exist (ambiguous)."""
        repo = self.make_repo_with_config()
        state_home = self._tmpdir()
        repo_info = resolve_repository(repo)

        for i in range(2):
            run_id = f"run-{i:08d}-000000"
            run_dir = run_dir_path(state_home, repo_info.id, run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            save_run_state(run_dir, _pinned_feature_state(repo_info, run_id))

        payload = json.dumps({"cwd": str(repo), "hook_event_name": "Stop"})
        env = {**os.environ, "CLAUDE_AUTONOMOUS_STATE_HOME": str(state_home)}

        result = subprocess.run(
            ["python3", str(STOP_GATE)],
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0)
        # Should NOT block when ambiguous
        self.assertNotIn('"decision": "block"', result.stdout)
        self.assertIn("multiple active feature runs", result.stderr)

    # -----------------------------------------------------------------------
    # 24. read-only plugin installation
    # -----------------------------------------------------------------------

    @unittest.skipIf(sys.platform == "win32", "not applicable on Windows")
    def test_read_only_plugin_installation(self) -> None:
        """Controller works with read-only prompts dir when state is written externally."""
        repo = self.make_repo_with_config()
        state_home = self._tmpdir()
        repo_info = resolve_repository(repo)

        # Create a run via direct state writing (simulating controller init)
        run_id = new_run_id()
        run_dir = run_dir_path(state_home, repo_info.id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        s = _minimal_state(run_id=run_id, status="active")
        s["repository"] = {
            "id": repo_info.id,
            "canonical_root": str(repo_info.canonical_root),
            "worktree_path": str(repo_info.worktree_path),
            "display_name": repo_info.display_name,
            "remote_display": repo_info.remote_display,
        }
        s["baseline"] = {
            "commit": repo_info.head_commit,
            "branch": repo_info.branch,
            "worktree_path": str(repo_info.worktree_path),
        }
        save_run_state(run_dir, s)

        # Make the run_dir read-only to simulate a read-only plugin dir
        # (state_home itself is writable, so we create a separate plugin-like dir)
        plugin_dir = self._tmpdir()
        os.chmod(str(plugin_dir), 0o555)

        try:
            # Verify state is still accessible from state_home even if plugin_dir is read-only
            loaded = load_run_state(run_dir, required=True)
            self.assertEqual(loaded["run_id"], run_id)
            self.assertEqual(loaded["status"], "active")

            # Verify find_active_runs works
            active = find_active_runs(state_home, repo_info.id)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].run_id, run_id)
        finally:
            os.chmod(str(plugin_dir), 0o755)

    # -----------------------------------------------------------------------
    # 25. paths with spaces and non-ASCII characters
    # -----------------------------------------------------------------------

    def test_paths_with_spaces_and_nonascii(self) -> None:
        """All operations work when repo path contains spaces and non-ASCII (é)."""
        base = Path(tempfile.mkdtemp())
        self._tmpdirs.append(base)

        repo_path = base / "my project é"
        repo_path.mkdir(parents=True, exist_ok=True)

        repo = _make_repo_with_config(repo_path)
        state_home = self._tmpdir()

        repo_info = resolve_repository(repo)
        self.assertIsNotNone(repo_info)
        self.assertIn("é", str(repo_info.canonical_root))

        # Create a run in the state store
        run_id = new_run_id()
        run_dir = run_dir_path(state_home, repo_info.id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        s = _minimal_state(run_id=run_id, status="active")
        s["repository"] = {"id": repo_info.id}
        s["baseline"] = {
            "commit": repo_info.head_commit,
            "branch": repo_info.branch,
            "worktree_path": str(repo_info.worktree_path),
        }
        save_run_state(run_dir, s)

        # Load and verify
        loaded = load_run_state(run_dir, required=True)
        self.assertEqual(loaded["run_id"], run_id)

        # Artifact paths with spaces
        artifact_file = run_dir / "output file é.txt"
        artifact_file.write_text("content", encoding="utf-8")
        resolved = resolve_artifact_path("output file é.txt", run_dir)
        self.assertEqual(resolved, artifact_file.resolve())


class RunIdValidationTests(unittest.TestCase):
    """P0: run-ID validation and path containment."""

    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    def _tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self._tmpdirs.append(d)
        return d

    def test_valid_generated_run_id_passes(self) -> None:
        rid = new_run_id()
        self.assertEqual(validate_run_id(rid), rid)

    def test_valid_custom_run_ids_pass(self) -> None:
        for rid in ("legacy", "my-feature-run", "run_01.2", "A", "x" * 80):
            with self.subTest(rid=rid):
                self.assertEqual(validate_run_id(rid), rid)

    def test_rejects_traversal_and_separators(self) -> None:
        bad = [
            "../../escape",
            "/absolute/path",
            "C:\\escape",
            "a/b",
            "a\\b",
            ".",
            "..",
            "",
            "-leading-hyphen",
            ".hidden",
            "x" * 81,
            "with space",
            "tab\there",
            "null\x00byte",
            "newline\nhere",
        ]
        for rid in bad:
            with self.subTest(rid=rid):
                with self.assertRaises(StateError):
                    validate_run_id(rid)

    def test_non_string_run_id_rejected(self) -> None:
        for rid in (123, None, ["a"], {"a": 1}):
            with self.subTest(rid=rid):
                with self.assertRaises(StateError):
                    validate_run_id(rid)  # type: ignore[arg-type]

    def test_run_dir_path_rejects_traversal(self) -> None:
        state_home = self._tmpdir()
        for rid in ("../../escape", "/etc/passwd", "a/b", ".."):
            with self.subTest(rid=rid):
                with self.assertRaises(StateError):
                    run_dir_path(state_home, "repoid0000000001", rid)

    def test_run_dir_path_confines_valid_id(self) -> None:
        state_home = self._tmpdir()
        run_dir = run_dir_path(state_home, "repoid0000000001", "valid-run-01")
        runs_base = (
            state_home / "repositories" / "repoid0000000001" / "runs"
        ).resolve()
        self.assertEqual(run_dir.parent, runs_base)

    def test_run_dir_path_symlink_escape_rejected(self) -> None:
        state_home = self._tmpdir()
        runs_base = state_home / "repositories" / "repoid0000000001" / "runs"
        runs_base.mkdir(parents=True, exist_ok=True)
        outside = self._tmpdir() / "outside-target"
        outside.mkdir(parents=True, exist_ok=True)
        link = runs_base / "evil"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unsupported on this platform")
        with self.assertRaises(StateError):
            run_dir_path(state_home, "repoid0000000001", "evil")

    def test_resolve_active_run_validates_run_id(self) -> None:
        state_home = self._tmpdir()
        repo_root = self._tmpdir()
        with self.assertRaises(StateError):
            resolve_active_run(
                state_home, "repoid0000000001", repo_root, run_id="../../escape"
            )

    def test_validate_state_rejects_bad_run_id(self) -> None:
        bad_state = {
            "schema_version": 2,
            "status": "active",
            "run_id": "../../escape",
        }
        with self.assertRaises(StateError):
            validate_state(bad_state)


if __name__ == "__main__":
    unittest.main()
