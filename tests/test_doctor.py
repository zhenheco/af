import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SKILLS = ("grill-with-docs", "to-spec", "to-tickets", "implement-spec", "implement", "tdd",
                   "code-review", "diagnosing-bugs", "pr", "retro")
OPTIONAL_SKILLS = ("writing-for-agents", "setup-matt-pocock-skills")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class DoctorCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.env.pop("AF_CONFIG", None)
        self.repo = self.base / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "fixture")
        self.skills = self.base / "skills"
        for name in (*REQUIRED_SKILLS, *OPTIONAL_SKILLS):
            path = self.skills / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {name}\ndescription: test\n---\n")
        self.config = self.base / "af.config.json"
        self.write_config()
        for name in ("pi", "codex", "gh", "gitleaks", "claude"):
            path = self.bin / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)

    def write_config(self, **extra):
        self.config.write_text(json.dumps({"skill_dirs": [str(self.skills)], **extra}))

    def run_af(self, *args, env=None):
        return subprocess.run([str(ROOT / "bin" / "af"), *map(str, args)], cwd=self.repo,
                              env=env or self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_reports_required_skills_optional_tools_and_valid_config(self):
        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["skills"]["found"], list(REQUIRED_SKILLS))
        self.assertEqual(report["skills"]["missing"], [])
        self.assertTrue(all(report["optional_tools"].values()))
        self.assertIn("claude", report["optional_tools"])
        self.assertTrue(report["config"]["valid"])

    def test_reports_global_and_effective_high_risk_pattern_counts(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"high_risk_paths": ["billing/", "auth/"]}))
        self.write_config(high_risk_paths=["auth/", "local/"])

        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["high_risk"], {
            "baseline_count": 2, "effective_count": 3, "warnings": []
        })

    def test_empty_high_risk_patterns_warn_without_changing_doctor_status(self):
        self.write_config(high_risk_paths=[])

        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["high_risk"], {
            "baseline_count": 0,
            "effective_count": 0,
            "warnings": ["high_risk_paths is empty: the merge gate auto-merges every path"],
        })

    def test_invalid_global_baseline_reports_null_counts_without_warning(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text("{")
        self.write_config(high_risk_paths=["billing/"])

        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertFalse(report["config"]["valid"])
        self.assertEqual(report["high_risk"], {
            "baseline_count": None, "effective_count": None, "warnings": []
        })

    def test_unreadable_global_parent_reports_null_high_risk_counts(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read a mode-000 directory")
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"high_risk_paths": ["*"]}))
        self.write_config(high_risk_paths=["billing/"])
        original_mode = global_config.parent.stat().st_mode & 0o777
        self.addCleanup(global_config.parent.chmod, original_mode)
        global_config.parent.chmod(0)

        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertFalse(report["config"]["valid"])
        self.assertEqual(report["high_risk"], {
            "baseline_count": None, "effective_count": None, "warnings": []
        })

    def test_broken_global_parent_symlink_reports_null_high_risk_counts(self):
        config_dir = self.home / ".config"
        config_dir.mkdir()
        (config_dir / "af").symlink_to(self.base / "unmounted" / "dotfiles" / "af")
        self.write_config(high_risk_paths=["billing/"])

        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertFalse(report["config"]["valid"])
        self.assertEqual(report["high_risk"], {
            "baseline_count": None, "effective_count": None, "warnings": []
        })

    def test_invalid_project_config_reports_null_effective_count_without_warning(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"high_risk_paths": ["billing/", "auth/"]}))
        self.write_config(bogus=1)

        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertFalse(report["config"]["valid"])
        self.assertEqual(report["high_risk"], {
            "baseline_count": 2, "effective_count": None, "warnings": []
        })

    def test_reports_effective_owner_approval_count_and_warns_when_empty(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"owner_approval_paths": ["payment/", "*stripe*"]}))
        self.write_config(owner_approval_paths=["*stripe*", "local/"])
        report = json.loads(self.run_af("doctor", "--repo", self.repo, "--config", self.config).stdout)
        self.assertEqual(report["owner_approval"], {"effective_count": 3, "warnings": []})

        global_config.unlink()
        self.write_config()
        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(report["owner_approval"], {
            "effective_count": 0,
            "warnings": ["owner_approval_paths is empty: payment diffs merge without the owner's approval"],
        })

        self.write_config(bogus=1)
        report = json.loads(self.run_af("doctor", "--repo", self.repo, "--config", self.config).stdout)
        self.assertEqual(report["owner_approval"], {"effective_count": None, "warnings": []})

    def test_missing_skill_and_invalid_config_fail(self):
        (self.skills / "retro" / "SKILL.md").unlink()
        missing = self.run_af("doctor", "--repo", self.repo, "--config", self.config)
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(json.loads(missing.stdout)["skills"]["missing"], ["retro"])

        self.write_config(unsupported_option="old-field")
        invalid = self.run_af("doctor", "--repo", self.repo, "--config", self.config)
        self.assertEqual(invalid.returncode, 1)
        self.assertFalse(json.loads(invalid.stdout)["config"]["valid"])

        self.write_config(tests={"focused": ["pytest", "tests"]})
        invalid_command = self.run_af("doctor", "--repo", self.repo, "--config", self.config)
        self.assertEqual(invalid_command.returncode, 1)
        self.assertFalse(json.loads(invalid_command.stdout)["config"]["valid"])

    def test_warns_when_setup_skill_docs_are_missing(self):
        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["repository_setup"]["missing_files"],
                         ["docs/agents/issue-tracker.md", "docs/agents/domain.md"])
        self.assertIn("/setup-matt-pocock-skills", report["repository_setup"]["warnings"][0])

    def test_setup_helpers_are_not_required_matt_skills(self):
        for name in ("writing-for-agents", "setup-matt-pocock-skills"):
            (self.skills / name / "SKILL.md").unlink()
        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        required = json.loads(result.stdout)["skills"]["required"]
        self.assertNotIn("writing-for-agents", required)
        self.assertNotIn("setup-matt-pocock-skills", required)

    def test_missing_git_is_reported(self):
        isolated = self.base / "without-git"
        isolated.mkdir()
        for name in ("bash", "python3", "env", "dirname"):
            (isolated / name).symlink_to(shutil.which(name))
        env = dict(self.env, PATH=str(isolated))
        result = self.run_af("doctor", "--repo", self.repo, "--config", self.config, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["git"]["available"])


if __name__ == "__main__":
    unittest.main()
