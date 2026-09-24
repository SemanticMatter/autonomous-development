---
name: enhance-idea
description: Use a fresh read-only Codex execution to turn a rough feature idea into a structured repository-grounded specification with requirements, acceptance criteria, assumptions, risks, and non-goals.
argument-hint: "[feature idea]"
disable-model-invocation: true
allowed-tools: Read Grep Glob Bash(git *) Bash(python3 *) Bash(codex *)
disallowed-tools: AskUserQuestion
---

# Enhance a feature idea

1. Inspect repository instructions, architecture, status, and relevant implementation files.
2. Initialize or reuse the local workflow:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init --reuse --feature "$ARGUMENTS"
```

`--reuse` adopts only a compatible active feature run. To continue a current-checkout run, add
`--worktree-mode current` and run from that checkout.

3. Run the structured read-only Codex phase:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase enhance
```

4. Read the Codex output. Its path is printed by the controller; you can also find it via
   `controller.py status --json` under `artifacts.enhance`.
5. Present a concise assessment of grounded requirements, assumptions, risky scope expansion, and blocking questions. Do not implement code in this skill.
