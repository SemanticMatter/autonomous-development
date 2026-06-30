# Changelog

## Unreleased

### Added
- Local marketplace manifest (`.claude-plugin/marketplace.json`) so the plugin can
  be registered and installed through Claude Code's `/plugin` UI (e.g. in the VS
  Code extension) with `claude plugin marketplace add <path>` followed by
  `claude plugin install autonomous-development@autonomous-development`, instead of
  only the ephemeral `claude --plugin-dir` flag.
- `doctor` now detects Codex's configured authentication method from
  `~/.codex/config.toml` (CODEX_HOME-aware). API-key providers — including custom
  ones such as Azure / MS Foundry (`preferred_auth_method = "apikey"`) — are
  recognised and verified by checking that the provider's `env_key` (e.g.
  `AZURE_OPENAI_API_KEY`) is exported, rather than incorrectly demanding a
  ChatGPT/OpenAI `codex login`. The ChatGPT-login path is unchanged for the
  built-in `openai` provider.
- Evidence-preserving cumulative review ledger: each entry in `cumulative_findings`
  now stores the full review evidence inline (`file`, `line_start`, `description`,
  `evidence`, `recommended_fix`) plus an `origin` provenance tag
  (`full`/`delta`/`regression`/`legacy`), so the ledger is self-contained — the
  completion gate, the audit trail, and the delta reviewer no longer need to
  re-open the raw `review-NN.codex.json`. Legacy entries are normalized to this
  shape on the next merge (idempotent backfill with null/"" defaults)
- Cumulative acceptance-criteria ledger (`cumulative_acceptance_criteria`): the full
  review's `acceptance_criteria_assessment` and each delta's
  `affected_acceptance_criteria` are merged into an id-keyed ledger keeping the
  latest `{id, status, evidence, round}` per criterion. Surfaced to the delta
  reviewer via a new `ACCEPTANCE CRITERIA (cumulative)` section in
  `prompts/code-review-delta.md`
- Per-round review checkpoints: each recorded review now stores a `checkpoint`
  (head commit, branch, baseline, the feature `changed_paths`, and per-path content
  fingerprints). Subsequent rounds compute the paths that changed since the previous
  checkpoint and pass them to the delta reviewer via `CHANGED SINCE THE PREVIOUS
  REVIEW`. An exact review-to-review patch is not reconstructed; the delta reviewer
  reviews the full current diff against the baseline focusing on the changed paths
  (`focused_full_fallback`), and the prompt states the exact patch is unavailable
- Finding-resolution provenance: a resolved cumulative finding records
  `resolved_at_round` and `resolution_source` (the resolving review round)
- Delta-review prompt now lists each open finding with its full evidence (not just
  `id`/`severity`), so a prior finding can be resolved or carried from the prompt
  alone
- Gate failure reasons now name the blocking findings (id, severity, category, and a
  short description snippet) instead of only counting them
- Token instrumentation: `codex` phases run with `codex exec --json`, retain the NDJSON
  event stream (`*.events.ndjson`), and record a per-phase usage block
  (`prompt_characters`, `output_characters`, `duration_seconds`, `model`,
  `reasoning_effort`, `verbosity`) in `codex_runs`
- `usage-report` command: per-phase prompt/output/duration table (`--json` for raw records)
- `next-action` command: machine-readable phase guidance (`phase`, `required_action`,
  `completion_condition`, `references`) so the main skill drives a state machine
- `triage` command: merge a JSON finding ledger (`fingerprint`/`status`/`reason`) so later
  review rounds do not re-raise rejected findings
- `init --mode {auto,lean,standard,rigorous}`: adaptive workflow depth. `auto` escalates
  conservatively to `rigorous` on risk classification and never downgrades an explicit mode
- `run-check --output {summary,full}` and `--failure-tail-lines N`: summary mode prints a
  one-line result and log path (success) or a bounded failure tail instead of replaying
  full streams into context; the complete log is always retained on disk
- `accept --source <codex-json> --decisions <delta-json>`: deterministically materialize
  `accepted-spec.json`/`.md` (and plan equivalents) from a reconciliation delta
  (`accept`/`reject`/`modify`/`add`) instead of rewriting full artifacts
- Phase-specific Codex reasoning profiles (`PHASE_PROFILES`) applied via `-c` overrides,
  configurable per installation through `CLAUDE_AUTONOMOUS_PHASE_PROFILES` and
  `CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>`
- Full-then-delta reviews: round 1 uses the full review schema, rounds 2+ use a compact
  `review-delta` schema merged into a cumulative finding ledger
- `prompts/code-review-delta.md` and `schemas/review-delta.schema.json`
- `schemas/accept-decisions.schema.json`
- `skills/autonomous-feature/references/`: per-phase guidance loaded only when needed

### Fixed
- `doctor` no longer reports "Codex is not authenticated; run `codex login`" when
  Codex is correctly configured to use an API-key provider. It instead fails closed
  with an actionable message when the required `env_key` is missing, making explicit
  that configuring `config.toml` alone is not enough — the named environment
  variable must be exported.

### Changed
- Completion gate now enforces review consistency (fail closed). Every cumulative
  acceptance criterion must be `satisfied` — `not_satisfied`, `partially_satisfied`
  and `not_verifiable` all block completion — and a review `verdict: pass` that
  coexists with unresolved blocking findings or unsatisfied acceptance criteria is
  rejected as internally inconsistent
- Delta resolution claims fail closed: `resolved_findings` is now validated when
  merging a delta review. An unknown id (not in the cumulative ledger), a duplicate
  id within the same round, or an id also reported as a new finding/regression that
  round raises instead of being silently dropped. `schemas/review-delta.schema.json`
  adds `uniqueItems: true` on `resolved_findings`
- Triage history rendering includes the cumulative `finding_id` when present so a
  rejected/resolved disposition is traceable to the finding it dispositioned
- Codex context is compacted: repository manifest (instructions/build manifests/primary
  modules/test roots/CI) instead of the first 250 tracked file names; latest-only
  verification checks; finding ledger instead of full prior review/triage prose
- `skills/autonomous-feature/SKILL.md` slimmed to a state-machine driver (`effort: high`)
- `migrate-legacy-state` is now non-destructive and crash-safe under contention: the
  migrated run is staged in a temporary sibling directory and atomically renamed into
  place under the repository init lock. An occupied target is never overwritten — re-running
  the same source is an idempotent no-op, any other occupant fails the command, and
  `--force` no longer authorizes overwrite (it cannot bypass run immutability). Use the new
  `--target-run-id` to migrate into a fresh, unused run id instead.
- A failed `codex exec` no longer mutates a run that became terminal while Codex ran. The
  failure handler stages its error log under an invocation-unique name, then re-validates run
  identity and exact-active status under the lock before publishing the canonical log or
  appending a note. If a concurrent cancel/block drove the run terminal, the staged log is
  discarded and the command reports both the Codex failure and the status change without
  touching the run.

### Known limitations
- `accept` artifact publication is exception-safe (it rolls back staged/backup files on a
  raised error) but not crash-safe: a process kill or power loss between backup creation,
  artifact publication, and state save can leave `.bak` files, a missing canonical artifact,
  or artifacts newer than `run-state.json`. Hardening this with a transaction journal and
  recovery-on-load is tracked as a separate P1 item.

## 0.2.0 - 2026-06-12

### Added
- `scripts/state.py`: shared module for git-root discovery, state-home resolution,
  repository identity, run selection, schema migration, atomic writes, and file locking
- `--state-dir` global option: override state home directory
- `--run-id` global option: select a specific run for run-scoped commands
- `list-runs` command: show active runs for the current repository
- `show-run` command: show full details for one run
- `migrate-legacy-state` command: non-destructive import of legacy `.ai/` state
- `archive-run` command: mark a run archived
- `accept-drift` command: acknowledge and record a new git baseline
- XDG-based external state storage: `~/.local/state/claude-autonomous/`
- Multiple concurrent runs per repository with collision-resistant run IDs
- Drift detection: blocks unsafe changes (branch/identity/worktree changes)
- File locking (`fcntl.flock`) for concurrent controller/stop-gate safety
- Schema version 2 with validation and backward-compatible v1 loading

### Changed
- Controller and stop gate now discover repository root via `git rev-parse --show-toplevel`
- Stop gate uses shared state resolver (no more hardcoded CWD-relative path)
- Run state stores artifact paths relative to run directory
- Legacy `.ai/autonomous-development/` layout auto-detected as fallback

### Backward compatible
- All existing commands (`init`, `codex`, `accept`, `run-check`, `status`, etc.) work unchanged
- Single-run workflows require no new flags
- Legacy state automatically detected and usable without migration

## 0.1.0 - 2026-06-12

- Initial plugin boilerplate.
- Added end-to-end autonomous feature workflow.
- Added modular idea, plan, implementation, verification, review, fix, adversarial-review, and status skills.
- Added schema-validated Codex execution controller and bounded stop gate.
- Added tests and project validation utilities.
