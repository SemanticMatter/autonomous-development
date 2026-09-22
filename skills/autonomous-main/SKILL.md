---
name: autonomous-main
description: Autonomously develop a repository feature directly in the current main/master checkout. Use only when the user has explicitly authorized direct edits on main/master. Same workflow as autonomous-current but passes --allow-main to bypass the main/master refusal. Still requires a clean working tree and never commits.
argument-hint: "[feature idea]"
disable-model-invocation: true
effort: high
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - LSP
  - Agent
  - Bash(git *)
  - Bash(python3 *)
  - Bash(codex *)
disallowed-tools:
  - AskUserQuestion
  - EnterWorktree
  - ExitWorktree
hooks:
  Stop:
    - hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/stop_gate.py"'
          timeout: 10
---

# Autonomous feature development — current checkout on main

Implement this feature idea directly in the user's current checkout, including when the
current branch is `main` or `master`:

> $ARGUMENTS

This is the explicit-opt-in variant of `autonomous-current`. By invoking this skill the user has
acknowledged that the agent's edits will land directly in `main`/`master`. Prefer
`autonomous-feature` (isolated worktree) or `autonomous-current` (clean feature branch) when
either is appropriate; this skill exists for cases where the user really does want changes in
the current `main`/`master` checkout.

## Non-negotiable boundaries

- This skill must NOT call `EnterWorktree` / `ExitWorktree`. All edits land in the current
  checkout. Do not create or enter `.claude/worktrees/*`.
- Require a clean working tree. `git status --porcelain` must be empty before initializing. If
  it is not, stop and report which entries are dirty.
- Do not create commits. The user will review and commit with their normal `git diff`/commit
  flow.
- Preserve unrelated user changes.
- Never push, merge, publish, deploy, rotate credentials, or modify remote infrastructure.
- Never use `danger-full-access`, `--yolo`, `bypassPermissions`, or equivalent unrestricted modes.
- Never apply an irreversible database migration or delete user data.
- Do not weaken authorization, validation, tests, or static checks to make the workflow pass.
- Codex planning and review executions must remain read-only.
- Use no more than the configured review-round budget.
- Treat every Codex finding as a proposal requiring evidence-based triage.

## Driver loop

1. Confirm this is a Git repository. Inspect `CLAUDE.md`, repository instructions, architecture,
   status, and tests. Verify the working tree is clean before initializing.

2. Initialize in current-checkout mode on main/master:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" doctor
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init \
     --feature "$ARGUMENTS" \
     --mode standard \
     --worktree-mode current \
     --allow-main
   ```

   `init` prints the `run-state.json` path and run ID. With multiple concurrent runs, pass
   `--run-id <run-id>` to all subsequent commands. If `doctor` reports a missing prerequisite,
   mark the run blocked and report it rather than bypassing it.

3. Repeatedly ask the controller for the next phase, then execute it:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" next-action --json
   ```

   The response gives `phase`, `required_action`, `completion_condition`, and `references`.
   Read the referenced file under `${CLAUDE_PLUGIN_ROOT}/skills/autonomous-feature/references/`
   for that phase and follow it until the completion condition holds.

4. Do not declare success until `evaluate` succeeds:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" evaluate
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" usage-report
   ```

## Final report

- the implemented behavior;
- principal files changed (visible to the user via plain `git diff`);
- verification commands and results;
- Codex review rounds and disposition of findings;
- adversarial review result when one was required;
- per-phase usage table from `usage-report`;
- remaining risks or explicit blocked reason;
- a suggested conventional commit message (the user commits manually — this skill must not
  commit).
