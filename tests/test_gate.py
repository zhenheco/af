import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class GateCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.test_log = self.base / "test-log"
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["TEST_LOG"] = str(self.test_log)
        self.env.pop("AF_CONFIG", None)
        self.repo = self.base / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        self.hooks = self.base / "empty-hooks"
        self.hooks.mkdir()
        git(self.repo, "config", "core.hooksPath", str(self.hooks))
        (self.repo / "README.md").write_text("fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "fixture")
        self.test_bin = self.bin / "fake-test"
        self.test_bin.write_text("#!/bin/sh\necho \"$1\" >> \"$TEST_LOG\"\nexit 0\n")
        self.test_bin.chmod(0o755)
        self.config = self.base / "af.config.json"
        self.config.write_text(json.dumps({"tests": {
            "focused": "fake-test focused", "full": "fake-test full"
        }, "auto_merge": True, "high_risk_paths": []}))

    def fake_gitleaks(self, exit_code=0):
        path = self.bin / "gitleaks"
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TEST_LOG\"\nexit {exit_code}\n")
        path.chmod(0o755)
        return path

    def disable_gitleaks(self):
        tools = self.base / "no-gitleaks"
        tools.mkdir(exist_ok=True)
        for name in ("bash", "python3", "git", "dirname", "env"):
            (tools / name).symlink_to(shutil.which(name))
        self.env["PATH"] = f"{self.bin}{os.pathsep}{tools}"

    def run_af(self, *args):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], cwd=self.repo, env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def create_flow(self, flow_id):
        created = self.run_af("worktree", "create", "--repo", self.repo,
                              "--flow-id", flow_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree = Path(json.loads(created.stdout)["path"])
        started = self.run_af("state", "start", "--repo", worktree, "--flow-id", flow_id,
                              "--base-sha", git(worktree, "rev-parse", "HEAD"),
                              "--config", self.config, "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)
        return worktree

    def test_commit_fails_closed_when_recorded_config_changes(self):
        worktree = self.create_flow("gate-config-change")
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": []}))
        (worktree / "change.txt").write_text("change\n")
        committed = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", "gate-config-change",
                                "--message", "change", "--paths", "change.txt")
        self.assertNotEqual(committed.returncode, 0)
        self.assertIn("config changed since flow start", committed.stderr)
        self.assertEqual(git(worktree, "rev-list", "--count", "HEAD"), git(self.repo, "rev-list", "--count", "HEAD"))

    def test_runs_the_configured_focused_and_full_test_commands(self):
        worktree = self.create_flow("gate-tests")
        for suite in ("focused", "full"):
            result = self.run_af("gate", "test", "--repo", worktree, "--flow-id", "gate-tests",
                                 "--suite", suite)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["suite"], suite)
            self.assertNotIn("stdout", report)
            self.assertNotIn("stderr", report)
        self.assertEqual(self.test_log.read_text().splitlines(), ["focused", "full"])

    def test_test_and_decision_require_a_recorded_flow(self):
        test = self.run_af("gate", "test", "--repo", self.repo, "--config", self.config, "--suite", "focused")
        decision = self.run_af("gate", "decide", "--repo", self.repo, "--config", self.config,
                               "--base", git(self.repo, "rev-parse", "HEAD"),
                               "--tests", "pass", "--reviews", "pass")
        self.assertNotEqual(test.returncode, 0)
        self.assertNotEqual(decision.returncode, 0)

    def test_invalid_global_config_blocks_gate_decision_with_exit_two(self):
        worktree = self.create_flow("gate-invalid-global")
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text("{")

        result = self.run_af("gate", "decide", "--repo", worktree,
                             "--flow-id", "gate-invalid-global", "--tests", "pass", "--reviews", "pass")

        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"], "blocked")
        self.assertIn("invalid global config", report["reasons"][0])

    def test_broken_global_baseline_symlink_blocks_gate_decision(self):
        worktree = self.create_flow("gate-broken-global")
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.symlink_to(self.base / "unmounted" / "af.config.json")

        result = self.run_af("gate", "decide", "--repo", worktree,
                             "--flow-id", "gate-broken-global", "--tests", "pass", "--reviews", "pass")

        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"], "blocked")
        self.assertIn("invalid global config", report["reasons"][0])

    def test_broken_global_parent_symlink_blocks_gate_decision(self):
        worktree = self.create_flow("gate-broken-global-parent")
        config_dir = self.home / ".config"
        config_dir.mkdir()
        (config_dir / "af").symlink_to(self.base / "unmounted" / "dotfiles" / "af")

        result = self.run_af("gate", "decide", "--repo", worktree,
                             "--flow-id", "gate-broken-global-parent", "--tests", "pass", "--reviews", "pass")

        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"], "blocked")
        self.assertIn("invalid global config", report["reasons"][0])

    def test_unreadable_global_parent_blocks_gate_decision(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read a mode-000 directory")
        worktree = self.create_flow("gate-unreadable-global-parent")
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"high_risk_paths": ["*"]}))
        original_mode = global_config.parent.stat().st_mode & 0o777
        self.addCleanup(global_config.parent.chmod, original_mode)
        global_config.parent.chmod(0)

        result = self.run_af("gate", "decide", "--repo", worktree,
                             "--flow-id", "gate-unreadable-global-parent", "--tests", "pass", "--reviews", "pass")

        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"], "blocked")
        self.assertIn("invalid global config", report["reasons"][0])

    def test_invalid_utf8_global_config_blocks_gate_decision(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"high_risk_paths": ["billing/"]}))
        created = self.run_af("worktree", "create", "--repo", self.repo,
                              "--flow-id", "gate-utf8-global")
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree = Path(json.loads(created.stdout)["path"])
        started = self.run_af("state", "start", "--repo", worktree, "--flow-id", "gate-utf8-global",
                              "--base-sha", git(worktree, "rev-parse", "HEAD"),
                              "--config", global_config, "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)
        global_config.write_bytes(b"\xff\xfe invalid utf-8")

        result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-utf8-global",
                             "--tests", "pass", "--reviews", "pass")

        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"], "blocked")
        self.assertIn("invalid global config", report["reasons"][0])

    def test_global_baseline_asks_for_billing_but_merges_readme(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"high_risk_paths": ["billing/"]}))

        billing = self.create_flow("gate-global-billing")
        (billing / "billing").mkdir()
        (billing / "billing" / "charge.py").write_text("value = 1\n")
        committed = self.run_af("gate", "commit", "--repo", billing, "--flow-id", "gate-global-billing",
                                "--message", "add billing change", "--paths", "billing/charge.py")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        decision = self.run_af("gate", "decide", "--repo", billing, "--flow-id", "gate-global-billing",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        report = json.loads(decision.stdout)
        self.assertEqual(report["decision"], "ask")
        self.assertEqual(report["high_risk_paths"], ["billing/charge.py"])

        readme = self.create_flow("gate-global-readme")
        (readme / "README.md").write_text("documentation\n")
        committed = self.run_af("gate", "commit", "--repo", readme, "--flow-id", "gate-global-readme",
                                "--message", "update readme", "--paths", "README.md")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        decision = self.run_af("gate", "decide", "--repo", readme, "--flow-id", "gate-global-readme",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        report = json.loads(decision.stdout)
        self.assertEqual(report["decision"], "merge")
        self.assertEqual(report["high_risk_paths"], [])

    def test_global_pattern_negation_cannot_reinclude_below_ignored_directory(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        cases = (
            ("file", ["scripts/ci*"], ["!scripts/ci-local.sh"], "merge", []),
            ("directory", ["scripts/"], ["!scripts/ci-local.sh"], "ask", ["scripts/ci-local.sh"]),
        )
        for suffix, baseline, local, expected_decision, expected_paths in cases:
            with self.subTest(suffix=suffix):
                global_config.write_text(json.dumps({"high_risk_paths": baseline}))
                self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": local}))
                flow_id = f"gate-global-negation-{suffix}"
                worktree = self.create_flow(flow_id)
                path = worktree / "scripts" / "ci-local.sh"
                path.parent.mkdir()
                path.write_text("fixture\n")
                committed = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", flow_id,
                                        "--message", "add CI file", "--paths", "scripts/ci-local.sh")
                self.assertEqual(committed.returncode, 0, committed.stderr)
                decision = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", flow_id,
                                       "--tests", "pass", "--reviews", "pass")
                self.assertEqual(decision.returncode, 0, decision.stderr)
                report = json.loads(decision.stdout)
                self.assertEqual(report["decision"], expected_decision)
                self.assertEqual(report["high_risk_paths"], expected_paths)

    def test_last_occurrence_dedupe_preserves_gitignore_reinclude_semantics(self):
        global_config = self.home / ".config" / "af" / "af.config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"high_risk_paths": ["secrets/"]}))
        self.config.write_text(json.dumps({
            "tests": {"focused": "fake-test focused", "full": "fake-test full"},
            "auto_merge": True, "high_risk_paths": ["!secrets/", "secrets/"]}))
        worktree = self.create_flow("gate-reinclude")
        (worktree / "secrets").mkdir()
        (worktree / "secrets" / "x").write_text("fixture\n")
        committed = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", "gate-reinclude",
                                "--message", "reincluded secrets path", "--paths", "secrets/x")
        self.assertEqual(committed.returncode, 0, committed.stderr)

        decision = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-reinclude",
                               "--tests", "pass", "--reviews", "pass")

        self.assertEqual(decision.returncode, 0, decision.stderr)
        report = json.loads(decision.stdout)
        self.assertEqual(report["decision"], "ask")
        self.assertEqual(report["high_risk_paths"], ["secrets/x"])

    def test_high_risk_checks_create_info_directory_with_custom_git_template(self):
        template = self.base / "git-template"
        template.mkdir()
        (self.home / ".gitconfig").write_text(f"[init]\n\ttemplateDir = {template}\n")
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": ["config/secrets"]}))
        worktree = self.create_flow("gate-template")
        (worktree / "config" / "secrets").mkdir(parents=True)
        (worktree / "config" / "secrets" / "token.json").write_text("fixture\n")

        committed = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", "gate-template",
                                "--message", "commit high-risk fixture", "--paths", "config/secrets/token.json")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-template",
                             "--tests", "pass", "--reviews", "pass")

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)
        self.assertEqual(decision["decision"], "ask")
        self.assertEqual(decision["high_risk_paths"], ["config/secrets/token.json"])

    def test_high_risk_paths_remain_visible_after_host_commit(self):
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": ["payments/**"]}))
        worktree = self.create_flow("gate-risk")
        (worktree / "payments").mkdir()
        (worktree / "payments" / "api.py").write_text("value = 1\n")
        committed = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-risk",
                                "--message", "commit high-risk fixture", "--paths", "payments/api.py")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        decision = self.run_af("gate", "decide", "--repo", str(worktree), "--flow-id", "gate-risk",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        self.assertEqual(json.loads(decision.stdout)["decision"], "ask")

    def test_decide_blocks_uncommitted_staged_unstaged_and_untracked_changes(self):
        worktree = self.create_flow("gate-dirty")

        def decide():
            result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-dirty",
                                 "--tests", "pass", "--reviews", "pass")
            self.assertEqual(result.returncode, 2, result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record["decision"], "blocked")
            self.assertEqual(record["reasons"], ["uncommitted changes: run af gate commit first"])

        (worktree / "README.md").write_text("unstaged\n")
        decide()
        git(worktree, "reset", "--hard", "HEAD")
        (worktree / "staged.txt").write_text("staged\n")
        git(worktree, "add", "staged.txt")
        decide()
        git(worktree, "reset", "--hard", "HEAD")
        (worktree / "untracked.txt").write_text("untracked\n")
        decide()

    def commit_file(self, worktree, flow_id, relative, text="value = 1\n"):
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        committed = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", flow_id,
                                "--message", f"add {relative}", "--paths", relative)
        self.assertEqual(committed.returncode, 0, committed.stderr)

    def decide(self, worktree, flow_id, tests="pass", reviews="pass"):
        result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", flow_id,
                             "--tests", tests, "--reviews", reviews)
        return result, json.loads(result.stdout)

    def write_gate_config(self, **extra):
        self.config.write_text(json.dumps({"tests": {"focused": "fake-test focused", "full": "fake-test full"},
                                           "auto_merge": True, "high_risk_paths": [], **extra}))

    def test_payment_diff_needs_the_owner_and_exits_three(self):
        self.write_gate_config(owner_approval_paths=["payments/", "*stripe*"])
        worktree = self.create_flow("gate-owner")
        self.commit_file(worktree, "gate-owner", "src/stripe_client.py")
        self.commit_file(worktree, "gate-owner", "docs/notes.md")

        result, report = self.decide(worktree, "gate-owner")

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(report["decision"], "owner")
        self.assertEqual(report["owner_approval_paths"], ["src/stripe_client.py"])
        self.assertEqual(report["high_risk_paths"], [])
        self.assertEqual(report["reasons"], ["payment paths need the owner's approval before merging"])

    def test_owner_outranks_ask_and_blocked_outranks_owner(self):
        self.write_gate_config(owner_approval_paths=["payments/"], high_risk_paths=["payments/"],
                               auto_merge=False)
        worktree = self.create_flow("gate-owner-rank")
        self.commit_file(worktree, "gate-owner-rank", "payments/api.py")

        result, report = self.decide(worktree, "gate-owner-rank")
        self.assertEqual((result.returncode, report["decision"]), (3, "owner"))
        self.assertEqual(report["high_risk_paths"], ["payments/api.py"])
        self.assertEqual(report["owner_approval_paths"], ["payments/api.py"])

        for tests, reviews in (("fail", "pass"), ("pass", "pending")):
            result, report = self.decide(worktree, "gate-owner-rank", tests, reviews)
            self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))
            self.assertEqual(report["owner_approval_paths"], ["payments/api.py"])

    def test_decide_without_payment_paths_reports_an_empty_owner_list(self):
        self.write_gate_config(owner_approval_paths=["payments/"])
        worktree = self.create_flow("gate-owner-none")
        self.commit_file(worktree, "gate-owner-none", "docs/notes.md")

        result, report = self.decide(worktree, "gate-owner-none")

        self.assertEqual((result.returncode, report["decision"]), (0, "merge"))
        self.assertEqual(report["owner_approval_paths"], [])


    def add_origin(self, set_head=True):
        origin = self.base / "origin.git"
        git(self.base, "init", "-q", "--bare", str(origin))
        git(self.repo, "remote", "add", "origin", str(origin))
        branch = git(self.repo, "branch", "--show-current")
        git(self.repo, "push", "-q", "origin", branch)
        git(self.repo, "fetch", "-q", "origin")
        if set_head:
            git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
        return origin

    def test_decide_blocks_a_base_that_is_not_on_origins_default_branch(self):
        self.add_origin()
        (self.repo / "local.txt").write_text("unpushed\n")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-qm", "unpushed local commit")
        worktree = self.create_flow("gate-base-off-origin")

        result, report = self.decide(worktree, "gate-base-off-origin")

        self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))
        self.assertEqual(report["reasons"], [
            "base_sha is not on origin's default branch: the PR would carry unpushed commits; "
            "run git fetch origin or start the flow from origin's default branch"])

    def test_decide_accepts_a_base_on_origin_and_skips_the_check_without_origin_head(self):
        self.add_origin()
        worktree = self.create_flow("gate-base-on-origin")
        self.commit_file(worktree, "gate-base-on-origin", "docs/notes.md")
        result, report = self.decide(worktree, "gate-base-on-origin")
        self.assertEqual((result.returncode, report["decision"]), (0, "merge"))

        git(self.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        (self.repo / "local.txt").write_text("unpushed\n")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-qm", "unpushed local commit")
        unchecked = self.create_flow("gate-no-origin-head")
        result, report = self.decide(unchecked, "gate-no-origin-head")
        self.assertEqual((result.returncode, report["decision"]), (0, "merge"))

    def install_gh(self):
        """Fake gh that logs every call and answers each subcommand from FAKE_GH_<SUB>_{OUT,ERR,EXIT}."""
        self.gh_log = self.base / "gh-calls"
        gh = self.bin / "gh"
        gh.write_text("#!/bin/sh\n"
                      "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
                      "case \"$1 $2\" in\n"
                      "  'pr view') out=$FAKE_GH_VIEW_OUT err=$FAKE_GH_VIEW_ERR code=${FAKE_GH_VIEW_EXIT:-0}\n"
                      "    case \"$*\" in *' --json state') out=$FAKE_GH_STATE_OUT code=${FAKE_GH_STATE_EXIT:-0};; esac;;\n"
                      "  'api graphql') out=$FAKE_GH_GRAPHQL_OUT err=$FAKE_GH_GRAPHQL_ERR code=${FAKE_GH_GRAPHQL_EXIT:-0};;\n"
                      "  'api repos/{owner}/{repo}/branches/'*) out=$FAKE_GH_BRANCH_OUT err=$FAKE_GH_BRANCH_ERR code=${FAKE_GH_BRANCH_EXIT:-0};;\n"
                      "  'api repos/{owner}/{repo}/rules/branches/'*) out=$FAKE_GH_RULES_OUT err=$FAKE_GH_RULES_ERR code=${FAKE_GH_RULES_EXIT:-0};;\n"
                      "  'pr merge') out=$FAKE_GH_MERGE_OUT err=$FAKE_GH_MERGE_ERR code=${FAKE_GH_MERGE_EXIT:-0};;\n"
                      "  *) echo \"unexpected gh call: $*\" >&2; exit 99;;\n"
                      "esac\n"
                      "printf '%s' \"$out\"; printf '%s' \"$err\" >&2; exit \"$code\"\n")
        gh.chmod(0o755)
        self.env["GH_LOG"] = str(self.gh_log)
        self.env["FAKE_GH_STATE_OUT"] = json.dumps({"state": "MERGED"})
        self.required_checks()

    def required_checks(self, contexts=(), rules=()):
        """Script the base branch protection (contexts) and repository rules (rule contexts) the fake gh reports."""
        protection = {"enabled": bool(contexts), "required_status_checks": {
            "contexts": list(contexts), "checks": [{"context": name, "app_id": None} for name in contexts],
            "enforcement_level": "non_admins"}} if contexts else {"enabled": False}
        self.env["FAKE_GH_BRANCH_OUT"] = json.dumps({"name": "main", "protection": protection})
        self.env["FAKE_GH_RULES_OUT"] = json.dumps(
            [{"type": "required_status_checks",
              "parameters": {"required_status_checks": [{"context": name} for name in rules]}}] if rules else [])

    def gh_calls(self):
        return self.gh_log.read_text().splitlines() if self.gh_log.exists() else []

    def gh_pr(self, tip, rollup=None, **extra):
        view = {"id": "PR_node1", "number": 7, "url": "https://example.invalid/o/r/pull/7", "state": "OPEN",
                "headRefOid": tip, "baseRefName": "main", "statusCheckRollup": [] if rollup is None else rollup, **extra}
        self.env["FAKE_GH_VIEW_OUT"] = json.dumps(view)

    def merge_flow(self, flow_id, path="docs/notes.md"):
        worktree = self.create_flow(flow_id)
        self.commit_file(worktree, flow_id, path)
        self.install_gh()
        tip = git(worktree, "rev-parse", f"refs/heads/af/{flow_id}")
        self.gh_pr(tip)
        return worktree, tip

    def gate_merge(self, worktree, flow_id, *extra, tests="pass", reviews="pass"):
        result = self.run_af("gate", "merge", "--repo", worktree, "--flow-id", flow_id,
                             "--tests", tests, "--reviews", reviews, *extra)
        return result, json.loads(result.stdout)

    def test_merge_stops_without_calling_gh_when_blocked_or_owner_approval_is_missing(self):
        self.write_gate_config(owner_approval_paths=["payments/"])
        worktree, _ = self.merge_flow("gate-merge-stop", path="payments/api.py")

        result, report = self.gate_merge(worktree, "gate-merge-stop", tests="fail")
        self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))
        result, report = self.gate_merge(worktree, "gate-merge-stop")
        self.assertEqual((result.returncode, report["decision"]), (3, "owner"))
        self.assertEqual(report["owner_approval_paths"], ["payments/api.py"])
        result, report = self.gate_merge(worktree, "gate-merge-stop", "--owner-approved", tests="fail")
        self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))
        self.assertEqual(self.gh_calls(), [])

    def test_merge_with_owner_approval_merges_a_payment_diff(self):
        self.write_gate_config(owner_approval_paths=["payments/"])
        worktree, tip = self.merge_flow("gate-merge-owner-ok", path="payments/api.py")

        result, report = self.gate_merge(worktree, "gate-merge-owner-ok", "--owner-approved")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((report["decision"], report["merge"]), ("owner", "merged"))
        self.assertEqual(self.gh_calls()[-2], f"pr merge 7 --squash --match-head-commit {tip}")

    def test_merge_treats_ask_as_approved_and_reports_pr_details(self):
        self.write_gate_config(high_risk_paths=["docs/"], auto_merge=False)
        worktree, tip = self.merge_flow("gate-merge-ask")

        result, report = self.gate_merge(worktree, "gate-merge-ask")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((report["decision"], report["merge"]), ("ask", "merged"))
        self.assertEqual(report["high_risk_paths"], ["docs/notes.md"])
        self.assertEqual(report["owner_approval_paths"], [])
        self.assertEqual((report["pr"], report["url"]), (7, "https://example.invalid/o/r/pull/7"))
        self.assertEqual(self.gh_calls(), [
            "pr view af/gate-merge-ask --json id,number,url,state,headRefOid,baseRefName,statusCheckRollup",
            f"pr merge 7 --squash --match-head-commit {tip}",
            "pr view af/gate-merge-ask --json state"])

    def test_merge_requires_an_open_pr_at_the_tested_tip(self):
        worktree, tip = self.merge_flow("gate-merge-pr")
        cases = [
            ("failed query", {"FAKE_GH_VIEW_EXIT": "1", "FAKE_GH_VIEW_OUT": ""}),
            ("bad json", {"FAKE_GH_VIEW_OUT": "not json"}),
            ("merged", None),
            ("moved tip", None),
        ]
        for label, overrides in cases:
            with self.subTest(label):
                self.env.pop("FAKE_GH_VIEW_EXIT", None)
                self.gh_pr(tip)
                if label == "merged": self.gh_pr(tip, state="MERGED")
                if label == "moved tip": self.gh_pr("0" * 40)
                self.env.update(overrides or {})
                result, report = self.gate_merge(worktree, "gate-merge-pr")
                self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))
                self.assertIn("push the tested tip and open the PR", report["reasons"][0])
        self.assertTrue(all(call.startswith("pr view") for call in self.gh_calls()))

        self.env.pop("FAKE_GH_VIEW_EXIT", None)
        (self.bin / "gh").unlink()
        tools = self.base / "no-gh"
        tools.mkdir()
        for name in ("bash", "python3", "git", "dirname", "env", "sh"):
            (tools / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(tools)
        result, report = self.gate_merge(worktree, "gate-merge-pr")
        self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))

    def test_merge_is_blocked_while_ci_is_failing(self):
        worktree, tip = self.merge_flow("gate-merge-ci-fail")
        failing = [
            [{"__typename": "CheckRun", "name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"},
             {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "TIMED_OUT"}],
            [{"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR"},
             {"__typename": "CheckRun", "name": "slow", "status": "IN_PROGRESS", "conclusion": ""}],
        ]
        for rollup, names in zip(failing, ("lint", "ci/legacy")):
            with self.subTest(names):
                self.gh_pr(tip, rollup)
                result, report = self.gate_merge(worktree, "gate-merge-ci-fail")
                self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))
                self.assertEqual(report["reasons"], [f"CI is failing: {names}"])
        self.assertTrue(all(call.startswith("pr view") for call in self.gh_calls()))

    def test_merge_enables_auto_merge_while_checks_are_pending(self):
        worktree, tip = self.merge_flow("gate-merge-pending")
        pending = [
            [{"__typename": "CheckRun", "name": "unit", "status": "QUEUED", "conclusion": ""}],
            [{"__typename": "StatusContext", "context": "ci/legacy", "state": "EXPECTED"}],
            [{"__typename": "CheckRun", "name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
             {"__typename": "CheckRun", "name": "b", "status": "IN_PROGRESS", "conclusion": ""}],
        ]
        for rollup in pending:
            with self.subTest(rollup=rollup):
                self.gh_log.unlink(missing_ok=True)
                self.gh_pr(tip, rollup)
                self.required_checks(contexts=["unit", "ci/legacy"], rules=["b"])
                result, report = self.gate_merge(worktree, "gate-merge-pending")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(report["merge"], "auto")
                calls = self.gh_calls()
                self.assertEqual([call.split(" ")[0:2] for call in calls],
                                 [["pr", "view"], ["api", "repos/{owner}/{repo}/branches/main"],
                                  ["api", "repos/{owner}/{repo}/rules/branches/main"], ["api", "graphql"]])
                for expected in ("enablePullRequestAutoMerge", "mergeMethod: SQUASH", "expectedHeadOid",
                                 "id=PR_node1", f"oid={tip}"):
                    self.assertIn(expected, calls[-1])

    def test_merge_waits_when_a_pending_check_is_not_required(self):
        worktree, tip = self.merge_flow("gate-merge-unrequired")
        self.gh_pr(tip, [{"__typename": "CheckRun", "name": "unit", "status": "QUEUED", "conclusion": ""},
                         {"__typename": "CheckRun", "name": "e2e", "status": "IN_PROGRESS", "conclusion": ""},
                         {"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING"}])
        self.required_checks(contexts=["unit"])

        result, report = self.gate_merge(worktree, "gate-merge-unrequired")

        self.assertEqual(result.returncode, 4)
        self.assertEqual(report["merge"], "waiting_for_ci")
        self.assertEqual(report["reasons"], ["GitHub auto-merge would not wait for these non-required checks: "
                                             "e2e, ci/legacy"])
        self.assertFalse(any(call.startswith(("api graphql", "pr merge")) for call in self.gh_calls()))

    def test_merge_decides_an_empty_rollup_with_workflows_by_the_required_checks(self):
        worktree, tip = self.merge_flow("gate-merge-registering", path=".github/workflows/ci.yml")
        self.required_checks(rules=["build"])
        result, report = self.gate_merge(worktree, "gate-merge-registering")
        self.assertEqual((result.returncode, report["merge"]), (0, "auto"))
        self.assertTrue(self.gh_calls()[-1].startswith("api graphql "))

        self.gh_log.unlink()
        self.required_checks()
        result, report = self.gate_merge(worktree, "gate-merge-registering")
        self.assertEqual((result.returncode, report["merge"]), (4, "waiting_for_ci"))
        self.assertEqual(report["reasons"], ["CI has not registered checks and none are required"])
        self.assertFalse(any(call.startswith(("api graphql", "pr merge")) for call in self.gh_calls()))

    def test_merge_waits_when_the_required_checks_cannot_be_read(self):
        worktree, tip = self.merge_flow("gate-merge-api-error")
        self.gh_pr(tip, [{"__typename": "CheckRun", "name": "unit", "status": "QUEUED", "conclusion": ""}])
        for label, overrides in (
                ("branch api fails", {"FAKE_GH_BRANCH_EXIT": "1", "FAKE_GH_BRANCH_ERR": "HTTP 403"}),
                ("rules api fails", {"FAKE_GH_RULES_EXIT": "1", "FAKE_GH_RULES_ERR": "HTTP 500"}),
                ("branch bad json", {"FAKE_GH_BRANCH_OUT": "not json"}),
                ("rules not a list", {"FAKE_GH_RULES_OUT": "{}"})):
            with self.subTest(label):
                self.required_checks(contexts=["unit"])
                self.gh_log.unlink(missing_ok=True)
                self.env.update(overrides)
                result, report = self.gate_merge(worktree, "gate-merge-api-error")
                self.assertEqual((result.returncode, report["merge"]), (4, "waiting_for_ci"))
                self.assertTrue(report["reasons"][0])
                self.assertFalse(any(call.startswith(("api graphql", "pr merge")) for call in self.gh_calls()))
            for name in overrides: self.env.pop(name)

    def test_merge_reports_waiting_for_ci_when_auto_merge_cannot_be_enabled(self):
        worktree, tip = self.merge_flow("gate-merge-waiting")
        self.gh_pr(tip, [{"__typename": "CheckRun", "name": "unit", "status": "QUEUED", "conclusion": ""}])
        self.required_checks(contexts=["unit"])
        self.env.update(FAKE_GH_GRAPHQL_EXIT="1", FAKE_GH_GRAPHQL_ERR="Auto merge is not allowed")

        result, report = self.gate_merge(worktree, "gate-merge-waiting")

        self.assertEqual(result.returncode, 4)
        self.assertEqual((report["merge"], report["reasons"]), ("waiting_for_ci", ["Auto merge is not allowed"]))
        self.assertFalse(any(call.startswith("pr merge") for call in self.gh_calls()))

    def test_merge_merges_directly_when_no_check_is_failing_or_pending(self):
        worktree, tip = self.merge_flow("gate-merge-green")
        green = [
            [],
            [{"__typename": "CheckRun", "name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
             {"__typename": "CheckRun", "name": "b", "status": "COMPLETED", "conclusion": "NEUTRAL"},
             {"__typename": "CheckRun", "name": "c", "status": "COMPLETED", "conclusion": "SKIPPED"},
             {"__typename": "StatusContext", "context": "ci/legacy", "state": "SUCCESS"}],
        ]
        for rollup in green:
            with self.subTest(rollup=rollup):
                self.gh_log.unlink(missing_ok=True)
                self.gh_pr(tip, rollup)
                result, report = self.gate_merge(worktree, "gate-merge-green")
                self.assertEqual((result.returncode, report["merge"]), (0, "merged"))
                self.assertEqual(self.gh_calls()[-2], f"pr merge 7 --squash --match-head-commit {tip}")

        self.env.update(FAKE_GH_MERGE_EXIT="1", FAKE_GH_MERGE_ERR="head moved")
        result, report = self.gate_merge(worktree, "gate-merge-green")
        self.assertEqual((result.returncode, report["decision"]), (2, "blocked"))
        self.assertEqual(report["reasons"], ["head moved"])

    def test_merge_reports_queued_until_github_says_merged(self):
        worktree, tip = self.merge_flow("gate-merge-queue")
        cases = [("merge queue", {"FAKE_GH_STATE_OUT": json.dumps({"state": "OPEN"})}),
                 ("state unreadable", {"FAKE_GH_STATE_EXIT": "1", "FAKE_GH_STATE_OUT": ""}),
                 ("merged", {})]
        for label, overrides in cases:
            with self.subTest(label):
                self.env.update(overrides)
                result, report = self.gate_merge(worktree, "gate-merge-queue")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(report["merge"], "merged" if label == "merged" else "queued")
            for name in overrides: self.env.pop(name)
            self.env["FAKE_GH_STATE_OUT"] = json.dumps({"state": "MERGED"})

    def test_decide_returns_exit_two_for_failed_checks_and_zero_for_merge(self):
        worktree = self.create_flow("gate-exit-status")
        for tests, reviews in (("fail", "pass"), ("pass", "fail")):
            result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-exit-status",
                                 "--tests", tests, "--reviews", reviews)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "blocked")

        merged = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-exit-status",
                             "--tests", "pass", "--reviews", "pass")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertEqual(json.loads(merged.stdout)["decision"], "merge")

    def test_decide_with_flow_id_reads_committed_changes_from_flow_branch(self):
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": ["src/auth"]}))
        worktree = self.create_flow("gate-flow-head")
        status = self.run_af("state", "status", "--repo", str(self.repo), "--flow-id", "gate-flow-head")
        self.assertEqual(json.loads(status.stdout)["worktree_path"], str(worktree))
        target = worktree / "src" / "auth" / "token.py"
        target.parent.mkdir(parents=True)
        target.write_text("value = 1\n")
        committed = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-flow-head",
                                "--message", "commit flow risk", "--paths", "src/auth/token.py")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        decision = self.run_af("gate", "decide", "--repo", str(self.repo), "--flow-id", "gate-flow-head",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        result = json.loads(decision.stdout)
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(result["high_risk_paths"], ["src/auth/token.py"])

    def test_high_risk_paths_follow_git_check_ignore_for_slash_patterns(self):
        patterns = ["src/auth", "src/auth/", "db/*", "config/secrets", "a/**/b",
                    "**/secrets", "/db/*.sql", "ignored/"]
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": patterns}))
        worktree = self.create_flow("gate-patterns")
        paths = ["src/auth/token.py", "db/migrations/1.sql", "config/secrets/client.json",
                 "a/x/b/file.txt", "nested/secrets/key.txt", "db/direct.sql", "nested/db/direct.sql",
                 "ignored/secret.txt"]
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(patterns) + "\n")
        for path in paths:
            target = worktree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture\n")
        tracked_paths = [path for path in paths if not path.startswith("ignored/")]
        git(worktree, "add", "-f", *tracked_paths)
        git(worktree, "commit", "-qm", "add risk pattern fixtures")
        expected = subprocess.run(
            ["git", "-C", str(self.repo), "check-ignore", "--no-index", "-z", "--stdin"],
            input=("\0".join(tracked_paths) + "\0").encode(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True).stdout.decode().split("\0")
        expected = sorted(path for path in expected if path)
        decision = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-patterns",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        result = json.loads(decision.stdout)
        self.assertEqual(result["high_risk_paths"], expected)
        self.assertEqual(result["decision"], "ask" if expected else "merge")

    def test_commit_scans_explicit_paths_and_commits_only_the_requested_file(self):
        self.fake_gitleaks()
        worktree = self.create_flow("gate-selected")
        (worktree / "safe.txt").write_text("ordinary content\n")
        (worktree / "other.txt").write_text("also changed\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-selected",
                             "--message", "commit selected path", "--paths", "safe.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["paths"], ["safe.txt"])
        changed = git(worktree, "show", "--pretty=format:", "--name-only", "HEAD").splitlines()
        self.assertEqual(changed, ["safe.txt"])
        self.assertTrue((worktree / "other.txt").exists())
        self.assertIn("protect --staged", self.test_log.read_text())

    def test_commit_accepts_internal_relative_symlink_and_scans_link_text(self):
        self.disable_gitleaks()
        worktree = self.create_flow("gate-relative-link")
        (worktree / "target.txt").write_text("safe target\n")
        (worktree / "alias.txt").symlink_to("target.txt")
        (worktree / "link.txt").symlink_to("alias.txt")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-relative-link",
                             "--message", "commit internal symlink", "--paths", "link.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(worktree, "show", "HEAD:link.txt"), "alias.txt")

        (worktree / "secret-link").symlink_to("ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789")
        before = git(worktree, "rev-parse", "HEAD")
        refused = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-relative-link",
                              "--message", "reject secret in symlink text", "--paths", "secret-link")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("secret pattern github-token", refused.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--name-only"), "")

    def test_commit_accepts_parent_relative_directory_symlink_inside_worktree(self):
        self.disable_gitleaks()
        target = self.repo / "agents" / "x"
        target.mkdir(parents=True)
        (target / "README.md").write_text("agent skill fixture\n")
        git(self.repo, "add", "agents/x/README.md")
        git(self.repo, "commit", "-qm", "add internal directory target")
        worktree = self.create_flow("gate-parent-relative-link")
        skills = worktree / "skills"
        skills.mkdir()
        (skills / "x").symlink_to("../agents/x")

        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-parent-relative-link",
                             "--message", "commit internal sibling directory symlink", "--paths", "skills/x")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(worktree, "show", "HEAD:skills/x"), "../agents/x")
        self.assertTrue((worktree / "skills" / "x").resolve().is_dir())

    def test_commit_refuses_unsafe_symlinks_and_restores_the_index(self):
        worktree = self.create_flow("gate-unsafe-links")
        (worktree / "preserved.txt").write_text("already staged\n")
        git(worktree, "add", "preserved.txt")
        original_index = git(worktree, "diff", "--cached", "--binary")
        cases = [
            ("absolute-link", "/etc/passwd"),
            ("escaping-link", "../../outside"),
        ]
        before = git(worktree, "rev-parse", "HEAD")
        for name, target in cases:
            (worktree / name).symlink_to(target)
            result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-unsafe-links",
                                 "--message", "refuse unsafe symlink", "--paths", name)
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn("symlink", result.stderr.lower(), name)
            self.assertIn("target", result.stderr.lower(), name)
            self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
            self.assertEqual(git(worktree, "diff", "--cached", "--binary"), original_index, name)
            (worktree / name).unlink()

    def test_commit_refuses_symlink_escape_through_an_intermediate_symlink(self):
        worktree = self.create_flow("gate-intermediate-escape")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file").write_text("outside\n")
        (worktree / "inner").symlink_to("../outside")
        outer = worktree / "outer"
        outer.symlink_to("inner/file")
        target = Path(os.readlink(outer))
        self.assertFalse(target.is_absolute())
        self.assertNotIn("..", target.parts)
        (worktree / "preserved.txt").write_text("already staged\n")
        git(worktree, "add", "preserved.txt")
        original_index = git(worktree, "diff", "--cached", "--binary")
        before = git(worktree, "rev-parse", "HEAD")

        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-intermediate-escape",
                             "--message", "refuse nested symlink escape", "--paths", "outer")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink target escapes the worktree", result.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--binary"), original_index)

    def test_commit_ignores_a_preexisting_token_on_an_unchanged_line(self):
        self.disable_gitleaks()
        token = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        fixture = self.repo / "existing_fixture.py"
        fixture.write_text(f"fixture = '{token}'\nvalue = 0\n")
        git(self.repo, "add", "existing_fixture.py")
        git(self.repo, "commit", "-qm", "add preexisting fake fixture")
        worktree = self.create_flow("gate-added-lines-existing")
        (worktree / "existing_fixture.py").write_text(f"fixture = '{token}'\nvalue = 1\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-added-lines-existing",
                             "--message", "change unrelated fixture line", "--paths", "existing_fixture.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(token, git(worktree, "show", "HEAD:existing_fixture.py"))

    def test_commit_rejects_tokens_on_added_lines_and_in_new_files(self):
        self.disable_gitleaks()
        fixture = self.repo / "fixture.py"
        fixture.write_text("value = 0\n")
        git(self.repo, "add", "fixture.py")
        git(self.repo, "commit", "-qm", "add fixture")
        worktree = self.create_flow("gate-added-lines-reject")
        token = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        before = git(worktree, "rev-parse", "HEAD")
        (worktree / "fixture.py").write_text(f"value = 0\nGITHUB_TOKEN = '{token}'\n")
        added = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-added-lines-reject",
                            "--message", "reject added token", "--paths", "fixture.py")
        self.assertNotEqual(added.returncode, 0)
        self.assertIn("secret pattern github-token", added.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--name-only"), "")

        (worktree / "fixture.py").write_text("value = 0\n")
        (worktree / "new_fixture.py").write_text(f"GITHUB_TOKEN = '{token}'\n")
        new_file = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-added-lines-reject",
                               "--message", "reject token in new file", "--paths", "new_fixture.py")
        self.assertNotEqual(new_file.returncode, 0)
        self.assertIn("secret pattern github-token", new_file.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--name-only"), "")

    def test_gitleaks_scans_the_staged_index(self):
        self.fake_gitleaks()
        worktree = self.create_flow("gate-gitleaks")
        (worktree / "review.txt").write_text("ordinary text\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-gitleaks",
                             "--message", "use gitleaks", "--paths", "review.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["secret_scan"], "gitleaks")
        self.assertEqual(self.test_log.read_text().strip(), "protect --staged")

    def test_fallback_secret_scan_blocks_common_key_patterns_without_committing(self):
        isolated_path = self.base / "without-gitleaks"
        isolated_path.mkdir()
        for name, target in (("bash", shutil.which("bash")), ("python3", shutil.which("python3")),
                             ("env", "/usr/bin/env"), ("git", "/usr/bin/git"),
                             ("dirname", "/usr/bin/dirname")):
            (isolated_path / name).symlink_to(target)
        self.env["PATH"] = str(isolated_path)
        worktree = self.create_flow("gate-secret")
        secret_file = worktree / "leak.txt"
        secret_file.write_text("api_key=abcdefghijklmnopqrstuvwxyz123456\n")
        before = git(worktree, "rev-parse", "HEAD")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-secret",
                             "--message", "must not commit", "--paths", "leak.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret pattern", result.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)

    def test_gitleaks_failure_and_auto_merge_disabled_both_stop(self):
        self.fake_gitleaks(exit_code=1)
        worktree = self.create_flow("gate-hook")
        (worktree / "review.txt").write_text("ordinary text\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-hook",
                             "--message", "scanner refusal", "--paths", "review.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(worktree, "status", "--porcelain"), "?? review.txt")

        self.config.write_text(json.dumps({"auto_merge": False, "high_risk_paths": []}))
        worktree = self.create_flow("gate-auto-merge")
        decision = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-auto-merge",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(json.loads(decision.stdout)["decision"], "ask")


if __name__ == "__main__":
    unittest.main()
