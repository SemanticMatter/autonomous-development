---
name: adversarial-review
description: Run a fresh read-only Codex challenge review for authentication, authorization, persistence, migration, concurrency, retry, data-loss, privacy, or external-service risks.
disable-model-invocation: true
effort: max
allowed-tools: Read Grep Glob Bash(git *) Bash(python3 *) Bash(codex *)
disallowed-tools: AskUserQuestion Edit Write
---

# Adversarial review

Use for high-risk changes after ordinary verification and code review.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" set-risk --require-adversarial --reason "$ARGUMENTS"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase adversarial
```

Read the generated `adversarial-NN.codex.json`. Distinguish concrete failure scenarios from speculation and identify the smallest evidence-backed mitigations. Do not edit product files in this skill.

Every round's `threats` merge into a cumulative ledger (`T-<n>` ids) that the completion gate checks unconditionally — independently of `verdict`, and independently of whether risk classification required adversarial review in the first place: an unresolved critical/high threat blocks completion even if the payload reports `verdict: pass`, and even on a run this skill was invoked for manually rather than because `requires_adversarial_review` was set. A threat is released only by `triage` (same mechanism as review findings — a severe threat needs a recorded rationale to close). An exact re-report of an already-released threat in a later round is recognized as "still released, seen again" rather than reopened under a fresh id (dedup matches across all statuses); re-blocking a released threat needs an explicit `triage` entry, not just a later scan finding it again.

If a later round reports an already-released threat again, byte-identical, its status is NOT changed automatically — but it is recorded (`reseen_after_release_round`) and surfaces three ways: in the prompt's `PRIOR_THREATS` section as `RELEASED-BUT-RESEEN`, as its own completion-gate blocker for critical/high severity until you re-triage it (confirm the release still holds, or reopen it if the regression is real), and in `next-action`'s guidance, which names re-triage directly rather than recommending another adversarial-review round (re-running the scan alone cannot clear this state). This applies to both triage dispositions: `already_resolved` (a fact about the code a re-report can falsify) and `rejected_with_evidence` (a judgment call) alike.

A PR touching only plugin configuration — `skills/*/SKILL.md`, `prompts/*.md`, `agents/*.md`, or a root/nested instruction-policy file (`AGENTS.md`/`CLAUDE.md`/`CONTRIBUTING.md`) — always sets `requires_adversarial_review` (category `plugin/reviewer-config`), regardless of the file's prose content: these files define agent behavior, granted tools, or (for `SKILL.md`) the read-only boundary this workflow is built around. A changed `.gitattributes` sets the same category, because it controls how git presents the PR's own content to the reviewer. The category still fires when a config file is renamed away (both the rename's source and destination path are classified, not only the destination) or lives under a path containing non-ASCII bytes, a tab, a quote, a backslash or a newline (path reads are NUL-delimited with quoting disabled, so paths are classified by their literal string). Content classifiers are likewise pinned with `--text`, so an in-tree `.gitattributes` setting `-diff` cannot blank out a file's content and leave nothing to match. Otherwise each of these would let the gate be evaded through how a change is renamed, encoded or presented rather than through its content.
