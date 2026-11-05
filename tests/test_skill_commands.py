import ast
import json
import re
from pathlib import Path
import shlex
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from af import build_parser
from config import FIELDS


def skill_commands():
    content = (ROOT / "skills" / "af" / "SKILL.md").read_text()
    commands = []
    for block in re.findall(r"```(?:sh|bash)\s*\n(.*?)```", content, re.DOTALL):
        lines, pending = [], ""
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            pending += (" " if pending else "") + stripped.removesuffix("\\").strip()
            if stripped.endswith("\\"):
                continue
            if pending.startswith("af "):
                lines.append(pending)
            pending = ""
        commands.extend(lines)
    commands.extend(span for span in re.findall(r"`([^`]+)`", content) if span.startswith("af "))
    return list(dict.fromkeys(commands))


def find_subparser(parser, command):
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and command in choices:
            return choices[command]
    return None


class SkillCommandContractTests(unittest.TestCase):
    def test_documented_af_subcommands_and_flags_exist(self):
        parser = build_parser()
        commands = skill_commands()
        self.assertTrue(commands, "SKILL.md should contain executable af commands")
        for command in commands:
            tokens = shlex.split(command)
            self.assertEqual(tokens[0], "af", command)
            current = parser
            index = 1
            while index < len(tokens) and not tokens[index].startswith("-"):
                selected = find_subparser(current, tokens[index])
                if selected is None:
                    break
                current = selected
                index += 1
            options = {option for action in current._actions for option in action.option_strings}
            for token in tokens[index:]:
                if token.startswith("-") and token != "--":
                    self.assertIn(token.split("=", 1)[0], options, f"{command}: unknown flag {token}")
        flattened = "\n".join(commands)
        self.assertIn("af state add-steps", flattened)
        self.assertIn("af gate test", flattened)
        self.assertIn("--flow-id", flattened)
        self.assertIn("--suite", flattened)
        self.assertIn("--writer-company", flattened)

    def test_cleanup_documentation_matches_supported_command(self):
        skill = (ROOT / "skills" / "af" / "SKILL.md").read_text()
        self.assertIn('af cleanup --repo "$R"', skill)
        self.assertIn('af worktree remove --repo "$R" --flow-id <id>', skill)

    def test_readme_config_table_matches_supported_fields_and_example(self):
        readme = (ROOT / "README.md").read_text()
        documented = {label for line in readme.splitlines() if line.startswith("|")
                      for label in re.findall(r"`([^`]+)`", line.split("|", 2)[1])}
        expected = {
            "roles", "quota_stop.exit_statuses", "quota_stop.patterns",
            "quota_retry_minutes", "worker_timeout_minutes", "test_timeout_minutes",
            "stale_after_minutes", "skill_dirs", "auto_merge", "high_risk_paths",
            "tests.focused", "tests.full", "record_retention.archive_after_days",
            "record_retention.archive_dir", "record_retention.delete_after_days",
        }
        self.assertEqual(documented, expected)
        example = json.loads((ROOT / "af.config.example.json").read_text())
        self.assertEqual(set(example), FIELDS)
        self.assertTrue(all(set(candidate) <= {"via", "provider", "model", "profile", "company"}
                            for candidates in example["roles"].values() for candidate in candidates))
        self.assertNotIn(429, example["quota_stop"]["exit_statuses"])

    def test_public_spec_has_no_owner_specific_examples_or_migration_section(self):
        spec = (ROOT / "docs" / "SPEC.md").read_text()
        private_examples = ("kimi-" + "coding", "l" + "una", "J" + "ev",
                            "Fly" + " JG", "ob" + "sidian", "/" + "Users/")
        for private in private_examples:
            self.assertNotIn(private.casefold(), spec.casefold())
        self.assertNotIn("Migration for the owner", spec)

    def test_library_has_no_unused_imports(self):
        for path in (ROOT / "lib").glob("*.py"):
            tree = ast.parse(path.read_text())
            used = {node.id for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
            imported = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend((alias.asname or alias.name.split(".")[0], node.lineno)
                                    for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.extend((alias.asname or alias.name, node.lineno)
                                    for alias in node.names if alias.name != "*")
            unused = [f"{name}@{line}" for name, line in imported if name not in used]
            self.assertEqual(unused, [], f"{path.name} has unused imports")


if __name__ == "__main__":
    unittest.main()
