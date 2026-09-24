#!/usr/bin/env python3
"""Skill-scoped Stop hook that keeps a bounded autonomous feature run moving.

The hook is attached only by the feature-workflow skills. It acts on at most one
run: the single active feature run pinned to the worktree the hook runs in. It
never selects, blocks, or mutates an existing-PR review run, a run of an unknown
workflow kind, a run pinned to another worktree, a run of another repository, or
a terminal run; with no applicable run, or several, it exits without blocking.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import (
    WORKFLOW_KIND_FEATURE,
    RepoInfo,
    RunStateLock,
    StateError,
    feature_origin_worktree,
    find_active_runs,
    load_run_state,
    require_active_run_state,
    resolve_repository,
    resolve_state_home,
    run_workflow_kind,
    save_run_state,
    detect_legacy_state,
    LEGACY_STATE_FILE_NAME,
)

MAX_GATE_BLOCKS = 3


def reason_for(state: dict, run_dir: Path) -> str:
    """Return a human-readable reason to continue the workflow."""
    if not (run_dir / "accepted-spec.md").exists():
        return (
            "Continue the autonomous workflow: reconcile the Codex proposal "
            "and create accepted-spec.md."
        )
    if not (run_dir / "accepted-plan.md").exists():
        return (
            "Continue the autonomous workflow: reconcile the Codex plan "
            "and create accepted-plan.md."
        )
    all_checks = state.get("verification", {}).get("checks", [])
    latest: dict[str, dict] = {}
    for check in all_checks:
        latest[str(check.get("name", "unnamed"))] = check
    checks = list(latest.values())
    if not checks:
        return "Continue the autonomous workflow: run and record relevant verification checks."
    if any(check.get("exit_code") != 0 for check in checks):
        return (
            "Continue the autonomous workflow: fix the failing verification checks "
            "and rerun them."
        )
    reviews = state.get("reviews", [])
    if not reviews:
        return (
            "Continue the autonomous workflow: run the independent Codex code review."
        )
    if reviews[-1].get("verdict") != "pass":
        return (
            "Continue the autonomous workflow: triage the latest Codex findings, "
            "fix valid issues, verify, and re-review."
        )
    if state.get("risk", {}).get("requires_adversarial_review"):
        adversarial = state.get("adversarial_reviews", [])
        if not adversarial or adversarial[-1].get("verdict") != "pass":
            return (
                "Continue the autonomous workflow: complete the required adversarial review "
                "and address valid risks."
            )
    return "Run the controller completion-gate evaluation and provide the final implementation report."


def is_applicable_feature_run(state: dict, repo: RepoInfo) -> bool:
    """Whether the hook may act on `state` from the worktree of `repo`.

    Only an active feature run (per `run_workflow_kind`) that records this
    repository's id and is pinned to this exact worktree applies.
    """
    repo_block = state.get("repository")
    return (
        run_workflow_kind(state) == WORKFLOW_KIND_FEATURE
        and state.get("status") == "active"
        and isinstance(repo_block, dict)
        and repo_block.get("id") == repo.id
        and feature_origin_worktree(state) == str(repo.worktree_path)
    )


def _block_and_exit(run_dir: Path, repo: RepoInfo | None) -> int:
    """Atomically increment stop_gate_blocks and persist; print block JSON or exhaust. Return 0.

    `repo` re-checks applicability under the lock; it is None only for the
    legacy in-repository layout, whose location already binds it to this
    worktree.
    """
    with RunStateLock(run_dir):
        try:
            state = load_run_state(run_dir, required=True)
        except Exception:
            return 0
        if run_workflow_kind(state) != WORKFLOW_KIND_FEATURE:
            return 0
        if repo is not None and not is_applicable_feature_run(state, repo):
            return 0
        # Require exactly "active" before mutating anything. An unknown, missing,
        # or non-string status (corruption, a partial write, or a status a future
        # build understands but this one does not) is NOT merely "not terminal";
        # the automatic Stop hook must fail safe and leave such state untouched
        # rather than incrementing the counter or flipping it to "blocked".
        try:
            require_active_run_state(state, str(state.get("run_id", "")), "block")
        except StateError:
            return 0
        blocks = int(state.get("stop_gate_blocks", 0))
        if blocks >= MAX_GATE_BLOCKS:
            state["status"] = "blocked"
            state["phase"] = "stop-gate-budget-exhausted"
            state.setdefault("notes", []).append(
                "The bounded Stop hook retry budget was exhausted; inspect manually."
            )
            save_run_state(run_dir, state)
            return 0
        state["stop_gate_blocks"] = blocks + 1
        reason = reason_for(state, run_dir)
        save_run_state(run_dir, state)
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()

    # Step 1: resolve repository — if not in a git repo, nothing to do
    try:
        repo = resolve_repository(cwd)
    except StateError:
        return 0
    except Exception:
        return 0

    # Step 2: resolve state home (env or XDG default; no CLI arg available here)
    try:
        state_home = resolve_state_home(None)
    except Exception:
        return 0

    # Step 3: find active runs
    try:
        active = find_active_runs(state_home, repo.id)
    except Exception:
        return 0

    if len(active) == 0:
        # Fall back to legacy state in repo root
        legacy_dir = detect_legacy_state(repo.canonical_root)
        if legacy_dir is None:
            return 0
        legacy_path = legacy_dir / LEGACY_STATE_FILE_NAME
        try:
            state = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if not isinstance(state, dict) or state.get("status") != "active":
            return 0
        if run_workflow_kind(state) != WORKFLOW_KIND_FEATURE:
            return 0
        return _block_and_exit(legacy_dir, None)

    # Step 4: act only on the active feature run pinned to this worktree
    applicable = [r for r in active if is_applicable_feature_run(r.state, repo)]
    if not applicable:
        return 0
    if len(applicable) > 1:
        ids = ", ".join(r.run_id for r in applicable)
        print(
            f"autonomous-development stop-gate: multiple active feature runs in "
            f"this worktree ({ids}); cannot auto-select — resolve manually.",
            file=sys.stderr,
        )
        return 0

    # Step 5: enforce bounded block counter
    return _block_and_exit(applicable[0].run_dir, repo)


if __name__ == "__main__":
    raise SystemExit(main())
