import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class AfRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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
        self.config = self.repo / "af.config.json"
        self.write_config()
        self.write_exe("pi", "#!/bin/sh\n"
                       "if [ \"${FAKE_PI_MODE:-}\" = quota ]; then echo 'quota exceeded' >&2; exit 75; fi\n"
                       "echo pi-ok\n")
        self.write_exe("codex", "#!/bin/sh\nexit 0\n")
        self.write_exe("gitleaks", "#!/bin/sh\nexit 0\n")

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, **overrides):
        value = {"roles": {"writer": [{"via": "pi", "provider": "openai",
                                         "model": "writer", "company": "OpenAI"}]},
                 "tests": {"focused": "true", "full": "true"},
                 "auto_merge": True, "high_risk_paths": [], **overrides}
        self.config.write_text(json.dumps(value))
        return self.config

    def write_exe(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def run_af(self, *args, cwd=None, env=None):
        return subprocess.run([str(ROOT / "bin" / "af"), *map(str, args)], cwd=cwd or self.repo,
                              env=env or self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def create_flow(self, flow_id="flow-001", chain=("tdd",)):
        created = self.run_af("worktree", "create", "--repo", self.repo,
                              "--flow-id", flow_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree = Path(json.loads(created.stdout)["path"])
        started = self.run_af("state", "start", "--repo", worktree, "--flow-id", flow_id,
                              "--base-sha", git(worktree, "rev-parse", "HEAD"),
                              "--config", self.config, "--chain", *chain)
        self.assertEqual(started.returncode, 0, started.stderr)
        return worktree, json.loads(started.stdout)

    def test_flow_uses_primary_config_and_recorded_config_path(self):
        marker = self.base / "configured-test-ran"
        self.write_config(roles={"writer": [{"via": "codex", "profile": "primary",
                                              "company": "OpenAI"}]},
                          tests={"focused": f"touch {marker}", "full": "true"})
        worktree, started = self.create_flow("flow-config")
        (worktree / "af.config.json").write_text(json.dumps({
            "roles": {"writer": [{"via": "codex", "profile": "worker", "company": "OpenAI"}]},
            "tests": {"focused": "false", "full": "false"}}))
        selected = self.run_af("select", "--repo", worktree, "--flow-id", "flow-config",
                               "--role", "writer")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(json.loads(selected.stdout)["selected"]["profile"], "primary")
        record = json.loads(Path(started["state_path"]).read_text())
        self.assertEqual(record["config_path"], str(self.config.resolve()))
        tested = self.run_af("gate", "test", "--repo", worktree, "--flow-id", "flow-config",
                             "--suite", "focused")
        self.assertEqual(tested.returncode, 0, tested.stderr)
        self.assertTrue(marker.exists())

    def test_gate_without_config_fails_closed(self):
        worktree, _ = self.create_flow("flow-missing-config")
        self.config.unlink()
        decision = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "flow-missing-config",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 2)
        self.assertEqual(json.loads(decision.stdout)["decision"], "blocked")
        tested = self.run_af("gate", "test", "--repo", worktree, "--flow-id", "flow-missing-config",
                             "--suite", "focused")
        self.assertEqual(tested.returncode, 2)
        self.assertFalse(json.loads(tested.stdout)["passed"])

    def test_state_rejects_invalid_flow_ids_without_touching_neighbor_files(self):
        outside = self.base / "victim.json"
        outside.write_text('{"flow_id":"victim"}\n')
        original = outside.read_bytes()
        result = self.run_af("state", "start", "--repo", self.repo, "--flow-id", "../victim",
                             "--base-sha", git(self.repo, "rev-parse", "HEAD"),
                             "--chain", "tdd")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_bytes(), original)

    def test_add_steps_preserves_completed_order(self):
        worktree, _ = self.create_flow("ticket-steps", ("to-tickets", "code-review", "pr"))
        started = self.run_af("state", "advance", "--repo", worktree, "--flow-id", "ticket-steps")
        self.assertEqual(started.returncode, 0, started.stderr)
        completed = self.run_af("state", "complete", "--repo", worktree, "--flow-id", "ticket-steps")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        added = self.run_af("state", "add-steps", "--repo", worktree, "--flow-id", "ticket-steps",
                            "--after", "to-tickets", "--steps", "implement-spec:T1", "implement-spec:T2")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(json.loads(added.stdout)["chain"],
                         ["to-tickets", "implement-spec:T1", "implement-spec:T2", "code-review", "pr"])
        self.run_af("state", "advance", "--repo", worktree, "--flow-id", "ticket-steps")
        self.run_af("state", "complete", "--repo", worktree, "--flow-id", "ticket-steps")
        refused = self.run_af("state", "add-steps", "--repo", worktree, "--flow-id", "ticket-steps",
                              "--after", "to-tickets", "--steps", "implement-spec:T3")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("after the last started", refused.stderr)

    def test_quota_exhausted_independent_review_waits_without_same_company_fallback(self):
        self.write_config(roles={"small_review": [
            {"via": "codex", "profile": "same-company", "company": "Anthropic"},
            {"via": "pi", "provider": "openai", "model": "independent", "company": "OpenAI"}]},
            quota_stop={"exit_statuses": [75], "patterns": []})
        worktree, _ = self.create_flow("review-quota")
        environment = dict(self.env, FAKE_PI_MODE="quota")
        result = self.run_af("dispatch", "--worktree", worktree, "--flow-id", "review-quota",
                             "--role", "small_review", "--writer-company", "Anthropic",
                             "--prompt", "review changes", env=environment)
        self.assertEqual(result.returncode, 75)
        record = json.loads(result.stdout)
        self.assertEqual(record["status"], "waiting_for_quota")
        self.assertEqual([attempt["via"] for attempt in record["attempts"]], ["pi"])
        self.assertEqual(record["retry_after_minutes"], 15)

    def test_gate_commit_uses_literal_paths_and_restores_unrelated_index(self):
        worktree, _ = self.create_flow("literal-paths")
        (worktree / "file[1].txt").write_text("literal path\n")
        commit = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", "literal-paths",
                             "--message", "literal", "--paths", "file[1].txt")
        self.assertEqual(commit.returncode, 0, commit.stderr)
        paths = git(worktree, "show", "--pretty=format:", "--name-only", "HEAD").splitlines()
        self.assertEqual(paths, ["file[1].txt"])

        (worktree / "chosen.txt").write_text("chosen\n")
        (worktree / "other.txt").write_text("already staged\n")
        git(worktree, "add", "other.txt")
        refused = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", "literal-paths",
                              "--message", "refuse extra staged path", "--paths", "chosen.txt")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(git(worktree, "diff", "--cached", "--name-only"), "other.txt")

    def test_worktree_remove_refuses_dirty_and_unmerged_state_until_forced(self):
        created = self.run_af("worktree", "create", "--repo", self.repo,
                              "--flow-id", "remove-guards")
        worktree = Path(json.loads(created.stdout)["path"])
        (worktree / "draft.txt").write_text("untracked work\n")
        dirty = self.run_af("worktree", "remove", "--repo", self.repo, "--flow-id", "remove-guards")
        self.assertNotEqual(dirty.returncode, 0)
        self.assertTrue(worktree.exists())

        git(worktree, "add", "draft.txt")
        git(worktree, "commit", "-qm", "flow work")
        unmerged = self.run_af("worktree", "remove", "--repo", self.repo,
                               "--flow-id", "remove-guards")
        self.assertNotEqual(unmerged.returncode, 0)
        self.assertTrue(worktree.exists())
        forced = self.run_af("worktree", "remove", "--repo", self.repo,
                             "--flow-id", "remove-guards", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertFalse(worktree.exists())


if __name__ == "__main__":
    unittest.main()
