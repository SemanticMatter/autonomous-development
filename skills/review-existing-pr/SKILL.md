---
name: review-existing-pr
description: Review an already-existing PR or branch with Codex, read-only, without running enhance/plan/implement. Imports the PR's diff, commits, and metadata into a review run, runs the structured Codex review, conditionally runs an adversarial review for high-risk changes, and reports the verdict, findings, and verification gaps.
disable-model-invocation: true
effort: max
allowed-tools: Read Grep Glob Bash(python3 *)
disallowed-tools: AskUserQuestion Edit Write
---

# Review an existing PR or branch (read-only)

Use this to get an independent Codex review verdict on a PR/branch that already
exists, without the autonomous implementation workflow having produced any run
state. Codex runs under `--sandbox read-only`.

This skill's own steps invoke neither raw `git` nor raw `codex`: every repository
and Codex operation is driven through `python3 controller.py`, and `Bash(git *)`
and `Bash(codex *)` are intentionally not granted (only `Bash(python3 *)` plus
`Read`/`Grep`/`Glob`). Be honest about what that does and does not guarantee: the
tool-permission patterns are NOT a sandbox — `Bash(python3 *)` can run arbitrary
Python, so (as with every skill in this plugin, e.g. codex-review /
adversarial-review) the read-only property comes from the CONTROLLER's behavior
and from the operator only running this skill against trusted inputs, not from the
coarse tool patterns themselves.

### Read-only boundary (precise)

- **What is enforced:** the workflow is read-only with respect to the target
  repository's git **history and remotes**. All target git access goes through the
  controller's hardened, read-only git path (`_git_ro` / a read-only verb allowlist
  with `--no-ext-diff`/`--no-textconv`/hooks-and-fsmonitor disabled): it never runs
  `commit`, `push`, `merge`, `rebase`, `reset`, `checkout`, branch deletion, or any
  remote operation, and never writes target repository files. State and generated
  artifacts are written only under the external state directory (which must be
  OUTSIDE the target worktree — `import-pr` refuses a `--state-dir` inside it).
- **What is NOT claimed:** the skill runs under the operator's shell trust.
  `Bash(python3 *)` is granted so it can invoke `controller.py`; the tool
  permissions do not by themselves prove read-only, and this plugin's trust model
  assumes the operator only runs it against trusted commands and inputs.
- **Codex review execution boundary (residual).** During the review/adversarial
  phases, Codex explores the repository itself under `--sandbox read-only`. That
  read-only sandbox is the containment boundary for executing untrusted PR content
  during the model's own exploration; the controller additionally passes the
  hardened git **environment** (scrubbed
  `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`/…, cleared
  `GIT_EXTERNAL_DIFF`/`GIT_PAGER`/`GIT_SSH_COMMAND`, `GIT_OPTIONAL_LOCKS=0`,
  `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` pointed at the null device) to the
  `codex exec` subprocess.
  **What that does and does not cover:** a `git` the *model* invokes itself
  inherits only those environment variables — so system and global config are
  disabled and no external-diff/pager/ssh helper can be selected. It does **not**
  inherit the hooks/fsmonitor/attributes-file/pager protections or the
  `--no-ext-diff`/`--no-textconv` refusals, because those are per-invocation `-c`
  and flag arguments on the commands the controller builds, and arguments cannot
  propagate through an environment to a subprocess the controller does not
  construct. What stays live for a model-invoked git is therefore
  repository-local `.git/config`. A PR diff cannot write `.git/config` or
  `.git/hooks` (neither is in the tree) and an in-tree `.gitattributes` still needs
  a driver *defined* in config, so the PR-content-only path is largely closed; the
  residual exposure is lost defence-in-depth against an **already-poisoned clone**,
  which is one more reason to review in a disposable checkout. Closing it fully
  needs a hardened `git` shim first on the subprocess `PATH` (follow-up). The
  subprocess environment is otherwise MINIMIZED to a portable allowlist (PATH, home,
  `CODEX_HOME`, temp, locale, proxy) so unrelated caller secrets (cloud keys, VCS
  tokens) are NOT exposed to sandboxed tool commands. It forwards **exactly one**
  credential — the provider's configured `env_key`, and only in API-key mode — with
  **no fallback list of well-known API keys**, so an unrelated key kept in the
  environment (e.g. an `OPENAI_API_KEY` used for something else) is never forwarded.
  Forwarded proxy variables (`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` + lowercase) have
  any embedded userinfo **credentials stripped** (only `scheme://host[:port]` is
  passed; an unparseable proxy value is dropped), while `NO_PROXY` passes through.
  Full containment of untrusted-PR execution is a Codex-platform concern (its
  sandbox), not fully enforceable by this controller — run the workflow in an
  appropriately isolated environment. The EFFECTIVE Codex home (`CODEX_HOME` if set,
  else `HOME`/`USERPROFILE` — the same fallback Codex's own config loader uses) plus
  `TMPDIR`/`TEMP`/`TMP` are refused before `codex exec` runs when any resolves
  **inside** the target worktree — the same containment `--state-dir` gets at import
  time — since Codex would otherwise read `<repo>/.codex/config.toml` as its OWN
  config or write session/cache/temp files inside the read-only-target boundary. A
  relative value is refused outright rather than resolved against the controller's
  own working directory.
- **Codex credential exposure (residual, privacy).** Reviewing an untrusted PR with
  Codex means whatever credential Codex uses to authenticate is reachable within the
  review execution context, and the controller cannot fully remove this — it is a
  Codex-platform sandbox boundary. Two facets: **(env key)** under API-key auth the
  provider key is read from an environment variable, so `codex exec` makes it visible
  to sandboxed tool commands (only that one configured key is forwarded; no fallback
  list); **(file auth)** under file-based login the credential lives in
  `~/.codex/auth.json`, which a prompt-injected review running in the same context
  could read. Reduce the residual by:
  - **Use a dedicated, least-privilege, short-lived Codex credential** for
    untrusted-PR review — never a broadly-scoped or long-lived key — so an exfil of
    the credential (env key OR `auth.json`) has minimal blast radius; rotate it after
    use. This is the strongest real mitigation, alongside `--sandbox read-only`.
  - **Prefer file-based login.** `codex login` (ChatGPT/OpenAI) keeps the credential
    in `~/.codex/auth.json` instead of the environment, so no API key is injected
    into the subprocess (the file-auth facet above still applies).
  - **Optional fail-closed mode.** Export `CLAUDE_AUTONOMOUS_REQUIRE_FILE_AUTH=1` to
    make an imported review/adversarial run **refuse** to invoke Codex whenever Codex
    would authenticate with an environment API key. Default is OFF (existing API-key
    setups keep working); turn it on to require file-based auth for imported reviews.
  - **Extra env vars go through the sanctioned hatch.** If Codex genuinely needs an
    additional variable (e.g. an authenticated proxy that requires embedded
    credentials), name it explicitly in `CLAUDE_AUTONOMOUS_CODEX_ENV_PASSTHROUGH`
    (comma-separated) and accept that exposure — there is no implicit fallback list.
- **Partial/blobless clones are not supported.** Every git invocation sets
  `GIT_NO_LAZY_FETCH=1`. In a partial clone (`git clone --filter=blob:none`) the
  `diff`/`log`/`show` calls that collect PR evidence would otherwise contact the
  promisor remote to materialize missing objects on demand — **network egress** from
  a workflow that is documented as offline-only, and a **write into the target's
  `.git/objects`** under a strict read-only guarantee. With lazy fetch forbidden git
  errors instead, so both guarantees hold and the failure is explicit rather than
  silent. If an import fails with a missing-object error, review from a **complete**
  clone (`git clone` without `--filter`, or `git fetch --refetch` to backfill).
- **Run untrusted-PR reviews in an ISOLATED checkout (operator responsibility).**
  Codex reviews untrusted PR content in your **real working tree**, where it can read
  anything present there — including files git ignores (`.env`, `.env.*`, `*.pem`,
  `id_rsa`, `.netrc`, `credentials`, `.aws/`/`.ssh/` contents, local caches) and the
  Codex credential itself. This is an **operator-isolation boundary the controller
  cannot enforce** (and, per project decision, there is no built-in "isolated review
  mode"). For untrusted PRs, review from a **disposable, clean checkout that contains
  ONLY tracked files at the target commit** (e.g. a fresh clone or `git worktree` with
  ignored/untracked files removed), with an **isolated `HOME` and `CODEX_HOME`** and a
  **dedicated, least-privilege, short-lived Codex credential** (ties together with the
  credential-exposure residual above — one coherent "isolate the environment" rule).
  As a safety net, `import-pr` emits a **best-effort WARNING** to stderr (it does not
  block) when it detects common secret-bearing files in the worktree, recommending an
  isolated checkout; absence of the warning is NOT a guarantee the tree is clean.
  The scan prunes `.git` plus the usual vendored/build trees (`node_modules`,
  `.venv`, `target`, `dist`, caches, …) and stops after a bounded number of entries,
  so a secret that exists *only* inside a vendored directory is not reported.
- **Base-pinned instruction files, including against Codex's own discovery.** The
  controller reads `AGENTS.md`/`CLAUDE.md`/`CONTRIBUTING.md` policy from the **base**
  commit and renders it inside an untrusted fence, so a PR cannot weaken the review
  constraints through the channel the controller builds. Codex would *also* discover
  `AGENTS.md` natively from the **working tree** — that is, from PR HEAD — which
  would let a PR that merely *adds* an `AGENTS.md` reach the reviewer through a
  channel the base-pinning defense never inspects. The review subprocess therefore
  runs with `-c project_doc_max_bytes=0`, which suppresses that native discovery, so
  the base-pinned fenced copy is the only policy Codex sees. `import-pr` still
  surfaces PR-added instruction files separately as non-authoritative evidence, so
  you can see when a PR touches them.
  **Version coupling (residual).** The suppression is a Codex config override, so a
  future Codex release that renames the key would turn it into a silent no-op. It is
  deliberately *not* paired with `--strict-config` at runtime, because that flag also
  validates your `~/.codex/config.toml` and rejects fields such as
  `preferred_auth_method` — which would break the API-key providers this plugin
  supports. Instead, `doctor` probes the key against an isolated `CODEX_HOME` and
  **fails** if it is no longer recognized, so run `doctor` after upgrading Codex.
  **Instruction-file selection is capped at 6 files, prioritized by relevance to
  the diff.** Only this many instruction files are excerpted per commit (a bound
  on prompt size), so once native discovery is suppressed this selection is the
  ONLY channel a policy can reach the reviewer through. A file that GOVERNS the
  PR's changed paths (lives in a directory that is an ancestor of some changed
  file) is prioritized over an unrelated one before falling back to
  root-first/lexicographic order; root-level files always rank first. Any
  candidate the cap excludes is recorded as a `NOT SHOWN` line in
  `repository-context.txt` rather than silently dropped.
- **Drift detection compares endpoints (residual, concurrency).** The imported-target
  guard snapshots HEAD/branch/dirty-state before a long Codex exec and re-checks it
  before publishing. A change that happens **and reverts entirely within** that
  window leaves both endpoints identical, so Codex may have inspected mixed content
  while the verdict is recorded against the original commit; `git status --porcelain`
  also ignores changes to ignored files. Closing this properly means reviewing an
  immutable snapshot (a `git worktree`/archive at the pinned commit) or hashing all
  review-visible content rather than comparing endpoints (follow-up). On a shared
  machine — e.g. another agent session working in the same checkout — do not rely on
  this guard; use a dedicated checkout.
- **Verification is external-CI-only for imported reviews (FR-8).** An imported
  review **never runs target-repository commands**, and local `run-check` is
  **intentionally unavailable** for an existing-PR review run — the controller
  refuses it outright — because executing an untrusted third-party PR's own commands
  (e.g. its `npm test`) could push/rewrite refs or exfiltrate, which would break the
  read-only guarantee. This is by design, not a missing feature: the only
  verification an imported review accepts is **imported external CI evidence**
  (`import-pr --verification-file <json>`), recorded with provenance and always
  treated as external — never as a locally executed check. The evidence file must
  live **outside** the reviewed worktree (a file planted inside the repo could forge
  a passing CI result); `import-pr` refuses an inside-worktree `--verification-file`
  (use an external path, or `--verification-file -` for stdin).
- **External CI requires explicit operator trust (H1).** A JSON file only proves
  shape, not authenticity — so imported external CI is **informational by default**
  and does NOT by itself complete a run. It only satisfies the completion
  verification gate when the operator explicitly asserts trust with
  `import-pr --trust-verification` (recorded in state as
  `verification.external_trusted`). Without the flag the run stays review-only with a
  "verification not operator-trusted" gap; **failed/stale/unauditable evidence still
  blocks regardless of the flag** (trust never turns a known-bad check into a pass).
  Assert trust only for CI you produced or verified from outside the PR.
- **Path to a completed imported run.** Consistent with the above, `evaluate` can
  mark an imported run `complete` only when: the latest Codex review verdict is
  `pass` (with no unresolved critical/high findings and all acceptance criteria
  satisfied), an adversarial review passes when risk requires it, AND verification
  is satisfied by at least one **fresh, auditable, passing, operator-trusted**
  external check — status `passed`, with a `target_sha` matching the reviewed head
  plus `source`/`command`, not stale, and imported with `--trust-verification`.
  Because local checks cannot run, that trusted external CI is the ONLY way to satisfy
  the verification gate. Missing, failed, stale, unauditable, or untrusted external CI
  is reported as a verification gap and blocks completion (it is never treated as
  passing). The Codex review verdict is the primary deliverable — without trusted
  passing external CI the run stays review-only with a reported gap.
  **`target_sha` matching:** informational (non-trusted) evidence accepts a
  case-insensitive hex PREFIX of 7+ characters (a prefix test against the reviewed
  HEAD, not an unambiguous-abbreviation check — nothing here consults the object
  database). Under `--trust-verification` — the only path that can satisfy the
  gate — the FULL 40-character SHA is required; `import-pr` refuses an abbreviated
  one up front rather than letting the assertion look accepted and fail later at
  `evaluate`, and a prefix that reaches the gate is treated as stale.
- **A delta review can never resolve a finding.** The reviewed target is
  pinned and only changes via `import-pr --refresh` — which, whenever the
  contract actually changes, resets the round counter and finding ledger, so
  the next review is a fresh round 1, never a delta. A round-2+ delta review
  is therefore, by construction, always reviewing the byte-identical diff the
  prior round saw — there is no operator action that reaches a delta round
  with a changed contract. `resolved_findings` in a delta payload is refused
  unconditionally (fail closed), not merely when a check happens to find
  nothing changed. Close a finding on an imported run through `triage`
  instead; `import-pr --refresh` starts a fresh full review of the new target
  rather than resolving findings on the old one.

v1 is offline: PR/issue and CI metadata come from CLI strings, files, or stdin —
no GitHub API call is made. A local diff-only review (just `--target-ref` and
`--base-ref`) works with no metadata at all.

## 0. Confirm the environment

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" doctor
```

## 1. Check out the PR branch/ref to review

Check out the PR branch in the target worktree before importing. The target ref
must be the currently checked-out HEAD with a clean worktree, so the reviewed
diff matches the imported target. Identify the base branch the PR diffs against
(for GitHub PRs this is usually `main`/`master`/the target branch).

**Use a ref that is actually current.** `--base-ref main` resolves the LOCAL
`main`, which is often stale — if `origin/main` has moved, the review silently
diffs against an old base and reports a much larger change as "what this PR
does". Prefer `--base-ref origin/main` (after `git fetch`), or confirm the local
ref is up to date first. `import-pr` cannot detect which you meant.

`import-pr` enforces this for you: it fails closed if `--target-ref` is not the
current HEAD, if the worktree has uncommitted changes (uncommitted target changes
are unsupported in v1 — commit or stash first; this is not bypassable with
`--force`), or if the base ref is ambiguous (a criss-cross history with multiple
merge bases — pass `--base-mode exact` with an explicit base commit).

## 2. Import the PR as a read-only review run

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" import-pr \
  --target-ref "<pr-branch>" \
  --base-ref "<base-branch-or-sha>"
```

Useful additions (all optional, all local — no network):

- `--base-mode merge-base` (default; reviews vs the common ancestor) or
  `--base-mode exact` (reviews vs the base ref's own commit).
- `--pr-url`, `--pr-number`, repeatable `--issue` for provenance.
- `--description "..."` or `--description-file <path>` for PR/issue text.
- `--metadata-file <json|->` for structured PR metadata
  (`schemas/pr-metadata.schema.json`).
- `--verification-file <json|->` for imported CI evidence
  (`schemas/imported-verification.schema.json`). This is recorded with
  provenance as **external** evidence; it is never treated as a locally executed
  check, and missing/failed/stale evidence is reported to Codex as a gap.

The import sets `baseline.commit` to the PR **base/merge-base** (not the PR
HEAD), records the reviewed HEAD separately under `review_target`, and
deterministically reconstructs `accepted-spec.md/json`, `accepted-plan.md/json`,
`repository-context.txt`, and `feature-request.md`. Repository policy content
(`AGENTS.md`/`CLAUDE.md`/`CONTRIBUTING.md`, fenced and untrusted-labelled) is taken
from the **base** commit as authoritative, so a PR that deletes or weakens those
files in its own diff cannot evade the repo's review constraints; PR-added
instruction files are shown separately as non-authoritative. All these reads are
pinned to the base/target commits, and the import re-validates the target
HEAD/branch/clean-worktree just before publishing (failing closed on a mid-import
change) so artifacts never mix snapshots. It also classifies risk and
sets `risk.requires_adversarial_review` (monotonically) when the diff touches
auth, persistence/migration, concurrency, retry/idempotency, destructive
behavior, privacy, external services, dependency manifests, or
deployment/config files (CI workflows, deploy/Terraform/Kubernetes/Helm,
service-endpoint config, or `.env`-like files — category `deployment/config`).

## 3. Run the Codex review

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase review
```

Read the printed `review-NN.codex.json`. Summarize the verdict and findings by
severity with exact file evidence, and surface any verification gap.

## 4. Run an adversarial review when required

Inspect whether adversarial review is required:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
# or, machine-readable:
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" show-run --json
```

If `risk.requires_adversarial_review` is `true`, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase adversarial
```

Read the printed `adversarial-NN.codex.json`. Distinguish concrete failure
scenarios from speculation and identify the smallest evidence-backed mitigations.

## 5. Report the verdict and remaining gates

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" evaluate
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" usage-report
```

The review is useful on its verdict alone. `evaluate` reports remaining
completion gates strictly: a high-risk import blocks until an adversarial review
passes, and missing, failed, stale, unauditable, or untrusted verification is
reported as an unmet gap. Imported CI never satisfies the *local* run-check gate —
local run-check is unavailable for imported reviews by design — but fresh,
auditable, passing CI that the operator has explicitly trusted with
`--trust-verification` **does** satisfy the verification gate, and is the only way
to satisfy it. See "Path to a completed imported run" in the boundaries section above.

## Target drift and refresh

Imported reviews are pinned to a fixed target HEAD/branch. If you switch
branches, the target advances, or the worktree becomes dirty, the controller
refuses `codex`, `run-check`, `evaluate`, `set-risk`, and `triage` for the run.
Re-check out the imported target, or re-import the current target (which
supersedes stale review/adversarial verdicts and stale local verification when
the HEAD changed). `--run-id` is a global option and must precede the `import-pr`
subcommand:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "<run-id>" \
  import-pr --refresh --target-ref "<pr-branch>" --base-ref "<base>"
# Add --base-mode exact if the original import used a non-default base mode.
```

**A refresh is stateless — re-supply what the original import carried.** Every
refresh rebuilds the PR metadata and the imported CI evidence from *its own*
command line, which keeps each refresh fully reproducible from its argv. The
consequence is that flags you omit are dropped, not inherited:

- Omitting `--description-file`/`--metadata-file` removes the PR's stated intent
  from the reconstructed `accepted-spec`. The review contract changes, prior
  verdicts are superseded, and the next round judges the diff against a **weaker**
  contract.
- Omitting `--verification-file` drops the imported CI, so verification reverts to
  UNPROVEN and the completion gate blocks.
- `--trust-verification` is re-asserted per refresh **by design**, so it must be
  passed again every time.

The controller now warns on stderr for each component a refresh drops, and the
recovery command it prints in drift/refresh errors names the flags you need to
re-supply.

Whether the base-commit's instruction-file policy could be read is also part
of the review contract: a refresh at the identical target/base commit that
recovers (or newly loses) that read supersedes prior verdicts too, exactly
like a metadata/CI change would.

## Boundaries

- Do not edit product files in this skill. Finding triage and repairs belong to
  `/autonomous-development:fix-findings`.
- Do not push, merge, close, approve, comment on, or otherwise mutate the PR or
  the target repository. All target git access is read-only.
- This workflow runs in the CURRENT checkout (the target ref must be the
  checked-out HEAD, and drift fails closed), so the run records
  `worktree_mode: current`. It is not the same as the `autonomous-current`/
  `autonomous-main` implementation workflows: those write to your checkout,
  whereas this one only reads it — so it needs no `--allow-main` and works on
  `main`/`master` or a detached HEAD. A dirty worktree is refused either way.
- **Reviewing an untrusted PR to THIS plugin's own repository executes that
  contributor's `controller.py` and skill files with the operator's privileges
  before any sandbox applies** — `Bash(python3 *)` is granted so this skill can
  invoke the controller, and it is not a sandbox (see the read-only-boundary note
  above); dogfooding this workflow on its own repository does not add a boundary
  beyond that.
