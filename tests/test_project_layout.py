from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectLayoutTests(unittest.TestCase):
    def test_manifest_and_components(self) -> None:
        manifest = json.loads(
            (ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "autonomous-development")
        expected = {
            "autonomous-feature",
            "autonomous-current",
            "autonomous-main",
            "enhance-idea",
            "implementation-plan",
            "implement-plan",
            "verify-feature",
            "codex-review",
            "adversarial-review",
            "fix-findings",
            "autonomous-status",
        }
        actual = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(actual, expected)

    def test_json_files_parse(self) -> None:
        for path in ROOT.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_output_schemas_are_strict_required(self) -> None:
        """Codex --output-schema objects must list every property in `required`
        (OpenAI strict structured outputs reject any omission)."""

        def violations(node: object, path: str) -> list[tuple[str, list[str]]]:
            found: list[tuple[str, list[str]]] = []
            if isinstance(node, dict):
                props = node.get("properties")
                if node.get("type") == "object" and isinstance(props, dict):
                    missing = sorted(set(props) - set(node.get("required", [])))
                    if missing:
                        found.append((path, missing))
                for key, value in node.items():
                    found += violations(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    found += violations(value, f"{path}[{index}]")
            return found

        for name in (
            "enhanced-idea",
            "implementation-plan",
            "review",
            "review-delta",
            "adversarial-review",
        ):
            path = ROOT / "schemas" / f"{name}.schema.json"
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(violations(schema, "(root)"), [], f"{name}: {path}")

    def test_prompt_placeholders_are_known(self) -> None:
        known = {
            "FEATURE",
            "BASELINE",
            "REPOSITORY_CONTEXT",
            "CODEX_SPEC",
            "ACCEPTED_SPEC",
            "ACCEPTED_PLAN",
            "VERIFICATION",
            "PREVIOUS_REVIEW",
            "LATEST_REVIEW",
            "FINDING_LEDGER",
            "OPEN_FINDINGS",
            "ACCEPTANCE_CRITERIA",
            "CHANGED_SINCE_LAST_REVIEW",
        }
        import re

        for path in (ROOT / "prompts").glob("*.md"):
            placeholders = set(
                re.findall(r"\{\{([A-Z0-9_]+)\}\}", path.read_text(encoding="utf-8"))
            )
            self.assertTrue(placeholders <= known, f"{path}: {placeholders - known}")

    def test_autonomous_feature_skill_mentions_worktree_modes(self) -> None:
        text = (ROOT / "skills" / "autonomous-feature" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--worktree-mode isolated", text)
        self.assertIn("--worktree-mode current", text)
        self.assertIn("EnterWorktree", text)
        self.assertIn("current-checkout mode", text)

    def test_autonomous_current_skill_uses_current_mode_without_worktree(self) -> None:
        text = (ROOT / "skills" / "autonomous-current" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--mode standard", text)
        self.assertIn("--worktree-mode current", text)
        self.assertNotIn("--allow-main", text)
        # Must NOT enter a disposable worktree.
        self.assertIn("EnterWorktree", text)  # mentioned only to disallow it
        # The frontmatter explicitly disallows the worktree tools.
        head = text.split("---", 2)[1]
        self.assertIn("EnterWorktree", head)
        self.assertIn("ExitWorktree", head)
        # Disallows main/master.
        self.assertIn("main", text)
        self.assertIn("master", text)
        # Never commits.
        self.assertIn("Do not create commits", text)
        # Does not create .claude/worktrees/*.
        self.assertIn(".claude/worktrees", text)

    def test_autonomous_main_skill_passes_allow_main(self) -> None:
        text = (ROOT / "skills" / "autonomous-main" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--mode standard", text)
        self.assertIn("--worktree-mode current", text)
        self.assertIn("--allow-main", text)
        # Must NOT enter a disposable worktree.
        head = text.split("---", 2)[1]
        self.assertIn("EnterWorktree", head)
        self.assertIn("ExitWorktree", head)
        # Never commits.
        self.assertIn("Do not create commits", text)
        # Still requires a clean tree.
        self.assertIn("clean working tree", text)
        # Does not create .claude/worktrees/*.
        self.assertIn(".claude/worktrees", text)

    def test_autonomous_feature_skill_remains_isolated_default(self) -> None:
        text = (ROOT / "skills" / "autonomous-feature" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        # The default invocation must keep the isolated worktree default.
        self.assertIn("--worktree-mode isolated", text)
        # And the frontmatter must still allow EnterWorktree/ExitWorktree so the
        # default workflow can enter a disposable worktree.
        head = text.split("---", 2)[1]
        self.assertIn("EnterWorktree", head)
        self.assertIn("ExitWorktree", head)


if __name__ == "__main__":
    unittest.main()
