# Autonomous Claude + Codex Development

A reusable Claude Code plugin for bounded autonomous feature development:

```text
feature idea
  -> Codex idea enhancement
  -> Codex implementation plan
  -> Claude reconciliation and implementation
  -> project verification
  -> independent Codex review
  -> Claude finding triage and fixes
  -> re-verification and re-review
```

The main entry point is:

```text
/autonomous-development:autonomous-feature "Add resumable file uploads"
```

The plugin deliberately keeps Claude as the implementation orchestrator and uses fresh, read-only Codex executions for independent planning and review. It does not push, merge, deploy, rotate credentials, alter remote infrastructure, or apply irreversible production migrations.

### Four entry-point workflows

| Slash command | Worktree | Branch | When to use |
|---|---|---|---|
| `/autonomous-development:autonomous-feature` | Isolated `.claude/worktrees/*` (default safe) | New worktree branch | Default. Edits land in a disposable worktree; your checkout is untouched. |
| `/autonomous-development:autonomous-current` | Current checkout | Your already-created non-`main`/`master` feature branch | You created and switched to a clean feature branch yourself and want changes to land there. |
| `/autonomous-development:autonomous-main` | Current checkout | `main` / `master` (explicit opt-in via `--allow-main`) | You explicitly want direct edits on `main`/`master`. Still requires a clean tree. |
| `/autonomous-development:review-existing-pr` | Current checkout, **read-only** | The PR/branch you already have checked out | Review a PR the plugin did not implement. No enhance/plan/implement, no edits, no commits. |

Both current-checkout **implementation** workflows (`autonomous-current` and `autonomous-main`) are opt-in. They do not create `.claude/worktrees/*`, do not enter a worktree, and never commit — the user reviews with normal `git diff` and commits manually. They require a clean working tree (`git status --porcelain` must be empty): modified, staged, deleted, and untracked files all make it unclean. They also require an attached named branch; detached HEAD is unsupported. `--allow-main` is valid only with `--worktree-mode current`; `autonomous-current` refuses `main`/`master`, while `autonomous-main` passes `--allow-main` to bypass that guard. `review-existing-pr` also runs in the current checkout and likewise requires a clean tree, but it is strictly read-only: it never edits, commits, or enters a worktree, and it works on `main`/`master` or a detached HEAD without `--allow-main` because it only reads the diff.

## Upstream

This plugin is based on [quaat/autonomous-development](https://github.com/quaat/autonomous-development). This fork keeps the original workflow and adds the current-checkout mode and the read-only existing-PR review workflow (`review-existing-pr`/`import-pr`), plus related documentation updates.

## Included skills

| Skill | Purpose |
|---|---|
| `/autonomous-development:autonomous-feature` | Safe default: run the complete workflow inside a disposable worktree |
| `/autonomous-development:autonomous-current` | Run the complete workflow directly on the user's already-created feature branch (current checkout, never commits) |
| `/autonomous-development:autonomous-main` | Run the complete workflow directly on `main`/`master` (explicit opt-in via `--allow-main`, never commits) |
| `/autonomous-development:enhance-idea` | Ask Codex to turn a rough feature idea into a structured proposal |
| `/autonomous-development:implementation-plan` | Ask a fresh Codex execution for a repository-grounded plan |
| `/autonomous-development:implement-plan` | Implement the accepted plan with Claude |
| `/autonomous-development:verify-feature` | Discover and run relevant repository checks |
| `/autonomous-development:codex-review` | Run an independent structured Codex review |
| `/autonomous-development:adversarial-review` | Challenge high-risk architecture and operational assumptions |
| `/autonomous-development:fix-findings` | Triage and fix validated review findings |
| `/autonomous-development:review-existing-pr` | Review an already-existing PR/branch read-only, without enhance/plan/implement |
| `/autonomous-development:autonomous-status` | Show workflow state and remaining gates |

## Requirements

- Claude Code with plugin and skill support.
- Python 3.11 or later.
- The `jsonschema` Python package (>=4.18), a declared runtime dependency used
  to validate every Codex output, reconciliation source/decision, and triage
  ledger before it can affect run state.
- Git.
- Codex CLI installed and authenticated.
- A Git repository for the target project.

Install the Python dependencies (from this plugin's directory):

```bash
pip install -e .
# or, to install just the runtime dependency:
pip install "jsonschema>=4.18"
```

Run `controller.py doctor` to confirm all prerequisites (including
`jsonschema`) are available before starting a workflow.

Install Codex CLI when needed:

```bash
npm install -g @openai/codex
```

### Codex authentication

The workflow drives Codex through `codex exec`, so Codex must be able to reach a
model. Two authentication methods are supported, and `controller.py doctor`
detects which one your `~/.codex/config.toml` selects (it is `CODEX_HOME`-aware):

1. **ChatGPT / OpenAI login** (the built-in `openai` provider): run `codex login`.
   `doctor` verifies this with `codex login status`.
2. **API-key provider** (`preferred_auth_method = "apikey"`), including custom
   providers such as **Azure / MS Foundry**: Codex reads the key from the
   environment variable named by that provider's `env_key`. For example:

   ```toml
   model_provider = "azure"
   preferred_auth_method = "apikey"

   [model_providers.azure]
   name = "Azure"
   base_url = "https://<resource>.openai.azure.com/openai/v1"
   env_key = "AZURE_OPENAI_API_KEY"
   wire_api = "responses"
   ```

   Configuring `config.toml` is **not** sufficient on its own — the variable named
   by `env_key` must be exported in the environment that runs the workflow:

   ```bash
   export AZURE_OPENAI_API_KEY="…"
   ```

   With an API-key provider configured, a missing `codex login` is expected and is
   **not** an error; `doctor` checks that the `env_key` variable is set instead.

The official OpenAI Codex plugin for Claude Code is optional for this project because the workflow invokes `codex exec` directly to obtain schema-validated output. It remains useful for manual `/codex:*` commands:

```text
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

## Install as a plugin

Register this repository as a local marketplace (it ships a
`.claude-plugin/marketplace.json`) and install it through Claude Code's plugin
system. This persists across restarts and works in the VS Code extension, which
manages plugins through `~/.claude/plugins` rather than CLI flags:

```bash
claude plugin marketplace add /path/to/autonomous-development
claude plugin install autonomous-development@autonomous-development
```

The `/autonomous-development:*` skills become available in the next session.

## Run locally

Alternatively, for an ephemeral load without installing, point Claude at the
plugin directory from its parent:

```bash
claude --plugin-dir ./autonomous-development
```

Then open a target repository and invoke:

```text
/autonomous-development:autonomous-feature "Add audit-log export as JSON and CSV"
```

For development validation:

```bash
make check
claude plugin validate . --strict
```

## Adaptive workflow modes

`init` accepts `--mode` to scale workflow depth to the change:

```bash
# auto (default): inspect the feature and escalate conservatively
controller.py init --feature "Add Stripe billing" --mode auto

# lean: clear, low-risk, localized work
# standard: normal feature work (skips independent idea enhancement)
# rigorous: full workflow with mandatory adversarial review
controller.py init --feature "Rename a button label" --mode standard
```

`auto` escalates to `rigorous` when it classifies the feature as touching auth/authz,
persistence/migrations, regulated data, billing, concurrency, public API compatibility, broad
architecture, or destructive behavior. It never downgrades an explicitly requested mode, and an
explicit `rigorous` (or escalated `auto`) run requires an adversarial review to complete.

The main skill is a state-machine driver: it repeatedly asks the controller for the next phase
and executes it.

## Reviewing an existing PR or branch

`/autonomous-development:review-existing-pr` (controller command `import-pr`) reviews a PR/branch
that already exists, read-only, **without** running enhance/plan/implement. It reconstructs the
overview artifacts the review phases need (`accepted-spec.md/json`, `accepted-plan.md/json`,
`repository-context.txt`, `feature-request.md`) from the PR's diff, commits, and supplied
metadata, then runs the normal Codex review (and a conditional adversarial review).

```bash
# From the checked-out PR branch, diffing against main (merge-base by default):
controller.py import-pr --target-ref my-feature-branch --base-ref main

# With provenance and an explicit base commit:
controller.py import-pr --target-ref my-feature-branch --base-ref origin/main \
  --base-mode merge-base --pr-url https://example/pr/42 --pr-number 42 \
  --issue ISSUE-7 --description-file /tmp/pr-body.md

controller.py codex --phase review
controller.py codex --phase adversarial   # only when risk requires it
controller.py evaluate
controller.py status
```

Key semantics:

- **Base vs target.** `baseline.commit` is the PR **base/merge-base**, never the PR HEAD, so Codex
  reviews the actual PR diff. The reviewed HEAD is recorded separately under `review_target`
  (`target_ref`, `target_branch`, `target_head`, `base_ref`, `base_commit`, `base_mode`, and the
  worktree branch/head at import). `--base-mode exact` diffs against the base ref's own commit
  instead of the common ancestor.
- **Untrusted repository/PR data is fenced.** The reconstructed context surfaces bounded excerpts of
  the repository's instruction-file **content** (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, …) so
  Codex sees the actual conventions/policies, not just file paths — but that content, and all
  PR-author-controlled text (title/description/commit subjects, and diff-derived paths, diffstat, and
  changed-symbol hints), is treated as UNTRUSTED **data**: it is neutralized (headings/code fences
  defanged) and clearly fenced, so a crafted instruction file, path, or commit message cannot inject
  a heading/fence/instruction into the review prompt.
- **Policy comes from the BASE commit, pinned to a SHA.** For an imported review the authoritative
  instruction/policy content is read from the PR's **base** commit (not the target HEAD), so a PR
  that deletes or weakens `AGENTS.md`/`CLAUDE.md` in its own diff cannot evade the repository's own
  review constraints. Instruction files the PR *adds* are surfaced separately, labelled untrusted and
  non-authoritative. All instruction/context reads are pinned to the base and target commits (never
  the mutable live HEAD), and `import-pr` re-validates the target HEAD/branch/clean-worktree just
  before publishing — if the target moves or the worktree is dirtied mid-import, it fails closed so no
  mixed-snapshot artifacts are published.
- **Offline / local model.** v1 has no GitHub API dependency. PR/issue and CI metadata come from
  CLI strings, `--description-file`, `--metadata-file`, or stdin (`-`). A diff-only review with
  just `--target-ref` and `--base-ref` works with no metadata.
- **External CI provenance + explicit trust (H1).** Imported CI evidence (`--verification-file`,
  `schemas/imported-verification.schema.json`) is recorded under `verification.external_checks`
  with provenance (command, status, source, target SHA) and is **never** represented as a locally
  executed `run-check`. Missing, failed, or stale (target-SHA-mismatched) evidence is reported to
  Codex as a verification gap, and `evaluate` never treats external evidence as a passing local
  check. Two anti-forgery controls: the evidence file must live **outside the reviewed worktree**
  (`import-pr` refuses an inside-repo `--verification-file`; use an external path or stdin `-`), and
  a JSON only proves shape, not authenticity — so external CI is **informational by default** and
  satisfies the completion gate only when the operator explicitly asserts trust with
  `import-pr --trust-verification` (recorded as `verification.external_trusted`). Failed/stale/
  unauditable evidence still blocks regardless of the flag.
- **Adversarial triggers.** Risk is classified deterministically from changed paths, diff text,
  commit messages, and PR/issue text. A diff touching authentication/authorization, persistence or
  migrations (e.g. `migrations/*.sql`), concurrency, retry/idempotency, destructive behavior,
  privacy, external services/webhooks, dependency manifests, or deployment/config files — CI
  workflows (`.github/workflows/*.yml`), deploy/Terraform/Kubernetes/Helm manifests,
  service-endpoint config, or `.env`-like files (category `deployment/config`) — sets
  `risk.requires_adversarial_review` (monotonically, with evidence-backed reasons — including a
  dedicated `plugin/reviewer-config` category for `skills/*/SKILL.md`, `prompts/*.md`, `agents/*.md`,
  and any root/nested instruction-policy file such as `AGENTS.md`/`CLAUDE.md`/`CONTRIBUTING.md`,
  regardless of prose content, plus `.gitattributes`, which controls how git presents the PR's own
  content to the reviewer). **Output-format pinning:** every git call that feeds classification has
  its output format pinned rather than inherited, because a PR can influence the defaults — a
  rename/copy contributes BOTH its source and destination path (not only the destination);
  path-consuming reads use NUL-delimited `-z` output plus `core.quotepath=false`, so a path containing
  non-ASCII bytes, a tab, a quote, a backslash or a newline is classified by its literal string rather
  than a quoted/escaped (or line-split) one; and content-producing diffs pass `--text` alongside
  `--no-textconv`/`--no-ext-diff`, so an in-tree `.gitattributes` setting `-diff` cannot replace a
  file's content with a binary marker and leave the content classifiers with nothing to match. Each of
  those would otherwise let a risk-triggering change evade detection through how it is renamed,
  encoded or presented rather than through its content. `evaluate` blocks completion — UNCONDITIONALLY, independent of
  whether risk classification required adversarial review at all — until no unresolved critical/high
  threat remains in the cumulative threat ledger (every round's `threats` merge into a `T-<n>`-keyed
  ledger, released only by `triage`, mirroring the review finding ledger — a `pass` verdict cannot
  mask an open severe threat, and an exact re-report of an already-released threat stays released
  rather than reopening under a fresh id). Docs/README-only PRs stay low risk; the plugin-config
  paths above are also excluded from that docs-only classification (they are executable
  configuration, not prose) via a built-in and operator-extensible
  (`CLAUDE_AUTONOMOUS_NON_DOCS_GLOBS`) override.
- **Read-only / no mutation (git history and remotes).** The workflow is read-only with respect to
  the target repository's git history and remotes: all target git access goes through the
  controller's hardened, read-only git path — a read-only verb allowlist (`rev-parse`, `merge-base`,
  `diff`, `log`, `status`, `show`, `name-rev`, ...) with external-diff/textconv/hooks/fsmonitor/pager
  disabled and output format pinned (`--text`, `-z`, `core.quotepath=false`) — that refuses any
  mutating verb. It performs no commit, push, merge, rebase, reset,
  checkout, branch deletion, deployment, production migration, or remote change, and never writes
  target repository files. State and generated artifacts are written only under the external state
  directory. A dirty worktree is a hard refusal at import (uncommitted target changes are unsupported
  in v1, not bypassable with `--force`). Note: this read-only property comes from the controller's
  behavior, not from the skill's tool permissions — `review-existing-pr` is not granted `Bash(git *)`
  or `Bash(codex *)`, but `Bash(python3 *)` (needed to invoke `controller.py`) can run arbitrary
  Python, so, as with every skill in this plugin, only run it against trusted inputs.
- **Verification is external-CI-only for imported reviews (by design).** An imported review never
  runs target-repository commands, and local `run-check` is **intentionally unavailable** for an
  existing-PR review run — the controller refuses it outright — because executing an untrusted
  third-party PR's own commands (e.g. its `npm test`) could push/rewrite refs or exfiltrate, which
  would break the read-only guarantee. This is a deliberate boundary, not a missing feature: the only
  verification an imported review accepts is imported external CI evidence
  (`import-pr --verification-file`, from OUTSIDE the reviewed worktree, and only completion-eligible
  when operator-trusted — see above), recorded with provenance and always treated as external.
  Because local checks cannot run, that trusted external CI is the ONLY way to satisfy an imported
  run's verification gate at completion (see below). (Normal, non-imported `run-check` is unchanged
  and runs commands as before.)
- **Path to a completed imported run.** `evaluate` marks an imported run `complete` when the latest
  Codex review verdict is `pass` (no unresolved critical/high findings, acceptance criteria
  satisfied), an adversarial review passes **with no unresolved critical/high threat in the
  cumulative threat ledger** when risk requires it (a `verdict: pass` that coexists with an open
  severe threat is rejected as an inconsistent review, mirroring the code-review check — a threat is
  released only by `triage`, never by a later round simply not re-reporting it), AND verification is satisfied by
  at least one **fresh, auditable, passing, operator-trusted** external check (status `passed`,
  `target_sha` matching the reviewed head, plus `source`/`command`, not stale, imported with
  `--trust-verification`). Failed, stale, unauditable, untrusted, or missing external CI is reported
  as a gap and always blocks completion (never treated as passing) — the review verdict remains the
  primary deliverable and the run stays review-only in that case. `target_sha` matching: informational
  evidence accepts a case-insensitive hex prefix of 7+ characters; evidence imported under
  `--trust-verification` requires the FULL 40-character SHA (a prefix is treated as stale), since that
  is the only path that can satisfy the gate. External CI does NOT satisfy a
  *non-imported* run's gate (their gate stays local-`run-check`-only). A delta review's
  `resolved_findings` is refused unconditionally on an imported run (the pinned target means a delta
  round without an intervening `import-pr --refresh` is, by construction, always reviewing the
  byte-identical diff the prior round saw — there is no operator action that reaches a delta round
  with a changed contract) — close a finding via `triage` instead. A threat released by `triage` that
  a later adversarial round reports again, byte-identical, is not auto-reopened but blocks completion
  (for critical/high severity) until re-triaged, recorded as `reseen_after_release_round`; this blocker
  is also surfaced by `next-action` (both the imported and non-imported guidance), not only by
  `evaluate`, so an operator hits the re-triage instruction before running the command that would fail
  on it.
- **Codex review execution boundary (residual).** During review/adversarial, Codex explores the
  repository under `--sandbox read-only`; that sandbox is the containment boundary for executing
  untrusted PR content. The controller also passes the hardened git **environment** (scrubbed
  `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`/…, cleared
  `GIT_EXTERNAL_DIFF`/`GIT_PAGER`/`GIT_SSH_COMMAND`, `GIT_OPTIONAL_LOCKS=0`,
  `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` pointed
  at the null device) to the `codex exec` subprocess. A git the *model* runs itself inherits those
  environment variables only — **not** the hooks/fsmonitor/attributes/pager protections or the
  `--no-ext-diff`/`--no-textconv` refusals, which are per-invocation `-c`/flag arguments on the
  commands the controller builds and cannot propagate through an environment. Repository-local
  `.git/config` therefore stays live for such a git; a PR diff cannot write `.git/config` or
  `.git/hooks`, so the residual exposure is lost defence-in-depth against an already-poisoned clone
  (one more reason to review from a disposable checkout). The review subprocess additionally runs
  with `-c project_doc_max_bytes=0`, which suppresses Codex's own project-instruction discovery so a
  PR-added `AGENTS.md` in the working tree cannot reach the reviewer and bypass the base-pinned
  policy; `doctor` probes that key against an isolated `CODEX_HOME` and fails if a Codex upgrade
  stops recognizing it. Only 6 instruction files are excerpted per commit (a prompt-size bound), and
  with native discovery suppressed that selection is the only channel a policy can reach the reviewer
  through — it prioritizes a file that GOVERNS a changed path (an ancestor directory of some changed
  file) over an unrelated one before falling back to root-first/lexicographic order, and records any
  candidate the cap excludes as a `NOT SHOWN` line rather than dropping it silently. Every git
  invocation also sets `GIT_NO_LAZY_FETCH=1`, so a partial/blobless
  clone errors instead of silently fetching objects from the network and writing them into the
  target's `.git` — review from a complete clone. The controller also MINIMIZES that subprocess environment to a portable allowlist (PATH, home,
  `CODEX_HOME`, temp, locale, proxy) so unrelated caller secrets (cloud keys, VCS tokens) are not
  exposed to sandboxed tool commands. It forwards **exactly one** credential — the provider's
  configured `env_key`, and only in API-key mode — with **no fallback list of well-known API keys**,
  so an unrelated key kept in the environment (e.g. an `OPENAI_API_KEY` used for something else) is
  never forwarded. Forwarded proxy variables (`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` + lowercase)
  have any embedded userinfo **credentials stripped** (only `scheme://host[:port]` is passed; a proxy
  value that cannot be safely stripped is dropped), while `NO_PROXY` passes through. Full containment
  of untrusted-PR execution is a Codex-platform concern, not fully enforceable here — run the
  workflow in an appropriately isolated environment. The EFFECTIVE Codex home (`CODEX_HOME` if set,
  else `HOME`/`USERPROFILE` — the same fallback Codex's own config loader uses) plus
  `TMPDIR`/`TEMP`/`TMP` are checked before `codex exec` runs and refused when any resolves **inside**
  the target worktree — the same containment `--state-dir` gets at import time — since Codex would
  otherwise read `<repo>/.codex/config.toml` as its OWN config (PR-controlled content) or write
  session/cache/temp files inside the read-only-target boundary, on paths a dirty-worktree check may
  not see if they land on a gitignored path. A relative value for any of these is refused outright
  rather than resolved against the controller's own working directory, which could silently land inside
  or outside the worktree depending on where the command happens to run.
- **Codex credential exposure (residual, privacy).** Reviewing an untrusted PR with Codex means
  whatever credential Codex uses to authenticate is reachable within the review execution context,
  and the controller cannot fully remove this — it is a Codex-platform sandbox boundary. Two facets:
  **(env key)** under API-key auth the provider key is read from an environment variable, so
  `codex exec` makes it visible to sandboxed tool commands (only that one configured key is
  forwarded; no fallback list); **(file auth)** under file-based login the credential lives in
  `~/.codex/auth.json`, which a prompt-injected review running in the same context could read. The
  strongest real mitigation is a **dedicated, least-privilege, short-lived** Codex credential used
  only for untrusted-PR review (never a broadly-scoped or long-lived key), rotated after use, plus
  `--sandbox read-only`. Additional controls: **(1)** file-based login (`codex login`) injects no API
  key into the subprocess; **(2)** the optional fail-closed mode — export
  `CLAUDE_AUTONOMOUS_REQUIRE_FILE_AUTH=1` to make an imported review/adversarial run **refuse** to
  invoke Codex whenever Codex would authenticate with an environment API key (default OFF, so
  existing API-key setups are unaffected). If you genuinely need an extra variable (e.g. an
  authenticated proxy that requires embedded credentials) in the Codex environment, add it explicitly
  via `CLAUDE_AUTONOMOUS_CODEX_ENV_PASSTHROUGH` (comma-separated names) and accept that exposure —
  there is no implicit fallback.
- **Isolation boundary — review untrusted PRs from a disposable, clean checkout (operator
  responsibility).** Codex reviews untrusted PR content in your **real working tree**, where it can
  read anything present there — including files git ignores (`.env`, `.env.*`, `*.pem`, `id_rsa`,
  `.netrc`, `credentials`, `.aws/`/`.ssh/` contents, caches) and the Codex credential itself. This is
  an **operator-isolation boundary the controller cannot enforce**, and (by project decision) there is
  no built-in isolated-review mode. For untrusted PRs, review from a **disposable, clean checkout
  containing ONLY tracked files at the target commit** (a fresh clone or `git worktree` with
  ignored/untracked files removed), with an **isolated `HOME`/`CODEX_HOME`** and a **dedicated,
  least-privilege, short-lived Codex credential** — the same isolation that bounds the
  credential-exposure residual above. As a safety net, `import-pr` prints a **best-effort warning**
  (it does not block) when it detects common secret-bearing files in the worktree; absence of the
  warning does not guarantee the tree is clean.
- **Target drift and refresh.** Imported reviews are pinned to a fixed target HEAD/branch. If you
  switch branches, the target advances, or the worktree becomes dirty, `codex`, `run-check`,
  `evaluate`, `set-risk`, and `triage` fail closed. Re-check out the imported target, or re-import
  the current target with `controller.py --run-id <id> import-pr --refresh --target-ref ... --base-ref ...`
  (the `--run-id` is a global option and must precede the `import-pr` subcommand; add `--base-mode`
  if the run used a non-default base mode), which supersedes stale review/adversarial verdicts and
  stale local verification when the target HEAD changed. A refresh is **stateless**: it rebuilds PR
  metadata and imported CI evidence from its own argv, so re-supply
  `--description-file`/`--metadata-file`/`--verification-file` and `--trust-verification` or they are
  dropped — omitting the description leaves the next review round judging the diff against a weaker
  contract. The controller warns on stderr for each component a refresh drops, and the recovery
  command printed in drift/refresh errors names the flags to re-supply.
- **Reporting.** The verdict and findings surface through the existing `status`, `show-run --json`,
  `evaluate`, `review-NN.codex.json`, `adversarial-NN.codex.json`, and `usage-report` outputs.

```bash
controller.py next-action --json
# -> { "phase": "verification", "required_action": "...",
#      "completion_condition": "...", "references": [...] }
```

## Token efficiency

The controller minimizes the context that flows back into the model:

```bash
# Summary output (default): one line plus the on-disk log path
controller.py run-check --name unit-tests --output summary -- pytest -q
# ✓ unit-tests passed in 18.4 s
#   command: pytest -q
#   full log: .../verification/03-unit-tests.log

# Failures show a bounded tail; --output full replays complete streams
controller.py run-check --name unit-tests --failure-tail-lines 80 -- pytest -q
```

Codex phases run with `codex exec --json`, retain the NDJSON event stream, and record per-phase
usage. Inspect it with:

```bash
controller.py usage-report
# Phase             Prompt chars   Output chars    Duration
# enhance                 14,220          5,810        67 s
# plan                    23,840          9,120       104 s
# review-01               31,440          6,330       119 s
```

Reconciliation uses deltas rather than rewriting whole artifacts. Claude writes a decision file
(`accept`/`reject`/`modify`/`add`) and the controller materializes the accepted artifact:

```bash
controller.py accept --kind spec \
  --source feature-spec.codex.json \
  --decisions spec-reconciliation.json
```

Review triage is recorded as a finding ledger so later rounds never re-raise rejected findings:

```bash
controller.py triage --file triage-01.json
```

## State location

State is stored outside the target repository by default. The resolver uses the following
precedence:

```bash
# 1. Explicit state directory (highest priority)
controller.py --state-dir /path/to/state init --feature "..."

# 2. Environment variable
export CLAUDE_AUTONOMOUS_STATE_HOME=~/.local/state/claude-autonomous
controller.py init --feature "..."

# 3. XDG default (Linux)
# Automatically uses ~/.local/state/claude-autonomous/

# 4. Legacy fallback (existing .ai/autonomous-development/ detected automatically)
```

On macOS the default is `~/Library/Application Support/claude-autonomous/`.
On Windows the default is `%LOCALAPPDATA%\claude-autonomous\`.

## State and generated artifacts

Each run is stored in its own directory under the state home:

```text
~/.local/state/claude-autonomous/
├── repositories/
│   └── <repo-id>/
│       ├── metadata.json
│       └── runs/
│           └── <run-id>/
│               ├── run-state.json
│               ├── feature-request.md
│               ├── repository-context.txt
│               ├── accepted-spec.md
│               ├── accepted-plan.md
│               ├── feature-spec.codex.json
│               ├── implementation-plan.codex.json
│               ├── review-01.codex.json
│               └── verification/
```

The legacy `.ai/autonomous-development/` layout is still supported for backward compatibility
and is auto-detected when present. To suppress it, add `.ai/` to your `.gitignore` once you
have migrated (see "Migration from legacy state" below) or if you never want to commit the
planning evidence.

## Multiple runs and run IDs

Each `init` creates a new run with a collision-resistant run ID of the form
`<YYYYMMDDTHHMMSSZ>-<8-hex-chars>` (for example `20260612T134500Z-a1b2c3d4`).

```bash
# List all active runs for the current repository
controller.py list-runs

# Show details for a specific run
controller.py show-run --run-id 20260612T134500Z-a1b2c3d4

# Start a second concurrent run with an optional human-readable label
controller.py init --feature "New feature" --label "experiment"

# Run commands against a specific run when multiple are active
controller.py status --run-id 20260612T134500Z-a1b2c3d4
```

When exactly one active run exists, `--run-id` is optional and the run is selected
automatically. When multiple active runs exist, commands that mutate state require
`--run-id` to avoid ambiguity.

## Migration from legacy state

```bash
# Migrate existing .ai/autonomous-development/ state to the new external layout
controller.py migrate-legacy-state

# The original .ai/autonomous-development/ directory is preserved unchanged.
# To use the migrated state, either:
export CLAUDE_AUTONOMOUS_STATE_HOME=~/.local/state/claude-autonomous
# or add .ai/ to your .gitignore and continue; legacy state remains accessible.
```

Migration is non-destructive and idempotent. Run it again with `--force` to overwrite an
already-migrated run directory.

## Drift detection and recovery

The controller detects when the repository state diverges from the recorded baseline before
any mutating command. Two kinds of drift are distinguished:

- **EXPECTED**: HEAD has advanced on the same branch (commits were added). No action required.
- **UNSAFE**: Branch changed, worktree path changed, or repository identity changed. Mutating
  commands are blocked until the drift is acknowledged.

```bash
# If an unsafe drift is detected (e.g., branch changed), you will see:
# error: Unsafe repository drift detected: branch changed: main -> experiment
# Recovery: Run `accept-drift` to acknowledge and record the new baseline.

controller.py accept-drift
```

## Archiving runs

```bash
# Archive a completed run (removes it from the default list-runs output)
controller.py archive-run --run-id 20260612T134500Z-a1b2c3d4

# Show all runs including archived ones
controller.py list-runs --all
```

Archiving is a metadata flag; no files are deleted.

## Security and permissions

- State directories are created with mode `0o700` (owner-only) on POSIX systems.
- Review artifacts and Codex responses may contain sensitive code, prompts, or design details.
- Do not share state directories across users or store them on world-readable paths.
- Remote URLs are stored with credentials stripped (the `user:pass@` portion is removed).

## Worktree modes

The controller supports two explicit execution modes:

```bash
# Default safe workflow: isolated worktree
# Claude Code's autonomous-feature skill enters a disposable worktree before init.
controller.py init --feature "Experiment feature" --mode auto --worktree-mode isolated
```

```bash
# Current-branch workflow: use the branch you created manually
git checkout -b experiment
controller.py init --feature "Experiment feature" --mode auto --worktree-mode current
# Review the result with normal `git diff` in your checkout.
```

```bash
# Current-checkout on main/master (explicit opt-in):
controller.py init --feature "Hotfix" --mode standard --worktree-mode current --allow-main
```

Two slash commands wrap these invocations so the user does not have to call `controller.py`
directly:

```text
/autonomous-development:autonomous-current "Experiment feature"   # already on a feature branch
/autonomous-development:autonomous-main "Hotfix"                  # explicit edits on main/master
```

Both commands run `controller.py init --mode standard --worktree-mode current`; the `main`
variant additionally passes `--allow-main`. Neither creates `.claude/worktrees/*`, neither
enters a worktree, and neither commits. The agent's changes land directly in your checkout so
normal `git diff`, local testing, and manual commit flow continue to work.

All commands still work correctly from any linked worktree. The repository identity is derived
from the shared git object store so runs created in different worktrees belong to the same
repository and are visible to `list-runs`. Current-checkout mode is opt-in for people who create
their own feature branches first and want the agent's changes to land directly in that checkout.
In current mode the controller uses the current project root for both
`repository.canonical_root` and `repository.worktree_path`, records the current branch in
baseline metadata, does not create a `.claude/worktrees/*` worktree or `worktree-*` branch,
refuses detached HEAD and `main`/`master` unless you pass `--allow-main`, and refuses a
dirty tree containing modified, staged, deleted, or untracked files.

## Completion rules

A run succeeds only when:

- an accepted specification and accepted plan exist;
- all recorded verification commands pass;
- the latest Codex review returns `pass`;
- no unresolved `critical` or `high` findings remain;
- an adversarial review passes when the change is classified as high risk.

A run stops as `blocked` when credentials or required services are unavailable, requirements materially conflict, verification cannot be performed, or the maximum review/fix rounds are exhausted.

## Security boundaries

The skill uses Codex with `--sandbox read-only`. Claude performs repository edits under Claude Code's normal permission system. The workflow explicitly prohibits:

- `danger-full-access`, `--yolo`, or bypassing sandbox controls;
- pushing, merging, publishing, or deploying;
- modifying production data or remote infrastructure;
- rotating or exposing credentials;
- deleting unrelated user changes;
- weakening tests or security controls to obtain a passing result.

By default, run autonomous development in an isolated worktree. This fork also supports an explicit current-checkout mode when you want changes to land directly in your own feature branch.

## Customization

- Edit `prompts/*.md` to tailor Codex's planning and review behavior.
- Edit `schemas/*.schema.json` to add organization-specific output requirements.
- Extend `skills/verify-feature/references/check-discovery.md` with project-specific commands.
- Change `max_review_rounds` through the controller's `init --max-review-rounds` option.
- Map workflow phases to locally available Codex models and reasoning settings with
  `CLAUDE_AUTONOMOUS_PHASE_PROFILES` (JSON) and `CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>`.

## Compatibility note

Claude Code and Codex evolve quickly. The project uses documented plugin-root component layout, `SKILL.md` frontmatter, skill-scoped hooks, `${CLAUDE_PLUGIN_ROOT}`, and `codex exec --output-schema`. Run `make check` and `claude plugin validate . --strict` after upgrading either CLI.
