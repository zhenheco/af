import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class SelectCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.fake_bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.env.pop("AF_CONFIG", None)
        self.env.pop("AF_NO_HOST", None)
        self.repo = self.base / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "fixture")

    def tearDown(self):
        self.temp.cleanup()

    def write_exe(self, name):
        path = self.fake_bin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    def candidate(self, via, company, name):
        if via == "pi":
            return {"via": via, "provider": "openai", "model": name, "company": company}
        if via == "host":
            return {"via": via, "model": name, "company": company}
        return {"via": via, "profile": name, "company": company}

    def config(self, name, roles, **extra):
        path = self.base / name
        path.write_text(json.dumps({"roles": roles, **extra}))
        return path

    def run_af(self, *args, cwd=None):
        return subprocess.run([str(ROOT / "bin" / "af"), *map(str, args)], cwd=cwd or self.repo,
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_skips_missing_cli_candidates_and_preserves_order(self):
        self.write_exe("codex")
        self.env["PATH"] = f"{self.fake_bin}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
        self.assertIsNone(shutil.which("pi", path=self.env["PATH"]))
        path = self.config("ordered.json", {"writer": [
            self.candidate("pi", "OpenAI", "first"),
            self.candidate("codex", "Anthropic", "second")]})
        result = self.run_af("select", "--role", "writer", "--config", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual([item["via"] for item in record["candidates"]], ["codex"])
        self.assertEqual(record["selected"]["profile"], "second")

    def test_select_uses_global_roles_when_project_config_omits_them(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"roles": {
            "writer": [self.candidate("host", "OpenAI", "global-writer")],
        }}))
        (self.repo / "af.config.json").write_text(json.dumps({"auto_merge": True}))

        result = self.run_af("select", "--role", "writer")

        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["selected"], self.candidate("host", "OpenAI", "global-writer"))

    def test_select_reports_missing_role_when_global_config_is_absent(self):
        (self.repo / "af.config.json").write_text(json.dumps({"auto_merge": True}))

        result = self.run_af("select", "--role", "writer")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "af: role not configured: writer\n")
        self.assertEqual(result.stdout, "")

    def test_select_preserves_global_config_error_when_global_file_is_invalid(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text("{")
        (self.repo / "af.config.json").write_text(json.dumps({"auto_merge": True}))

        result = self.run_af("select", "--role", "writer")

        self.assertEqual(result.returncode, 2)
        self.assertIn(f"af: invalid global config {global_config.resolve()}:", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_review_skips_same_company_when_a_distinct_company_is_configured(self):
        self.write_exe("pi")
        self.write_exe("codex")
        path = self.config("review.json", {"small_review": [
            self.candidate("codex", "Anthropic", "same-company"),
            self.candidate("pi", "OpenAI", "independent")]})
        result = self.run_af("select", "--role", "small_review", "--writer-company", "Anthropic",
                             "--config", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["review"], "independent")
        self.assertEqual([item["via"] for item in record["candidates"]], ["pi"])

    def test_same_company_review_is_allowed_only_when_no_other_company_is_configured(self):
        self.write_exe("codex")
        path = self.config("single-company.json", {"small_review": [
            self.candidate("codex", "Anthropic", "reviewer")]})
        result = self.run_af("select", "--role", "small_review", "--writer-company", "Anthropic",
                             "--config", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["review"], "same-company")
        self.assertEqual(record["selected"]["company"], "Anthropic")

    def test_host_candidate_is_available_without_a_cli_and_respects_reviewer_independence(self):
        path = self.config("host-review.json", {"small_review": [
            self.candidate("host", "OpenAI", "same-company"),
            self.candidate("host", "Anthropic", "claude-reviewer")]})
        result = self.run_af("select", "--role", "small_review", "--writer-company", "OpenAI",
                             "--config", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["review"], "independent")
        self.assertEqual(record["selected"], self.candidate("host", "Anthropic", "claude-reviewer"))

    def test_no_host_selects_codex_after_reviewer_independence(self):
        self.write_exe("codex")
        self.write_exe("pi")
        path = self.config("reviewers.json", {"small_review": [
            self.candidate("host", "Anthropic", "claude-reviewer"),
            self.candidate("codex", "OpenAI", "codex-reviewer"),
            self.candidate("pi", "Moonshot", "kimi-reviewer")]})
        self.env["AF_NO_HOST"] = "1"
        result = self.run_af("select", "--role", "small_review", "--writer-company", "DeepSeek",
                             "--config", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["review"], "independent")
        self.assertEqual([item["via"] for item in record["candidates"]], ["codex", "pi"])
        self.assertEqual(record["selected"]["profile"], "codex-reviewer")

    def test_no_host_applies_to_non_review_roles(self):
        self.write_exe("codex")
        self.write_exe("pi")
        path = self.config("writer.json", {"writer": [
            self.candidate("host", "Anthropic", "claude-writer"),
            self.candidate("codex", "OpenAI", "codex-writer"),
            self.candidate("pi", "Moonshot", "kimi-writer")]})
        self.env["AF_NO_HOST"] = "1"
        result = self.run_af("select", "--role", "writer", "--config", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual([item["via"] for item in record["candidates"]], ["codex", "pi"])
        self.assertEqual(record["selected"]["profile"], "codex-writer")

    def test_no_host_keeps_only_independent_pi_reviewer(self):
        self.write_exe("codex")
        self.write_exe("pi")
        path = self.config("reviewers.json", {"small_review": [
            self.candidate("host", "Anthropic", "claude-reviewer"),
            self.candidate("codex", "OpenAI", "codex-reviewer"),
            self.candidate("pi", "Moonshot", "kimi-reviewer")]})
        self.env["AF_NO_HOST"] = "1"
        result = self.run_af("select", "--role", "small_review", "--writer-company", "OpenAI",
                             "--config", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["review"], "independent")
        self.assertEqual([item["via"] for item in record["candidates"]], ["pi"])
        self.assertEqual(record["selected"]["model"], "kimi-reviewer")

    def test_no_host_reports_no_available_worker_when_role_only_has_host_candidates(self):
        path = self.config("host-only.json", {"small_review": [
            self.candidate("host", "Anthropic", "claude-reviewer"),
            self.candidate("host", "OpenAI", "gpt-reviewer")]})
        self.env["AF_NO_HOST"] = "1"
        result = self.run_af("select", "--role", "small_review", "--writer-company", "DeepSeek",
                             "--config", path)
        self.assertEqual(result.returncode, 2, result.stderr)
        record = json.loads(result.stdout)
        self.assertIsNone(record["selected"])
        self.assertEqual(record["error"], "no configured worker CLI is available")

    def test_other_no_host_values_keep_host_candidates_available(self):
        path = self.config("reviewers.json", {"small_review": [
            self.candidate("host", "Anthropic", "claude-reviewer"),
            self.candidate("codex", "OpenAI", "codex-reviewer"),
            self.candidate("pi", "Moonshot", "kimi-reviewer")]})
        self.write_exe("codex")
        self.write_exe("pi")
        for value in (None, "true"):
            with self.subTest(value=value):
                if value is None:
                    self.env.pop("AF_NO_HOST", None)
                else:
                    self.env["AF_NO_HOST"] = value
                result = self.run_af("select", "--role", "small_review", "--writer-company", "DeepSeek",
                                     "--config", path)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(result.stdout)
                self.assertEqual(record["candidates"][0]["via"], "host")
                self.assertEqual(record["selected"]["model"], "claude-reviewer")

    def test_missing_independent_cli_does_not_fall_back_to_same_company(self):
        self.write_exe("codex")
        isolated = self.base / "minimal-path"
        isolated.mkdir()
        for name in ("bash", "python3", "dirname", "env"):
            (isolated / name).symlink_to(shutil.which(name))
        path = self.config("missing-review.json", {"small_review": [
            self.candidate("codex", "Anthropic", "same-company"),
            self.candidate("pi", "OpenAI", "independent")]})
        env = dict(self.env, PATH=f"{self.fake_bin}{os.pathsep}{isolated}")
        result = subprocess.run([str(ROOT / "bin" / "af"), "select", "--role", "small_review",
                                 "--writer-company", "Anthropic", "--config", str(path)],
                                cwd=self.repo, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        record = json.loads(result.stdout)
        self.assertEqual(record["selected"], None)

    def test_review_requires_known_writer_company(self):
        path = self.config("unknown.json", {"small_review": [
            self.candidate("codex", "OpenAI", "reviewer")]})
        result = self.run_af("select", "--role", "small_review", "--writer-company", "unknown",
                             "--config", path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writer company", result.stderr.lower())

    def test_select_uses_the_flow_recorded_config(self):
        self.write_exe("codex")
        recorded = self.config("recorded.json", {"writer": [self.candidate("codex", "OpenAI", "recorded")]})
        created = self.run_af("worktree", "create", "--repo", self.repo, "--flow-id", "select-config")
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree = Path(json.loads(created.stdout)["path"])
        started = self.run_af("state", "start", "--repo", worktree, "--flow-id", "select-config",
                              "--base-sha", git(worktree, "rev-parse", "HEAD"),
                              "--config", recorded, "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)
        (self.base / "af.config.json").write_text(json.dumps({
            "roles": {"writer": [self.candidate("codex", "OpenAI", "wrong-cwd")]}}))
        result = self.run_af("select", "--repo", worktree, "--flow-id", "select-config",
                             "--role", "writer", cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["selected"]["profile"], "recorded")

    def test_config_lookup_order_is_cli_then_environment_then_primary_checkout_then_home(self):
        self.write_exe("codex")
        explicit = self.config("explicit.json", {"writer": [self.candidate("codex", "OpenAI", "explicit")]})
        env_file = self.config("env.json", {"writer": [self.candidate("codex", "OpenAI", "env")]})
        (self.repo / "af.config.json").write_text(json.dumps({
            "roles": {"writer": [self.candidate("codex", "OpenAI", "checkout")]}}))
        home_file = self.home / ".config" / "af" / "af.config.json"
        home_file.parent.mkdir(parents=True)
        home_file.write_text(json.dumps({
            "roles": {"writer": [self.candidate("codex", "OpenAI", "home")]}}))

        result = self.run_af("select", "--role", "writer", "--config", explicit)
        self.assertEqual(json.loads(result.stdout)["selected"]["profile"], "explicit")
        self.env["AF_CONFIG"] = str(env_file)
        result = self.run_af("select", "--role", "writer")
        self.assertEqual(json.loads(result.stdout)["selected"]["profile"], "env")
        self.env.pop("AF_CONFIG")
        result = self.run_af("select", "--role", "writer")
        self.assertEqual(json.loads(result.stdout)["selected"]["profile"], "checkout")
        (self.repo / "af.config.json").unlink()
        result = self.run_af("select", "--role", "writer")
        self.assertEqual(json.loads(result.stdout)["selected"]["profile"], "home")

    def test_explicit_and_environment_config_paths_inside_linked_af_worktree_are_refused(self):
        linked = self.base / "linked-af-worktree"
        git(self.repo, "worktree", "add", "-q", "-b", "af/config-source", str(linked), "HEAD")
        unsafe = linked / "af.config.json"
        unsafe.write_text(json.dumps({"roles": {"writer": [
            self.candidate("host", "Anthropic", "claude-sonnet")]}}))
        started = self.run_af("state", "start", "--repo", str(self.repo), "--flow-id", "unsafe-config",
                              "--base-sha", git(self.repo, "rev-parse", "HEAD"),
                              "--config", str(unsafe), "--chain", "tdd")
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("worktree on af/* branch", started.stderr)
        common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertFalse((common / ".af" / "flows" / "unsafe-config.json").exists())

        alias = self.repo / "flow-config.json"
        alias.symlink_to(unsafe)
        self.env["AF_CONFIG"] = str(alias)
        selected = self.run_af("select", "--role", "writer", cwd=linked)
        self.assertNotEqual(selected.returncode, 0)
        self.assertIn("worktree on af/* branch", selected.stderr)

    def test_config_inside_primary_checkout_on_af_branch_is_refused(self):
        unsafe = self.repo / "af.config.json"
        unsafe.write_text(json.dumps({"roles": {"writer": [
            self.candidate("host", "Anthropic", "claude-sonnet")]}}))
        git(self.repo, "switch", "-q", "-c", "af/primary-config")
        selected = self.run_af("select", "--role", "writer", "--config", str(unsafe))
        self.assertNotEqual(selected.returncode, 0)
        self.assertIn("worktree on af/* branch", selected.stderr)


if __name__ == "__main__":
    unittest.main()
