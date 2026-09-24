# Example invocations

```text
# Safe default: edits land in a disposable isolated worktree.
/autonomous-development:autonomous-feature "Add resumable multipart uploads with checksum validation and backward-compatible API behavior"
```

```text
# Direct edits on an already-created feature branch.
# Refuses main/master and a dirty tree. Never commits — review with `git diff`.
git checkout -b feature/uploads
/autonomous-development:autonomous-current "Add resumable multipart uploads"
```

```text
# Explicit opt-in: direct edits on main/master.
# Still requires a clean tree. Never commits — review with `git diff`.
/autonomous-development:autonomous-main "Patch a security hotfix in place"
```

```text
/autonomous-development:enhance-idea "Add organization-level API usage limits"
/autonomous-development:implementation-plan "Prioritize backward compatibility and migration safety"
/autonomous-development:implement-plan
/autonomous-development:verify-feature
/autonomous-development:codex-review
/autonomous-development:fix-findings
/autonomous-development:codex-review
/autonomous-development:autonomous-status
```

For a high-risk feature:

```text
/autonomous-development:adversarial-review "The change modifies authorization and persistent access-token storage"
```

## Reviewing an existing PR or branch

```text
/autonomous-development:review-existing-pr "Review the checked-out feature branch against main"
```

Local diff-only review (offline, no metadata, no GitHub API):

```bash
git switch my-feature-branch
controller.py import-pr --target-ref my-feature-branch --base-ref main
controller.py codex --phase review
controller.py evaluate
controller.py status
```

Review with PR/issue and imported CI metadata files:

```bash
# pr.json conforms to schemas/pr-metadata.schema.json
# ci.json conforms to schemas/imported-verification.schema.json (external provenance)
controller.py import-pr --target-ref my-feature-branch --base-ref origin/main \
  --metadata-file pr.json --verification-file ci.json
controller.py codex --phase review
# Imported CI is recorded as external evidence; missing/failed/stale evidence is
# reported as a verification gap and never counts as a local run-check.
controller.py evaluate
```

High-risk PR that triggers an adversarial review:

```bash
# A diff touching auth/migrations/etc. sets risk.requires_adversarial_review.
controller.py import-pr --target-ref add-auth-and-migration --base-ref main \
  --description "Adds login/session handling and a DB migration"
controller.py codex --phase review
controller.py codex --phase adversarial   # required before evaluate can complete
controller.py evaluate
```

The workflow is strictly read-only against the target repository: no commits,
pushes, merges, rebases, branch deletions, or remote changes occur. If the target
branch/HEAD changes after import, re-import with `--refresh`. `--run-id` is a
global option and must precede the `import-pr` subcommand:

```bash
controller.py --run-id <run-id> import-pr --refresh \
  --target-ref my-feature-branch --base-ref main
# Add --base-mode exact when the original import used a non-default base mode.
#
# A refresh is STATELESS: it rebuilds PR metadata and imported CI evidence from its
# own argv. Re-supply everything the original import carried, or it is dropped:
controller.py --run-id <run-id> import-pr --refresh \
  --target-ref my-feature-branch --base-ref main \
  --description-file /tmp/pr-description.txt \
  --verification-file /tmp/ci.json --trust-verification
```

## Controller modes and reporting

`auto` mode (the default) scales the workflow to the change and escalates conservatively:

```bash
# Low-risk, localized work runs lean/standard; high-risk work escalates to rigorous.
controller.py init --feature "Rename a button label" --mode auto

# Force the full workflow with mandatory adversarial review.
controller.py init --feature "Add tenant-scoped billing" --mode rigorous

# Use your manually created feature branch instead of a disposable worktree.
git switch -c experiment
controller.py init --feature "Experiment feature" --mode auto --worktree-mode current

# Same mode, but explicitly authorized to land on main/master.
controller.py init --feature "Hotfix" --mode standard --worktree-mode current --allow-main
```

Drive phases and inspect token usage:

```bash
controller.py next-action --json
controller.py run-check --name unit-tests --output summary -- pytest -q
controller.py usage-report
```
