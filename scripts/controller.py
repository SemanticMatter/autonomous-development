#!/usr/bin/env python3
"""Stateful controller for the Claude + Codex autonomous-development plugin."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import (
    StateError,
    RepoInfo,
    DriftKind,
    resolve_repository,
    resolve_state_home,
    detect_legacy_state,
    new_run_id,
    run_dir_path,
    make_relative_path,
    resolve_artifact_path,
    RunStateLock,
    RepoInitLock,
    migrate_v1_to_v2,
    validate_run_id,
    validate_state,
    load_run_state,
    save_run_state,
    load_repo_metadata,
    save_repo_metadata,
    find_active_runs,
    find_all_runs,
    resolve_active_run,
    resolve_run_for_inspection,
    resolve_run_for_active_mutation,
    resolve_run_for_transition,
    require_active_run_state,
    verify_loaded_run_identity,
    assert_transition_allowed,
    detect_drift,
    repository_context,
    LEGACY_STATE_REL,
)
from schema_validation import SchemaValidationError, validate_payload

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PHASE_OUTPUTS = {
    "enhance": (
        "prompts/enhance-idea.md",
        "schemas/enhanced-idea.schema.json",
        "feature-spec.codex.json",
    ),
    "plan": (
        "prompts/implementation-plan.md",
        "schemas/implementation-plan.schema.json",
        "implementation-plan.codex.json",
    ),
    "review": ("prompts/code-review.md", "schemas/review.schema.json", None),
    "adversarial": (
        "prompts/adversarial-review.md",
        "schemas/adversarial-review.schema.json",
        None,
    ),
}

# Round-2+ code review uses a compact delta prompt/schema.
REVIEW_DELTA_PROMPT = "prompts/code-review-delta.md"
REVIEW_DELTA_SCHEMA = "schemas/review-delta.schema.json"

# Phase-specific Codex reasoning profiles. Installations may override these via the
# CLAUDE_AUTONOMOUS_PHASE_PROFILES env var (a JSON object keyed by phase) and select a
# per-phase model with CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>.
PHASE_PROFILES: dict[str, dict[str, str]] = {
    "enhance": {"reasoning": "medium", "verbosity": "low", "reasoning_summary": "none"},
    "plan": {"reasoning": "high", "verbosity": "low", "reasoning_summary": "none"},
    "review": {"reasoning": "high", "verbosity": "low", "reasoning_summary": "none"},
    "adversarial": {
        "reasoning": "xhigh",
        "verbosity": "low",
        "reasoning_summary": "none",
    },
}
_DEFAULT_PROFILE = {"reasoning": "high", "verbosity": "low", "reasoning_summary": "none"}

WORKFLOW_MODES = ("auto", "lean", "standard", "rigorous")
WORKTREE_MODES = ("isolated", "current")

# Conservative risk categories used by `--mode auto` escalation. Matching any
# category escalates an `auto` run to rigorous.
MODE_RISK_PATTERNS: dict[str, list[str]] = {
    "auth/authz": [
        r"\bauth",
        r"authoriz",
        r"authentic",
        r"\blogin\b",
        r"permission",
        r"\brbac\b",
        r"\bacl\b",
        r"\bsession",
        r"credential",
    ],
    "persistence/migration": [
        r"migrat",
        r"\bschema\b",
        r"database",
        r"\bsql\b",
        r"persist",
        r"\borm\b",
    ],
    "personal/regulated data": [
        r"\bpii\b",
        r"personal data",
        r"regulated",
        r"\bgdpr\b",
        r"\bhipaa\b",
        r"\bpci\b",
        r"\bprivacy\b",
    ],
    "billing": [
        r"billing",
        r"payment",
        r"invoice",
        r"\bcharge",
        r"\bstripe\b",
        r"subscription",
    ],
    "concurrency/retries": [
        r"concurren",
        r"\brace\b",
        r"\bretr(y|ies|ied)\b",
        r"idempoten",
        r"\bmutex\b",
        r"\bthread",
    ],
    "public-API compatibility": [
        r"public api",
        r"public interface",
        r"backward compat",
        r"breaking change",
        r"api compatibility",
        r"\bcontract\b",
    ],
    "destructive/irreversible": [
        r"\bdelete\b",
        r"\bdestroy\b",
        r"\bdrop\b",
        r"irreversib",
        r"\bpurge\b",
        r"truncate",
        r"rm -rf",
    ],
    "broad architectural change": [
        r"architectur",
        r"\brewrite\b",
        r"redesign",
    ],
}

# Backward-compat alias
WorkflowError = StateError


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# Generous default so legitimately long Codex reviews/verification runs are not
# killed; `CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT` overrides it (set to 0/empty to
# disable the timeout entirely for unusual environments).
DEFAULT_PROCESS_TIMEOUT_SECONDS = 3600.0
PROCESS_TIMEOUT_EXIT_CODE = 124


def _resolve_process_timeout() -> float | None:
    raw = os.environ.get("CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT")
    if raw is None:
        return DEFAULT_PROCESS_TIMEOUT_SECONDS
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PROCESS_TIMEOUT_SECONDS
    return value if value > 0 else None


def run_process(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_timeout = timeout if timeout is not None else _resolve_process_timeout()
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
            timeout=effective_timeout,
        )
    except FileNotFoundError as exc:
        raise WorkflowError(f"Required executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        # subprocess.run terminates the child before raising. Surface the timeout
        # as a non-zero result (fail closed) with whatever partial output exists,
        # so verification checks block and Codex phases raise rather than hang.
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        marker = (
            f"\n[controller] command timed out after {effective_timeout}s "
            "and was terminated."
        )
        if check:
            raise WorkflowError(marker.strip()) from exc
        return subprocess.CompletedProcess(
            args, PROCESS_TIMEOUT_EXIT_CODE, stdout, stderr + marker
        )


def git(root: Path, *args: str, check: bool = True) -> str:
    result = run_process(["git", *args], cwd=root)
    if check and result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def read_optional(path: Path) -> str:
    if not path.exists():
        return "(not available)"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or "(empty)"


def render(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", rendered)))
    if unresolved:
        raise WorkflowError(f"Unresolved prompt placeholders: {', '.join(unresolved)}")
    return rendered


def slug(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-").lower()
    return clean[:80] or "check"


# ---------------------------------------------------------------------------
# Shared context helpers
# ---------------------------------------------------------------------------


def get_context(args: argparse.Namespace) -> tuple[RepoInfo, Path, str | None]:
    """Return (repo, state_home, run_id_override) from parsed args."""
    start = Path(args.project_root).resolve() if args.project_root else None
    repo = resolve_repository(start)
    state_home = resolve_state_home(getattr(args, "state_dir", None))
    run_id = getattr(args, "run_id", None)
    return repo, state_home, run_id


def require_no_unsafe_drift(state: dict, repo: RepoInfo) -> None:
    """Raise WorkflowError if unsafe drift detected. Expected drift is allowed."""
    drift = detect_drift(state, repo)
    if drift.kind == DriftKind.UNSAFE:
        raise WorkflowError(
            f"Unsafe repository drift detected: {drift.message}\n"
            f"Recovery: {drift.recovery}\n"
            f"Use `accept-drift` to record the new baseline when safe."
        )


# ---------------------------------------------------------------------------
# Prompt values helper
# ---------------------------------------------------------------------------


def prompt_values(run_dir: Path, state: dict[str, Any]) -> dict[str, str]:
    """Compute template placeholder values for Codex prompts."""
    artifacts = state.get("artifacts", {})

    def artifact_text(key: str, fallback: str) -> str:
        rel = artifacts.get(key, "")
        if rel:
            try:
                path = resolve_artifact_path(str(rel), run_dir)
                return read_optional(path)
            except (StateError, OSError):
                pass
        # fallback path relative to run_dir
        fallback_path = run_dir / fallback
        return read_optional(fallback_path)

    finding_ledger = render_finding_ledger(state)

    return {
        "FEATURE": state.get("feature", "(missing)"),
        "BASELINE": state.get("baseline", {}).get("commit", "(missing)"),
        "REPOSITORY_CONTEXT": artifact_text(
            "repository_context", "repository-context.txt"
        ),
        "CODEX_SPEC": artifact_text("enhance", "feature-spec.codex.json"),
        "ACCEPTED_SPEC": artifact_text("accepted_spec", "accepted-spec.md"),
        "ACCEPTED_PLAN": artifact_text("accepted_plan", "accepted-plan.md"),
        "VERIFICATION": json.dumps(compact_verification_view(state), indent=2),
        "PREVIOUS_REVIEW": finding_ledger,
        "LATEST_REVIEW": finding_ledger,
        "FINDING_LEDGER": finding_ledger,
        "OPEN_FINDINGS": render_open_findings(state),
        "ACCEPTANCE_CRITERIA": render_acceptance_criteria(state),
        # Overridden with the real path list for delta reviews in cmd_codex (which
        # has repo access to fingerprint the current worktree).
        "CHANGED_SINCE_LAST_REVIEW": "(not applicable to this phase)",
    }


# ---------------------------------------------------------------------------
# Latest checks / finding helpers
# ---------------------------------------------------------------------------


def latest_verification_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the latest result for each logical verification check name."""
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for check in checks:
        name = str(check.get("name", "unnamed"))
        if name not in latest:
            order.append(name)
        latest[name] = check
    return [latest[name] for name in order]


def unresolved_severe_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        finding
        for finding in review.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") in {"critical", "high"}
    ]


# ---------------------------------------------------------------------------
# Phase profiles
# ---------------------------------------------------------------------------


def resolve_phase_profile(phase: str) -> dict[str, str]:
    """Resolve the effective Codex reasoning profile for a phase.

    Defaults come from PHASE_PROFILES; installations may override via the
    CLAUDE_AUTONOMOUS_PHASE_PROFILES env var (JSON object keyed by phase) and
    select a per-phase model via CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>.
    """
    profile = dict(PHASE_PROFILES.get(phase, _DEFAULT_PROFILE))
    override_raw = os.environ.get("CLAUDE_AUTONOMOUS_PHASE_PROFILES", "").strip()
    if override_raw:
        try:
            overrides = json.loads(override_raw)
        except json.JSONDecodeError:
            overrides = None
        if isinstance(overrides, dict):
            phase_override = overrides.get(phase)
            if isinstance(phase_override, dict):
                profile.update({k: str(v) for k, v in phase_override.items()})
    model_env = os.environ.get(
        f"CLAUDE_AUTONOMOUS_CODEX_MODEL_{phase.upper()}", ""
    ).strip()
    if model_env:
        profile["model"] = model_env
    return profile


def codex_profile_args(profile: dict[str, str]) -> list[str]:
    """Render a phase profile as Codex CLI arguments (`-c key=value` and `--model`)."""
    args: list[str] = []
    mapping = (
        ("reasoning", "model_reasoning_effort"),
        ("reasoning_summary", "model_reasoning_summary"),
        ("verbosity", "model_verbosity"),
    )
    for key, cfg in mapping:
        value = profile.get(key)
        if value:
            args += ["-c", f"{cfg}={value}"]
    model = profile.get("model")
    if model:
        args += ["--model", model]
    return args


# ---------------------------------------------------------------------------
# Codex usage telemetry
# ---------------------------------------------------------------------------


def parse_codex_usage(ndjson_text: str) -> dict[str, int]:
    """Best-effort extraction of token usage from Codex `--json` NDJSON events.

    Returns the last-seen input/output/total token counts when present. Unknown
    event shapes are ignored and absent token fields are acceptable (character
    counts serve as the fallback metric).
    """
    usage: dict[str, int] = {}
    aliases = (
        ("input_tokens", "input_tokens"),
        ("prompt_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
        ("total_token_usage", "total_tokens"),
    )
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidates = [event]
        for key in ("usage", "info", "token_usage", "msg"):
            sub = event.get(key)
            if isinstance(sub, dict):
                candidates.append(sub)
        for cand in candidates:
            for src, dst in aliases:
                val = cand.get(src)
                if isinstance(val, int):
                    usage[dst] = val
    return usage


def parse_codex_model(ndjson_text: str) -> str | None:
    """Best-effort extraction of the concrete model id from Codex NDJSON events.

    Returns the first non-empty `model` string found (the session-configuration
    event reports the actually-selected model, including when it is inherited
    from global Codex config rather than an explicit phase profile).
    """
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidates = [event]
        for key in ("msg", "info", "session", "config", "turn_context"):
            sub = event.get(key)
            if isinstance(sub, dict):
                candidates.append(sub)
        for cand in candidates:
            model = cand.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    return None


# ---------------------------------------------------------------------------
# Workflow modes
# ---------------------------------------------------------------------------


def classify_feature_risk(feature: str) -> list[str]:
    """Return the conservative risk categories matched by the feature text."""
    text = feature.lower()
    matched: list[str] = []
    for category, patterns in MODE_RISK_PATTERNS.items():
        if any(re.search(pattern, text) for pattern in patterns):
            matched.append(category)
    return matched


def select_mode(requested: str, feature: str) -> tuple[str, list[str]]:
    """Resolve the effective workflow mode and the reasons for it.

    `auto` escalates conservatively to rigorous on any risk signal, otherwise
    standard. Explicit modes are respected verbatim; explicit rigorous is never
    downgraded.
    """
    if requested == "auto":
        risks = classify_feature_risk(feature)
        if risks:
            return "rigorous", [
                f"auto escalated to rigorous: detected {', '.join(risks)}"
            ]
        return "standard", ["auto selected standard: no high-risk signals detected"]
    return requested, [f"explicit mode requested: {requested}"]


def worktree_mode_label(worktree_mode: object) -> str:
    """Human-readable label for the init worktree mode."""
    if worktree_mode == "current":
        return "current checkout"
    if worktree_mode == "isolated":
        return "isolated worktree"
    if isinstance(worktree_mode, str) and worktree_mode.strip():
        return worktree_mode.strip()
    return "(unknown)"


def repository_state_block(repo: RepoInfo, *, worktree_mode: str | None = None) -> dict[str, str]:
    """Return the repository block written into run-state.json."""
    block = {
        "id": repo.id,
        "canonical_root": str(repo.canonical_root),
        "git_common_dir": str(repo.git_common_dir),
        "worktree_path": str(repo.worktree_path),
        "display_name": repo.display_name,
        "remote_display": repo.remote_display,
    }
    if worktree_mode:
        block["worktree_mode"] = worktree_mode
    return block


# ---------------------------------------------------------------------------
# Compact prompt context helpers
# ---------------------------------------------------------------------------


def compact_verification_view(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Latest logical verification check per name as {name, command, exit_code}."""
    checks = latest_verification_checks(state.get("verification", {}).get("checks", []))
    return [
        {
            "name": check.get("name"),
            "command": check.get("command"),
            "exit_code": check.get("exit_code"),
        }
        for check in checks
    ]


def render_finding_ledger(state: dict[str, Any]) -> str:
    """Render the compact triage finding ledger for inclusion in review prompts."""
    ledger = state.get("review_ledger", [])
    compact: list[dict[str, Any]] = []
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {
            "fingerprint": entry.get("fingerprint"),
            "status": entry.get("status"),
        }
        # Surface the canonical finding id so the reviewer can correlate a triage
        # disposition with the `F-<n>` id it references (e.g. to avoid re-raising
        # a finding already rejected/resolved under that id).
        if entry.get("finding_id"):
            item["finding_id"] = entry["finding_id"]
        if entry.get("resolution"):
            item["resolution"] = entry["resolution"]
        if entry.get("reason"):
            item["reason"] = entry["reason"]
        compact.append(item)
    if not compact:
        return "(none)"
    return json.dumps(compact, indent=2)


def render_open_findings(state: dict[str, Any]) -> str:
    """Render still-open cumulative findings with their `F-<n>` ids.

    Delta reviews must reference prior findings by `F-<n>` id in
    `resolved_findings`, but the triage ledger is keyed by fingerprint. Without
    the ids the reviewer cannot reliably resolve a prior finding, so a severe
    finding could remain open indefinitely. Surface each open finding with its
    full evidence (file/line/description/evidence/recommended fix) so the delta
    reviewer can resolve or carry it from the prompt alone, without re-opening
    the raw review-NN.codex.json.
    """
    open_findings = [
        {
            "id": f.get("id"),
            "severity": f.get("severity"),
            "category": f.get("category"),
            "status": f.get("status"),
            "round": f.get("round"),
            "origin": f.get("origin"),
            "file": f.get("file"),
            "line_start": f.get("line_start"),
            "description": f.get("description"),
            "evidence": f.get("evidence"),
            "recommended_fix": f.get("recommended_fix"),
        }
        for f in state.get("cumulative_findings", [])
        if isinstance(f, dict) and f.get("status") == "open"
    ]
    if not open_findings:
        return "(none)"
    return json.dumps(open_findings, indent=2)


def render_acceptance_criteria(state: dict[str, Any]) -> str:
    """Render the cumulative acceptance-criteria ledger for the delta reviewer.

    A delta review only reports the criteria it touched
    (`affected_acceptance_criteria`). Surfacing the cumulative status of every
    criterion lets the reviewer judge the change against the full set without
    re-reading the round-1 full review.
    """
    ledger = state.get("cumulative_acceptance_criteria", [])
    items = [
        {
            "id": c.get("id"),
            "status": c.get("status"),
            "evidence": c.get("evidence"),
            "round": c.get("round"),
        }
        for c in ledger
        if isinstance(c, dict) and c.get("id")
    ]
    if not items:
        return "(none)"
    return json.dumps(items, indent=2)


# ---------------------------------------------------------------------------
# Review checkpoints (focused-full-fallback delta)
# ---------------------------------------------------------------------------

# A review record stores a checkpoint of the worktree it reviewed so a later
# round can identify what changed since. We cannot reconstruct an *exact*
# review-to-review patch from this (we do not retain prior file content), so the
# delta reviewer is asked to review the full current feature diff while focusing
# on the paths changed since the previous checkpoint. The prompt states this
# explicitly rather than claiming a true review-to-review delta.
REVIEW_CONTEXT_MODE = "focused_full_fallback"


def _feature_changed_paths(repo: RepoInfo, baseline_commit: str | None) -> list[str]:
    """Paths that differ from the feature baseline (committed or in the worktree).

    The union of (a) the diff between the baseline commit and the current worktree
    for tracked files and (b) `git status --porcelain` entries (which also surface
    untracked files). Best-effort: git failures degrade to whatever was collected.
    """
    root = repo.canonical_root
    paths: set[str] = set()
    if baseline_commit:
        diff = git(root, "diff", "--name-only", baseline_commit, check=False)
        paths.update(p.strip() for p in diff.splitlines() if p.strip())
    status = git(root, "status", "--porcelain", check=False)
    for line in status.splitlines():
        entry = line[3:].strip() if len(line) > 3 else ""
        if not entry:
            continue
        if " -> " in entry:  # rename/copy: record the destination path
            entry = entry.split(" -> ", 1)[1]
        paths.add(entry.strip().strip('"'))
    return sorted(paths)


def _path_fingerprints(root: Path, paths: list[str]) -> dict[str, str | None]:
    """sha256 of each path's current bytes; None if deleted/unreadable."""
    fingerprints: dict[str, str | None] = {}
    for rel in paths:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            fingerprints[rel] = None
            continue
        fingerprints[rel] = "sha256:" + hashlib.sha256(data).hexdigest()
    return fingerprints


def _latest_review_checkpoint(state: dict[str, Any]) -> dict[str, Any] | None:
    for review in reversed(state.get("reviews", [])):
        if isinstance(review, dict) and isinstance(review.get("checkpoint"), dict):
            return review["checkpoint"]
    return None


def capture_review_checkpoint(
    repo: RepoInfo, state: dict[str, Any], *, checkpoint_id: str
) -> dict[str, Any]:
    """Snapshot the worktree this review round saw, for later change detection.

    Call this *before* appending the current round's review record so
    `previous_checkpoint_id` resolves to the prior round.
    """
    baseline_commit = (state.get("baseline") or {}).get("commit")
    changed = _feature_changed_paths(repo, baseline_commit)
    previous = _latest_review_checkpoint(state)
    return {
        "id": checkpoint_id,
        "captured_at": utc_now(),
        "head_commit": repo.head_commit,
        "branch": repo.branch,
        "baseline_commit": baseline_commit,
        "changed_paths": changed,
        "path_fingerprints": _path_fingerprints(repo.canonical_root, changed),
        "previous_checkpoint_id": previous.get("id") if previous else None,
        "review_context_mode": REVIEW_CONTEXT_MODE,
    }


def changed_paths_since_last_review(
    repo: RepoInfo, state: dict[str, Any]
) -> list[str] | None:
    """Feature paths whose current content differs from the last review checkpoint.

    Returns None when there is no prior checkpoint (the next review is effectively
    a full review). A path counts as changed when its current fingerprint differs
    from the checkpoint's, when it is newly part of the feature diff, or when it
    was in the checkpoint but is no longer part of the feature diff (reverted).
    """
    previous = _latest_review_checkpoint(state)
    if previous is None or not isinstance(previous.get("path_fingerprints"), dict):
        return None
    previous_fps = previous["path_fingerprints"]
    baseline_commit = (state.get("baseline") or {}).get("commit")
    current_paths = _feature_changed_paths(repo, baseline_commit)
    current_fps = _path_fingerprints(repo.canonical_root, current_paths)
    changed: set[str] = set()
    for path, fingerprint in current_fps.items():
        if previous_fps.get(path) != fingerprint:
            changed.add(path)
    for path in previous_fps:
        if path not in current_fps:
            changed.add(path)
    return sorted(changed)


def render_changed_since_previous(repo: RepoInfo, state: dict[str, Any]) -> str:
    changed = changed_paths_since_last_review(repo, state)
    if changed is None:
        return "(no prior review checkpoint; treat this as a full review)"
    if not changed:
        return "(no file changes detected since the previous review checkpoint)"
    return json.dumps(changed, indent=2)


# ---------------------------------------------------------------------------
# Review ledger merge (full-then-delta)
# ---------------------------------------------------------------------------


_CANONICAL_FINDING_ID = re.compile(r"^F-(\d+)$")


def _index_findings(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    findings = state.get("cumulative_findings", [])
    return {f["id"]: f for f in findings if isinstance(f, dict) and "id" in f}


def _canonical_id_allocator(
    index: dict[str, dict[str, Any]], incoming_ids: list[str]
) -> Callable[[], str]:
    """Return an allocator that hands out fresh, collision-free canonical IDs.

    Duplicate finding IDs must be remapped to a real `F-<n>` id (not a synthetic
    `F-1#dup` key) so a triage entry — whose schema is `^F-[0-9]+$` — can still
    reference the remapped finding. Seed the counter past every canonical id in
    both the existing index and the incoming batch so a remapped id can never
    collide with an id that appears later in the same review.
    """
    max_n = 0
    for key in list(index) + list(incoming_ids):
        m = _CANONICAL_FINDING_ID.match(str(key))
        if m:
            max_n = max(max_n, int(m.group(1)))
    counter = {"n": max_n}

    def allocate() -> str:
        counter["n"] += 1
        candidate = f"F-{counter['n']}"
        while candidate in index:
            counter["n"] += 1
            candidate = f"F-{counter['n']}"
        return candidate

    return allocate


def migrate_cumulative_finding_ids(state: dict[str, Any]) -> None:
    """Remap legacy synthetic finding IDs (`F-1#dup..`/`F-1#r..`) to canonical IDs.

    Older runs recorded duplicate findings under unreferenceable synthetic keys.
    Rewrite each to the next free `F-<n>`, preserving the original under
    `legacy_id` and folding any `reused_id` into `source_id`. Never drop a
    finding. Idempotent: once all IDs are canonical this is a no-op.
    """
    findings = state.get("cumulative_findings")
    if not isinstance(findings, list):
        return
    max_n = 0
    has_legacy = False
    for f in findings:
        if not isinstance(f, dict):
            continue
        m = _CANONICAL_FINDING_ID.match(str(f.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
        elif str(f.get("id", "")).strip():
            has_legacy = True
    if not has_legacy:
        return
    for f in findings:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id", ""))
        if not fid.strip() or _CANONICAL_FINDING_ID.match(fid):
            continue
        max_n += 1
        f.setdefault("legacy_id", fid)
        if "reused_id" in f and "source_id" not in f:
            f["source_id"] = f.pop("reused_id")
        f["id"] = f"F-{max_n}"


# The evidence fields every cumulative finding preserves verbatim from the
# validated Codex review payload, so the ledger is self-contained: the gate
# report, the audit trail, and the delta reviewer never need to re-open the raw
# review-NN.codex.json. Stored inline because run-state is the single source of
# truth the (future) run-state schema will freeze.
_FINDING_EVIDENCE_DEFAULTS: dict[str, Any] = {
    "file": None,
    "line_start": None,
    "description": "",
    "evidence": "",
    "recommended_fix": "",
}


def _cumulative_finding(
    finding: dict[str, Any],
    *,
    fid: str,
    status: str,
    round_num: int,
    origin: str,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Build a canonical cumulative finding, preserving evidence inline.

    `finding` is an already schema-validated review finding, so the evidence
    fields are present; `.get` with defaults keeps this robust if called on a
    sparser dict. `origin` records provenance (full | delta | regression) and
    `source_id` is set only when a colliding id was remapped to a fresh one.
    """
    entry: dict[str, Any] = {
        "id": fid,
        "severity": finding.get("severity"),
        "category": finding.get("category"),
        "status": status,
        # `round` is retained for backward compatibility (it is the round the
        # finding was opened). `round_opened`/`round_last_seen` make the audit
        # trail explicit: a carried-forward finding keeps `round_last_seen` at the
        # last round a reviewer actually reported it (delta reviews only report
        # changes), so "we have not re-confirmed this since round N" is legible.
        "round": round_num,
        "round_opened": round_num,
        "round_last_seen": round_num,
        "origin": origin,
    }
    for key, default in _FINDING_EVIDENCE_DEFAULTS.items():
        entry[key] = finding.get(key, default)
    if source_id is not None:
        entry["source_id"] = source_id
    return entry


def _finalize_cumulative(
    index: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalize every cumulative finding to the canonical key set.

    Carried-forward and legacy entries (recorded before evidence was preserved)
    are backfilled with evidence defaults and an `origin` of "legacy" so the
    whole ledger has one uniform shape. Idempotent: an already-canonical entry is
    unchanged. Run at every site that rebuilds `cumulative_findings`.
    """
    for entry in index.values():
        if not isinstance(entry, dict):
            continue
        entry.setdefault("origin", "legacy")
        for key, default in _FINDING_EVIDENCE_DEFAULTS.items():
            entry.setdefault(key, default)
        # Backfill round metadata for entries recorded before it was tracked.
        opened = entry.get("round")
        entry.setdefault("round_opened", opened)
        entry.setdefault("round_last_seen", opened)
    return list(index.values())


def _require_finding_items(items: list[Any], context: str) -> None:
    """Fail closed if any review finding item is malformed.

    Top-level type checks cannot see inside list items, so a downgraded/partial
    Codex payload could include a `new_findings`/`findings` entry that is not a
    dict or lacks an `id`. Silently skipping such an entry would *drop* a
    potentially blocking finding (fail open). Raise instead so a malformed
    finding blocks the merge rather than vanishing.
    """
    for finding in items:
        if not isinstance(finding, dict) or not str(finding.get("id", "")).strip():
            raise WorkflowError(
                f"Codex review {context} contains a malformed finding entry "
                f"(not an object or missing id): {finding!r}; refusing to merge "
                "(fail closed)."
            )


def merge_full_review(state: dict[str, Any], parsed: dict[str, Any], round_num: int) -> None:
    """Seed the cumulative finding set from a round-1 full review."""
    migrate_cumulative_finding_ids(state)
    index = _index_findings(state)
    findings = list(parsed.get("findings", []))
    _require_finding_items(findings, "findings")
    allocate = _canonical_id_allocator(index, [str(f["id"]) for f in findings])
    for finding in findings:
        fid = finding["id"]
        # A full review can return two findings sharing one id: the schema
        # enforces id *format* (^F-[0-9]+$) but not uniqueness. Overwriting by
        # id would silently drop the first entry — potentially a severe finding —
        # from the seeded baseline (fail open). Remap the colliding entry to a
        # fresh canonical id (referenceable by triage) and record the model's
        # original id under `source_id`.
        if fid in index:
            new_id = allocate()
            index[new_id] = _cumulative_finding(
                finding,
                fid=new_id,
                status="open",
                round_num=round_num,
                origin="full",
                source_id=fid,
            )
            continue
        index[fid] = _cumulative_finding(
            finding,
            fid=fid,
            status="open",
            round_num=round_num,
            origin="full",
        )
    state["cumulative_findings"] = _finalize_cumulative(index)


def merge_delta_review(
    state: dict[str, Any], parsed: dict[str, Any], round_num: int
) -> None:
    """Merge a round-2+ delta review into the cumulative finding set."""
    migrate_cumulative_finding_ids(state)
    index = _index_findings(state)
    new_findings = list(parsed.get("new_findings", []))
    regressions = list(parsed.get("regressions", []))
    # Fail closed on malformed nested items rather than dropping them silently.
    _require_finding_items(new_findings, "new_findings")
    _require_finding_items(regressions, "regressions")
    reintroduced_ids = {f["id"] for f in new_findings} | {f["id"] for f in regressions}
    resolved_ids = list(parsed.get("resolved_findings", []))
    resolution_source = f"review-{round_num:02d}"
    seen_resolved: set[str] = set()
    for fid in resolved_ids:
        # Fail closed on a resolution claim that cannot be substantiated, rather
        # than silently ignoring it (which would let a delta *appear* to close a
        # finding while leaving the ledger — and the gate — unaffected, or worse,
        # mask a contradiction).
        #   * duplicate id within resolved_findings: the reviewer cannot resolve
        #     the same finding twice in one round;
        #   * unknown id (not in the cumulative ledger): there is nothing to
        #     resolve, so the claim is spurious;
        #   * id also reported as a new finding/regression this round: a finding
        #     cannot be simultaneously resolved and reintroduced.
        if fid in seen_resolved:
            raise WorkflowError(
                f"Codex delta review resolves finding {fid!r} more than once; "
                "refusing to merge (fail closed)."
            )
        seen_resolved.add(fid)
        if fid not in index:
            raise WorkflowError(
                f"Codex delta review resolves unknown finding {fid!r} (not in the "
                "cumulative ledger); refusing to merge (fail closed)."
            )
        if fid in reintroduced_ids:
            raise WorkflowError(
                f"Codex delta review reports finding {fid!r} as both resolved and "
                "reintroduced (new finding/regression); refusing to merge "
                "(fail closed)."
            )
        finding = index[fid]
        finding["status"] = "resolved"
        finding["resolved_at_round"] = round_num
        finding["resolution_source"] = resolution_source
    incoming_ids = [str(f["id"]) for f in new_findings] + [
        str(f["id"]) for f in regressions
    ]
    allocate = _canonical_id_allocator(index, incoming_ids)
    # Iterate new findings and regressions separately so provenance is preserved
    # in `origin` ("delta" vs "regression").
    for origin, items in (("delta", new_findings), ("regression", regressions)):
        for finding in items:
            fid = finding["id"]
            existing = index.get(fid)
            # Never let a delta overwrite an existing finding (any status): doing
            # so could downgrade or drop an unresolved critical/high. Preserve the
            # original and remap the colliding report to a fresh canonical id so
            # it stays referenceable by triage, recording the model's id as
            # `source_id`.
            if existing is not None:
                new_id = allocate()
                index[new_id] = _cumulative_finding(
                    finding,
                    fid=new_id,
                    status="open",
                    round_num=round_num,
                    origin=origin,
                    source_id=fid,
                )
                continue
            index[fid] = _cumulative_finding(
                finding,
                fid=fid,
                status="open",
                round_num=round_num,
                origin=origin,
            )
    state["cumulative_findings"] = _finalize_cumulative(index)


# Triage dispositions that release a finding from blocking completion. A finding
# left `open` (or marked `requires_human_decision`) still blocks the gate.
NON_BLOCKING_TRIAGE_STATUSES = {
    "rejected",
    "rejected_with_evidence",
    "already_resolved",
    "out_of_scope_but_recorded",
    "resolved",
}

# Triage statuses that (re)assert blocking. A later triage round can escalate a
# previously closed finding back to blocking; transitions are bidirectional so a
# reclassification cannot leave a severe finding silently released.
BLOCKING_TRIAGE_STATUSES = {"open", "requires_human_decision"}


def _triage_rationale(entry: dict[str, Any]) -> str:
    """Return the recorded justification for a triage disposition, if any."""
    for key in ("reason", "evidence", "resolution", "justification"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def apply_triage_to_cumulative(
    state: dict[str, Any], entries: list[Any]
) -> None:
    """Close cumulative findings that triage dispositions release from blocking.

    A triage entry references the finding via `finding_id` (e.g. `F-1`). When its
    `status` is a non-blocking disposition, the matching cumulative finding's
    status is updated so a validly rejected high/critical finding does not keep
    `evaluate`/`next-action` looping until the review budget is exhausted.
    """
    index = _index_findings(state)
    if not index:
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fid = entry.get("finding_id")
        status = entry.get("status")
        if fid not in index:
            continue
        finding = index[fid]
        # A blocking-intent triage status reopens a finding (the fail-safe
        # direction), so a later reclassification can re-block a previously
        # closed finding.
        if status in BLOCKING_TRIAGE_STATUSES:
            finding["status"] = "open"
            continue
        if status not in NON_BLOCKING_TRIAGE_STATUSES:
            continue
        # Closing a severe finding requires a recorded rationale so a critical
        # or high finding cannot be released from the gate by status metadata
        # alone. Non-severe findings may be closed without a written reason.
        severe = finding.get("severity") not in NON_SEVERE_SEVERITIES
        if severe and not _triage_rationale(entry):
            continue
        finding["status"] = status
    state["cumulative_findings"] = _finalize_cumulative(index)


def merge_acceptance_criteria(
    state: dict[str, Any], parsed: dict[str, Any], round_num: int
) -> None:
    """Merge a review's acceptance-criteria assessment into a cumulative ledger.

    Full reviews carry the complete assessment under
    `acceptance_criteria_assessment`; delta reviews carry only the criteria they
    touched under `affected_acceptance_criteria`. Both item shapes are
    `{id, status, evidence}`. The ledger keeps the latest disposition per id (with
    the round it was last updated) so the audit trail and the delta reviewer have
    the full criterion set, not just the most recent round's slice.

    Gate semantics are unchanged: the completion gate does not (yet) block on AC
    status; this only persists evidence that was previously discarded.
    """
    items = parsed.get("acceptance_criteria_assessment")
    if not isinstance(items, list):
        items = parsed.get("affected_acceptance_criteria", [])
    if not isinstance(items, list):
        return
    ledger = state.get("cumulative_acceptance_criteria")
    if not isinstance(ledger, list):
        ledger = []
    index: dict[str, dict[str, Any]] = {
        c["id"]: c for c in ledger if isinstance(c, dict) and c.get("id")
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            continue
        index[cid] = {
            "id": cid,
            "status": item.get("status"),
            "evidence": item.get("evidence", ""),
            "round": round_num,
        }
    state["cumulative_acceptance_criteria"] = list(index.values())


# Severities the schemas treat as non-blocking. Anything else on an open
# finding (including a missing/unknown value) fails closed and keeps blocking,
# so malformed review output cannot slip an unreported issue past the gate.
NON_SEVERE_SEVERITIES = {"low", "medium"}


def cumulative_unresolved_severe(state: dict[str, Any]) -> list[dict[str, Any]]:
    # Fail closed in two directions:
    #   * a non-dict entry has no readable status/severity, so treat it as an
    #     unresolved severe finding rather than silently skipping it; and
    #   * a severe finding blocks unless it carries an explicitly-released status
    #     (NON_BLOCKING_TRIAGE_STATUSES, e.g. `resolved`/`rejected`). A missing or
    #     unknown status must NOT be read as "not open" — otherwise a malformed or
    #     migrated finding like {"id": "F-1", "severity": "critical"} (no status)
    #     would slip past the gate.
    severe: list[dict[str, Any]] = []
    for f in state.get("cumulative_findings", []):
        if not isinstance(f, dict):
            severe.append({"id": "(malformed)", "status": "open", "severity": "high"})
            continue
        is_severe = f.get("severity") not in NON_SEVERE_SEVERITIES
        released = f.get("status") in NON_BLOCKING_TRIAGE_STATUSES
        if is_severe and not released:
            severe.append(f)
    return severe


# The only acceptance-criteria status that does not block completion. Everything
# else — `not_satisfied`, `partially_satisfied`, `not_verifiable`, and any
# missing/unknown value — blocks (fail closed): a reviewer cannot mark a run
# complete while a criterion is unmet or unverifiable.
SATISFIED_ACCEPTANCE_STATUS = "satisfied"


def blocking_acceptance_criteria(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Cumulative acceptance criteria that are not `satisfied` (fail closed)."""
    blocking: list[dict[str, Any]] = []
    for criterion in state.get("cumulative_acceptance_criteria", []):
        if not isinstance(criterion, dict):
            blocking.append({"id": "(malformed)", "status": "(unknown)"})
            continue
        if criterion.get("status") != SATISFIED_ACCEPTANCE_STATUS:
            blocking.append(criterion)
    return blocking


def _describe_blocking_acceptance_criteria(
    blocking: list[dict[str, Any]], *, limit: int = 5
) -> str:
    parts: list[str] = []
    for criterion in blocking[:limit]:
        cid = criterion.get("id", "(no id)") if isinstance(criterion, dict) else "(?)"
        status = (
            criterion.get("status", "(no status)")
            if isinstance(criterion, dict)
            else "(?)"
        )
        parts.append(f"{cid} [{status}]")
    if len(blocking) > limit:
        parts.append(f"(+{len(blocking) - limit} more)")
    return "; ".join(parts)


def _describe_blocking_findings(
    severe: list[dict[str, Any]], *, limit: int = 5, snippet: int = 80
) -> str:
    """Summarize the blocking findings for the gate failure reason.

    The cumulative ledger now stores evidence inline, so the gate can name the
    findings (id / severity / category + a short description snippet) instead of
    only counting them. Pure reporting; the block/pass decision is unchanged.
    """
    parts: list[str] = []
    for f in severe[:limit]:
        if not isinstance(f, dict):
            parts.append("(malformed)")
            continue
        fid = f.get("id", "(no id)")
        severity = f.get("severity", "(no severity)")
        category = f.get("category")
        label = f"{fid} [{severity}"
        if category:
            label += f"/{category}"
        label += "]"
        description = f.get("description")
        if isinstance(description, str) and description.strip():
            text = description.strip()
            if len(text) > snippet:
                text = text[: snippet - 1].rstrip() + "…"
            label += f" {text}"
        parts.append(label)
    if len(severe) > limit:
        parts.append(f"(+{len(severe) - limit} more)")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# cmd_doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    # Use project_root fallback for doctor; git check is optional
    start = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    failures: list[str] = []
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        failures.append("Python 3.11 or later is required")

    for executable in ("git", "codex"):
        path = shutil.which(executable)
        print(f'{executable}: {path or "not found"}')
        if path is None:
            failures.append(f"{executable} is not installed or not on PATH")

    # jsonschema is a declared runtime dependency: every Codex output, the
    # reconciliation source/decisions, and the triage ledger are validated
    # against the bundled schemas before they can affect run state. Without it
    # those structural gates cannot run, so surface a missing install here.
    try:
        import jsonschema  # noqa: F401

        jsonschema_version = getattr(jsonschema, "__version__", "unknown")
        print(f"jsonschema: {jsonschema_version}")
    except ImportError:
        print("jsonschema: not found")
        failures.append(
            "jsonschema is not installed; install the package dependencies "
            "(e.g. `pip install -e .` or `pip install 'jsonschema>=4.18'`)"
        )

    # Verify via resolve_repository as well as raw git check
    inside = git(start, "rev-parse", "--is-inside-work-tree", check=False)
    print(f'Git repository: {inside == "true"}')
    if inside != "true":
        failures.append(f"{start} is not inside a Git worktree")
    else:
        try:
            resolve_repository(start)
        except StateError as exc:
            failures.append(f"Repository resolver failed: {exc}")

    if shutil.which("codex"):
        version = run_process(["codex", "--version"], cwd=start)
        print(
            f'Codex version: {(version.stdout or version.stderr).strip() or "unknown"}'
        )
        auth = run_process(["codex", "login", "status"], cwd=start)
        print(
            f'Codex authentication: {"ready" if auth.returncode == 0 else "not ready"}'
        )
        if auth.returncode != 0:
            failures.append("Codex is not authenticated; run `codex login`")

    if failures:
        print("\nDoctor found problems:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("\nAll required local prerequisites are available.")
    return 0


# ---------------------------------------------------------------------------
# cmd_init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)

    if not repo.head_commit:
        raise WorkflowError(
            "Git repository has no commits; cannot initialize a workflow run."
        )

    feature = args.feature.strip()
    if not feature:
        raise WorkflowError("Feature idea must not be empty")

    worktree_mode = getattr(args, "worktree_mode", "isolated")
    if getattr(args, "allow_main", False) and worktree_mode != "current":
        raise WorkflowError(
            "--allow-main is only valid with --worktree-mode current."
        )

    label = ""
    if getattr(args, "label", None):
        raw_label = args.label.strip()
        label = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_label).strip("-").lower()[:80]

    # Serialize the active-run check and run creation per repository. With
    # generated IDs two concurrent inits pick different run IDs, so the per-run
    # RunStateLock cannot serialize them; without this repo-level lock both could
    # observe "no active run" and each create one. The loser sees the winner's
    # active run and fails closed (unless --force authorizes an additional run).
    with RepoInitLock(state_home, repo.id):
        active_runs = find_active_runs(state_home, repo.id)

        if active_runs:
            if args.reuse:
                if len(active_runs) > 1:
                    ids = ", ".join(r.run_id for r in active_runs)
                    raise WorkflowError(
                        f"Multiple active runs exist: {ids}. "
                        "Use --run-id to select one explicitly."
                    )
                run_ref = active_runs[0]
                run_dir = run_ref.run_dir
                state_path = run_dir / "run-state.json"
                print(state_path)
                return 0
            if not args.force:
                ids = ", ".join(r.run_id for r in active_runs)
                raise WorkflowError(
                    f"Active workflow run(s) already exist: {ids}. "
                    "Use `status`, `cancel`, `--reuse`, or `--force`."
                )

        run_id = run_id_override or new_run_id()
        run_dir = run_dir_path(state_home, repo.id, run_id)

        # Never overwrite an existing run, active or terminal. `--force` may create
        # an additional run while another is active (handled above); it must not
        # authorize clobbering an existing run ID's state.
        if (run_dir / "run-state.json").exists():
            raise WorkflowError(
                f"A run with ID {run_id!r} already exists at {run_dir}. Refusing to "
                "overwrite it. Use a different --run-id, `--reuse` to continue an "
                "active run, or `archive-run`/`list-runs` to manage existing runs."
            )

        with RunStateLock(run_dir):
            # Re-check under the lock to close the TOCTOU window against a concurrent
            # init creating the same run ID first.
            if (run_dir / "run-state.json").exists():
                raise WorkflowError(
                    f"A run with ID {run_id!r} already exists at {run_dir}. "
                    "Refusing to overwrite it."
                )
            run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

            dirty = git(
                repo.canonical_root, "status", "--porcelain", check=False
            ).splitlines()
            if worktree_mode == "current":
                if not repo.branch:
                    raise WorkflowError(
                        "Current-checkout mode does not support detached HEAD. "
                        "Check out a named branch before initializing."
                    )
                if repo.branch in {"main", "master"} and not getattr(args, "allow_main", False):
                    raise WorkflowError(
                        "Current-checkout mode refuses to initialize on "
                        f"branch {repo.branch!r}. Create and check out a feature "
                        "branch first, or pass --allow-main to override this guard "
                        "on main/master."
                    )
                if dirty:
                    preview = ", ".join(dirty[:5])
                    suffix = "" if len(dirty) <= 5 else f" (+{len(dirty) - 5} more)"
                    raise WorkflowError(
                        "Current-checkout mode requires a clean working tree. "
                        "Modified, staged, deleted, and untracked files all make "
                        "the working tree unclean. "
                        f"Dirty entries: {preview}{suffix}"
                    )

            ctx_text = repository_context(repo) + (
                f"\nWorktree mode: {worktree_mode_label(worktree_mode)}\n"
            )
            (run_dir / "feature-request.md").write_text(feature + "\n", encoding="utf-8")
            (run_dir / "repository-context.txt").write_text(ctx_text, encoding="utf-8")

            requested_mode = getattr(args, "mode", "auto")
            effective_mode, mode_reasons = select_mode(requested_mode, feature)
            # `auto`/explicit rigorous runs are safety-sensitive: require adversarial.
            risk_reasons: list[str] = []
            requires_adversarial = effective_mode == "rigorous"
            if requires_adversarial:
                risk_reasons.append(
                    f"{effective_mode} mode selected (requested={requested_mode})"
                )

            state: dict[str, Any] = {
                "schema_version": 2,
                "run_id": run_id,
                "label": label,
                "feature": feature,
                "status": "active",
                "phase": "initialized",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "repository": repository_state_block(repo, worktree_mode=worktree_mode),
                "baseline": {
                    "commit": repo.head_commit,
                    "branch": repo.branch,
                    "dirty_entries_at_init": dirty,
                },
                "requested_mode": requested_mode,
                "effective_mode": effective_mode,
                "mode_reasons": mode_reasons,
                "max_review_rounds": args.max_review_rounds,
                "review_round": 0,
                "stop_gate_blocks": 0,
                "artifacts": {
                    "feature_request": "feature-request.md",
                    "repository_context": "repository-context.txt",
                },
                "verification": {"checks": [], "passed": False},
                "reviews": [],
                "adversarial_reviews": [],
                "cumulative_findings": [],
                "cumulative_acceptance_criteria": [],
                "review_ledger": [],
                "codex_runs": [],
                "risk": {
                    "requires_adversarial_review": requires_adversarial,
                    "reasons": risk_reasons,
                },
                "notes": [],
            }
            save_run_state(run_dir, state)

        # Save repo metadata while still holding RepoInitLock. Two concurrent
        # `init --force` processes share this one metadata.json; updating it
        # outside the lock would let their read-modify-write cycles interleave
        # and lose one update (or observe a half-written file). Keeping it inside
        # the repository-level lock serializes the metadata mutation too.
        meta = load_repo_metadata(state_home, repo.id)
        meta.update(
            {
                "id": repo.id,
                "display_name": repo.display_name,
                "canonical_root": str(repo.canonical_root),
                "remote_display": repo.remote_display,
                "last_run_id": run_id,
            }
        )
        save_repo_metadata(state_home, repo.id, meta)

    print(run_dir / "run-state.json")
    return 0


# ---------------------------------------------------------------------------
# cmd_codex
# ---------------------------------------------------------------------------


def cmd_codex(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="codex"
    )
    state = run_ref.state
    run_dir = run_ref.run_dir

    require_no_unsafe_drift(state, repo)

    if state.get("status") != "active":
        raise WorkflowError(f"Workflow is not active: {state.get('status')}")

    phase = args.phase
    prompt_rel, schema_rel, static_output = PHASE_OUTPUTS[phase]

    if phase == "plan":
        spec_path = resolve_artifact_path(
            state.get("artifacts", {}).get("accepted_spec", "accepted-spec.md"), run_dir
        )
        if not spec_path.exists():
            raise WorkflowError(
                "Create accepted-spec.md (in the run directory) before planning"
            )
    if phase in {"review", "adversarial"}:
        plan_path = resolve_artifact_path(
            state.get("artifacts", {}).get("accepted_plan", "accepted-plan.md"), run_dir
        )
        if not plan_path.exists():
            raise WorkflowError(
                "Create accepted-plan.md (in the run directory) before review"
            )
        if not state.get("verification", {}).get("checks"):
            raise WorkflowError("Record at least one verification check before review")

    is_delta_review = False
    if phase == "review":
        next_round = int(state.get("review_round", 0)) + 1
        maximum = int(state.get("max_review_rounds", 3))
        if next_round > maximum:
            with RunStateLock(run_dir):
                fresh = load_run_state(run_dir)
                verify_loaded_run_identity(
                    fresh, run_dir=run_dir, expected_repo_id=repo.id
                )
                # The exhaustion decision was made from a pre-lock snapshot. A
                # concurrent cancel/block may have made the run terminal in the
                # meantime; flipping it to "blocked" now would overwrite that
                # terminal decision (terminal-to-terminal) and rewrite the user's
                # cancellation. Require an exactly-active run before recording
                # exhaustion so the concurrent decision sticks.
                require_active_run_state(
                    fresh, run_ref.run_id, "mark review budget exhausted"
                )
                # Recompute from the fresh state: a concurrent invocation may have
                # changed review_round/max_review_rounds. If it is no longer
                # exhausted, do not block on the stale pre-lock decision — signal a
                # retryable state change instead.
                fresh_round = int(fresh.get("review_round", 0)) + 1
                fresh_max = int(fresh.get("max_review_rounds", 3))
                if fresh_round <= fresh_max:
                    raise WorkflowError(
                        "Review budget changed concurrently (now round "
                        f"{fresh_round} of {fresh_max}); retry the review."
                    )
                fresh["status"] = "blocked"
                fresh["phase"] = "review-budget-exhausted"
                fresh.setdefault("notes", []).append(
                    f"Maximum review rounds exhausted ({fresh_max})"
                )
                save_run_state(run_dir, fresh)
            raise WorkflowError(f"Maximum review rounds exhausted ({fresh_max})")
        # Round 1 is a full review; rounds 2+ use the compact delta schema/prompt.
        # A delta review only carries forward findings relative to a recorded
        # full-review baseline (which seeds cumulative_findings). If no full
        # review has ever been recorded for this run (e.g. a run migrated from an
        # older state, or whose round-1 artifact was lost), a delta `pass` with
        # no new findings could clear the gate without any baseline of severe
        # findings ever being established. Require a recorded full review before
        # allowing delta mode; otherwise fall back to a full review that re-seeds
        # the cumulative ledger.
        has_full_review = any(
            isinstance(r, dict) and r.get("delta") is False
            for r in state.get("reviews", [])
        )
        is_delta_review = next_round >= 2 and has_full_review
        if is_delta_review:
            prompt_rel = REVIEW_DELTA_PROMPT
            schema_rel = REVIEW_DELTA_SCHEMA
        output_name = f"review-{next_round:02d}.codex.json"
    elif phase == "adversarial":
        index = len(state.get("adversarial_reviews", [])) + 1
        output_name = f"adversarial-{index:02d}.codex.json"
    else:
        output_name = static_output
        assert output_name is not None

    template = (PLUGIN_ROOT / prompt_rel).read_text(encoding="utf-8")
    values = prompt_values(run_dir, state)
    if is_delta_review:
        values["CHANGED_SINCE_LAST_REVIEW"] = render_changed_since_previous(repo, state)
    prompt = render(template, values)
    prompt_path = run_dir / f"{phase}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    # Stage Codex output/events under invocation-unique names so a concurrent or
    # overlapping retry of the same phase cannot clobber this invocation's
    # artifacts; the canonical round files are published under the lock below.
    stage_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    output_path = run_dir / f".staging-{stage_id}.codex.json"

    profile = resolve_phase_profile(phase)
    command = [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(PLUGIN_ROOT / schema_rel),
        "--output-last-message",
        str(output_path),
        *codex_profile_args(profile),
        "-",
    ]
    started_at = utc_now()
    started_monotonic = time.monotonic()
    result = run_process(
        command,
        cwd=repo.canonical_root,
        input_text=prompt,
        timeout=getattr(args, "timeout", None),
    )
    duration_seconds = round(time.monotonic() - started_monotonic, 1)

    # Canonical name (under the lock) for the raw NDJSON event stream; the
    # staging write happens inside the guarded block below so a write failure
    # (disk full / permission) triggers the same deterministic cleanup as any
    # later failure instead of orphaning the already-written Codex output.
    events_path = run_dir / f".staging-{stage_id}.events.ndjson"

    if result.returncode != 0:
        # Stage the failure log under an invocation-unique name first. The status
        # was checked before the (long) Codex exec; a concurrent cancel/block may
        # have driven the run terminal while Codex ran. Publishing the canonical
        # error log and appending a note without re-checking would mutate — and so
        # resurrect — a terminal run. Re-validate identity and exact-active status
        # under the lock, and only then publish; otherwise leave the run untouched.
        staged_error = run_dir / f".staging-{stage_id}.stderr.log"
        staged_error.write_text(result.stderr, encoding="utf-8")
        for staged in (output_path, events_path):
            staged.unlink(missing_ok=True)
        codex_failure = result.stderr.strip() or f"Codex {phase} failed"
        with RunStateLock(run_dir):
            err_state = load_run_state(run_dir)
            verify_loaded_run_identity(
                err_state, run_dir=run_dir, expected_repo_id=repo.id
            )
            try:
                require_active_run_state(
                    err_state, run_ref.run_id, f"record Codex {phase} failure"
                )
            except WorkflowError as status_exc:
                # The run became terminal while Codex ran. Do not modify it: drop
                # the staged log and report both the Codex failure and the status
                # change without touching the run.
                staged_error.unlink(missing_ok=True)
                raise WorkflowError(
                    f"Codex {phase} failed ({codex_failure}); the run is no "
                    f"longer active and was left unchanged: {status_exc}"
                ) from status_exc
            error_path = run_dir / f"{phase}.codex.stderr.log"
            staged_error.replace(error_path)
            err_state.setdefault("notes", []).append(
                f"Codex {phase} failed; see {make_relative_path(error_path, run_dir)}"
            )
            save_run_state(run_dir, err_state)
        raise WorkflowError(codex_failure)

    # Any failure after this point (NDJSON staging, parse, schema validation,
    # locked merge) must not leave the invocation-unique staging files on disk:
    # they hold the raw prompt response / NDJSON event stream and would otherwise
    # accumulate and retain sensitive content across retries. Success publishes
    # them to canonical names under the lock (so `published` is set only once
    # that completes).
    staged_output, staged_events = output_path, events_path
    published = False
    try:
        events_path.write_text(result.stdout, encoding="utf-8")
        try:
            output_text = output_path.read_text(encoding="utf-8")
            parsed = json.loads(output_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"Codex did not produce valid JSON at {output_path}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise WorkflowError(f"Codex output must be an object: {output_path}")
        # Full Draft 2020-12 schema validation. Fail closed if a (e.g. downgraded)
        # Codex CLI returns syntactically valid but schema-violating output: a
        # delta review missing `new_findings`, a mistyped `new_findings: {}`, or a
        # finding with an out-of-enum severity must all be rejected before the
        # cumulative merge rather than silently mis-merged. This validates nested
        # finding/criterion items too, not just top-level keys.
        try:
            validate_payload(
                parsed, schema_rel, label=f"Codex {phase} output at {output_path}"
            )
        except SchemaValidationError as exc:
            raise WorkflowError(str(exc)) from exc
        # Defense in depth before publishing the canonical artifact under the lock:
        # the cumulative merge also fails closed on malformed finding entries, but
        # checking here keeps a malformed payload from being renamed to its
        # canonical name and left behind when the merge subsequently rejects it.
        if phase == "review":
            if is_delta_review:
                _require_finding_items(
                    list(parsed.get("new_findings", []))
                    + list(parsed.get("regressions", [])),
                    "new_findings/regressions",
                )
            else:
                _require_finding_items(list(parsed.get("findings", [])), "findings")

        token_usage = parse_codex_usage(result.stdout)
        # Prefer the concrete model reported by Codex; fall back to the explicit
        # profile model, then a placeholder when the model is inherited from config.
        recorded_model = (
            parse_codex_model(result.stdout) or profile.get("model") or "(default)"
        )

        # Recompute round/index from fresh state inside the lock to close the
        # TOCTOU gap between the pre-Codex snapshot check and the post-Codex write.
        final_path = output_path
        with RunStateLock(run_dir):
            state = load_run_state(run_dir)
            verify_loaded_run_identity(
                state, run_dir=run_dir, expected_repo_id=repo.id
            )
            # Status was checked before the (long) Codex execution. A concurrent
            # `cancel`/`block` may have driven the run to a terminal state while
            # Codex ran; merging now would append reviews/findings and reset the
            # phase, effectively resurrecting a cancelled/blocked run. Re-check
            # under the lock and fail closed so terminal decisions stick.
            if state.get("status") != "active":
                raise WorkflowError(
                    "Run is no longer active "
                    f"(status={state.get('status')!r}); refusing to merge "
                    f"{phase} output produced before the status change."
                )
            phase_label = phase
            if phase == "review":
                next_round = int(state.get("review_round", 0)) + 1
                maximum = int(state.get("max_review_rounds", 3))
                if next_round > maximum:
                    state["status"] = "blocked"
                    state["phase"] = "review-budget-exhausted"
                    state.setdefault("notes", []).append(
                        f"Maximum review rounds exhausted ({maximum})"
                    )
                    save_run_state(run_dir, state)
                    raise WorkflowError(
                        f"Maximum review rounds exhausted ({maximum})"
                    )
                # The round mode (full vs delta) and the schema/prompt used to
                # drive Codex were chosen before acquiring the lock, from a
                # pre-lock round snapshot. If a concurrent same-run invocation
                # advanced the round in between, the locked round may no longer
                # match the mode this payload was produced under. Merging a full
                # review as a delta (or vice versa) silently corrupts the
                # cumulative ledger, so fail closed instead.
                has_full_review = any(
                    isinstance(r, dict) and r.get("delta") is False
                    for r in state.get("reviews", [])
                )
                expected_delta = next_round >= 2 and has_full_review
                if expected_delta != is_delta_review:
                    raise WorkflowError(
                        "Review round-mode mismatch (concurrent invocation?): "
                        f"payload was produced as a "
                        f"{'delta' if is_delta_review else 'full'} review but "
                        f"round {next_round} under the lock requires a "
                        f"{'delta' if expected_delta else 'full'} review; "
                        "refusing to merge with inconsistent semantics."
                    )
                canonical = run_dir / f"review-{next_round:02d}.codex.json"
                if output_path != canonical:
                    output_path.replace(canonical)
                final_path = canonical
                phase_label = f"review-{next_round:02d}"
            elif phase == "adversarial":
                index = len(state.get("adversarial_reviews", [])) + 1
                canonical = run_dir / f"adversarial-{index:02d}.codex.json"
                if output_path != canonical:
                    output_path.replace(canonical)
                final_path = canonical
                phase_label = f"adversarial-{index:02d}"
            else:
                # Static-name phases (enhance/plan): publish the staged output to
                # the fixed canonical name under the lock.
                canonical = run_dir / output_name
                if output_path != canonical:
                    output_path.replace(canonical)
                final_path = canonical
            # Keep the events artifact name aligned with the canonical round so the
            # recorded `events_artifact` cannot be misattributed if the round
            # number changed between the pre-Codex snapshot and this locked write.
            events_canonical = (
                run_dir / f"{final_path.stem.replace('.codex', '')}.events.ndjson"
            )
            if events_path != events_canonical and events_path.exists():
                events_path.replace(events_canonical)
                events_path = events_canonical
            state.setdefault("artifacts", {})[phase] = make_relative_path(
                final_path, run_dir
            )
            if phase == "enhance":
                state["phase"] = "idea-enhanced"
            elif phase == "plan":
                state["phase"] = "plan-proposed"
            elif phase == "review":
                state["review_round"] = next_round
                state["phase"] = "reviewed"
                # Capture the checkpoint before appending so previous_checkpoint_id
                # resolves to the prior round's checkpoint.
                checkpoint = capture_review_checkpoint(
                    repo, state, checkpoint_id=phase_label
                )
                state.setdefault("reviews", []).append(
                    {
                        "round": next_round,
                        "path": make_relative_path(final_path, run_dir),
                        "verdict": parsed.get("verdict"),
                        "delta": is_delta_review,
                        "checkpoint": checkpoint,
                    }
                )
                if is_delta_review:
                    merge_delta_review(state, parsed, next_round)
                else:
                    merge_full_review(state, parsed, next_round)
                merge_acceptance_criteria(state, parsed, next_round)
            elif phase == "adversarial":
                state["phase"] = "adversarially-reviewed"
                state.setdefault("adversarial_reviews", []).append(
                    {
                        "round": index,
                        "path": make_relative_path(final_path, run_dir),
                        "verdict": parsed.get("verdict"),
                    }
                )

            usage_record: dict[str, Any] = {
                "phase": phase_label,
                "prompt_characters": len(prompt),
                "output_characters": len(output_text),
                "duration_seconds": duration_seconds,
                "model": recorded_model,
                "reasoning_effort": profile.get("reasoning"),
                "verbosity": profile.get("verbosity"),
                "started_at": started_at,
                "events_artifact": make_relative_path(events_path, run_dir),
                "output_artifact": make_relative_path(final_path, run_dir),
            }
            if token_usage:
                usage_record["tokens"] = token_usage
            state.setdefault("codex_runs", []).append(usage_record)

            state["stop_gate_blocks"] = 0
            save_run_state(run_dir, state)
        published = True
    finally:
        # On any failure before the locked publish completes, remove the
        # invocation-unique staging files so partial/invalid prompt responses and
        # event streams are not retained on disk across retries.
        if not published:
            for staged in (staged_output, staged_events):
                staged.unlink(missing_ok=True)
    print(final_path)
    return 0


# ---------------------------------------------------------------------------
# cmd_accept
# ---------------------------------------------------------------------------


def _decision_maps(
    decisions: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    reject_map: dict[str, str] = {}
    for entry in decisions.get("reject", []):
        if isinstance(entry, dict) and "id" in entry:
            reject_map[str(entry["id"])] = str(entry.get("reason", ""))
    modify_map: dict[str, str] = {}
    for entry in decisions.get("modify", []):
        if isinstance(entry, dict) and "id" in entry:
            modify_map[str(entry["id"])] = str(entry.get("replacement", ""))
    return reject_map, modify_map


def _apply_decisions_to_items(
    items: list[Any],
    id_key: str,
    text_key: str,
    reject_map: dict[str, str],
    modify_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Keep each item unless explicitly rejected; apply text modifications.

    Items that are neither rejected nor modified are accepted verbatim. This
    keeps the accepted artifact complete by default, reducing accidental omission.
    """
    kept: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = str(item.get(id_key, ""))
        if iid in reject_map:
            continue
        new_item = dict(item)
        if iid in modify_map:
            new_item[text_key] = modify_map[iid]
        kept.append(new_item)
    return kept


def _render_spec_markdown(a: dict[str, Any]) -> str:
    lines = [f"# Accepted specification — {a.get('title', '')}".rstrip(), ""]
    if a.get("problem_statement"):
        lines += ["## Problem statement", "", a["problem_statement"], ""]
    lines += ["## Functional requirements", ""]
    for fr in a["functional_requirements"]:
        priority = fr.get("priority")
        suffix = f" ({priority})" if priority else ""
        lines.append(f"- **{fr.get('id', '')}**{suffix}: {fr.get('requirement', '')}")
    lines += ["", "## Acceptance criteria", ""]
    for ac in a["acceptance_criteria"]:
        lines.append(f"- **{ac.get('id', '')}**: {ac.get('criterion', '')}")
    if a.get("non_functional_requirements"):
        lines += ["", "## Non-functional requirements", ""]
        lines += [f"- {n}" for n in a["non_functional_requirements"]]
    if a.get("non_goals"):
        lines += ["", "## Non-goals", ""]
        lines += [f"- {n}" for n in a["non_goals"]]
    if a.get("added"):
        lines += ["", "## Added during reconciliation", ""]
        lines += [f"- {json.dumps(item, sort_keys=True)}" for item in a["added"]]
    if a.get("rejected"):
        lines += ["", "## Rejected (with reasons)", ""]
        lines += [f"- {r['id']}: {r['reason']}" for r in a["rejected"]]
    return "\n".join(lines) + "\n"


def _render_plan_markdown(a: dict[str, Any]) -> str:
    lines = ["# Accepted implementation plan", ""]
    if a.get("summary"):
        lines += [a["summary"], ""]
    lines += ["## Steps", ""]
    for step in a["implementation_steps"]:
        files = step.get("files") or []
        files_str = f" (files: {', '.join(files)})" if files else ""
        lines.append(
            f"{step.get('order', '?')}. **{step.get('id', '')}** "
            f"{step.get('description', '')}{files_str}"
        )
    if a.get("added"):
        lines += ["", "## Added during reconciliation", ""]
        lines += [f"- {json.dumps(item, sort_keys=True)}" for item in a["added"]]
    if a.get("rejected"):
        lines += ["", "## Rejected (with reasons)", ""]
        lines += [f"- {r['id']}: {r['reason']}" for r in a["rejected"]]
    return "\n".join(lines) + "\n"


def _source_item_ids(kind: str, source: dict[str, Any]) -> set[str]:
    """Collect the ids a reconciliation delta may legitimately reference."""
    ids: set[str] = set()
    if kind == "spec":
        for key in ("functional_requirements", "acceptance_criteria"):
            for item in source.get(key, []):
                if isinstance(item, dict) and "id" in item:
                    ids.add(str(item["id"]))
    else:
        for step in source.get("implementation_steps", []):
            if isinstance(step, dict):
                ids.add(str(step.get("id", f"S{step.get('order', '?')}")))
    return ids


def _validate_decision_ids(
    kind: str, source: dict[str, Any], decisions: dict[str, Any]
) -> None:
    """Fail closed when accept/reject/modify target ids absent from the source.

    A silent typo (e.g. `AC-21` for `AC-12`) would otherwise leave the intended
    change unapplied while the materialized artifact looks complete.
    """
    valid = _source_item_ids(kind, source)
    referenced: list[str] = []
    for entry in decisions.get("accept", []):
        referenced.append(str(entry))
    for key in ("reject", "modify"):
        for entry in decisions.get(key, []):
            if isinstance(entry, dict) and "id" in entry:
                referenced.append(str(entry["id"]))
    unknown = sorted({rid for rid in referenced if rid not in valid})
    if unknown:
        raise WorkflowError(
            "Reconciliation decisions reference unknown source id(s): "
            f"{', '.join(unknown)}. Known ids: {', '.join(sorted(valid)) or '(none)'}"
        )


def _validate_decision_shape(decisions: dict[str, Any]) -> None:
    """Fail closed on malformed decision containers/entries.

    A directive supplied with the wrong shape (e.g. ``"reject": "FR-3"`` instead
    of a list of objects) would otherwise be silently skipped, leaving the
    intended change unapplied while the materialized artifact still looks
    complete.
    """
    for key in ("accept", "reject", "modify", "add"):
        if key in decisions and not isinstance(decisions[key], list):
            raise WorkflowError(
                f"Reconciliation decisions field '{key}' must be a list, got "
                f"{type(decisions[key]).__name__}."
            )
    for entry in decisions.get("accept", []):
        if not isinstance(entry, (str, int)):
            raise WorkflowError(
                "Each 'accept' entry must be an id scalar, got "
                f"{type(entry).__name__}."
            )
    required_field = {"reject": "reason", "modify": "replacement"}
    for key in ("reject", "modify"):
        field = required_field[key]
        for entry in decisions.get(key, []):
            if not isinstance(entry, dict) or "id" not in entry:
                raise WorkflowError(
                    f"Each '{key}' entry must be an object with an 'id'; got "
                    f"{json.dumps(entry)[:80]}."
                )
            value = entry.get(field)
            # Fail closed: a `modify` without a `replacement` would otherwise
            # blank the accepted item text; a `reject` without a `reason` would
            # leave an unauditable rejection.
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError(
                    f"'{key}' entry for id {entry['id']!r} requires a non-empty "
                    f"'{field}'."
                )


def _validate_source_sections(kind: str, source: dict[str, Any]) -> None:
    """Fail closed when the reconciliation source is missing or malformed.

    Guards against pointing `accept --source` at the wrong/malformed JSON, which
    would otherwise materialize an empty accepted spec/plan and silently weaken
    downstream review against a blank contract. A non-empty section whose items
    are not objects is just as dangerous: _apply_decisions_to_items (and the
    plan-step filter) silently drop non-dict entries, so a section of bare
    strings would pass a length check yet materialize to nothing. Reject such
    items loudly rather than producing a blank contract.
    """
    primary = "functional_requirements" if kind == "spec" else "implementation_steps"
    section = source.get(primary)
    if not isinstance(section, list) or not section:
        raise WorkflowError(
            f"Reconciliation source for kind '{kind}' must contain a non-empty "
            f"'{primary}' section; refusing to materialize a blank accepted artifact."
        )
    # Validate item shape for every section that materialize_acceptance feeds
    # through the (silently-dropping) item filters. acceptance_criteria is
    # optional, so only validate it when present; the primary section is always
    # checked because it must be non-empty.
    item_sections = (
        ("functional_requirements", "acceptance_criteria")
        if kind == "spec"
        else ("implementation_steps",)
    )
    for key in item_sections:
        value = source.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise WorkflowError(
                f"Reconciliation source '{key}' must be a list when present."
            )
        for item in value:
            if not isinstance(item, dict):
                raise WorkflowError(
                    f"Each '{key}' entry must be an object; got "
                    f"{json.dumps(item)[:80]} — refusing to silently drop it from "
                    "the accepted artifact (fail closed)."
                )


def materialize_acceptance(
    kind: str, source: dict[str, Any], decisions: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Deterministically materialize an accepted spec/plan from a reconciliation delta."""
    _validate_source_sections(kind, source)
    # Structural validation against the bundled decision schema first (rejects
    # unknown keys / mistyped containers), then the semantic checks below which
    # enforce non-empty reasons/replacements with targeted messages.
    try:
        validate_payload(
            decisions,
            "schemas/accept-decisions.schema.json",
            label="Reconciliation decisions",
        )
    except SchemaValidationError as exc:
        raise WorkflowError(str(exc)) from exc
    _validate_decision_shape(decisions)
    _validate_decision_ids(kind, source, decisions)
    reject_map, modify_map = _decision_maps(decisions)
    rejected = [{"id": k, "reason": v} for k, v in sorted(reject_map.items())]
    added = list(decisions.get("add", []))

    if kind == "spec":
        frs = _apply_decisions_to_items(
            source.get("functional_requirements", []),
            "id",
            "requirement",
            reject_map,
            modify_map,
        )
        acs = _apply_decisions_to_items(
            source.get("acceptance_criteria", []),
            "id",
            "criterion",
            reject_map,
            modify_map,
        )
        accepted = {
            "kind": "spec",
            "title": source.get("title", ""),
            "problem_statement": source.get("problem_statement", ""),
            "functional_requirements": frs,
            "acceptance_criteria": acs,
            "non_functional_requirements": source.get(
                "non_functional_requirements", []
            ),
            "non_goals": source.get("non_goals", []),
            "added": added,
            "rejected": rejected,
            "decisions": decisions,
        }
        return accepted, _render_spec_markdown(accepted)

    steps_src: list[dict[str, Any]] = []
    for step in source.get("implementation_steps", []):
        if isinstance(step, dict):
            enriched = dict(step)
            enriched.setdefault("id", f"S{enriched.get('order', '?')}")
            steps_src.append(enriched)
    steps = _apply_decisions_to_items(
        steps_src, "id", "description", reject_map, modify_map
    )
    accepted = {
        "kind": "plan",
        "summary": source.get("summary", ""),
        "implementation_steps": steps,
        "added": added,
        "rejected": rejected,
        "decisions": decisions,
    }
    return accepted, _render_plan_markdown(accepted)


def _resolve_source_path(
    source_arg: str, run_dir: Path, label: str = "Source artifact"
) -> Path:
    # Resolve against the run directory first so a bare artifact filename (the
    # form the skill recommends, e.g. `implementation-plan.codex.json`) always
    # binds to this run's artifact rather than a same-named file shadowing it
    # from the current working directory / repository — which could otherwise
    # feed an unintended source into acceptance and suppress risk/adversarial
    # gating. An absolute path (the form the skill uses for orchestrator-authored
    # decisions/triage ledgers, e.g. /tmp/claude/...) still binds to that literal
    # path because `run_dir / "/abs"` yields the absolute path, so this run-dir
    # preference is shadow-safe without breaking the documented workflow.
    in_run = run_dir / source_arg
    if in_run.is_file():
        return in_run.resolve()
    candidate = Path(source_arg)
    if candidate.is_file():
        return candidate.resolve()
    raise WorkflowError(f"{label} not found: {source_arg}")


def cmd_accept(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="accept"
    )
    state = run_ref.state
    run_dir = run_ref.run_dir
    run_id = run_ref.run_id

    require_no_unsafe_drift(state, repo)

    kind = args.kind
    md_name = "accepted-spec.md" if kind == "spec" else "accepted-plan.md"
    json_name = "accepted-spec.json" if kind == "spec" else "accepted-plan.json"
    destination = run_dir / md_name

    # Build the artifact content in memory OUTSIDE the lock. Nothing is written to
    # a canonical path here, so a failure (bad input, cancellation) cannot leave a
    # half-written accepted-spec.md or a stale accepted-spec.json behind.
    md_text: str
    json_text: str | None = None
    if getattr(args, "decisions", None):
        if not getattr(args, "source", None):
            raise WorkflowError("--decisions requires --source <codex-json>")
        source_path = _resolve_source_path(args.source, run_dir)
        decisions_path = _resolve_source_path(
            args.decisions, run_dir, label="Decisions file"
        )
        try:
            source_obj = json.loads(source_path.read_text(encoding="utf-8"))
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"Cannot read structured acceptance inputs: {exc}"
            ) from exc
        if not isinstance(source_obj, dict) or not isinstance(decisions, dict):
            raise WorkflowError("Source and decisions must each be a JSON object")
        # Fully validate the structured acceptance source against its phase
        # schema before materializing: the accepted artifact becomes the contract
        # that downstream review and the completion gate are judged against, so a
        # malformed/wrong-shaped source (mistyped sections, bad FR-/AC- ids,
        # missing evidence) must be rejected, not silently degraded.
        source_schema = (
            "schemas/enhanced-idea.schema.json"
            if kind == "spec"
            else "schemas/implementation-plan.schema.json"
        )
        try:
            validate_payload(
                source_obj,
                source_schema,
                label=f"Reconciliation source for kind '{kind}'",
            )
        except SchemaValidationError as exc:
            raise WorkflowError(str(exc)) from exc
        accepted_obj, md_text = materialize_acceptance(kind, source_obj, decisions)
        json_text = json.dumps(accepted_obj, indent=2, sort_keys=True) + "\n"
    else:
        if not getattr(args, "file", None):
            raise WorkflowError("Provide either --file or --source with --decisions")
        source = Path(args.file).resolve()
        if not source.is_file():
            raise WorkflowError(f"Accepted artifact does not exist: {source}")
        md_text = source.read_text(encoding="utf-8")

    accepted_risks = classify_feature_risk(md_text)

    # Stage each artifact to an invocation-unique temp path on the same filesystem
    # so it can be published with an atomic os.replace under the lock.
    stage_token = uuid.uuid4().hex
    staged: list[tuple[Path, Path]] = []  # (temp, canonical)
    md_tmp = run_dir / f".{md_name}.{stage_token}.tmp"
    md_tmp.write_text(md_text, encoding="utf-8")
    staged.append((md_tmp, destination))
    if json_text is not None:
        json_tmp = run_dir / f".{json_name}.{stage_token}.tmp"
        json_tmp.write_text(json_text, encoding="utf-8")
        staged.append((json_tmp, run_dir / json_name))

    try:
        with RunStateLock(run_dir):
            state = load_run_state(run_dir)
            verify_loaded_run_identity(
                state, run_dir=run_dir, expected_repo_id=repo.id
            )
            require_active_run_state(state, run_id, "accept")
            # Publish all artifacts and the state as one all-or-nothing unit. Each
            # canonical file that already exists is moved to an invocation-unique
            # backup before being overwritten; on ANY failure (a later
            # os.replace, the state save) every published artifact is rolled back
            # to its pre-accept bytes, so a partial publication can never leave the
            # canonical artifacts and run state inconsistent.
            published: list[tuple[Path, Path | None]] = []  # (canonical, backup|None)
            try:
                for tmp_path, canonical_path in staged:
                    backup: Path | None = None
                    if canonical_path.exists():
                        backup = canonical_path.with_name(
                            f".{canonical_path.name}.{stage_token}.bak"
                        )
                        os.replace(canonical_path, backup)
                    # Record before the publish replace so rollback can undo even
                    # if this replace itself fails after the backup move.
                    published.append((canonical_path, backup))
                    os.replace(tmp_path, canonical_path)
                state.setdefault("artifacts", {})[f"accepted_{kind}"] = md_name
                if json_text is not None:
                    state["artifacts"][f"accepted_{kind}_json"] = json_name
                state["phase"] = "spec-accepted" if kind == "spec" else "plan-accepted"
                # Risk is sticky upward: if the accepted artifact reveals high-risk
                # scope that the initial feature text did not, escalate the
                # adversarial gate. Never downgrade an already-required gate here.
                risk = state.setdefault("risk", {})
                if accepted_risks and not risk.get("requires_adversarial_review"):
                    risk["requires_adversarial_review"] = True
                    risk.setdefault("reasons", []).append(
                        f"accepted {kind} escalated to rigorous: detected "
                        f"{', '.join(accepted_risks)}"
                    )
                state["stop_gate_blocks"] = 0
                save_run_state(run_dir, state)
            except BaseException:
                # Roll back published artifacts to their pre-accept state. The run
                # state file is written atomically (temp+replace), so a failed save
                # leaves the prior state intact; restoring the artifacts therefore
                # restores full artifact/state consistency.
                for canonical_path, backup in reversed(published):
                    if backup is not None:
                        if backup.exists():
                            os.replace(backup, canonical_path)
                    else:
                        canonical_path.unlink(missing_ok=True)
                raise
            else:
                for _canonical, backup in published:
                    if backup is not None:
                        backup.unlink(missing_ok=True)
    finally:
        for tmp_path, _ in staged:
            tmp_path.unlink(missing_ok=True)
    print(destination)
    return 0


# ---------------------------------------------------------------------------
# cmd_run_check
# ---------------------------------------------------------------------------


def cmd_run_check(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="run-check"
    )
    run_dir = run_ref.run_dir
    run_id = run_ref.run_id

    require_no_unsafe_drift(run_ref.state, repo)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise WorkflowError("Provide a verification command after `--`")

    verification_dir = run_dir / "verification"
    verification_dir.mkdir(parents=True, exist_ok=True)

    # Run the check outside the lock — may be long-running.
    started = utc_now()
    started_monotonic = time.monotonic()
    result = run_process(
        command, cwd=repo.canonical_root, timeout=getattr(args, "timeout", None)
    )
    duration_seconds = round(time.monotonic() - started_monotonic, 1)
    completed = utc_now()

    # Acquire lock to compute a collision-free index and persist atomically.
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        # TOCTOU guard: a `cancel`/`block` may have driven the run terminal while
        # this (possibly long) check ran. Publishing now would resurrect it.
        require_active_run_state(state, run_id, "run-check")
        index = len(state.get("verification", {}).get("checks", [])) + 1
        log_path = verification_dir / f"{index:02d}-{slug(args.name)}.log"
        combined = (
            f"COMMAND: {json.dumps(command)}\n"
            f"STARTED: {started}\n"
            f"EXIT CODE: {result.returncode}\n\n"
            f"STDOUT\n{result.stdout}\n\nSTDERR\n{result.stderr}\n"
        )
        log_path.write_text(combined, encoding="utf-8")
        check_record = {
            "name": args.name,
            "command": command,
            "exit_code": result.returncode,
            "duration_seconds": duration_seconds,
            "log": make_relative_path(log_path, run_dir),
            "started_at": started,
            "completed_at": completed,
        }
        state.setdefault("verification", {}).setdefault("checks", []).append(
            check_record
        )
        checks = state["verification"]["checks"]
        effective_checks = latest_verification_checks(checks)
        state["verification"]["passed"] = bool(effective_checks) and all(
            c["exit_code"] == 0 for c in effective_checks
        )
        state["phase"] = (
            "verified" if state["verification"]["passed"] else "verification-failed"
        )
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)

    output_mode = getattr(args, "output", "summary")
    command_str = " ".join(command)
    if output_mode == "full":
        # Full troubleshooting output: replay the complete streams.
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        print(f"\nVerification log: {log_path}", file=sys.stderr)
    elif result.returncode == 0:
        print(f"✓ {args.name} passed in {duration_seconds} s")
        print(f"  command: {command_str}")
        print(f"  full log: {log_path}")
    else:
        tail_n = max(0, int(getattr(args, "failure_tail_lines", 80)))
        combined_streams = f"{result.stdout}{result.stderr}"
        tail_lines = combined_streams.splitlines()[-tail_n:] if tail_n else []
        print(
            f"✗ {args.name} failed with exit code {result.returncode}",
            file=sys.stderr,
        )
        print(f"  command: {command_str}", file=sys.stderr)
        if tail_lines:
            print(f"  showing final {len(tail_lines)} lines", file=sys.stderr)
            for line in tail_lines:
                print(f"  {line}", file=sys.stderr)
        print(f"  full log: {log_path}", file=sys.stderr)
    return result.returncode


# ---------------------------------------------------------------------------
# cmd_set_phase
# ---------------------------------------------------------------------------


def cmd_set_phase(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="set-phase"
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        require_active_run_state(state, run_ref.run_id, "set-phase")
        require_no_unsafe_drift(state, repo)
        state["phase"] = args.phase
        if args.note:
            state.setdefault("notes", []).append(args.note)
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    print(args.phase)
    return 0


# ---------------------------------------------------------------------------
# cmd_set_risk
# ---------------------------------------------------------------------------


def cmd_set_risk(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="set-risk"
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        require_active_run_state(state, run_ref.run_id, "set-risk")
        require_no_unsafe_drift(state, repo)
        risk = state.setdefault("risk", {})
        currently_required = bool(risk.get("requires_adversarial_review"))
        # Monotonic-upward gate: once adversarial review is required (auto/rigorous
        # escalation or a prior explicit request), set-risk may raise the bar but
        # must not silently lower it. Otherwise a high-risk run could be downgraded
        # post-init and complete without the mandatory adversarial review.
        if currently_required and not args.require_adversarial:
            raise WorkflowError(
                "Refusing to clear requires_adversarial_review: the adversarial "
                "review gate is monotonic-upward once set, so a high-risk run "
                "cannot be downgraded past the adversarial completion gate."
            )
        risk["requires_adversarial_review"] = args.require_adversarial
        if args.reason:
            risk.setdefault("reasons", []).append(args.reason)
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    return 0


# ---------------------------------------------------------------------------
# cmd_evaluate
# ---------------------------------------------------------------------------


def cmd_evaluate(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="evaluate"
    )
    run_dir = run_ref.run_dir
    run_id = run_ref.run_id
    # Drift check uses snapshot; git state is outside our file lock anyway.
    require_no_unsafe_drift(run_ref.state, repo)

    # Rebuild all gate conditions from freshly-loaded state AND current
    # filesystem state inside the lock so that completion_gate_failures reflects
    # current reality. Checking artifact existence outside the lock would let a
    # deletion between the check and the commit mark a run complete with a stale
    # (fail-open) artifact result, so the existence check lives under the lock.
    reasons: list[str] = []
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        # A concurrent cancel/block may have made the run terminal after
        # resolution; evaluate must never flip a terminal run to complete/active.
        require_active_run_state(state, run_id, "evaluate")

        for artifact_key, filename in (
            ("accepted_spec", "accepted-spec.md"),
            ("accepted_plan", "accepted-plan.md"),
        ):
            rel = state.get("artifacts", {}).get(artifact_key, filename)
            try:
                path = resolve_artifact_path(str(rel), run_dir)
            except StateError:
                path = run_dir / filename
            if not path.exists():
                reasons.append(f"Missing {filename}")

        checks = latest_verification_checks(
            state.get("verification", {}).get("checks", [])
        )
        if not checks:
            reasons.append("No verification checks recorded")
        elif any(check.get("exit_code") != 0 for check in checks):
            reasons.append("One or more verification checks failed")

        reviews = state.get("reviews", [])
        if not reviews:
            reasons.append("No Codex code review recorded")
        else:
            last_review = reviews[-1]
            rel_path = last_review.get("path", "")
            try:
                review_path = resolve_artifact_path(str(rel_path), run_dir)
                review = json.loads(review_path.read_text(encoding="utf-8"))
            except (StateError, OSError, json.JSONDecodeError) as exc:
                reasons.append(f"Could not read latest review: {exc}")
                review = {}
            verdict = review.get("verdict")
            if verdict != "pass":
                reasons.append(
                    f"Latest Codex review verdict is {verdict}"
                )
            # Prefer the cumulative finding ledger (full-then-delta reviews); fall
            # back to the latest review object only when no ledger exists. A
            # truthy-but-malformed (non-list) ledger is scanned too — the scan
            # flags non-dict entries as severe — so a corrupted ledger fails
            # closed rather than being mistaken for "no findings".
            if state.get("cumulative_findings"):
                severe = cumulative_unresolved_severe(state)
            else:
                severe = unresolved_severe_findings(review)
            if severe:
                reasons.append(
                    f"{len(severe)} unresolved critical/high finding(s) in "
                    f"review ledger: {_describe_blocking_findings(severe)}"
                )
            # Completion requires every acceptance criterion to be satisfied
            # (fail closed). A criterion left not_satisfied/partially_satisfied/
            # not_verifiable blocks the gate.
            blocking_ac = blocking_acceptance_criteria(state)
            if blocking_ac:
                reasons.append(
                    f"{len(blocking_ac)} acceptance criteria not satisfied: "
                    f"{_describe_blocking_acceptance_criteria(blocking_ac)}"
                )
            # Reject an internally-inconsistent review: a `pass` verdict cannot
            # coexist with unresolved blocking findings or unsatisfied acceptance
            # criteria. Surfacing the inconsistency explicitly stops a contradictory
            # review from being read as evidence of completion.
            if verdict == "pass" and (severe or blocking_ac):
                reasons.append(
                    "Latest review verdict is 'pass' but "
                    f"{len(severe)} blocking finding(s) and {len(blocking_ac)} "
                    "unsatisfied acceptance criteria remain (inconsistent review)"
                )

        requires_adversarial = bool(
            state.get("risk", {}).get("requires_adversarial_review")
        )
        if requires_adversarial:
            adversarial = state.get("adversarial_reviews", [])
            if not adversarial:
                reasons.append("High-risk change requires an adversarial review")
            elif adversarial[-1].get("verdict") != "pass":
                reasons.append(
                    f"Latest adversarial review verdict is "
                    f"{adversarial[-1].get('verdict')}"
                )

        if reasons:
            state["status"] = "active"
            state["phase"] = "completion-gates-failed"
            state["completion_gate_failures"] = reasons
        else:
            state["status"] = "complete"
            state["phase"] = "complete"
            state["completion_gate_failures"] = []
        save_run_state(run_dir, state)

    if reasons:
        for reason in reasons:
            print(f"- {reason}", file=sys.stderr)
        return 1
    print("Workflow complete")
    return 0


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)

    if run_id_override:
        run_ref = resolve_run_for_inspection(
            state_home, repo.id, repo.canonical_root, run_id_override
        )
        runs = [run_ref]
    else:
        active = find_active_runs(state_home, repo.id)
        if not active:
            # Read-only: fall back to the most recent run (terminal included) or
            # legacy state so `status` still works after a run completes.
            run_ref = resolve_run_for_inspection(
                state_home, repo.id, repo.canonical_root, None
            )
            runs = [run_ref]
        elif len(active) > 1:
            if args.json:
                print(json.dumps([r.state for r in active], indent=2, sort_keys=True))
                return 0
            print(f"Multiple active runs ({len(active)}):")
            for r in active:
                lbl = r.state.get("label", "")
                print(
                    f'  {r.run_id}  label={lbl or "(none)"}  '
                    f'phase={r.state.get("phase")}  status={r.state.get("status")}'
                )
            print("Use --run-id to inspect a specific run.")
            return 0
        else:
            runs = active

    state = runs[0].state

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    checks = latest_verification_checks(state.get("verification", {}).get("checks", []))
    passed = sum(1 for item in checks if item.get("exit_code") == 0)
    print(f"Run: {state.get('run_id')}")
    if state.get("label"):
        print(f"Label: {state['label']}")
    print(f"Status: {state.get('status')}")
    print(f"Phase: {state.get('phase')}")
    print(f"Feature: {state.get('feature')}")
    repo_block = state.get("repository", {})
    worktree_mode = repo_block.get("worktree_mode") if isinstance(repo_block, dict) else None
    if worktree_mode:
        print(f"Worktree mode: {worktree_mode_label(worktree_mode)}")
    print(f"Baseline: {state.get('baseline', {}).get('commit')}")
    print(f"Verification: {passed}/{len(checks)} passing")
    print(
        f"Reviews: {state.get('review_round', 0)}/{state.get('max_review_rounds', 3)}"
    )
    if state.get("reviews"):
        print(f"Latest review: {state['reviews'][-1].get('verdict')}")
    if state.get("risk", {}).get("requires_adversarial_review"):
        verdict = (
            state.get("adversarial_reviews", [{}])[-1].get("verdict")
            if state.get("adversarial_reviews")
            else "missing"
        )
        print(f"Adversarial review required: {verdict}")
    failures = state.get("completion_gate_failures", [])
    if failures:
        print("Remaining gates:")
        for failure in failures:
            print(f"- {failure}")
    return 0


# ---------------------------------------------------------------------------
# cmd_cancel
# ---------------------------------------------------------------------------


def cmd_cancel(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_transition(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        assert_transition_allowed(state.get("status"), "cancel", run_ref.run_id)
        require_no_unsafe_drift(state, repo)
        state["status"] = "cancelled"
        state["phase"] = "cancelled"
        if args.reason:
            state.setdefault("notes", []).append(args.reason)
        save_run_state(run_dir, state)
    print("Workflow cancelled")
    return 0


# ---------------------------------------------------------------------------
# cmd_block
# ---------------------------------------------------------------------------


def cmd_block(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_transition(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        assert_transition_allowed(state.get("status"), "block", run_ref.run_id)
        require_no_unsafe_drift(state, repo)
        state["status"] = "blocked"
        state["phase"] = "blocked"
        state.setdefault("notes", []).append(args.reason)
        save_run_state(run_dir, state)
    print("Workflow blocked")
    return 0


# ---------------------------------------------------------------------------
# cmd_list_runs
# ---------------------------------------------------------------------------


def cmd_list_runs(args: argparse.Namespace) -> int:
    repo, state_home, _run_id_override = get_context(args)

    if getattr(args, "all", False):
        runs = find_all_runs(state_home, repo.id)
    else:
        # Default: active runs only (exclude archived and terminal)
        runs = [
            r
            for r in find_active_runs(state_home, repo.id)
            if r.state.get("status") != "archived"
        ]

    if args.json:
        print(json.dumps([r.state for r in runs], indent=2, sort_keys=True))
        return 0

    if not runs:
        print("No runs found.")
        return 0

    header = f"{'RUN_ID':<30}  {'LABEL':<20}  {'STATUS':<12}  {'PHASE':<28}  CREATED"
    print(header)
    print("-" * len(header))
    for r in runs:
        s = r.state
        run_id_str = (s.get("run_id") or r.run_id)[:30]
        label_str = (s.get("label") or "")[:20]
        status_str = (s.get("status") or "")[:12]
        phase_str = (s.get("phase") or "")[:28]
        created_str = (s.get("created_at") or "")[:25]
        print(
            f"{run_id_str:<30}  {label_str:<20}  {status_str:<12}  "
            f"{phase_str:<28}  {created_str}"
        )
    return 0


# ---------------------------------------------------------------------------
# cmd_show_run
# ---------------------------------------------------------------------------


def cmd_show_run(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)

    # run_id from --run-id arg on this subcommand, or global --run-id
    run_id = getattr(args, "show_run_id", None) or run_id_override
    run_ref = resolve_run_for_inspection(
        state_home, repo.id, repo.canonical_root, run_id
    )

    if args.json:
        print(json.dumps(run_ref.state, indent=2, sort_keys=True))
        return 0

    state = run_ref.state
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# cmd_migrate_legacy_state
# ---------------------------------------------------------------------------


def cmd_migrate_legacy_state(args: argparse.Namespace) -> int:
    repo, state_home, _run_id_override = get_context(args)

    legacy_dir = detect_legacy_state(repo.canonical_root)
    if legacy_dir is None:
        raise WorkflowError(
            f"No legacy run-state.json found under {repo.canonical_root / LEGACY_STATE_REL}. "
            "Nothing to migrate."
        )

    legacy_state_path = legacy_dir / "run-state.json"
    try:
        legacy_state = json.loads(legacy_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read legacy state: {exc}") from exc
    if not isinstance(legacy_state, dict):
        raise WorkflowError("Legacy state must be a JSON object.")

    # Choose the destination run ID. An explicit --target-run-id allows an
    # intentional migration to a fresh, unused ID without colliding with the
    # legacy run's own ID; otherwise reuse the legacy run_id (or mint one when
    # the legacy state has none).
    target_run_id = getattr(args, "target_run_id", None) or legacy_state.get(
        "run_id"
    ) or new_run_id()
    try:
        validate_run_id(target_run_id)
    except StateError as exc:
        raise WorkflowError(str(exc)) from exc

    # Everything below — the target-existence check, staging, atomic publication,
    # and metadata update — runs as one critical section so a concurrent init or
    # migration for this repository cannot interleave with publication.
    with RepoInitLock(state_home, repo.id):
        new_run_dir = run_dir_path(state_home, repo.id, target_run_id)
        legacy_source = str(legacy_dir)

        existing_state_path = new_run_dir / "run-state.json"
        if new_run_dir.exists():
            # The only non-error outcome for an existing target is an idempotent
            # re-run of the *same* legacy source. Any other occupant — an active
            # run, a terminal run, or a migration from a different source — is
            # immutable here and must never be overwritten (not even with
            # --force).
            existing: dict | None = None
            if existing_state_path.exists():
                try:
                    loaded = json.loads(
                        existing_state_path.read_text(encoding="utf-8")
                    )
                    if isinstance(loaded, dict):
                        existing = loaded
                except (OSError, json.JSONDecodeError):
                    existing = None
            if (
                existing is not None
                and existing.get("run_id") == target_run_id
                and existing.get("migrated_from") == legacy_source
            ):
                print(
                    f"Already migrated: run {target_run_id!r} exists at {new_run_dir}"
                )
                return 0
            raise WorkflowError(
                f"Run directory already exists and will not be overwritten: "
                f"{new_run_dir}. Migrating here would destroy an existing run. "
                "Re-run with --target-run-id <new-unused-id> to migrate into a "
                "fresh run instead."
            )

        # Build the migrated run in a temporary sibling directory and publish it
        # by an atomic rename only after conversion and validation succeed. A
        # failure at any earlier step leaves only the temp dir, which we remove,
        # so a partially built run is never visible at the canonical path.
        runs_base = new_run_dir.parent
        runs_base.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_dir = runs_base / f".migrate-{uuid.uuid4().hex}.tmp"
        try:
            staging_dir.mkdir(mode=0o700)
            for src in legacy_dir.iterdir():
                if src.is_file():
                    shutil.copy2(str(src), str(staging_dir / src.name))
                elif src.is_dir():
                    shutil.copytree(str(src), str(staging_dir / src.name))

            migrated = migrate_v1_to_v2(
                legacy_state, staging_dir, repo, legacy_dir=legacy_dir
            )
            migrated["run_id"] = target_run_id
            migrated["migrated_from"] = legacy_source
            migrated["migrated_at"] = utc_now()
            validate_state(migrated)
            save_run_state(staging_dir, migrated)

            # Re-check existence under the lock right before publishing. The lock
            # already excludes concurrent writers, so this only guards against a
            # stray pre-existing directory; never overwrite it.
            if new_run_dir.exists():
                raise WorkflowError(
                    f"Run directory appeared during migration: {new_run_dir}. "
                    "Aborting without overwriting it."
                )
            os.rename(str(staging_dir), str(new_run_dir))
        except BaseException:
            shutil.rmtree(str(staging_dir), ignore_errors=True)
            raise

        # Confirm the published run validates and is correctly attributed before
        # recording it in repository metadata.
        published = load_run_state(new_run_dir)
        verify_loaded_run_identity(
            published, run_dir=new_run_dir, expected_repo_id=repo.id
        )

        meta = load_repo_metadata(state_home, repo.id)
        meta.update(
            {
                "id": repo.id,
                "display_name": repo.display_name,
                "canonical_root": str(repo.canonical_root),
                "remote_display": repo.remote_display,
                "last_run_id": target_run_id,
            }
        )
        save_repo_metadata(state_home, repo.id, meta)

    print(f"Migrated legacy state from {legacy_dir} to {new_run_dir}")
    print(f"Run ID: {target_run_id}")
    print("The original legacy directory has NOT been modified.")
    return 0


# ---------------------------------------------------------------------------
# cmd_archive_run
# ---------------------------------------------------------------------------


def cmd_archive_run(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_transition(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_id_str = run_ref.run_id
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        # Idempotent: re-archiving an archived run is a no-op that must not alter
        # any other data.
        if state.get("status") == "archived":
            print(f"Run {run_id_str!r} is already archived.")
            return 0
        assert_transition_allowed(state.get("status"), "archive-run", run_id_str)
        require_no_unsafe_drift(state, repo)
        state["status"] = "archived"
        state.setdefault("notes", []).append(f"Archived at {utc_now()}")
        save_run_state(run_dir, state)
    print(f"Run {run_id_str!r} archived.")
    return 0


# ---------------------------------------------------------------------------
# cmd_accept_drift
# ---------------------------------------------------------------------------


def cmd_accept_drift(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home,
        repo.id,
        repo.canonical_root,
        run_id_override,
        operation="accept-drift",
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        require_active_run_state(state, run_ref.run_id, "accept-drift")
        old_baseline = dict(state.get("baseline", {}))
        old_repo_block = dict(state.get("repository", {}))
        old_worktree_mode = old_repo_block.get("worktree_mode")

        state["repository"] = repository_state_block(
            repo, worktree_mode=old_worktree_mode if isinstance(old_worktree_mode, str) else None
        )
        state["baseline"] = {
            "commit": repo.head_commit,
            "branch": repo.branch,
            "worktree_path": str(repo.worktree_path),
            "dirty_entries_at_init": old_baseline.get("dirty_entries_at_init", []),
        }
        state.setdefault("notes", []).append(
            f"drift_accepted_at={utc_now()} "
            f"drift_accepted_commit={repo.head_commit}"
        )
        save_run_state(run_dir, state)

    print("Drift accepted. Updated baseline:")
    old_commit = old_baseline.get("commit", "(unknown)")
    old_branch = old_baseline.get("branch", "(unknown)")
    old_worktree = old_baseline.get(
        "worktree_path", old_repo_block.get("worktree_path", "")
    )
    if old_commit != repo.head_commit:
        print(f"  commit: {old_commit} -> {repo.head_commit}")
    if old_branch != repo.branch:
        print(f"  branch: {old_branch} -> {repo.branch}")
    if old_worktree and old_worktree != str(repo.worktree_path):
        print(f"  worktree: {old_worktree} -> {repo.worktree_path}")
    if old_repo_block.get("id") and old_repo_block["id"] != repo.id:
        print(f'  repo_id: {old_repo_block["id"]} -> {repo.id}')
    return 0


# ---------------------------------------------------------------------------
# cmd_usage_report
# ---------------------------------------------------------------------------


def cmd_usage_report(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    # Read-only: resolve the most recent run when none is active so the usage
    # report (FR-1) remains viewable after the run completes.
    run_ref = resolve_run_for_inspection(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    runs = run_ref.state.get("codex_runs", [])

    if getattr(args, "json", False):
        print(json.dumps(runs, indent=2))
        return 0

    header = (
        f"{'Phase':<16}{'Prompt chars':>14}{'Output chars':>14}{'Duration':>12}"
    )
    print(header)
    print("-" * len(header))
    for record in runs:
        phase = str(record.get("phase", ""))[:16]
        prompt_chars = f"{int(record.get('prompt_characters', 0)):,}"
        output_chars = f"{int(record.get('output_characters', 0)):,}"
        duration = record.get("duration_seconds")
        duration_str = f"{duration} s" if duration is not None else "-"
        print(f"{phase:<16}{prompt_chars:>14}{output_chars:>14}{duration_str:>12}")
    if not runs:
        print("(no Codex phases recorded yet)")
    return 0


# ---------------------------------------------------------------------------
# cmd_triage
# ---------------------------------------------------------------------------


def cmd_triage(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="triage"
    )
    run_dir = run_ref.run_dir

    file_path = _resolve_source_path(args.file, run_dir, label="Triage ledger")
    try:
        entries = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read triage ledger: {exc}") from exc
    # Validate the whole ledger BEFORE any disposition is applied: a malformed
    # entry (missing fingerprint, unknown status) must not partially close
    # cumulative findings or release the completion gate.
    try:
        validate_payload(entries, "schemas/triage.schema.json", label="Triage ledger")
    except SchemaValidationError as exc:
        raise WorkflowError(str(exc)) from exc

    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        verify_loaded_run_identity(state, run_dir=run_dir, expected_repo_id=repo.id)
        require_active_run_state(state, run_ref.run_id, "triage")
        require_no_unsafe_drift(state, repo)
        ledger = state.setdefault("review_ledger", [])
        index = {
            e.get("fingerprint"): e for e in ledger if isinstance(e, dict)
        }
        merged = 0
        for entry in entries:
            # Fail closed on unauditable entries: an entry without a fingerprint
            # is not recorded in the review ledger, yet apply_triage_to_cumulative
            # would still close a cumulative finding by finding_id — closing a
            # severe finding (and unblocking the gate) with no audit trail. Reject
            # such entries so every gate-affecting closure is recorded.
            if not isinstance(entry, dict) or not str(
                entry.get("fingerprint", "")
            ).strip():
                raise WorkflowError(
                    "Every triage entry must carry a non-empty 'fingerprint' so a "
                    "gate-affecting closure is recorded in the audit ledger; "
                    f"refusing to apply an unauditable entry: {entry!r}"
                )
            index[entry["fingerprint"]] = entry
            merged += 1
        state["review_ledger"] = list(index.values())
        apply_triage_to_cumulative(state, entries)
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    print(f"Recorded {merged} triage finding(s) in the review ledger")
    return 0


# ---------------------------------------------------------------------------
# cmd_next_action
# ---------------------------------------------------------------------------


def _reference(name: str) -> str:
    return f"skills/autonomous-feature/references/{name}"


def compute_next_action(state: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Derive machine-readable phase guidance from current state and mode."""
    status = state.get("status")
    mode = state.get("effective_mode") or "standard"
    artifacts = state.get("artifacts", {})

    def have(key: str, fname: str) -> bool:
        rel = artifacts.get(key, fname)
        try:
            return resolve_artifact_path(str(rel), run_dir).exists()
        except StateError:
            return (run_dir / fname).exists()

    if status in {"complete", "blocked", "cancelled", "archived"}:
        return {
            "phase": status,
            "required_action": f"Run is {status}; no further action.",
            "completion_condition": "n/a",
            "references": [],
        }

    if not have("accepted_spec", "accepted-spec.md"):
        if mode == "rigorous" and "enhance" not in artifacts:
            return {
                "phase": "enhance",
                "required_action": "Run `codex --phase enhance`, then reconcile the "
                "output into an accepted spec.",
                "completion_condition": "accepted-spec.md exists (accept --kind spec).",
                "references": [_reference("specification.md")],
            }
        action = (
            "Inspect the repository and write a concise accepted spec."
            if mode == "lean"
            else "Reconcile requirements into an accepted spec."
        )
        return {
            "phase": "specification",
            "required_action": action,
            "completion_condition": "accepted-spec.md exists (accept --kind spec).",
            "references": [_reference("specification.md")],
        }

    if not have("accepted_plan", "accepted-plan.md"):
        action = (
            "Write a concise accepted implementation plan from repository inspection."
            if mode == "lean"
            else "Run `codex --phase plan`, then reconcile into an accepted plan."
        )
        return {
            "phase": "planning",
            "required_action": action,
            "completion_condition": "accepted-plan.md exists (accept --kind plan).",
            "references": [_reference("planning.md")],
        }

    checks = latest_verification_checks(
        state.get("verification", {}).get("checks", [])
    )
    verified = bool(checks) and all(c.get("exit_code") == 0 for c in checks)
    if not verified:
        return {
            "phase": "verification",
            "required_action": "Implement the plan and run repository checks via "
            "`run-check`.",
            "completion_condition": "All latest logical checks have exit_code 0.",
            "references": [
                _reference("implementation.md"),
                _reference("verification.md"),
            ],
        }

    reviews = state.get("reviews", [])
    latest_pass = bool(reviews) and reviews[-1].get("verdict") == "pass"
    if not latest_pass or cumulative_unresolved_severe(state):
        return {
            "phase": "review",
            "required_action": "Run `codex --phase review`, triage findings via "
            "`triage`, fix accepted ones, then re-review.",
            "completion_condition": "Latest review verdict is pass with no unresolved "
            "critical/high findings.",
            "references": [_reference("review.md")],
        }

    if state.get("risk", {}).get("requires_adversarial_review"):
        adversarial = state.get("adversarial_reviews", [])
        if not adversarial or adversarial[-1].get("verdict") != "pass":
            return {
                "phase": "adversarial",
                "required_action": "Run `codex --phase adversarial` and address any "
                "required actions.",
                "completion_condition": "Latest adversarial review verdict is pass.",
                "references": [_reference("review.md")],
            }

    return {
        "phase": "evaluate",
        "required_action": "Run `controller.py evaluate`.",
        "completion_condition": "All completion gates pass.",
        "references": [],
    }


def cmd_next_action(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_inspection(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    info = compute_next_action(run_ref.state, run_ref.run_dir)
    print(json.dumps(info, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root", help="Target repository; defaults to current directory"
    )
    parser.add_argument("--state-dir", help="Override state home directory")
    parser.add_argument("--run-id", help="Specify run ID for run-scoped commands")
    sub = parser.add_subparsers(dest="command_name", required=True)

    doctor = sub.add_parser(
        "doctor", help="Check Git, Python, Codex, and authentication"
    )
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="Initialize a workflow run")
    init.add_argument("--feature", required=True)
    init.add_argument("--label", help="Human-readable label stored in state")
    init.add_argument(
        "--mode",
        choices=WORKFLOW_MODES,
        default="auto",
        help="Workflow rigor mode; auto escalates conservatively by risk",
    )
    init.add_argument(
        "--worktree-mode",
        choices=WORKTREE_MODES,
        default="isolated",
        help="Repository execution mode; isolated stays in a disposable worktree, current uses the current checkout",
    )
    init.add_argument(
        "--allow-main",
        action="store_true",
        help="Allow current-checkout mode on main/master (still requires a clean tree)",
    )
    init.add_argument("--max-review-rounds", type=int, default=3, choices=range(1, 6))
    init.add_argument("--reuse", action="store_true")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    codex = sub.add_parser("codex", help="Run a structured, read-only Codex phase")
    codex.add_argument("--phase", required=True, choices=sorted(PHASE_OUTPUTS))
    codex.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-invocation timeout (seconds); overrides the global default",
    )
    codex.set_defaults(func=cmd_codex)

    accept = sub.add_parser(
        "accept", help="Record Claude-reconciled specification or plan"
    )
    accept.add_argument("--kind", required=True, choices=("spec", "plan"))
    accept.add_argument("--file", help="Accepted Markdown artifact (legacy mode)")
    accept.add_argument(
        "--source", help="Codex source JSON for structured decision-based acceptance"
    )
    accept.add_argument(
        "--decisions",
        help="Reconciliation delta JSON (accept/reject/modify/add) for structured mode",
    )
    accept.set_defaults(func=cmd_accept)

    run_check = sub.add_parser(
        "run-check", help="Execute and record one verification command"
    )
    run_check.add_argument("--name", required=True)
    run_check.add_argument(
        "--output",
        choices=("summary", "full"),
        default="summary",
        help="Terminal output policy; full replays complete stdout/stderr",
    )
    run_check.add_argument(
        "--failure-tail-lines",
        type=int,
        default=80,
        help="Number of trailing log lines to show on failure in summary mode",
    )
    run_check.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-command timeout (seconds); overrides the global default",
    )
    run_check.add_argument("command", nargs=argparse.REMAINDER)
    run_check.set_defaults(func=cmd_run_check)

    phase = sub.add_parser("set-phase", help="Update phase and optional note")
    phase.add_argument("--phase", required=True)
    phase.add_argument("--note")
    phase.set_defaults(func=cmd_set_phase)

    risk = sub.add_parser("set-risk", help="Set whether adversarial review is required")
    risk.add_argument(
        "--require-adversarial", action=argparse.BooleanOptionalAction, default=True
    )
    risk.add_argument("--reason")
    risk.set_defaults(func=cmd_set_risk)

    evaluate = sub.add_parser("evaluate", help="Evaluate all completion gates")
    evaluate.set_defaults(func=cmd_evaluate)

    usage_report = sub.add_parser(
        "usage-report", help="Per-phase Codex usage regression table"
    )
    usage_report.add_argument("--json", action="store_true", help="Output JSON")
    usage_report.set_defaults(func=cmd_usage_report)

    next_action = sub.add_parser(
        "next-action", help="Machine-readable next-phase guidance"
    )
    next_action.add_argument(
        "--json", action="store_true", help="Output JSON (default format)"
    )
    next_action.set_defaults(func=cmd_next_action)

    triage = sub.add_parser(
        "triage", help="Merge triage finding-ledger entries into run state"
    )
    triage.add_argument(
        "--file", required=True, help="JSON array of {fingerprint, status, ...} entries"
    )
    triage.set_defaults(func=cmd_triage)

    status = sub.add_parser("status", help="Show workflow state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    cancel = sub.add_parser("cancel", help="Cancel the active workflow")
    cancel.add_argument("--reason")
    cancel.set_defaults(func=cmd_cancel)

    block = sub.add_parser("block", help="Mark the workflow blocked")
    block.add_argument("--reason", required=True)
    block.set_defaults(func=cmd_block)

    list_runs = sub.add_parser(
        "list-runs", help="List workflow runs for this repository"
    )
    list_runs.add_argument("--json", action="store_true", help="Output JSON array")
    list_runs.add_argument(
        "--all", action="store_true", help="Include archived/terminal runs"
    )
    list_runs.set_defaults(func=cmd_list_runs)

    show_run = sub.add_parser("show-run", help="Show all fields of a specific run")
    show_run.add_argument("--run-id", dest="show_run_id", help="Run ID to display")
    show_run.add_argument("--json", action="store_true", help="Output JSON")
    show_run.set_defaults(func=cmd_show_run)

    migrate = sub.add_parser(
        "migrate-legacy-state",
        help="Migrate legacy .ai/autonomous-development state to new layout",
    )
    migrate.add_argument(
        "--target-run-id",
        default=None,
        help=(
            "Migrate into this run ID instead of reusing the legacy run_id. Use "
            "to migrate into a fresh, unused run when the default target is "
            "already occupied. Must not name an existing run."
        ),
    )
    migrate.add_argument(
        "--force",
        action="store_true",
        help=(
            "Accepted for compatibility. Never overwrites an existing run; an "
            "occupied target still fails. Use --target-run-id to migrate "
            "elsewhere."
        ),
    )
    migrate.set_defaults(func=cmd_migrate_legacy_state)

    archive = sub.add_parser(
        "archive-run", help="Archive a run (exclude from default listing)"
    )
    archive.set_defaults(func=cmd_archive_run)

    accept_drift = sub.add_parser(
        "accept-drift", help="Accept current repository state as new drift baseline"
    )
    accept_drift.set_defaults(func=cmd_accept_drift)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (WorkflowError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
