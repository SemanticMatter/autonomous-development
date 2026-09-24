"""Workflow-kind, checkout-guard, reuse, accept-drift, and Stop-hook isolation.

Regression tests for the shared-workflow isolation and drift guards: every
probe uses throwaway repositories, linked worktrees, and state homes, and never
a real repository's default branch.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/controller.py"
STOP_GATE = ROOT / "scripts/stop_gate.py"

sys.path.insert(0, str(ROOT / "scripts"))
import controller  # noqa: E402
from state import (  # noqa: E402
    WORKFLOW_KIND_EXISTING_PR_REVIEW,
    WORKFLOW_KIND_FEATURE,
    WORKFLOW_KIND_UNKNOWN,
    feature_origin_worktree,
    feature_worktree_mode,
    find_all_runs,
    recorded_allow_main,
    resolve_repository,
    run_workflow_kind,
)


class _RepoFixture(unittest.TestCase):
    """Throwaway repositories, worktrees, state homes, and command helpers."""

    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            shutil.rmtree(str(d), ignore_errors=True)

    def tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp()).resolve()
        self._tmpdirs.append(d)
        return d

    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def make_repo(self, *, initial_branch: str = "main", feature: str | None = "feature") -> Path:
        """A repository with one commit on `initial_branch`, then `feature` checked out."""
        repo = self.tmpdir() / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q", ".")
        self.git(repo, "checkout", "-q", "-B", initial_branch)
        self.git(repo, "config", "user.email", "t@example.com")
        self.git(repo, "config", "user.name", "T")
        (repo / "README.md").write_text("# base\n", encoding="utf-8")
        self.git(repo, "add", "README.md")
        self.git(repo, "commit", "-qm", "base")
        if feature:
            self.git(repo, "checkout", "-q", "-b", feature)
        return repo

    def add_worktree(self, repo: Path, branch: str, *, new: bool = True) -> Path:
        path = self.tmpdir() / f"wt-{branch}"
        if new:
            self.git(repo, "worktree", "add", "-q", "-b", branch, str(path))
        else:
            self.git(repo, "worktree", "add", "-q", str(path), branch)
        return path.resolve()

    def commit_file(self, repo: Path, name: str) -> None:
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
        self.git(repo, "add", name)
        self.git(repo, "commit", "-qm", f"add {name}")

    def ctl(self, cwd: Path, state_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONTROLLER), "--project-root", str(cwd),
             "--state-dir", str(state_home), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def gate(self, cwd: Path, state_home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(STOP_GATE)],
            input=json.dumps({"cwd": str(cwd), "hook_event_name": "Stop"}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "CLAUDE_AUTONOMOUS_STATE_HOME": str(state_home)},
        )

    def init(self, cwd: Path, state_home: Path, *extra: str) -> Path:
        """Run `init` and return the created run-state.json path."""
        result = self.ctl(cwd, state_home, "init", "--feature", "Feature", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip().splitlines()[-1])

    def import_pr(self, repo: Path, state_home: Path) -> Path:
        """Import the checked-out `feature` branch against `main` as a review run."""
        result = self.ctl(
            repo, state_home, "import-pr", "--target-ref", "feature", "--base-ref", "main"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        repo_info = resolve_repository(repo)
        for ref in find_all_runs(state_home, repo_info.id):
            if ref.state.get("workflow_kind") == WORKFLOW_KIND_EXISTING_PR_REVIEW:
                return ref.run_dir / "run-state.json"
        raise AssertionError("imported run not found")

    def pr_repo(self) -> Path:
        repo = self.make_repo()
        self.commit_file(repo, "change.py")
        return repo

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def dump(path: Path, state: dict) -> None:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def edit(self, path: Path, **changes: object) -> None:
        state = self.load(path)
        for dotted, value in changes.items():
            node = state
            parts = dotted.split("__")
            for part in parts[:-1]:
                node = node[part]
            if value is _DELETE:
                node.pop(parts[-1], None)
            else:
                node[parts[-1]] = value
        self.dump(path, state)

    def make_pre_pin(self, path: Path) -> None:
        """Strip the fields `init` now records, reproducing a run created before them."""
        self.edit(
            path,
            workflow_kind=_DELETE,
            feature_authorization=_DELETE,
            baseline__worktree_path=_DELETE,
        )

    def run_count(self, repo: Path, state_home: Path) -> int:
        return len(find_all_runs(state_home, resolve_repository(repo).id))


_DELETE = object()


class WorkflowKindTests(unittest.TestCase):
    """`run_workflow_kind` is explicit, compatible, and fails closed."""

    def test_classification(self) -> None:
        cases = [
            ("pre-kind feature run", {"status": "active"}, WORKFLOW_KIND_FEATURE),
            ("explicit feature", {"workflow_kind": "feature"}, WORKFLOW_KIND_FEATURE),
            (
                "incidental fields do not change the kind",
                {"repository": {"worktree_mode": "current"}, "reviews": [{}]},
                WORKFLOW_KIND_FEATURE,
            ),
            (
                "existing_pr_review",
                {"workflow_kind": "existing_pr_review", "review_target": {}},
                WORKFLOW_KIND_EXISTING_PR_REVIEW,
            ),
            ("future kind", {"workflow_kind": "pr_iteration"}, WORKFLOW_KIND_UNKNOWN),
            ("non-string kind", {"workflow_kind": 123}, WORKFLOW_KIND_UNKNOWN),
            ("null kind", {"workflow_kind": None}, WORKFLOW_KIND_UNKNOWN),
            ("empty kind", {"workflow_kind": ""}, WORKFLOW_KIND_UNKNOWN),
            ("list kind", {"workflow_kind": ["feature"]}, WORKFLOW_KIND_UNKNOWN),
            ("review_target without kind", {"review_target": {}}, WORKFLOW_KIND_UNKNOWN),
            (
                "feature contradicted by review_target",
                {"workflow_kind": "feature", "review_target": {}},
                WORKFLOW_KIND_UNKNOWN,
            ),
        ]
        for label, state, expected in cases:
            with self.subTest(label):
                self.assertEqual(run_workflow_kind(state), expected)
        self.assertEqual(run_workflow_kind(["not", "a", "dict"]), WORKFLOW_KIND_UNKNOWN)

    def test_review_target_alone_keeps_imported_guards_but_is_not_a_feature(self) -> None:
        state = {"review_target": {"target_head": "abc"}}
        self.assertTrue(controller.is_imported_run(state))
        self.assertEqual(run_workflow_kind(state), WORKFLOW_KIND_UNKNOWN)

    def test_feature_worktree_mode(self) -> None:
        self.assertEqual(feature_worktree_mode({}), "isolated")
        self.assertEqual(feature_worktree_mode({"repository": {}}), "isolated")
        self.assertEqual(
            feature_worktree_mode({"repository": {"worktree_mode": "current"}}), "current"
        )
        for bad in ("weird", 123, None, ""):
            with self.subTest(mode=bad):
                self.assertIsNone(feature_worktree_mode({"repository": {"worktree_mode": bad}}))
        self.assertIsNone(feature_worktree_mode({"repository": "not-a-dict"}))

    def test_feature_origin_worktree(self) -> None:
        pinned = {"baseline": {"worktree_path": "/work/a"}, "repository": {"worktree_path": "/work/b"}}
        self.assertEqual(feature_origin_worktree(pinned), "/work/a")
        pre_pin = {"baseline": {"branch": "x"}, "repository": {"worktree_path": "/work/b"}}
        self.assertEqual(feature_origin_worktree(pre_pin), "/work/b")
        for bad in ("", 5, None, "relative/path"):
            with self.subTest(pin=bad):
                state = {"baseline": {"worktree_path": bad}, "repository": {"worktree_path": "/work/b"}}
                self.assertIsNone(feature_origin_worktree(state))
        self.assertIsNone(feature_origin_worktree({"baseline": {}, "repository": {}}))
        self.assertIsNone(feature_origin_worktree({"baseline": [], "repository": {"worktree_path": "/w"}}))
        self.assertIsNone(feature_origin_worktree({}))

    def test_recorded_allow_main_is_never_inferred(self) -> None:
        self.assertIs(recorded_allow_main({"feature_authorization": {"allow_main": True}}), True)
        self.assertIs(recorded_allow_main({"feature_authorization": {"allow_main": False}}), False)
        for bad in ({}, {"feature_authorization": {}}, {"feature_authorization": {"allow_main": "yes"}},
                    {"feature_authorization": {"allow_main": 1}}, {"feature_authorization": True}):
            with self.subTest(state=bad):
                self.assertIsNone(recorded_allow_main(bad))


class CheckoutGuardTests(_RepoFixture):
    """`require_attached_clean_checkout` fails closed and holds no branch policy."""

    def guard(self, repo: Path):
        return controller.require_attached_clean_checkout(
            resolve_repository(repo), context="Test guard"
        )

    def refused(self, repo: Path, fragment: str) -> None:
        with self.assertRaises(controller.WorkflowError) as ctx:
            self.guard(repo)
        self.assertIn(fragment, str(ctx.exception))

    def fake_git(self, *, match: tuple[str, ...], result: str | None = None):
        """Patch `_git_ro` so the call starting with `match` fails (result None) or returns `result`."""
        real = controller._git_ro

        def fake(root, *args, check=False):
            if tuple(args[: len(match)]) == match:
                if result is None:
                    raise controller.WorkflowError(f"simulated failure of git {' '.join(args)}")
                return result
            return real(root, *args, check=check)

        return mock.patch.object(controller, "_git_ro", side_effect=fake)

    def test_clean_attached_branch_returns_verified_identity(self) -> None:
        repo = self.make_repo()
        identity = self.guard(repo)
        self.assertEqual(identity.branch, "feature")
        self.assertEqual(identity.worktree_path, repo.resolve())
        self.assertEqual(identity.head_commit, self.git(repo, "rev-parse", "HEAD"))

    def test_guard_has_no_branch_policy_or_bypass(self) -> None:
        repo = self.make_repo(feature=None)
        self.assertEqual(self.guard(repo).branch, "main")
        params = set(inspect.signature(controller.require_attached_clean_checkout).parameters)
        self.assertEqual(params, {"repo", "context"})

    def test_dirty_tracked_staged_and_untracked_are_refused(self) -> None:
        for label in ("modified", "staged", "untracked", "deleted"):
            with self.subTest(label):
                repo = self.make_repo()
                if label == "modified":
                    (repo / "README.md").write_text("changed\n", encoding="utf-8")
                elif label == "staged":
                    (repo / "new.txt").write_text("x\n", encoding="utf-8")
                    self.git(repo, "add", "new.txt")
                elif label == "untracked":
                    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
                else:
                    (repo / "README.md").unlink()
                self.refused(repo, "requires a clean working tree")

    def test_ignored_files_do_not_make_the_checkout_unclean(self) -> None:
        repo = self.make_repo()
        (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        self.git(repo, "add", ".gitignore")
        self.git(repo, "commit", "-qm", "ignore logs")
        (repo / "debug.log").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.guard(repo).branch, "feature")

    def test_detached_head_is_refused(self) -> None:
        repo = self.make_repo()
        self.git(repo, "checkout", "-q", "--detach", "HEAD")
        self.refused(repo, "does not support detached HEAD")

    def test_empty_branch_result_is_refused(self) -> None:
        repo = self.make_repo()
        with self.fake_git(match=("branch", "--show-current"), result=""):
            self.refused(repo, "does not support detached HEAD")

    def test_git_failures_are_refused(self) -> None:
        repo = self.make_repo()
        for match in (
            ("rev-parse", "--show-toplevel"),
            ("rev-parse", "--verify", "HEAD"),
            ("branch", "--show-current"),
            ("status", "--porcelain"),
        ):
            with self.subTest(command=" ".join(match)), self.fake_git(match=match):
                self.refused(repo, "could not inspect the checkout")

    def test_real_status_failure_is_refused(self) -> None:
        repo = self.make_repo()
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        (repo / ".git" / "index").write_bytes(b"corrupt index")
        self.refused(repo, "could not inspect the checkout")

    def test_unexpected_output_is_refused(self) -> None:
        repo = self.make_repo()
        other = self.tmpdir()
        cases = [
            (("rev-parse", "--show-toplevel"), str(other), "could not verify the worktree"),
            (("rev-parse", "--show-toplevel"), "", "could not verify the worktree"),
            (("rev-parse", "--verify", "HEAD"), "0" * 40, "could not verify HEAD"),
            (("branch", "--show-current"), "other-branch", "could not verify the current branch"),
            (("branch", "--show-current"), "feature\nmain", "could not verify the current branch"),
        ]
        for match, output, fragment in cases:
            with self.subTest(command=" ".join(match), output=output), self.fake_git(
                match=match, result=output
            ):
                self.refused(repo, fragment)

    def test_feature_branch_policy_takes_explicit_authorization(self) -> None:
        for branch in ("main", "master"):
            with self.subTest(branch=branch):
                with self.assertRaises(controller.WorkflowError) as ctx:
                    controller.require_feature_branch_allowed(
                        branch, allow_main=False, operation="test on", recovery="Recover."
                    )
                self.assertIn(repr(branch), str(ctx.exception))
                controller.require_feature_branch_allowed(
                    branch, allow_main=True, operation="test on", recovery="Recover."
                )
        # The policy is the literal main/master set; the default branch is not resolved.
        for branch in ("feature", "trunk"):
            controller.require_feature_branch_allowed(
                branch, allow_main=False, operation="test on", recovery="Recover."
            )


class InitPersistenceTests(_RepoFixture):
    """`init` records workflow kind, worktree pin, and feature-only authorization."""

    def test_isolated_init_records_kind_mode_pin_and_no_authorization(self) -> None:
        repo = self.make_repo()
        state = self.load(self.init(repo, self.tmpdir()))
        self.assertEqual(state["workflow_kind"], "feature")
        self.assertNotIn("review_target", state)
        self.assertEqual(run_workflow_kind(state), WORKFLOW_KIND_FEATURE)
        self.assertEqual(state["repository"]["worktree_mode"], "isolated")
        self.assertEqual(state["baseline"]["worktree_path"], str(repo.resolve()))
        self.assertEqual(state["repository"]["worktree_path"], str(repo.resolve()))
        self.assertEqual(state["feature_authorization"], {"allow_main": False})

    def test_isolated_init_in_linked_worktree_pins_that_worktree(self) -> None:
        repo = self.make_repo()
        worktree = self.add_worktree(repo, "worktree-x")
        state_home = self.tmpdir()
        state = self.load(self.init(worktree, state_home))
        self.assertEqual(state["baseline"]["worktree_path"], str(worktree))
        self.assertEqual(state["repository"]["id"], resolve_repository(repo).id)
        # Commands from the same worktree (and a subdirectory of it) see no drift.
        (worktree / "sub").mkdir()
        for cwd in (worktree, worktree / "sub"):
            check = self.ctl(cwd, state_home, "run-check", "--name", "t", "--", sys.executable, "-c", "0")
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_current_init_records_exact_worktree_and_allow_main(self) -> None:
        repo = self.make_repo()
        state = self.load(self.init(repo, self.tmpdir(), "--worktree-mode", "current"))
        self.assertEqual(state["repository"]["worktree_mode"], "current")
        self.assertEqual(state["baseline"]["worktree_path"], str(repo.resolve()))
        self.assertEqual(state["feature_authorization"], {"allow_main": False})

        main_repo = self.make_repo(feature=None)
        state = self.load(
            self.init(main_repo, self.tmpdir(), "--worktree-mode", "current", "--allow-main")
        )
        self.assertEqual(state["feature_authorization"], {"allow_main": True})
        self.assertEqual(state["baseline"]["branch"], "main")

    def test_allow_main_stays_invalid_outside_current_feature_mode(self) -> None:
        repo = self.make_repo(feature=None)
        state_home = self.tmpdir()
        result = self.ctl(repo, state_home, "init", "--feature", "F", "--allow-main")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.run_count(repo, state_home), 0)
        pr = self.pr_repo()
        result = self.ctl(
            pr, state_home, "import-pr", "--target-ref", "feature", "--base-ref", "main",
            "--allow-main",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_imported_review_has_no_feature_authorization(self) -> None:
        repo = self.pr_repo()
        state = self.load(self.import_pr(repo, self.tmpdir()))
        self.assertEqual(state["repository"]["worktree_mode"], "current")
        self.assertNotIn("feature_authorization", state)
        self.assertIsNone(recorded_allow_main(state))

    def test_current_init_fails_closed_when_git_status_fails(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        (repo / ".git" / "index").write_bytes(b"corrupt index")
        result = self.ctl(repo, state_home, "init", "--feature", "F", "--worktree-mode", "current")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not inspect the checkout", result.stderr)
        self.assertEqual(self.run_count(repo, state_home), 0)


class ReuseTests(_RepoFixture):
    """`init --reuse` adopts only an active, mode- and worktree-compatible feature run."""

    def reuse(self, cwd: Path, state_home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.ctl(cwd, state_home, "init", "--feature", "Z", "--reuse", *extra)

    def assert_reused(self, result: subprocess.CompletedProcess[str], path: Path) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), path)

    def assert_refused(self, result, state_home: Path, repo: Path, before: dict[Path, bytes],
                       fragment: str) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--reuse found no compatible active feature run", result.stderr)
        self.assertIn(fragment, result.stderr)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.run_count(repo, state_home), len(before))

    def test_same_mode_reuse(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        isolated = self.init(repo, state_home)
        self.assert_reused(self.reuse(repo, state_home), isolated)

        repo2 = self.make_repo()
        state_home2 = self.tmpdir()
        current = self.init(repo2, state_home2, "--worktree-mode", "current")
        (repo2 / "README.md").write_text("in-progress edit\n", encoding="utf-8")
        self.assert_reused(self.reuse(repo2, state_home2, "--worktree-mode", "current"), current)

    def test_cross_mode_reuse_is_refused(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        isolated = self.init(repo, state_home)
        self.assert_refused(
            self.reuse(repo, state_home, "--worktree-mode", "current"),
            state_home, repo, {isolated: isolated.read_bytes()},
            "it runs in isolated worktree mode, not current checkout mode",
        )

        repo2 = self.make_repo()
        state_home2 = self.tmpdir()
        current = self.init(repo2, state_home2, "--worktree-mode", "current")
        self.assert_refused(
            self.reuse(repo2, state_home2), state_home2, repo2, {current: current.read_bytes()},
            "it runs in current checkout mode, not isolated worktree mode",
        )

    def test_current_reuse_from_another_worktree_is_refused(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        current = self.init(repo, state_home, "--worktree-mode", "current")
        other = self.add_worktree(repo, "other")
        self.assert_refused(
            self.reuse(other, state_home, "--worktree-mode", "current"),
            state_home, repo, {current: current.read_bytes()}, "it is pinned to worktree",
        )

    def test_imported_unknown_and_malformed_runs_are_refused(self) -> None:
        repo = self.pr_repo()
        state_home = self.tmpdir()
        imported = self.import_pr(repo, state_home)
        for mode in ("isolated", "current"):
            with self.subTest(mode=mode):
                self.assert_refused(
                    self.reuse(repo, state_home, "--worktree-mode", mode),
                    state_home, repo, {imported: imported.read_bytes()},
                    "it is a read-only existing-PR review run",
                )

        cases = [
            ("unknown kind", {"workflow_kind": "pr_iteration"}, "'pr_iteration' is not a feature workflow"),
            ("malformed mode", {"repository__worktree_mode": 7}, "worktree mode is malformed"),
            ("foreign repository id", {"repository__id": "0" * 16}, "does not record this repository's id"),
        ]
        for label, changes, fragment in cases:
            with self.subTest(label):
                repo2 = self.make_repo()
                home2 = self.tmpdir()
                path = self.init(repo2, home2, "--worktree-mode", "current")
                self.edit(path, **changes)
                self.assert_refused(
                    self.reuse(repo2, home2, "--worktree-mode", "current"),
                    home2, repo2, {path: path.read_bytes()}, fragment,
                )

    def test_terminal_run_is_not_reused(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        cancelled = self.init(repo, state_home)
        self.assertEqual(self.ctl(repo, state_home, "cancel").returncode, 0)
        result = self.reuse(repo, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(Path(result.stdout.strip()), cancelled)
        self.assertEqual(self.load(cancelled)["status"], "cancelled")

    def test_compatible_run_is_selected_among_incompatible_ones(self) -> None:
        repo = self.pr_repo()
        state_home = self.tmpdir()
        self.import_pr(repo, state_home)
        feature = self.init(repo, state_home, "--worktree-mode", "current", "--force")
        self.assert_reused(self.reuse(repo, state_home, "--worktree-mode", "current"), feature)

    def test_reuse_with_force_falls_back_to_a_new_run(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        isolated = self.init(repo, state_home)
        result = self.reuse(repo, state_home, "--worktree-mode", "current", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(Path(result.stdout.strip()), isolated)
        self.assertEqual(self.run_count(repo, state_home), 2)

    def test_pre_pin_runs_follow_the_compatibility_policy(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        isolated = self.init(repo, state_home)
        self.make_pre_pin(isolated)
        self.edit(isolated, repository__worktree_mode=_DELETE)
        self.assert_reused(self.reuse(repo, state_home), isolated)

        repo2 = self.make_repo()
        home2 = self.tmpdir()
        current = self.init(repo2, home2, "--worktree-mode", "current")
        self.make_pre_pin(current)
        self.assert_reused(self.reuse(repo2, home2, "--worktree-mode", "current"), current)
        other = self.add_worktree(repo2, "other")
        self.assert_refused(
            self.reuse(other, home2, "--worktree-mode", "current"),
            home2, repo2, {current: current.read_bytes()}, "it is pinned to worktree",
        )
        self.edit(current, repository__worktree_path=_DELETE)
        self.assert_refused(
            self.reuse(repo2, home2, "--worktree-mode", "current"),
            home2, repo2, {current: current.read_bytes()}, "no valid originating worktree",
        )


class AcceptDriftTests(_RepoFixture):
    """`accept-drift` is feature-only, worktree-pinned, and re-applies the branch guard."""

    def accept(self, cwd: Path, state_home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.ctl(cwd, state_home, "accept-drift", *extra)

    def assert_refused(self, result, path: Path, before: bytes, fragment: str) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(fragment, result.stderr)
        self.assertEqual(path.read_bytes(), before)

    def current_run(self, *extra: str, initial_branch: str = "main") -> tuple[Path, Path, Path]:
        repo = self.make_repo(initial_branch=initial_branch)
        self.git(repo, "branch", "master" if initial_branch == "main" else "main")
        state_home = self.tmpdir()
        return repo, state_home, self.init(repo, state_home, "--worktree-mode", "current", *extra)

    def test_isolated_feature_drift_is_accepted(self) -> None:
        repo = self.make_repo()
        worktree = self.add_worktree(repo, "worktree-x")
        state_home = self.tmpdir()
        path = self.init(worktree, state_home)
        self.git(worktree, "checkout", "-q", "-b", "worktree-y")
        result = self.accept(worktree, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load(path)
        self.assertEqual(state["baseline"]["branch"], "worktree-y")
        self.assertEqual(state["baseline"]["worktree_path"], str(worktree))
        self.assertEqual(state["feature_authorization"], {"allow_main": False})
        self.assertEqual(state["workflow_kind"], "feature")

    def test_current_feature_drift_to_allowed_branch_is_accepted(self) -> None:
        repo, state_home, path = self.current_run()
        self.git(repo, "checkout", "-q", "-b", "feature-2")
        result = self.accept(repo, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load(path)
        self.assertEqual(state["baseline"]["branch"], "feature-2")
        self.assertEqual(state["feature_authorization"], {"allow_main": False})
        self.assertEqual(
            self.ctl(repo, state_home, "run-check", "--name", "t", "--", sys.executable, "-c", "0").returncode,
            0,
        )

    def test_imported_and_unknown_kinds_are_refused(self) -> None:
        repo = self.pr_repo()
        state_home = self.tmpdir()
        imported = self.import_pr(repo, state_home)
        self.edit(imported, feature_authorization={"allow_main": True})
        self.assert_refused(
            self.accept(repo, state_home), imported, imported.read_bytes(), "existing-PR review run"
        )

        for kind in ("pr_iteration", 42):
            with self.subTest(kind=kind):
                repo2, home2, path = self.current_run()
                self.edit(path, workflow_kind=kind, feature_authorization={"allow_main": True})
                self.git(repo2, "checkout", "-q", "main")
                self.assert_refused(
                    self.accept(repo2, home2), path, path.read_bytes(), "is not a feature workflow"
                )

    def test_protected_branches_need_persisted_authorization(self) -> None:
        for branch in ("main", "master"):
            with self.subTest(branch=branch):
                repo, state_home, path = self.current_run()
                self.git(repo, "checkout", "-q", branch)
                self.assert_refused(
                    self.accept(repo, state_home), path, path.read_bytes(),
                    f"refuses to accept drift onto branch {branch!r}",
                )
                self.assertIn("initialized without --allow-main", self.accept(repo, state_home).stderr)
                blocked = self.ctl(repo, state_home, "run-check", "--name", "t", "--", "true")
                self.assertNotEqual(blocked.returncode, 0)

    def test_persisted_allow_main_permits_protected_branch(self) -> None:
        repo, state_home, path = self.current_run("--allow-main")
        self.git(repo, "checkout", "-q", "main")
        result = self.accept(repo, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load(path)
        self.assertEqual(state["baseline"]["branch"], "main")
        self.assertEqual(state["feature_authorization"], {"allow_main": True})

    def test_default_branch_follows_literal_feature_policy(self) -> None:
        repo, state_home, path = self.current_run(initial_branch="trunk")
        self.git(repo, "checkout", "-q", "trunk")
        result = self.accept(repo, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load(path)["baseline"]["branch"], "trunk")

    def test_authorization_cannot_be_added_or_inferred(self) -> None:
        repo, state_home, path = self.current_run()
        self.git(repo, "checkout", "-q", "main")
        before = path.read_bytes()
        flag = self.accept(repo, state_home, "--allow-main")
        self.assertNotEqual(flag.returncode, 0)
        self.assertIn("unrecognized arguments", flag.stderr)
        self.assertEqual(path.read_bytes(), before)

        for label, value in (("pre-authorization run", _DELETE), ("malformed", {"allow_main": "yes"})):
            with self.subTest(label):
                self.edit(path, feature_authorization=value)
                self.assert_refused(
                    self.accept(repo, state_home), path, path.read_bytes(),
                    "records no valid --allow-main authorization",
                )

    def test_pre_pin_current_run_can_still_move_to_a_feature_branch(self) -> None:
        repo, state_home, path = self.current_run()
        self.make_pre_pin(path)
        self.git(repo, "checkout", "-q", "main")
        self.assert_refused(
            self.accept(repo, state_home), path, path.read_bytes(),
            "records no valid --allow-main authorization",
        )
        self.git(repo, "checkout", "-q", "-b", "feature-2")
        result = self.accept(repo, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.load(path)
        self.assertEqual(state["baseline"]["worktree_path"], str(repo.resolve()))
        self.assertNotIn("feature_authorization", state)

    def test_worktree_mismatch_is_refused(self) -> None:
        repo, state_home, path = self.current_run()
        self.git(repo, "checkout", "-q", "-b", "elsewhere")
        other = self.add_worktree(repo, "feature", new=False)
        blocked = self.ctl(other, state_home, "run-check", "--name", "t", "--", "true")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Worktree changed", blocked.stderr)
        self.assert_refused(
            self.accept(other, state_home), path, path.read_bytes(),
            "never re-binds a run to another worktree",
        )

        repo2 = self.make_repo()
        linked = self.add_worktree(repo2, "worktree-x")
        home2 = self.tmpdir()
        isolated = self.init(linked, home2)
        self.assert_refused(
            self.accept(repo2, home2), isolated, isolated.read_bytes(),
            "never re-binds a run to another worktree",
        )

    def test_unclean_detached_or_failing_checkout_is_refused(self) -> None:
        repo, state_home, path = self.current_run()
        self.git(repo, "checkout", "-q", "-b", "feature-2")
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        self.assert_refused(
            self.accept(repo, state_home), path, path.read_bytes(), "requires a clean working tree"
        )
        self.git(repo, "checkout", "-q", "--", "README.md")
        self.git(repo, "checkout", "-q", "--detach")
        self.assert_refused(
            self.accept(repo, state_home), path, path.read_bytes(), "does not support detached HEAD"
        )
        self.git(repo, "checkout", "-q", "feature-2")
        (repo / ".git" / "index").write_bytes(b"corrupt index")
        self.assert_refused(
            self.accept(repo, state_home), path, path.read_bytes(), "could not inspect the checkout"
        )

    def test_unknown_mode_or_origin_is_refused(self) -> None:
        repo, state_home, path = self.current_run()
        self.git(repo, "checkout", "-q", "-b", "feature-2")
        self.edit(path, repository__worktree_mode="sideways")
        self.assert_refused(
            self.accept(repo, state_home), path, path.read_bytes(), "worktree mode is malformed"
        )
        self.edit(path, repository__worktree_mode="current", baseline__worktree_path="")
        self.assert_refused(
            self.accept(repo, state_home), path, path.read_bytes(), "no valid originating worktree"
        )


class StopHookIsolationTests(_RepoFixture):
    """The Stop hook acts only on the active feature run pinned to its worktree."""

    def assert_blocks(self, cwd: Path, state_home: Path, path: Path) -> None:
        before = self.load(path)["stop_gate_blocks"]
        result = self.gate(cwd, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision": "block"', result.stdout)
        self.assertEqual(self.load(path)["stop_gate_blocks"], before + 1)

    def assert_ignored(self, cwd: Path, state_home: Path, paths: list[Path], times: int = 4) -> str:
        before = {p: p.read_bytes() for p in paths}
        stderr = ""
        for _ in range(times):
            result = self.gate(cwd, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            stderr = result.stderr
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data)
        return stderr

    def test_applicable_feature_run_keeps_bounded_gate(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        path = self.init(repo, state_home, "--worktree-mode", "current")
        for _ in range(3):
            self.assert_blocks(repo, state_home, path)
        self.assertEqual(self.gate(repo, state_home).stdout, "")
        self.assertEqual(self.load(path)["status"], "blocked")

    def test_imported_review_is_never_selected_or_mutated(self) -> None:
        repo = self.pr_repo()
        state_home = self.tmpdir()
        imported = self.import_pr(repo, state_home)
        self.assert_ignored(repo, state_home, [imported])
        self.assertEqual(self.load(imported)["status"], "active")

    def test_feature_run_in_another_worktree_is_ignored(self) -> None:
        repo = self.make_repo()
        linked = self.add_worktree(repo, "worktree-x")
        state_home = self.tmpdir()
        path = self.init(linked, state_home)
        self.assert_ignored(repo, state_home, [path])
        self.assert_blocks(linked, state_home, path)

    def test_mixed_runs_select_only_the_applicable_feature_run(self) -> None:
        repo = self.pr_repo()
        state_home = self.tmpdir()
        imported = self.import_pr(repo, state_home)
        linked = self.add_worktree(repo, "worktree-x")
        elsewhere = self.init(linked, state_home, "--force")
        here = self.init(repo, state_home, "--worktree-mode", "current", "--force")
        unknown = self.init(repo, state_home, "--worktree-mode", "current", "--force")
        self.edit(unknown, workflow_kind="pr_iteration")
        malformed = self.init(repo, state_home, "--worktree-mode", "current", "--force")
        self.edit(malformed, repository="not-a-dict")
        foreign = self.init(repo, state_home, "--worktree-mode", "current", "--force")
        self.edit(foreign, repository__id="0" * 16)
        frozen = [imported, elsewhere, unknown, malformed, foreign]
        before = {p: p.read_bytes() for p in frozen}
        self.assert_blocks(repo, state_home, here)
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data)

    def test_terminal_and_absent_runs_do_not_block(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        self.assert_ignored(repo, state_home, [], times=1)
        path = self.init(repo, state_home)
        self.assertEqual(self.ctl(repo, state_home, "cancel").returncode, 0)
        self.assert_ignored(repo, state_home, [path])

    def test_several_applicable_runs_are_ambiguous(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        first = self.init(repo, state_home, "--worktree-mode", "current")
        second = self.init(repo, state_home, "--worktree-mode", "current", "--force")
        stderr = self.assert_ignored(repo, state_home, [first, second])
        self.assertIn("multiple active feature runs in this worktree", stderr)

    def test_unreadable_unrelated_state_fails_safe(self) -> None:
        repo = self.make_repo()
        state_home = self.tmpdir()
        path = self.init(repo, state_home, "--worktree-mode", "current")
        broken = path.parent.parent / "20000101T000000Z-deadbeef"
        broken.mkdir()
        (broken / "run-state.json").write_text("{not json", encoding="utf-8")
        self.assert_ignored(repo, state_home, [path, broken / "run-state.json"])

    def test_pre_pin_feature_run_is_selected_by_recorded_worktree(self) -> None:
        repo = self.make_repo()
        linked = self.add_worktree(repo, "worktree-x")
        state_home = self.tmpdir()
        path = self.init(linked, state_home)
        self.make_pre_pin(path)
        self.assert_ignored(repo, state_home, [path])
        self.assert_blocks(linked, state_home, path)


if __name__ == "__main__":
    unittest.main()
