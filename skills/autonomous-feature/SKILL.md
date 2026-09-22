---
name: autonomous-feature
description: Autonomously develop a repository feature from a high-level idea. Codex independently enhances the idea, proposes a detailed plan, and reviews the implementation while Claude reconciles requirements, implements, verifies, triages findings, and fixes valid issues. Use when the user delegates an end-to-end feature change.
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
  - EnterWorktree
  - ExitWorktree
  - Bash(git *)
  - Bash(python3 *)
  - Bash(codex *)
disallowed-tools:
  - AskUserQuestion
hooks:
  Stop:
    - hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/stop_gate.py"'
          timeout: 10
---

# Autonomous feature development

Implement this feature idea:

> $ARGUMENTS

Use ultrathink for architecture, compatibility, and review triage. Operate as a state-machine
driver: ask the controller what to do next, execute that phase, repeat. Detailed per-phase guidance
lives in `references/` and is loaded only when a phase needs it.

## Non-negotiable boundaries

- Preserve unrelated user changes.
- Never push, merge, publish, deploy, rotate credentials, or modify remote infrastructure.
- Never use `danger-full-access`, `--yolo`, `bypassPermissions`, or equivalent unrestricted modes.
- Never apply an irreversible database migration or delete user data.
- Do not weaken authorization, validation, tests, or static checks to make the workflow pass.
- Codex planning and review executions must remain read-only.
- Use no more than the configured review-round budget.
- Treat every Codex finding as a proposal requiring evidence-based triage.
- Do not create commits unless the user explicitly requested them.

## Driver loop

1. Confirm this is a Git repository. Inspect `CLAUDE.md`, repository instructions, architecture,
   status, and tests. Use `EnterWorktree` for an isolated worktree whenever available — mandatory
   when the starting worktree has uncommitted changes. If the user explicitly asks for
   current-checkout mode, stay in the current branch instead of entering a worktree, and require a
   clean feature branch before proceeding.
2. Initialize:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" doctor
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init --feature "$ARGUMENTS" --mode auto --worktree-mode isolated
   ```

   `init` prints the `run-state.json` path and run ID. With multiple concurrent runs, pass
   `--run-id <run-id>` to all subsequent commands. If `doctor` reports a missing prerequisite, mark
   the run blocked and report it rather than bypassing it.

   For current-checkout mode, do not call `EnterWorktree`. Instead, keep the current branch checked
   out and run:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init --feature "$ARGUMENTS" --mode auto --worktree-mode current
   ```

   The controller refuses `main`/`master` unless the user also passes `--allow-main`, and it
   refuses a dirty tree in current-checkout mode.

3. Repeatedly ask the controller for the next phase, then execute it:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" next-action --json
   ```

   The response gives `phase`, `required_action`, `completion_condition`, and `references`. Read the
   referenced file under `references/` for that phase and follow it until the completion condition
   holds. Phase references:

   - `references/specification.md` — produce the accepted spec.
   - `references/planning.md` — produce the accepted plan and set the risk gate.
   - `references/implementation.md` — implement the plan.
   - `references/verification.md` — run and record checks.
   - `references/review.md` — Codex review, triage, and adversarial review.

4. Do not declare success until `evaluate` succeeds:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" evaluate
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" usage-report
   ```

## Final report

- the implemented behavior;
- principal files changed;
- verification commands and results;
- Codex review rounds and disposition of findings;
- adversarial review result when required;
- per-phase usage table from `usage-report`;
- remaining risks or explicit blocked reason;
- a suggested conventional commit message.
