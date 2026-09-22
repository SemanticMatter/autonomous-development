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
CONTROLLER = ROOT / "scripts/controller.py"
STOP_GATE = ROOT / "scripts/stop_gate.py"

# Make state module importable for helpers
sys.path.insert(0, str(ROOT / "scripts"))
import argparse  # noqa: E402

import controller  # noqa: E402
from state import (  # noqa: E402
    CrossProcessLock,
    find_active_runs,
    resolve_repository,
)


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    def make_repo(self) -> Path:
        temp = Path(tempfile.mkdtemp())
        self._tmpdirs.append(temp)
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "Test User"], check=True
        )
        (temp / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "initial"], check=True)
        return temp

    def make_state_home(self) -> Path:
        """Create a temporary directory for state storage."""
        d = Path(tempfile.mkdtemp())
        self._tmpdirs.append(d)
        return d

    def run_controller(
        self, repo: Path, *args: str, state_home: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["python3", str(CONTROLLER), "--project-root", str(repo)]
        if state_home is not None:
            cmd += ["--state-dir", str(state_home)]
        cmd += list(args)
        return subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _find_state_path(self, repo: Path, state_home: Path) -> Path:
        """Locate run-state.json for the single active run in state_home."""
        repo_info = resolve_repository(repo)
        active = find_active_runs(state_home, repo_info.id)
        if not active:
            raise AssertionError(
                f"No active runs found in {state_home} for repo {repo_info.id}"
            )
        return active[0].run_dir / "run-state.json"

    def _current_branch(self, repo: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def _checkout_feature_branch(self, repo: Path, name: str = "feature-branch") -> None:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", "-b", name],
            check=True,
        )

    def test_init_and_status(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        result = self.run_controller(
            repo, "init", "--feature", "Add a test feature", state_home=state_home
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        state_path = self._find_state_path(repo, state_home)
        self.assertTrue(state_path.exists(), f"State file not found at {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["feature"], "Add a test feature")

        status = self.run_controller(repo, "status", state_home=state_home)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("Phase: initialized", status.stdout)
        self.assertIn("Worktree mode: isolated worktree", status.stdout)
        self.assertEqual(state["repository"]["worktree_mode"], "isolated")

    def test_init_current_mode_records_current_checkout_path(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self._checkout_feature_branch(repo)

        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Feature",
            "--worktree-mode",
            "current",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["repository"]["canonical_root"], str(repo.resolve()))
        self.assertEqual(state["repository"]["worktree_path"], str(repo.resolve()))
        self.assertEqual(state["repository"]["worktree_mode"], "current")
        self.assertEqual(state["baseline"]["branch"], self._current_branch(repo))

        status = self.run_controller(repo, "status", state_home=state_home)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("Worktree mode: current checkout", status.stdout)

    def test_current_mode_refuses_main_and_master(self) -> None:
        for branch_name in ("main", "master"):
            with self.subTest(branch=branch_name):
                repo = self.make_repo()
                state_home = self.make_state_home()
                subprocess.run(
                    ["git", "-C", str(repo), "branch", "-m", branch_name],
                    check=True,
                )
                result = self.run_controller(
                    repo,
                    "init",
                    "--feature",
                    "Feature",
                    "--worktree-mode",
                    "current",
                    state_home=state_home,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(branch_name, result.stderr)

    def test_current_mode_allows_main_and_master_with_override(self) -> None:
        for branch_name in ("main", "master"):
            with self.subTest(branch=branch_name):
                repo = self.make_repo()
                state_home = self.make_state_home()
                subprocess.run(
                    ["git", "-C", str(repo), "branch", "-m", branch_name],
                    check=True,
                )
                result = self.run_controller(
                    repo,
                    "init",
                    "--feature",
                    "Feature",
                    "--worktree-mode",
                    "current",
                    "--allow-main",
                    state_home=state_home,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_mode_refuses_detached_head_even_with_main_override(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        subprocess.run(["git", "-C", str(repo), "branch", "-m", "main"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--detach", "HEAD"], check=True
        )

        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Feature",
            "--worktree-mode",
            "current",
            "--allow-main",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached HEAD", result.stderr)
        self.assertIn("Check out a named branch", result.stderr)

    def test_allow_main_requires_current_worktree_mode(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Feature",
            "--allow-main",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "--allow-main is only valid with --worktree-mode current", result.stderr
        )

    def test_current_mode_refuses_dirty_tree(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self._checkout_feature_branch(repo)
        (repo / "README.md").write_text("# dirty\n", encoding="utf-8")

        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Feature",
            "--worktree-mode",
            "current",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean working tree", result.stderr)

    def test_current_mode_refuses_untracked_file(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self._checkout_feature_branch(repo)
        (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Feature",
            "--worktree-mode",
            "current",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("untracked files", result.stderr)
        self.assertIn("untracked.txt", result.stderr)

    def test_current_mode_does_not_create_claude_worktrees(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self._checkout_feature_branch(repo)
        worktrees_dir = repo / ".claude" / "worktrees"

        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Feature",
            "--worktree-mode",
            "current",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(worktrees_dir.exists())

    def test_current_mode_with_allow_main_does_not_create_claude_worktrees(self) -> None:
        """The /autonomous-development:autonomous-main wrapper passes --allow-main; it
        must still skip the disposable-worktree path."""
        for branch_name in ("main", "master"):
            with self.subTest(branch=branch_name):
                repo = self.make_repo()
                state_home = self.make_state_home()
                subprocess.run(
                    ["git", "-C", str(repo), "branch", "-m", branch_name],
                    check=True,
                )
                worktrees_dir = repo / ".claude" / "worktrees"

                result = self.run_controller(
                    repo,
                    "init",
                    "--feature",
                    "Feature",
                    "--worktree-mode",
                    "current",
                    "--allow-main",
                    state_home=state_home,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(worktrees_dir.exists())

    def test_isolated_default_unchanged_for_autonomous_feature(self) -> None:
        """The /autonomous-development:autonomous-feature wrapper relies on the default
        worktree-mode staying `isolated` and on the run state recording it as such."""
        repo = self.make_repo()
        state_home = self.make_state_home()

        # No --worktree-mode → relies on the default.
        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Feature",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self._find_state_path(repo, state_home).read_text(encoding="utf-8"))
        self.assertEqual(state["repository"]["worktree_mode"], "isolated")

    def test_record_passing_check(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Feature", state_home=state_home
            ).returncode,
            0,
        )
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "truth",
            "--",
            "python3",
            "-c",
            'print("ok")',
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["verification"]["passed"])

    def test_rerun_supersedes_failed_check(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Feature", state_home=state_home
            ).returncode,
            0,
        )
        failed = self.run_controller(
            repo,
            "run-check",
            "--name",
            "tests",
            "--",
            "python3",
            "-c",
            "raise SystemExit(1)",
            state_home=state_home,
        )
        self.assertEqual(failed.returncode, 1)
        passed = self.run_controller(
            repo,
            "run-check",
            "--name",
            "tests",
            "--",
            "python3",
            "-c",
            'print("fixed")',
            state_home=state_home,
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)

        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["verification"]["passed"])

    def test_stop_gate_is_bounded(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Feature", state_home=state_home
            ).returncode,
            0,
        )

        # Capture state path while the run is still active (before budget exhaustion)
        state_path = self._find_state_path(repo, state_home)

        payload = json.dumps({"cwd": str(repo), "hook_event_name": "Stop"})
        env = {**os.environ, "CLAUDE_AUTONOMOUS_STATE_HOME": str(state_home)}

        for _ in range(3):
            result = subprocess.run(
                ["python3", str(STOP_GATE)],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn('"decision": "block"', result.stdout)

        final = subprocess.run(
            ["python3", str(STOP_GATE)],
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.assertEqual(final.returncode, 0)
        self.assertEqual(final.stdout, "")

        # After budget exhausted the run is terminal; read state directly by path
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")

    def test_stop_gate_leaves_non_active_status_byte_identical(self) -> None:
        """The automatic Stop hook must mutate only an exactly-active run. A
        status that is unknown, missing, or non-string is NOT merely
        'not terminal'; the hook must fail safe and leave such state
        byte-identical, neither incrementing the counter nor blocking."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Feature", state_home=state_home
            ).returncode,
            0,
        )
        state_path = self._find_state_path(repo, state_home)
        payload = json.dumps({"cwd": str(repo), "hook_event_name": "Stop"})
        env = {**os.environ, "CLAUDE_AUTONOMOUS_STATE_HOME": str(state_home)}

        missing = object()
        cases = [
            ("unknown", "some-unknown-status"),
            ("missing", missing),
            ("non-string", 123),
        ]
        for label, status in cases:
            with self.subTest(case=label):
                s = json.loads(state_path.read_text(encoding="utf-8"))
                if status is missing:
                    s.pop("status", None)
                else:
                    s["status"] = status
                state_path.write_text(json.dumps(s), encoding="utf-8")
                before = state_path.read_bytes()
                result = subprocess.run(
                    ["python3", str(STOP_GATE)],
                    input=payload,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                # No block decision was emitted.
                self.assertEqual(result.stdout, "")
                # The run-state file is untouched.
                self.assertEqual(state_path.read_bytes(), before)

    def test_reuse_ambiguous_multiple_runs_errors(self) -> None:
        """init --reuse with multiple active runs must error rather than silently pick one."""
        repo = self.make_repo()
        state_home = self.make_state_home()

        # Create two distinct active runs using --force
        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Run A", state_home=state_home
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Run B", "--force", state_home=state_home
            ).returncode,
            0,
        )

        result = self.run_controller(
            repo, "init", "--feature", "ignored", "--reuse", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple active runs", result.stderr)

    def test_init_mode_auto_escalates_on_risk(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Add Stripe billing and payment migration",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(state["requested_mode"], "auto")
        self.assertEqual(state["effective_mode"], "rigorous")
        self.assertTrue(state["risk"]["requires_adversarial_review"])

    def test_init_mode_auto_standard_when_low_risk(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        result = self.run_controller(
            repo, "init", "--feature", "Rename a button label", state_home=state_home
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(state["effective_mode"], "standard")
        self.assertFalse(state["risk"]["requires_adversarial_review"])

    def test_init_explicit_lean_not_escalated(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Add login auth flow",
            "--mode",
            "lean",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(state["effective_mode"], "lean")
        self.assertFalse(state["risk"]["requires_adversarial_review"])

    def test_set_risk_cannot_clear_required_adversarial_gate(self) -> None:
        """Once adversarial review is required, set-risk must not lower it: a
        high-risk run cannot be downgraded past the adversarial completion gate."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        # A rigorous-classified feature initializes with the gate required.
        self.run_controller(
            repo,
            "init",
            "--feature",
            "Add Stripe billing and payment migration",
            state_home=state_home,
        )
        state_path = self._find_state_path(repo, state_home)
        self.assertTrue(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )
        # Attempting to clear the gate fails closed and leaves it set.
        result = self.run_controller(
            repo, "set-risk", "--no-require-adversarial", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("monotonic-upward", result.stderr)
        self.assertTrue(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )

    def test_set_risk_can_escalate_low_to_required(self) -> None:
        """set-risk may raise the gate (the conservative direction)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(
            repo, "init", "--feature", "Rename a button label", state_home=state_home
        )
        state_path = self._find_state_path(repo, state_home)
        self.assertFalse(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )
        result = self.run_controller(
            repo,
            "set-risk",
            "--require-adversarial",
            "--reason",
            "touches auth after review",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )

    def test_run_check_summary_output(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "unit-tests",
            "--",
            "python3",
            "-c",
            'print("noise" * 100)',
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("✓ unit-tests passed", result.stdout)
        self.assertIn("full log:", result.stdout)
        # Summary mode must NOT replay the command's stdout into context.
        self.assertNotIn("noisenoise", result.stdout)

    def test_run_check_full_output_replays_streams(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "unit-tests",
            "--output",
            "full",
            "--",
            "python3",
            "-c",
            'print("UNIQUEMARKER")',
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UNIQUEMARKER", result.stdout)

    def test_run_check_failure_tail(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        script = (
            "import sys\n"
            "for i in range(200):\n"
            "    print('line', i)\n"
            "sys.exit(1)\n"
        )
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "tests",
            "--failure-tail-lines",
            "10",
            "--",
            "python3",
            "-c",
            script,
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("✗ tests failed with exit code 1", result.stderr)
        self.assertIn("showing final 10 lines", result.stderr)
        self.assertIn("line 199", result.stderr)
        self.assertNotIn("line 150", result.stderr)

    def test_accept_structured_decisions_materializes(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        run_dir = self._find_state_path(repo, state_home).parent

        source = run_dir / "spec.codex.json"
        source.write_text(
            json.dumps(
                {
                    "title": "T",
                    "problem_statement": "P",
                    "user_outcomes": ["o"],
                    "functional_requirements": [
                        {
                            "id": "FR-1",
                            "requirement": "orig",
                            "priority": "must",
                            "evidence": "e",
                        },
                        {
                            "id": "FR-2",
                            "requirement": "drop",
                            "priority": "should",
                            "evidence": "e",
                        },
                    ],
                    "non_functional_requirements": ["nfr"],
                    "acceptance_criteria": [
                        {"id": "AC-1", "criterion": "c", "verification": "v"}
                    ],
                    "assumptions": [],
                    "open_questions": [],
                    "risks": [],
                    "non_goals": [],
                }
            ),
            encoding="utf-8",
        )
        decisions = Path(tempfile.mkdtemp())
        self._tmpdirs.append(decisions)
        decisions_file = decisions / "spec-decisions.json"
        decisions_file.write_text(
            json.dumps(
                {
                    "accept": ["FR-1"],
                    "reject": [{"id": "FR-2", "reason": "scope"}],
                    "modify": [{"id": "FR-1", "replacement": "newtext"}],
                    "add": [],
                }
            ),
            encoding="utf-8",
        )
        result = self.run_controller(
            repo,
            "accept",
            "--kind",
            "spec",
            "--source",
            str(source),
            "--decisions",
            str(decisions_file),
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        accepted = json.loads(
            (run_dir / "accepted-spec.json").read_text(encoding="utf-8")
        )
        ids = [fr["id"] for fr in accepted["functional_requirements"]]
        self.assertEqual(ids, ["FR-1"])
        self.assertEqual(accepted["functional_requirements"][0]["requirement"], "newtext")
        md = (run_dir / "accepted-spec.md").read_text(encoding="utf-8")
        self.assertIn("newtext", md)

    def test_accept_rejects_incomplete_structured_source(self) -> None:
        """The reconciliation source is fully validated against its phase schema
        before materialization: an incomplete enhanced-idea (here missing the
        required `evidence` on a functional requirement and several top-level
        sections) must be rejected, and no accepted artifact may be written."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        run_dir = self._find_state_path(repo, state_home).parent

        source = run_dir / "spec.codex.json"
        source.write_text(
            json.dumps(
                {
                    "title": "T",
                    "problem_statement": "P",
                    "functional_requirements": [
                        {"id": "FR-1", "requirement": "x", "priority": "must"}
                    ],
                    "acceptance_criteria": [],
                }
            ),
            encoding="utf-8",
        )
        decisions = run_dir / "spec-decisions.json"
        decisions.write_text(
            json.dumps({"accept": ["FR-1"], "reject": [], "modify": [], "add": []}),
            encoding="utf-8",
        )
        result = self.run_controller(
            repo, "accept", "--kind", "spec",
            "--source", str(source), "--decisions", str(decisions),
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema validation", result.stderr)
        self.assertFalse((run_dir / "accepted-spec.json").exists())
        self.assertFalse((run_dir / "accepted-spec.md").exists())

    # --- rollback-safe accept publication (failure injection) ---

    def _structured_accept_args(
        self, repo: Path, state_home: Path, run_dir: Path
    ) -> argparse.Namespace:
        """Stage a valid two-artifact (Markdown + JSON) structured spec accept so
        the publish loop performs two backup+publish replace pairs."""
        source = run_dir / "spec.codex.json"
        source.write_text(
            json.dumps(
                {
                    "title": "T",
                    "problem_statement": "P",
                    "user_outcomes": ["o"],
                    "functional_requirements": [
                        {
                            "id": "FR-1",
                            "requirement": "orig",
                            "priority": "must",
                            "evidence": "e",
                        }
                    ],
                    "non_functional_requirements": ["nfr"],
                    "acceptance_criteria": [
                        {"id": "AC-1", "criterion": "c", "verification": "v"}
                    ],
                    "assumptions": [],
                    "open_questions": [],
                    "risks": [],
                    "non_goals": [],
                }
            ),
            encoding="utf-8",
        )
        decisions = run_dir / "spec-decisions.json"
        decisions.write_text(
            json.dumps({"accept": ["FR-1"], "reject": [], "modify": [], "add": []}),
            encoding="utf-8",
        )
        return argparse.Namespace(
            project_root=str(repo),
            state_dir=str(state_home),
            run_id=None,
            kind="spec",
            file=None,
            source=str(source),
            decisions=str(decisions),
        )

    def _failing_replace(self, fail_on_call: int):
        """Return an os.replace wrapper that raises on the Nth invocation.

        Within cmd_accept, os.replace is used only by the publish loop (backup
        and publish moves), so the call index deterministically targets a
        specific point in the all-or-nothing publication.
        """
        real = controller.os.replace
        counter = {"n": 0}

        def fake(src, dst, *a, **k):
            counter["n"] += 1
            if counter["n"] == fail_on_call:
                raise OSError("injected os.replace failure")
            return real(src, dst, *a, **k)

        return fake

    def _assert_no_temp_or_backup_artifacts(self, run_dir: Path) -> None:
        leftovers = list(run_dir.glob(".accepted-spec.*"))
        self.assertEqual(leftovers, [], leftovers)

    def test_accept_rollback_when_first_replace_fails(self) -> None:
        """If the very first publish replace fails, no canonical artifact may
        change and the run state must be untouched (save never reached)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent
        canonical_md = run_dir / "accepted-spec.md"
        canonical_json = run_dir / "accepted-spec.json"
        canonical_md.write_text("ORIGINAL MD\n", encoding="utf-8")
        canonical_json.write_text('{"original": true}\n', encoding="utf-8")
        md_before = canonical_md.read_bytes()
        json_before = canonical_json.read_bytes()
        state_before = state_path.read_bytes()

        args = self._structured_accept_args(repo, state_home, run_dir)
        original = controller.os.replace
        controller.os.replace = self._failing_replace(1)
        try:
            with self.assertRaises(OSError):
                controller.cmd_accept(args)
        finally:
            controller.os.replace = original

        self.assertEqual(canonical_md.read_bytes(), md_before)
        self.assertEqual(canonical_json.read_bytes(), json_before)
        self.assertEqual(state_path.read_bytes(), state_before)
        self._assert_no_temp_or_backup_artifacts(run_dir)

    def test_accept_rollback_when_second_artifact_replace_fails(self) -> None:
        """The first artifact is fully published, then the second artifact's
        publish replace fails. Both canonical artifacts must be restored to
        their pre-accept bytes and the run state left unchanged."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent
        canonical_md = run_dir / "accepted-spec.md"
        canonical_json = run_dir / "accepted-spec.json"
        canonical_md.write_text("ORIGINAL MD\n", encoding="utf-8")
        canonical_json.write_text('{"original": true}\n', encoding="utf-8")
        md_before = canonical_md.read_bytes()
        json_before = canonical_json.read_bytes()
        state_before = state_path.read_bytes()

        args = self._structured_accept_args(repo, state_home, run_dir)
        # Calls: 1=backup md, 2=publish md, 3=backup json, 4=publish json.
        original = controller.os.replace
        controller.os.replace = self._failing_replace(4)
        try:
            with self.assertRaises(OSError):
                controller.cmd_accept(args)
        finally:
            controller.os.replace = original

        self.assertEqual(canonical_md.read_bytes(), md_before)
        self.assertEqual(canonical_json.read_bytes(), json_before)
        self.assertEqual(state_path.read_bytes(), state_before)
        self._assert_no_temp_or_backup_artifacts(run_dir)

    def test_accept_rollback_when_state_save_fails_with_both_prior(self) -> None:
        """Both artifacts are published, then save_run_state fails. Because the
        state file is written atomically (the prior state survives a failed
        save), restoring both canonical artifacts from backup restores full
        artifact/state consistency."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent
        canonical_md = run_dir / "accepted-spec.md"
        canonical_json = run_dir / "accepted-spec.json"
        canonical_md.write_text("ORIGINAL MD\n", encoding="utf-8")
        canonical_json.write_text('{"original": true}\n', encoding="utf-8")
        md_before = canonical_md.read_bytes()
        json_before = canonical_json.read_bytes()
        state_before = state_path.read_bytes()

        args = self._structured_accept_args(repo, state_home, run_dir)
        original_save = controller.save_run_state

        def failing_save(*a, **k):
            raise OSError("injected save_run_state failure")

        controller.save_run_state = failing_save
        try:
            with self.assertRaises(OSError):
                controller.cmd_accept(args)
        finally:
            controller.save_run_state = original_save

        self.assertEqual(canonical_md.read_bytes(), md_before)
        self.assertEqual(canonical_json.read_bytes(), json_before)
        self.assertEqual(state_path.read_bytes(), state_before)
        self._assert_no_temp_or_backup_artifacts(run_dir)

    def test_accept_rollback_removes_artifacts_when_no_prior_canonical(self) -> None:
        """When no canonical artifact existed before the accept, a failed
        publication must remove the just-published artifacts entirely (there is
        no prior file to restore) and leave the run state unchanged."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent
        canonical_md = run_dir / "accepted-spec.md"
        canonical_json = run_dir / "accepted-spec.json"
        self.assertFalse(canonical_md.exists())
        self.assertFalse(canonical_json.exists())
        state_before = state_path.read_bytes()

        args = self._structured_accept_args(repo, state_home, run_dir)
        original_save = controller.save_run_state

        def failing_save(*a, **k):
            raise OSError("injected save_run_state failure")

        controller.save_run_state = failing_save
        try:
            with self.assertRaises(OSError):
                controller.cmd_accept(args)
        finally:
            controller.save_run_state = original_save

        self.assertFalse(canonical_md.exists())
        self.assertFalse(canonical_json.exists())
        self.assertEqual(state_path.read_bytes(), state_before)
        self._assert_no_temp_or_backup_artifacts(run_dir)

    def test_doctor_reports_jsonschema(self) -> None:
        """`doctor` must surface jsonschema availability (a declared runtime
        dependency required for every structural validation gate)."""
        repo = self.make_repo()
        result = self.run_controller(repo, "doctor")
        self.assertIn("jsonschema", result.stdout)

    def test_next_action_progression(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(
            repo,
            "init",
            "--feature",
            "Rename a label",
            "--mode",
            "standard",
            state_home=state_home,
        )
        first = self.run_controller(repo, "next-action", state_home=state_home)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["phase"], "specification")

        run_dir = self._find_state_path(repo, state_home).parent
        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        self.run_controller(
            repo,
            "set-phase",
            "--phase",
            "spec-accepted",
            state_home=state_home,
        )
        # accept --file to register the artifact key the way the workflow does.
        spec_src = run_dir / "src-spec.md"
        spec_src.write_text("spec", encoding="utf-8")
        self.run_controller(
            repo,
            "accept",
            "--kind",
            "spec",
            "--file",
            str(spec_src),
            state_home=state_home,
        )
        second = self.run_controller(repo, "next-action", state_home=state_home)
        self.assertEqual(json.loads(second.stdout)["phase"], "planning")

    def test_usage_report_empty_and_json(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        text = self.run_controller(repo, "usage-report", state_home=state_home)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("no Codex phases recorded", text.stdout)
        js = self.run_controller(
            repo, "usage-report", "--json", state_home=state_home
        )
        self.assertEqual(js.returncode, 0, js.stderr)
        self.assertEqual(json.loads(js.stdout), [])

    def test_evaluate_blocks_on_missing_accepted_artifact(self) -> None:
        """The completion gate checks accepted-artifact existence under the lock,
        so a missing accepted-spec/plan blocks completion (fail closed)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        # No accepted-spec.md / accepted-plan.md created.
        result = self.run_controller(repo, "evaluate", state_home=state_home)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Missing accepted-spec.md", result.stderr)
        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(state.get("status"), "complete")

    def test_review_checkpoint_captures_and_detects_changes(self) -> None:
        """A review checkpoint snapshots the worktree it saw; a later round
        detects which feature paths changed since that checkpoint."""
        repo = self.make_repo()
        repo_info = resolve_repository(repo)
        baseline = repo_info.head_commit
        state: dict = {"reviews": [], "baseline": {"commit": baseline}}
        # Introduce a feature change relative to the baseline commit.
        (repo / "feature.py").write_text("v1\n", encoding="utf-8")
        checkpoint = controller.capture_review_checkpoint(
            repo_info, state, checkpoint_id="review-01"
        )
        self.assertIn("feature.py", checkpoint["changed_paths"])
        self.assertEqual(checkpoint["review_context_mode"], controller.REVIEW_CONTEXT_MODE)
        self.assertIsNone(checkpoint["previous_checkpoint_id"])
        # No prior checkpoint yet → "treat as full review".
        self.assertIsNone(controller.changed_paths_since_last_review(repo_info, state))
        state["reviews"].append({"round": 1, "checkpoint": checkpoint})
        # Edit the same file; the next round must see it as changed.
        (repo / "feature.py").write_text("v2 changed\n", encoding="utf-8")
        changed = controller.changed_paths_since_last_review(repo_info, state)
        self.assertEqual(changed, ["feature.py"])

    def test_evaluate_blocks_on_unsatisfied_acceptance_criterion(self) -> None:
        """The completion gate fails closed on an acceptance criterion that is not
        `satisfied`, and rejects a `pass` verdict that coexists with it."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent
        # Satisfy every gate condition EXCEPT the acceptance criteria.
        (run_dir / "accepted-spec.md").write_text("spec\n", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan\n", encoding="utf-8")
        (run_dir / "review-01.codex.json").write_text(
            json.dumps({"verdict": "pass", "summary": "ok"}), encoding="utf-8"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["verification"] = {
            "checks": [{"name": "unit", "command": ["true"], "exit_code": 0}]
        }
        state["reviews"] = [
            {"round": 1, "verdict": "pass", "delta": False, "path": "review-01.codex.json"}
        ]
        state["cumulative_findings"] = []
        state["cumulative_acceptance_criteria"] = [
            {"id": "AC-1", "status": "not_satisfied", "evidence": "incomplete", "round": 1}
        ]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        result = self.run_controller(repo, "evaluate", state_home=state_home)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("acceptance criteria not satisfied", result.stderr)
        self.assertIn("AC-1", result.stderr)
        self.assertIn("inconsistent review", result.stderr)
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(after.get("status"), "complete")

    def test_usage_report_works_after_run_is_terminal(self) -> None:
        """The usage report (FR-1) must remain viewable after the run reaches a
        terminal status, without requiring an explicit --run-id."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # Drive the run to a terminal status and record a usage row.
        state["status"] = "complete"
        state["phase"] = "complete"
        state["codex_runs"] = [
            {
                "phase": "review-01",
                "prompt_characters": 100,
                "output_characters": 50,
                "duration_seconds": 1.0,
            }
        ]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        # No active run remains, and no --run-id is supplied.
        text = self.run_controller(repo, "usage-report", state_home=state_home)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("review-01", text.stdout)
        # status should likewise resolve the most-recent terminal run.
        st = self.run_controller(repo, "status", state_home=state_home)
        self.assertEqual(st.returncode, 0, st.stderr)

    def test_triage_merges_ledger(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        ledger_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ledger_dir)
        ledger_file = ledger_dir / "triage.json"
        ledger_file.write_text(
            json.dumps(
                [
                    {
                        "fingerprint": "export.py:csv-style",
                        "status": "rejected",
                        "reason": "formatter enforces style",
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = self.run_controller(
            repo, "triage", "--file", str(ledger_file), state_home=state_home
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recorded 1 triage finding", result.stdout)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["review_ledger"][0]["fingerprint"], "export.py:csv-style"
        )

    def test_triage_rejects_unauditable_entry_without_fingerprint(self) -> None:
        """A triage entry lacking a fingerprint must fail closed: it would close a
        cumulative finding (unblocking the gate) without an audit-ledger record."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        # Seed a blocking severe cumulative finding.
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["cumulative_findings"] = [
            {"id": "F-1", "severity": "high", "status": "open"}
        ]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        ledger_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ledger_dir)
        ledger_file = ledger_dir / "triage.json"
        # No fingerprint, but a finding_id that would close F-1 unaudited.
        ledger_file.write_text(
            json.dumps(
                [{"finding_id": "F-1", "status": "rejected", "reason": "nope"}]
            ),
            encoding="utf-8",
        )
        result = self.run_controller(
            repo, "triage", "--file", str(ledger_file), state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fingerprint", result.stderr)
        # The severe finding remains open (gate still blocked).
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["cumulative_findings"][0]["status"], "open")

    def test_source_path_prefers_run_dir_over_cwd_shadow(self) -> None:
        """A bare --source filename must bind to the run artifact, not a same-named
        file shadowing it from the current working directory."""
        run_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(run_dir)
        cwd_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(cwd_dir)
        (run_dir / "plan.codex.json").write_text("RUN", encoding="utf-8")
        (cwd_dir / "plan.codex.json").write_text("SHADOW", encoding="utf-8")

        prev = os.getcwd()
        os.chdir(cwd_dir)
        try:
            resolved = controller._resolve_source_path("plan.codex.json", run_dir)
        finally:
            os.chdir(prev)
        self.assertEqual(resolved, (run_dir / "plan.codex.json").resolve())
        self.assertEqual(resolved.read_text(encoding="utf-8"), "RUN")

    def test_source_path_absolute_outside_run_dir_still_resolves(self) -> None:
        """An absolute path (the form the skill uses for orchestrator-authored
        decisions/triage ledgers, e.g. /tmp/claude/...) must still bind to that
        literal file so the run-dir-first preference does not break the
        documented workflow."""
        run_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(run_dir)
        ext_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ext_dir)
        external = ext_dir / "decisions.json"
        external.write_text("EXTERNAL", encoding="utf-8")

        resolved = controller._resolve_source_path(
            str(external), run_dir, label="Decisions file"
        )
        self.assertEqual(resolved, external.resolve())
        self.assertEqual(resolved.read_text(encoding="utf-8"), "EXTERNAL")

    def test_source_path_not_found_uses_label(self) -> None:
        run_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(run_dir)
        with self.assertRaises(controller.WorkflowError) as ctx:
            controller._resolve_source_path(
                "missing.json", run_dir, label="Triage ledger"
            )
        self.assertIn("Triage ledger not found", str(ctx.exception))

    def test_codex_review_records_usage_and_delta(self) -> None:
        """cmd_codex success path: --json + profile args, NDJSON artifact, usage
        record, and round-aware full-then-delta review selection."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        captured: list[list[str]] = []

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            # The review-merge path captures a git checkpoint; let real git run.
            if cmd and cmd[0] == "git":
                return original(
                    cmd, cwd=cwd, input_text=input_text, check=check, timeout=timeout
                )
            captured.append(list(cmd))
            # Emulate codex writing the output-last-message file.
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            is_delta = "review-delta.schema.json" in " ".join(cmd)
            if is_delta:
                payload = {
                    "verdict": "pass",
                    "summary": "ok",
                    "resolved_findings": [],
                    "new_findings": [],
                    "regressions": [],
                    "affected_acceptance_criteria": [],
                    "confidence": 1.0,
                }
            else:
                payload = {
                    "verdict": "pass",
                    "summary": "ok",
                    "findings": [],
                    "verification_gaps": [],
                    "acceptance_criteria_assessment": [],
                    "confidence": 1.0,
                }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            ndjson = (
                '{"msg": {"type": "session_configured", "model": "gpt-x-test"}}\n'
                '{"type": "token_count", "info": {"input_tokens": 10, '
                '"output_tokens": 5, "total_tokens": 15}}\n'
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=ndjson, stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            self.assertEqual(controller.cmd_codex(args), 0)
            self.assertEqual(controller.cmd_codex(args), 0)
        finally:
            controller.run_process = original

        # Both rounds applied --json and reasoning-effort overrides.
        for cmd in captured:
            self.assertIn("--json", cmd)
            self.assertTrue(
                any(c.startswith("model_reasoning_effort=") for c in cmd),
                cmd,
            )
        # Round 1 full schema, round 2 delta schema.
        self.assertIn("review.schema.json", " ".join(captured[0]))
        self.assertIn("review-delta.schema.json", " ".join(captured[1]))

        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(final["codex_runs"]), 2)
        rec = final["codex_runs"][0]
        self.assertEqual(rec["phase"], "review-01")
        self.assertEqual(rec["model"], "gpt-x-test")
        self.assertEqual(rec["reasoning_effort"], "high")
        self.assertIn("prompt_characters", rec)
        self.assertIn("output_characters", rec)
        self.assertIn("duration_seconds", rec)
        self.assertTrue((run_dir / "review-01.events.ndjson").exists())
        self.assertTrue((run_dir / "review-02.events.ndjson").exists())
        self.assertFalse(final["reviews"][0]["delta"])
        self.assertTrue(final["reviews"][1]["delta"])

    def test_review_round_mode_mismatch_fails_closed(self) -> None:
        """If a concurrent same-run invocation advances review_round between the
        pre-lock mode selection and the locked merge, cmd_codex must fail closed
        rather than merge a full-review payload under delta semantics."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            # This call begins as round 1 (full review). Simulate a concurrent
            # invocation completing round 1 first by advancing the persisted
            # round AND recording its full-review baseline before this
            # invocation acquires the lock (a real round-1 completion appends to
            # `reviews`, which is what makes round 2 select delta mode).
            mid = json.loads(state_path.read_text(encoding="utf-8"))
            mid["review_round"] = 1
            mid.setdefault("reviews", []).append(
                {"round": 1, "verdict": "pass", "delta": False}
            )
            state_path.write_text(json.dumps(mid), encoding="utf-8")
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            payload = {
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
                "verification_gaps": [],
                "acceptance_criteria_assessment": [],
                "confidence": 1.0,
            }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(controller.WorkflowError) as ctx:
                controller.cmd_codex(args)
        finally:
            controller.run_process = original

        self.assertIn("round-mode mismatch", str(ctx.exception).lower())
        # Staged artifacts are cleaned up; the failing invocation's review was
        # NOT merged (only the simulated concurrent round-1 baseline remains).
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            final.get("reviews", []),
            [{"round": 1, "verdict": "pass", "delta": False}],
        )
        self.assertEqual(final.get("review_round"), 1)
        self.assertFalse(list(run_dir.glob(".staging-*")))

    def test_review_without_full_baseline_runs_full_not_delta(self) -> None:
        """A run whose review_round is already >= 1 but has NO recorded full
        review (e.g. migrated state, or a lost round-1 artifact) must run the
        next review as a FULL review, not a delta. Selecting delta purely from
        review_round would let a delta `pass` with no new findings clear the
        gate without any severe-findings baseline ever being established."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        # Simulate a migrated/legacy run: the round counter is advanced but no
        # full-review baseline was ever recorded and cumulative_findings is empty.
        state["review_round"] = 1
        state["reviews"] = []
        state["cumulative_findings"] = {}
        state_path.write_text(json.dumps(state), encoding="utf-8")

        captured: list[list[str]] = []

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            if cmd and cmd[0] == "git":
                return original(
                    cmd, cwd=cwd, input_text=input_text, check=check, timeout=timeout
                )
            captured.append(list(cmd))
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            payload = {
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
                "verification_gaps": [],
                "acceptance_criteria_assessment": [],
                "confidence": 1.0,
            }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            self.assertEqual(controller.cmd_codex(args), 0)
        finally:
            controller.run_process = original

        # Despite review_round == 1 (next_round == 2), the absence of a recorded
        # full review forces a FULL review: full schema, not the delta schema.
        joined = " ".join(captured[0])
        self.assertIn("review.schema.json", joined)
        self.assertNotIn("review-delta.schema.json", joined)
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(final["reviews"][-1]["delta"])

    def test_codex_merge_aborts_if_run_made_terminal_during_exec(self) -> None:
        """A concurrent cancel/block while Codex runs drives the run to a
        terminal status; the post-exec locked merge must fail closed rather than
        append a review and resurrect the cancelled run."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            # Simulate a concurrent `cancel` completing while Codex runs.
            mid = json.loads(state_path.read_text(encoding="utf-8"))
            mid["status"] = "cancelled"
            mid["phase"] = "cancelled"
            state_path.write_text(json.dumps(mid), encoding="utf-8")
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            payload = {
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
                "verification_gaps": [],
                "acceptance_criteria_assessment": [],
                "confidence": 1.0,
            }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(controller.WorkflowError) as ctx:
                controller.cmd_codex(args)
        finally:
            controller.run_process = original

        self.assertIn("no longer active", str(ctx.exception).lower())
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final.get("status"), "cancelled")
        self.assertEqual(final.get("reviews", []), [])
        self.assertFalse(list(run_dir.glob(".staging-*")))

    def test_codex_staging_cleaned_on_events_write_failure(self) -> None:
        """If persisting the staged NDJSON event stream fails (e.g. disk full),
        the already-written Codex output must not be orphaned: no `.staging-*`
        files remain and no review is merged."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            out_path.write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="events", stderr="")

        real_write_text = Path.write_text

        def failing_write_text(self, *a, **k):
            if self.name.endswith(".events.ndjson"):
                raise OSError("disk full")
            return real_write_text(self, *a, **k)

        original = controller.run_process
        controller.run_process = fake_run_process
        Path.write_text = failing_write_text
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(OSError):
                controller.cmd_codex(args)
        finally:
            Path.write_text = real_write_text
            controller.run_process = original

        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final.get("reviews", []), [])
        self.assertFalse(list(run_dir.glob(".staging-*")))

    def test_codex_staging_cleaned_on_validation_failure(self) -> None:
        """A schema-incomplete payload must fail closed AND leave no staging
        artifacts on disk (raw prompt response / NDJSON are not retained)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            # Top-level-valid JSON object but missing the required `findings` key,
            # so _missing_required_fields fails closed after the staging write.
            out_path.write_text(
                json.dumps({"verdict": "pass", "summary": "ok"}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(controller.WorkflowError):
                controller.cmd_codex(args)
        finally:
            controller.run_process = original

        self.assertFalse(
            list(run_dir.glob(".staging-*")),
            "staging artifacts must be removed on validation failure",
        )
        # No review was recorded.
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8")).get("reviews", []), []
        )

    def test_review_budget_exhausted_sets_blocked(self) -> None:
        """cmd_codex --phase review over budget must atomically set status=blocked."""
        import json as _json

        repo = self.make_repo()
        state_home = self.make_state_home()

        init = self.run_controller(
            repo, "init", "--feature", "Feature", state_home=state_home
        )
        self.assertEqual(init.returncode, 0, init.stderr)

        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        # Force review_round to the maximum so the next review attempt exceeds budget.
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        state["review_round"] = state.get("max_review_rounds", 3)
        state_path.write_text(_json.dumps(state), encoding="utf-8")

        # Write required artifacts to pass pre-flight checks.
        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(_json.dumps(state), encoding="utf-8")

        result = self.run_controller(
            repo, "codex", "--phase", "review", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)

        final_state = _json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["status"], "blocked")
        self.assertEqual(final_state["phase"], "review-budget-exhausted")


TERMINAL_STATUSES = ("complete", "blocked", "cancelled", "archived")


class TerminalStateIntegrityTests(unittest.TestCase):
    """P0 W1: terminal runs are immutable and cannot be resurrected.

    These tests assert the run-access contracts end-to-end through the CLI:
    a mutating command named with an explicit --run-id must refuse a terminal
    run, read-only inspection must keep working on terminal runs, lifecycle
    transitions must obey the transition table, and run-identity invariants
    must be enforced.
    """

    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    # --- shared helpers (mirrors ControllerTests, kept local for isolation) ---

    def make_repo(self) -> Path:
        temp = Path(tempfile.mkdtemp())
        self._tmpdirs.append(temp)
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "Test User"], check=True
        )
        (temp / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "initial"], check=True)
        return temp

    def make_state_home(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self._tmpdirs.append(d)
        return d

    def run_controller(
        self, repo: Path, *args: str, state_home: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["python3", str(CONTROLLER), "--project-root", str(repo)]
        if state_home is not None:
            cmd += ["--state-dir", str(state_home)]
        cmd += list(args)
        return subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def init_run(self, repo: Path, state_home: Path, feature: str = "F") -> Path:
        res = self.run_controller(
            repo, "init", "--feature", feature, state_home=state_home
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return self._state_path(repo, state_home)

    def _state_path(self, repo: Path, state_home: Path) -> Path:
        repo_info = resolve_repository(repo)
        runs = state_home / "repositories" / repo_info.id / "runs"
        dirs = [d for d in runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, f"expected exactly one run dir, got {dirs}")
        return dirs[0] / "run-state.json"

    def _set_status(self, state_path: Path, status: str) -> str:
        s = json.loads(state_path.read_text(encoding="utf-8"))
        s["status"] = status
        s["phase"] = status
        state_path.write_text(json.dumps(s), encoding="utf-8")
        return s["run_id"]

    def _mutation_commands(self) -> list[tuple[str, list[str]]]:
        ledger_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ledger_dir)
        ledger = ledger_dir / "triage.json"
        ledger.write_text(
            json.dumps(
                [{"fingerprint": "x.py:y", "status": "rejected", "reason": "r"}]
            ),
            encoding="utf-8",
        )
        spec = ledger_dir / "spec.md"
        spec.write_text("spec", encoding="utf-8")
        return [
            ("run-check", ["run-check", "--name", "t", "--", "python3", "-c", "print(1)"]),
            ("set-phase", ["set-phase", "--phase", "planning"]),
            ("set-risk", ["set-risk", "--require-adversarial", "--reason", "x"]),
            ("evaluate", ["evaluate"]),
            ("accept-drift", ["accept-drift"]),
            ("triage", ["triage", "--file", str(ledger)]),
            ("accept", ["accept", "--kind", "spec", "--file", str(spec)]),
            ("codex", ["codex", "--phase", "review"]),
        ]

    # --- tests ---

    def test_terminal_runs_reject_every_mutation(self) -> None:
        """Explicit-`--run-id` mutations must be refused for every terminal
        status, the error must name the run id + status + operation, and the
        persisted state bytes must be unchanged."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        commands = self._mutation_commands()

        for status in TERMINAL_STATUSES:
            run_id = self._set_status(state_path, status)
            before = state_path.read_bytes()
            for op, argv in commands:
                with self.subTest(status=status, op=op):
                    result = self.run_controller(
                        repo, "--run-id", run_id, *argv, state_home=state_home
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    err = result.stderr.lower()
                    self.assertIn("terminal", err)
                    self.assertIn(run_id, result.stderr)
                    self.assertIn(status, result.stderr)
                    # Immutability: the run-state file is byte-identical.
                    self.assertEqual(state_path.read_bytes(), before)

    def test_read_only_commands_inspect_every_terminal_status(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)

        for status in TERMINAL_STATUSES:
            run_id = self._set_status(state_path, status)
            before = state_path.read_bytes()
            inspections = [
                ["status", "--run-id", run_id],
                ["show-run", "--run-id", run_id],
                ["usage-report", "--run-id", run_id],
                ["next-action", "--run-id", run_id],
                ["list-runs", "--all"],
            ]
            for argv in inspections:
                with self.subTest(status=status, cmd=argv[0]):
                    # Global --run-id must precede the subcommand; show-run takes
                    # its run id as a subcommand option, the rest as global.
                    if argv[0] == "show-run":
                        result = self.run_controller(
                            repo, *argv, state_home=state_home
                        )
                    elif argv[0] == "list-runs":
                        result = self.run_controller(
                            repo, *argv, state_home=state_home
                        )
                    else:
                        result = self.run_controller(
                            repo,
                            "--run-id",
                            run_id,
                            argv[0],
                            state_home=state_home,
                        )
                    self.assertEqual(
                        result.returncode, 0, f"{argv[0]}: {result.stderr}"
                    )
                    self.assertEqual(state_path.read_bytes(), before)

    def test_evaluate_cannot_resurrect_completed_run(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = self._set_status(state_path, "complete")
        result = self.run_controller(
            repo, "--run-id", run_id, "evaluate", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["status"], "complete"
        )

    def test_cancel_and_block_cannot_rewrite_completed_run(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        for op in ("cancel", "block"):
            run_id = self._set_status(state_path, "complete")
            argv = [op] if op == "cancel" else [op, "--reason", "x"]
            result = self.run_controller(
                repo, "--run-id", run_id, *argv, state_home=state_home
            )
            with self.subTest(op=op):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only allowed from", result.stderr)
                self.assertEqual(
                    json.loads(state_path.read_text(encoding="utf-8"))["status"],
                    "complete",
                )

    def test_active_run_cannot_be_archived(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = json.loads(state_path.read_text(encoding="utf-8"))["run_id"]
        result = self.run_controller(
            repo, "--run-id", run_id, "archive-run", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only allowed from", result.stderr)
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["status"], "active"
        )

    def test_archive_is_idempotent_and_preserves_data(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = self._set_status(state_path, "complete")
        first = self.run_controller(
            repo, "--run-id", run_id, "archive-run", state_home=state_home
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        archived_bytes = state_path.read_bytes()
        self.assertEqual(
            json.loads(archived_bytes)["status"], "archived"
        )
        # Re-archiving is a no-op that must not alter any data.
        second = self.run_controller(
            repo, "--run-id", run_id, "archive-run", state_home=state_home
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already archived", second.stdout)
        self.assertEqual(state_path.read_bytes(), archived_bytes)

    def test_init_cannot_overwrite_existing_terminal_run(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = self._set_status(state_path, "complete")
        before = state_path.read_bytes()
        # Even with --force, an existing run ID must not be clobbered.
        result = self.run_controller(
            repo,
            "--run-id",
            run_id,
            "init",
            "--feature",
            "overwrite attempt",
            "--force",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_state_run_id_directory_mismatch_is_rejected(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        s = json.loads(state_path.read_text(encoding="utf-8"))
        dir_name = state_path.parent.name
        s["run_id"] = "tampered-does-not-match-dir"
        state_path.write_text(json.dumps(s), encoding="utf-8")
        result = self.run_controller(
            repo, "--run-id", dir_name, "set-phase", "--phase", "planning",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match its run directory name", result.stderr)

    def test_state_repository_id_mismatch_is_rejected(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        s = json.loads(state_path.read_text(encoding="utf-8"))
        run_id = s["run_id"]
        s["repository"]["id"] = "some-other-repo-id"
        state_path.write_text(json.dumps(s), encoding="utf-8")
        result = self.run_controller(
            repo, "--run-id", run_id, "set-phase", "--phase", "planning",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("records repository", result.stderr)

    def test_run_check_aborts_if_run_made_terminal_during_check(self) -> None:
        """A concurrent cancel while the verification command runs must prevent
        the check from being published (TOCTOU guard under the lock)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_dir = state_path.parent

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            mid = json.loads(state_path.read_text(encoding="utf-8"))
            mid["status"] = "cancelled"
            mid["phase"] = "cancelled"
            state_path.write_text(json.dumps(mid), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                name="unit-tests",
                command=["python3", "-c", "print(1)"],
                timeout=None,
                output="summary",
                failure_tail_lines=80,
            )
            with self.assertRaises((controller.WorkflowError, Exception)) as ctx:
                controller.cmd_run_check(args)
        finally:
            controller.run_process = original

        self.assertIn("terminal", str(ctx.exception).lower())
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final["status"], "cancelled")
        # The check was never published to state.
        self.assertEqual(final.get("verification", {}).get("checks", []), [])

    def test_concurrent_same_run_id_inits_cannot_both_create(self) -> None:
        """Two concurrent `init --run-id X` processes must not both materialize
        the same run: exactly one wins, the other fails closed without
        clobbering the survivor."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        run_id = "fixed-shared-run-id"

        def spawn() -> subprocess.Popen[str]:
            cmd = [
                "python3", str(CONTROLLER),
                "--project-root", str(repo),
                "--state-dir", str(state_home),
                "--run-id", run_id,
                "init", "--feature", "concurrent",
            ]
            return subprocess.Popen(
                cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

        p1 = spawn()
        p2 = spawn()
        out1 = p1.communicate()
        out2 = p2.communicate()
        codes = sorted([p1.returncode, p2.returncode])
        # Exactly one wins (0); the loser fails closed (non-zero).
        self.assertEqual(codes[0], 0, (out1, out2))
        self.assertNotEqual(codes[1], 0, (out1, out2))
        loser_err = out1[1] if p1.returncode != 0 else out2[1]
        # The loser may lose at either guard depending on timing: the pre-init
        # active-run check ("already exist") or the run-id overwrite protection
        # ("already exists"). Both are correct fail-closed outcomes.
        self.assertIn("already exist", loser_err)

        # Exactly one run materialized and it is intact + active.
        repo_info = resolve_repository(repo)
        runs = state_home / "repositories" / repo_info.id / "runs"
        dirs = [d for d in runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1)
        state = json.loads(
            (dirs[0] / "run-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["run_id"], run_id)

    def test_unknown_status_rejects_every_mutation(self) -> None:
        """An unrecognized status (corruption, partial write, or a status a
        future build understands but this one does not) must fail closed for
        every active-only mutation: 'not terminal' is not the same as 'active'.
        The persisted state must be byte-identical afterwards."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = self._set_status(state_path, "some-unknown-status")
        before = state_path.read_bytes()
        for op, argv in self._mutation_commands():
            with self.subTest(op=op):
                result = self.run_controller(
                    repo, "--run-id", run_id, *argv, state_home=state_home
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("requires an active run", result.stderr)
                self.assertIn(run_id, result.stderr)
                self.assertIn("some-unknown-status", result.stderr)
                self.assertEqual(state_path.read_bytes(), before)

    def test_missing_repository_id_rejects_mutation(self) -> None:
        """An external-layout run whose state omits repository.id (not merely a
        mismatched non-empty id) must be refused: an unbound run cannot be
        mutated under an assumed repository identity."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        s = json.loads(state_path.read_text(encoding="utf-8"))
        run_id = s["run_id"]
        s["repository"]["id"] = ""
        state_path.write_text(json.dumps(s), encoding="utf-8")
        before = state_path.read_bytes()
        result = self.run_controller(
            repo, "--run-id", run_id, "set-phase", "--phase", "planning",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not record a repository id", result.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_cancellation_during_accept_keeps_artifacts_and_state(self) -> None:
        """If the run is cancelled mid-accept (after artifacts are staged but
        before they are published under the lock), the canonical artifact and
        the run state must remain byte-identical and no staging files may be
        left behind. Exercises the staged-then-atomic-publish path."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_dir = state_path.parent

        # Pre-existing canonical accepted-spec.md whose bytes must not change.
        canonical_md = run_dir / "accepted-spec.md"
        canonical_md.write_text("ORIGINAL SPEC\n", encoding="utf-8")
        md_before = canonical_md.read_bytes()

        # The simulated external cancel writes this exact blob; the accept must
        # add nothing further, so the final state must equal it byte-for-byte.
        cancelled = json.loads(state_path.read_text(encoding="utf-8"))
        cancelled["status"] = "cancelled"
        cancelled["phase"] = "cancelled"
        cancelled_bytes = json.dumps(cancelled).encode("utf-8")

        new_spec = run_dir / "new-spec.md"
        new_spec.write_text("REPLACEMENT SPEC\n", encoding="utf-8")

        original_lock = controller.RunStateLock

        class FlipLock(original_lock):  # type: ignore[valid-type,misc]
            def __enter__(self_inner):  # noqa: N805
                entered = super().__enter__()
                state_path.write_bytes(cancelled_bytes)
                return entered

        controller.RunStateLock = FlipLock
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                kind="spec",
                file=str(new_spec),
                source=None,
                decisions=None,
            )
            with self.assertRaises(controller.WorkflowError) as ctx:
                controller.cmd_accept(args)
        finally:
            controller.RunStateLock = original_lock

        self.assertIn("terminal", str(ctx.exception).lower())
        # Canonical artifact untouched; staged temp files cleaned up.
        self.assertEqual(canonical_md.read_bytes(), md_before)
        self.assertEqual(
            list(run_dir.glob(".accepted-spec.md.*.tmp")), []
        )
        # State equals exactly what the external cancel wrote — accept added nothing.
        self.assertEqual(state_path.read_bytes(), cancelled_bytes)

    def test_concurrent_generated_id_inits_make_one_active_run(self) -> None:
        """Two concurrent `init` calls WITHOUT --run-id mint different generated
        IDs, so a per-run lock cannot serialize them. The repository-level init
        lock must still ensure exactly one active run is created; the loser
        fails closed because no --force authorizes an additional run."""
        repo = self.make_repo()
        state_home = self.make_state_home()

        def spawn() -> subprocess.Popen[str]:
            cmd = [
                "python3", str(CONTROLLER),
                "--project-root", str(repo),
                "--state-dir", str(state_home),
                "init", "--feature", "concurrent",
            ]
            return subprocess.Popen(
                cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

        p1 = spawn()
        p2 = spawn()
        out1 = p1.communicate()
        out2 = p2.communicate()
        codes = sorted([p1.returncode, p2.returncode])
        self.assertEqual(codes[0], 0, (out1, out2))
        self.assertNotEqual(codes[1], 0, (out1, out2))
        loser_err = out1[1] if p1.returncode != 0 else out2[1]
        self.assertIn("already exist", loser_err)

        repo_info = resolve_repository(repo)
        runs = state_home / "repositories" / repo_info.id / "runs"
        dirs = [d for d in runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, dirs)
        actives = find_active_runs(state_home, repo_info.id)
        self.assertEqual(len(actives), 1)

    def test_concurrent_generated_id_inits_via_portable_lock(self) -> None:
        """The same one-active-run invariant must hold on the Windows-compatible
        portable lock backend (atomic O_CREAT|O_EXCL lock file), not only on the
        POSIX fcntl path. Run in-process with the backend pinned so the portable
        implementation's real mutual exclusion is exercised."""
        repo = self.make_repo()
        state_home = self.make_state_home()

        def do_init() -> None:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                feature="concurrent-portable",
                label=None,
                mode="lean",
                max_review_rounds=3,
                reuse=False,
                force=False,
            )
            try:
                controller.cmd_init(args)
            except controller.WorkflowError:
                pass  # the loser fails closed; that is the expected outcome

        original_backend = CrossProcessLock.force_backend
        CrossProcessLock.force_backend = "portable"
        try:
            threads = [threading.Thread(target=do_init) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            CrossProcessLock.force_backend = original_backend

        repo_info = resolve_repository(repo)
        runs = state_home / "repositories" / repo_info.id / "runs"
        dirs = [d for d in runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, dirs)
        actives = find_active_runs(state_home, repo_info.id)
        self.assertEqual(len(actives), 1)

    def test_concurrent_force_inits_keep_metadata_consistent(self) -> None:
        """Two concurrent `init --force` processes (each authorized to add an
        independent run) must both succeed, leave the shared metadata.json as
        valid JSON, create intact active run states, and leave no staging temp
        files. Exercises metadata publication serialized inside RepoInitLock and
        the invocation-unique temp file in atomic_write_json."""
        repo = self.make_repo()
        state_home = self.make_state_home()

        def spawn() -> subprocess.Popen[str]:
            cmd = [
                "python3", str(CONTROLLER),
                "--project-root", str(repo),
                "--state-dir", str(state_home),
                "init", "--feature", "concurrent-force", "--force",
            ]
            return subprocess.Popen(
                cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

        p1 = spawn()
        p2 = spawn()
        out1 = p1.communicate()
        out2 = p2.communicate()
        # Both are authorized (the first finds no active run; the second finds
        # one but --force permits an additional independent run), so both win.
        self.assertEqual(p1.returncode, 0, out1)
        self.assertEqual(p2.returncode, 0, out2)

        repo_info = resolve_repository(repo)
        repo_dir = state_home / "repositories" / repo_info.id

        # metadata.json is intact, valid JSON.
        meta = json.loads((repo_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["id"], repo_info.id)
        self.assertIn("last_run_id", meta)

        # No staging temp file from the unique-temp atomic write was left behind.
        leftovers = list(repo_dir.rglob("*.tmp"))
        self.assertEqual(leftovers, [], leftovers)

        # Exactly two intact, active runs exist; each state file parses.
        dirs = [d for d in (repo_dir / "runs").iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 2, dirs)
        for d in dirs:
            s = json.loads((d / "run-state.json").read_text(encoding="utf-8"))
            self.assertEqual(s["status"], "active")
        self.assertEqual(len(find_active_runs(state_home, repo_info.id)), 2)

    def test_budget_exhaustion_does_not_overwrite_concurrent_cancel(self) -> None:
        """When the review budget is exhausted, cmd_codex flips the run to
        'blocked' under the lock. If a concurrent cancel makes the run terminal
        first, that re-check inside the lock must refuse to overwrite it, so the
        run stays byte-identical to the cancellation (no terminal-to-terminal
        rewrite)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_dir = state_path.parent

        # Drive the run to the exhaustion boundary and satisfy the pre-budget
        # review prerequisites (a plan artifact and a recorded check).
        s = json.loads(state_path.read_text(encoding="utf-8"))
        s["status"] = "active"
        s["phase"] = "review"
        s["review_round"] = 3
        s["max_review_rounds"] = 3
        s["verification"] = {
            "checks": [{"name": "t", "command": ["true"], "exit_code": 0}]
        }
        state_path.write_text(json.dumps(s), encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("PLAN\n", encoding="utf-8")

        # The simulated concurrent cancel writes exactly this blob; the budget
        # branch must add nothing further.
        cancelled = json.loads(state_path.read_text(encoding="utf-8"))
        cancelled["status"] = "cancelled"
        cancelled["phase"] = "cancelled"
        cancelled_bytes = json.dumps(cancelled).encode("utf-8")

        original_lock = controller.RunStateLock

        class FlipLock(original_lock):  # type: ignore[valid-type,misc]
            def __enter__(self_inner):  # noqa: N805
                entered = super().__enter__()
                state_path.write_bytes(cancelled_bytes)
                return entered

        controller.RunStateLock = FlipLock
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
                timeout=None,
            )
            with self.assertRaises(controller.WorkflowError) as ctx:
                controller.cmd_codex(args)
        finally:
            controller.RunStateLock = original_lock

        self.assertIn("terminal", str(ctx.exception).lower())
        # The run equals exactly the cancellation; exhaustion wrote nothing.
        self.assertEqual(state_path.read_bytes(), cancelled_bytes)

    def test_failed_codex_does_not_mutate_concurrently_cancelled_run(self) -> None:
        """A non-zero `codex exec` writes a phase error log and appends a note
        under the lock. If a concurrent cancel makes the run terminal while Codex
        ran, the failure handler must re-check status under the lock and leave the
        run byte-identical (no resurrected note, no published canonical log)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_dir = state_path.parent

        s = json.loads(state_path.read_text(encoding="utf-8"))
        s["status"] = "active"
        s["phase"] = "review"
        s["review_round"] = 0
        s["max_review_rounds"] = 3
        s["verification"] = {
            "checks": [{"name": "t", "command": ["true"], "exit_code": 0}]
        }
        state_path.write_text(json.dumps(s), encoding="utf-8")
        (run_dir / "accepted-spec.md").write_text("SPEC\n", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("PLAN\n", encoding="utf-8")

        cancelled = json.loads(state_path.read_text(encoding="utf-8"))
        cancelled["status"] = "cancelled"
        cancelled["phase"] = "cancelled"
        cancelled_bytes = json.dumps(cancelled).encode("utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        original_lock = controller.RunStateLock

        class FlipLock(original_lock):  # type: ignore[valid-type,misc]
            def __enter__(self_inner):  # noqa: N805
                entered = super().__enter__()
                state_path.write_bytes(cancelled_bytes)
                return entered

        original = controller.run_process
        controller.run_process = fake_run_process
        controller.RunStateLock = FlipLock
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
                timeout=None,
            )
            with self.assertRaises(controller.WorkflowError) as ctx:
                controller.cmd_codex(args)
        finally:
            controller.run_process = original
            controller.RunStateLock = original_lock

        msg = str(ctx.exception).lower()
        self.assertIn("no longer active", msg)
        # The run is byte-identical to the cancellation: no note was appended.
        self.assertEqual(state_path.read_bytes(), cancelled_bytes)
        # The canonical error log was not published, and no staging log leaked.
        self.assertFalse((run_dir / "review.codex.stderr.log").exists())
        self.assertFalse(list(run_dir.glob(".staging-*")))

    # --- lifecycle-transition identity invariants (cancel/block/archive-run) ---

    # cancel/block require an active source; archive-run requires a terminal one.
    # Identity is validated before the transition table, so each command is set
    # up in its own valid source status to prove that identity — not the
    # transition policy — is what refuses the tampered run.
    _TRANSITIONS = (
        ("cancel", ["cancel"], "active"),
        ("block", ["block", "--reason", "x"], "active"),
        ("archive-run", ["archive-run"], "complete"),
    )

    def test_lifecycle_transitions_reject_run_id_directory_mismatch(self) -> None:
        """cancel/block/archive-run mutate state, so they must enforce the same
        run-identity invariants as active mutations. A state whose run_id does
        not match its run directory name must be refused before any transition,
        leaving the persisted bytes unchanged."""
        for op, argv, status in self._TRANSITIONS:
            with self.subTest(op=op):
                repo = self.make_repo()
                state_home = self.make_state_home()
                state_path = self.init_run(repo, state_home)
                s = json.loads(state_path.read_text(encoding="utf-8"))
                dir_name = state_path.parent.name
                s["status"] = status
                s["phase"] = status
                s["run_id"] = "tampered-does-not-match-dir"
                state_path.write_text(json.dumps(s), encoding="utf-8")
                before = state_path.read_bytes()
                result = self.run_controller(
                    repo, "--run-id", dir_name, *argv, state_home=state_home
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(
                    "does not match its run directory name", result.stderr
                )
                self.assertEqual(state_path.read_bytes(), before)

    def test_lifecycle_transitions_reject_repository_id_violations(self) -> None:
        """A lifecycle transition on an external-layout run whose repository.id
        is missing (unbound) or mismatched (belongs to another repository) must
        fail closed before any transition, leaving the persisted bytes
        unchanged."""
        cases = (
            ("", "does not record a repository id"),
            ("some-other-repo-id", "records repository"),
        )
        for op, argv, status in self._TRANSITIONS:
            for repo_id, expected in cases:
                with self.subTest(op=op, repo_id=repo_id):
                    repo = self.make_repo()
                    state_home = self.make_state_home()
                    state_path = self.init_run(repo, state_home)
                    s = json.loads(state_path.read_text(encoding="utf-8"))
                    run_id = s["run_id"]
                    s["status"] = status
                    s["phase"] = status
                    s["repository"]["id"] = repo_id
                    state_path.write_text(json.dumps(s), encoding="utf-8")
                    before = state_path.read_bytes()
                    result = self.run_controller(
                        repo, "--run-id", run_id, *argv, state_home=state_home
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(expected, result.stderr)
                    self.assertEqual(state_path.read_bytes(), before)


class LegacyMigrationIntegrityTests(unittest.TestCase):
    """P0: `migrate-legacy-state` must be non-destructive, locked, and staged.

    Migration may create a new-format run from legacy state, but it must never
    overwrite an existing run (active or terminal), even with --force. A failed
    conversion must leave no partially built run at the canonical path.
    """

    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    def make_repo(self) -> Path:
        temp = Path(tempfile.mkdtemp())
        self._tmpdirs.append(temp)
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "Test User"], check=True
        )
        (temp / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "initial"], check=True)
        return temp

    def make_state_home(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self._tmpdirs.append(d)
        return d

    def run_controller(
        self, repo: Path, *args: str, state_home: Path
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            "python3", str(CONTROLLER),
            "--project-root", str(repo),
            "--state-dir", str(state_home),
            *args,
        ]
        return subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def write_legacy_state(self, repo: Path, run_id: str) -> Path:
        legacy_dir = repo / ".ai/autonomous-development"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_id": run_id,
                    "status": "active",
                    "phase": "implementation",
                }
            ),
            encoding="utf-8",
        )
        return legacy_dir

    def runs_root(self, repo: Path, state_home: Path) -> Path:
        repo_id = resolve_repository(repo).id
        return state_home / "repositories" / repo_id / "runs"

    def new_run_dir(self, repo: Path, state_home: Path, run_id: str) -> Path:
        return self.runs_root(repo, state_home) / run_id

    def init_run_with_id(
        self, repo: Path, state_home: Path, run_id: str
    ) -> Path:
        res = self.run_controller(
            repo, "--run-id", run_id, "init", "--feature", "F",
            state_home=state_home,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return self.new_run_dir(repo, state_home, run_id) / "run-state.json"

    # --- tests ---

    def test_migrate_creates_run_from_legacy_source(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        legacy_dir = self.write_legacy_state(repo, "legacy-run-aaa")
        legacy_before = (legacy_dir / "run-state.json").read_bytes()

        res = self.run_controller(repo, "migrate-legacy-state", state_home=state_home)
        self.assertEqual(res.returncode, 0, res.stderr)

        published = self.new_run_dir(repo, state_home, "legacy-run-aaa") / "run-state.json"
        self.assertTrue(published.exists())
        s = json.loads(published.read_text(encoding="utf-8"))
        self.assertEqual(s["run_id"], "legacy-run-aaa")
        self.assertEqual(s["schema_version"], 2)
        self.assertEqual(s["migrated_from"], str(legacy_dir))
        # Original legacy state is untouched.
        self.assertEqual((legacy_dir / "run-state.json").read_bytes(), legacy_before)
        # No staging temp dir survives.
        self.assertEqual(
            list(self.runs_root(repo, state_home).glob(".migrate-*.tmp")), []
        )

    def test_migrate_is_idempotent_for_same_source(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.write_legacy_state(repo, "legacy-run-bbb")

        first = self.run_controller(repo, "migrate-legacy-state", state_home=state_home)
        self.assertEqual(first.returncode, 0, first.stderr)
        published = self.new_run_dir(repo, state_home, "legacy-run-bbb") / "run-state.json"
        after_first = published.read_bytes()

        second = self.run_controller(repo, "migrate-legacy-state", state_home=state_home)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already migrated", second.stdout)
        # A re-run of the same source rewrites nothing.
        self.assertEqual(published.read_bytes(), after_first)

    def test_migrate_refuses_to_overwrite_existing_run(self) -> None:
        """An init-created run occupying the legacy run_id must not be
        overwritten, with or without --force; its bytes stay identical."""
        for force in (False, True):
            with self.subTest(force=force):
                repo = self.make_repo()
                state_home = self.make_state_home()
                existing = self.init_run_with_id(repo, state_home, "collide-1")
                before = existing.read_bytes()
                self.write_legacy_state(repo, "collide-1")

                argv = ["migrate-legacy-state"]
                if force:
                    argv.append("--force")
                res = self.run_controller(repo, *argv, state_home=state_home)
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("will not be overwritten", res.stderr)
                self.assertEqual(existing.read_bytes(), before)

    def test_migrate_force_cannot_overwrite_terminal_run(self) -> None:
        """Every terminal target must remain byte-identical even under --force."""
        for status in TERMINAL_STATUSES:
            with self.subTest(status=status):
                repo = self.make_repo()
                state_home = self.make_state_home()
                existing = self.init_run_with_id(repo, state_home, "collide-term")
                s = json.loads(existing.read_text(encoding="utf-8"))
                s["status"] = status
                s["phase"] = status
                existing.write_text(json.dumps(s), encoding="utf-8")
                before = existing.read_bytes()
                self.write_legacy_state(repo, "collide-term")

                res = self.run_controller(
                    repo, "migrate-legacy-state", "--force", state_home=state_home
                )
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertEqual(existing.read_bytes(), before)

    def test_target_run_id_migrates_into_fresh_run(self) -> None:
        """--target-run-id lets an occupied default target be sidestepped: the
        migration lands at the fresh id and the existing run is untouched."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        existing = self.init_run_with_id(repo, state_home, "collide-2")
        before = existing.read_bytes()
        self.write_legacy_state(repo, "collide-2")

        res = self.run_controller(
            repo, "migrate-legacy-state", "--target-run-id", "migrated-fresh",
            state_home=state_home,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        fresh = self.new_run_dir(repo, state_home, "migrated-fresh") / "run-state.json"
        self.assertTrue(fresh.exists())
        self.assertEqual(
            json.loads(fresh.read_text(encoding="utf-8"))["run_id"], "migrated-fresh"
        )
        self.assertEqual(existing.read_bytes(), before)

    def test_failed_conversion_leaves_no_partial_run(self) -> None:
        """If conversion raises mid-migration, no run may appear at the canonical
        path and no staging temp dir may survive."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.write_legacy_state(repo, "legacy-run-ccc")
        target = self.new_run_dir(repo, state_home, "legacy-run-ccc")

        original = controller.migrate_v1_to_v2

        def boom(*a, **k):
            raise RuntimeError("conversion failed")

        controller.migrate_v1_to_v2 = boom
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                target_run_id=None,
                force=False,
            )
            with self.assertRaises(RuntimeError):
                controller.cmd_migrate_legacy_state(args)
        finally:
            controller.migrate_v1_to_v2 = original

        self.assertFalse(target.exists())
        self.assertEqual(
            list(self.runs_root(repo, state_home).glob(".migrate-*.tmp")), []
        )

    def test_concurrent_migration_and_init_stay_consistent(self) -> None:
        """A migration and an `init --force` racing on the same repository are
        serialized by the repository init lock: both publish intact runs, the
        shared metadata stays valid JSON, and no staging temp dirs survive."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.write_legacy_state(repo, "legacy-race")

        def spawn(argv: list[str]) -> subprocess.Popen[str]:
            cmd = [
                "python3", str(CONTROLLER),
                "--project-root", str(repo),
                "--state-dir", str(state_home),
                *argv,
            ]
            return subprocess.Popen(
                cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

        p1 = spawn(["migrate-legacy-state"])
        p2 = spawn(["init", "--feature", "race", "--force"])
        out1 = p1.communicate()
        out2 = p2.communicate()
        self.assertEqual(p1.returncode, 0, out1)
        self.assertEqual(p2.returncode, 0, out2)

        repo_id = resolve_repository(repo).id
        repo_dir = state_home / "repositories" / repo_id
        meta = json.loads((repo_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["id"], repo_id)

        runs = repo_dir / "runs"
        self.assertEqual(list(runs.glob(".migrate-*.tmp")), [])
        self.assertTrue((runs / "legacy-race" / "run-state.json").exists())
        for d in [d for d in runs.iterdir() if d.is_dir()]:
            json.loads((d / "run-state.json").read_text(encoding="utf-8"))


class EvidencePreservingReviewTests(unittest.TestCase):
    """W3: the cumulative review ledger preserves full evidence inline and the
    acceptance-criteria ledger is cumulative."""

    @staticmethod
    def _finding(fid: str, severity: str = "high", **over: object) -> dict:
        finding = {
            "id": fid,
            "severity": severity,
            "category": "security",
            "file": f"{fid}.py",
            "line_start": 3,
            "description": f"{fid} description",
            "evidence": f"{fid} evidence",
            "recommended_fix": f"{fid} fix",
        }
        finding.update(over)
        return finding

    _EVIDENCE_KEYS = ("file", "line_start", "description", "evidence", "recommended_fix")

    def _assert_evidence(self, entry: dict, source: dict) -> None:
        for key in self._EVIDENCE_KEYS:
            self.assertEqual(entry[key], source[key], key)

    def test_full_review_merge_preserves_evidence(self) -> None:
        state: dict = {"cumulative_findings": []}
        src = self._finding("F-1")
        controller.merge_full_review(state, {"findings": [src]}, 1)
        entry = state["cumulative_findings"][0]
        self._assert_evidence(entry, src)
        self.assertEqual(entry["origin"], "full")
        self.assertEqual(entry["status"], "open")

    def test_delta_merge_preserves_evidence_and_origin(self) -> None:
        state: dict = {"cumulative_findings": [], "reviews": [{"delta": False}]}
        controller.merge_full_review(state, {"findings": [self._finding("F-1")]}, 1)
        new = self._finding("F-2", severity="critical")
        regr = self._finding("F-3", severity="high")
        controller.merge_delta_review(
            state,
            {
                "resolved_findings": ["F-1"],
                "new_findings": [new],
                "regressions": [regr],
            },
            2,
        )
        by_id = {f["id"]: f for f in state["cumulative_findings"]}
        self._assert_evidence(by_id["F-2"], new)
        self.assertEqual(by_id["F-2"]["origin"], "delta")
        self._assert_evidence(by_id["F-3"], regr)
        self.assertEqual(by_id["F-3"]["origin"], "regression")
        # A carried-forward finding keeps its evidence after being resolved.
        self.assertEqual(by_id["F-1"]["status"], "resolved")
        self.assertEqual(by_id["F-1"]["evidence"], "F-1 evidence")

    def test_full_remap_collision_preserves_evidence_and_source_id(self) -> None:
        state: dict = {"cumulative_findings": []}
        a = self._finding("F-1", evidence="first")
        b = self._finding("F-1", evidence="second")
        controller.merge_full_review(state, {"findings": [a, b]}, 1)
        findings = state["cumulative_findings"]
        self.assertEqual(len(findings), 2)
        remapped = [f for f in findings if f.get("source_id") == "F-1"][0]
        self.assertNotEqual(remapped["id"], "F-1")
        self.assertEqual(remapped["evidence"], "second")
        self.assertEqual(remapped["origin"], "full")

    def test_delta_remap_collision_preserves_evidence_and_source_id(self) -> None:
        state: dict = {"cumulative_findings": [], "reviews": [{"delta": False}]}
        controller.merge_full_review(state, {"findings": [self._finding("F-1")]}, 1)
        clash = self._finding("F-1", evidence="delta-clash")
        controller.merge_delta_review(state, {"new_findings": [clash]}, 2)
        remapped = [
            f for f in state["cumulative_findings"] if f.get("source_id") == "F-1"
        ][0]
        self.assertNotEqual(remapped["id"], "F-1")
        self.assertEqual(remapped["evidence"], "delta-clash")
        self.assertEqual(remapped["origin"], "delta")

    def test_legacy_normalization_is_idempotent(self) -> None:
        state: dict = {
            "cumulative_findings": [
                {"id": "F-1", "severity": "high", "status": "open"}
            ],
            "reviews": [{"delta": False}],
        }
        controller.merge_delta_review(state, {"new_findings": []}, 2)
        entry = {f["id"]: f for f in state["cumulative_findings"]}["F-1"]
        self.assertEqual(entry["origin"], "legacy")
        self.assertIsNone(entry["file"])
        self.assertIsNone(entry["line_start"])
        self.assertEqual(entry["description"], "")
        self.assertEqual(entry["evidence"], "")
        self.assertEqual(entry["recommended_fix"], "")
        # A second pass leaves the canonical shape unchanged.
        before = json.dumps(state["cumulative_findings"], sort_keys=True)
        controller.merge_delta_review(state, {"new_findings": []}, 3)
        after = {f["id"]: f for f in state["cumulative_findings"]}["F-1"]
        self.assertEqual(after["origin"], "legacy")
        self.assertEqual(after["evidence"], "")
        self.assertIn('"origin": "legacy"', before)

    def test_render_open_findings_includes_evidence(self) -> None:
        state: dict = {"cumulative_findings": []}
        controller.merge_full_review(state, {"findings": [self._finding("F-1")]}, 1)
        rendered = controller.render_open_findings(state)
        parsed = json.loads(rendered)
        self.assertEqual(parsed[0]["file"], "F-1.py")
        self.assertEqual(parsed[0]["evidence"], "F-1 evidence")
        self.assertEqual(parsed[0]["recommended_fix"], "F-1 fix")
        self.assertEqual(parsed[0]["description"], "F-1 description")

    def test_acceptance_criteria_ledger_seed_and_delta_update(self) -> None:
        state: dict = {"cumulative_acceptance_criteria": []}
        controller.merge_acceptance_criteria(
            state,
            {
                "acceptance_criteria_assessment": [
                    {"id": "AC-1", "status": "satisfied", "evidence": "ev1"},
                    {"id": "AC-2", "status": "not_satisfied", "evidence": "ev2"},
                ]
            },
            1,
        )
        controller.merge_acceptance_criteria(
            state,
            {
                "affected_acceptance_criteria": [
                    {"id": "AC-2", "status": "satisfied", "evidence": "ev2b"}
                ]
            },
            2,
        )
        by_id = {c["id"]: c for c in state["cumulative_acceptance_criteria"]}
        self.assertEqual(by_id["AC-1"]["status"], "satisfied")
        self.assertEqual(by_id["AC-1"]["round"], 1)
        self.assertEqual(by_id["AC-2"]["status"], "satisfied")
        self.assertEqual(by_id["AC-2"]["evidence"], "ev2b")
        self.assertEqual(by_id["AC-2"]["round"], 2)

    def test_describe_blocking_findings_lists_ids(self) -> None:
        state: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            state,
            {
                "findings": [
                    self._finding("F-1", severity="critical"),
                    self._finding("F-2", severity="high"),
                ]
            },
            1,
        )
        severe = controller.cumulative_unresolved_severe(state)
        described = controller._describe_blocking_findings(severe)
        self.assertIn("F-1", described)
        self.assertIn("critical", described)
        self.assertIn("F-2", described)
        # block/pass detection itself is unchanged: both are still severe+open.
        self.assertEqual(len(severe), 2)

    def test_valid_resolution_records_round_and_source(self) -> None:
        state: dict = {"cumulative_findings": []}
        controller.merge_full_review(state, {"findings": [self._finding("F-1")]}, 1)
        controller.merge_delta_review(
            state, {"resolved_findings": ["F-1"], "new_findings": []}, 2
        )
        entry = {f["id"]: f for f in state["cumulative_findings"]}["F-1"]
        self.assertEqual(entry["status"], "resolved")
        self.assertEqual(entry["resolved_at_round"], 2)
        self.assertEqual(entry["resolution_source"], "review-02")

    def test_resolving_unknown_finding_fails_closed(self) -> None:
        state: dict = {"cumulative_findings": []}
        controller.merge_full_review(state, {"findings": [self._finding("F-1")]}, 1)
        with self.assertRaises(controller.WorkflowError):
            controller.merge_delta_review(
                state, {"resolved_findings": ["F-9"], "new_findings": []}, 2
            )
        # The ledger is untouched: F-1 is still open and blocking.
        entry = {f["id"]: f for f in state["cumulative_findings"]}["F-1"]
        self.assertEqual(entry["status"], "open")

    def test_resolving_same_finding_twice_fails_closed(self) -> None:
        state: dict = {"cumulative_findings": []}
        controller.merge_full_review(state, {"findings": [self._finding("F-1")]}, 1)
        with self.assertRaises(controller.WorkflowError):
            controller.merge_delta_review(
                state,
                {"resolved_findings": ["F-1", "F-1"], "new_findings": []},
                2,
            )

    def test_blocking_acceptance_criteria_flags_all_unsatisfied(self) -> None:
        # Only `satisfied` is non-blocking (fail closed): not_satisfied,
        # partially_satisfied and not_verifiable all block completion.
        state: dict = {
            "cumulative_acceptance_criteria": [
                {"id": "AC-1", "status": "satisfied"},
                {"id": "AC-2", "status": "partially_satisfied"},
                {"id": "AC-3", "status": "not_satisfied"},
                {"id": "AC-4", "status": "not_verifiable"},
            ]
        }
        blocking = controller.blocking_acceptance_criteria(state)
        blocked_ids = {c["id"] for c in blocking}
        self.assertEqual(blocked_ids, {"AC-2", "AC-3", "AC-4"})


if __name__ == "__main__":
    unittest.main()
