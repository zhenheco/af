import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class WorktreeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env.pop("AF_CONFIG", None)
        self.repo = self.base / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "fixture")
        self.checkout_sha = git(self.repo, "rev-parse", "HEAD")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.gh_log = self.base / "gh-args"
        (self.bin / "gh").write_text("#!/bin/sh\n"
                                     f"printf '%s\\n' \"$*\" >> {self.gh_log}\n"
                                     "printf '%s' \"${FAKE_GH_JSON:-}\"\n"
                                     "exit ${FAKE_GH_EXIT:-0}\n")
        (self.bin / "gh").chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_af(self, *args):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_create_and_remove_owned_worktree_without_touching_checkout(self):
        created = self.run_af("worktree", "create", "--repo", str(self.repo),
                              "--flow-id", "test-001")
        self.assertEqual(created.returncode, 0, created.stderr)
        record = json.loads(created.stdout)
        worktree = Path(record["path"])
        self.assertEqual(record["branch"], "af/test-001")
        self.assertTrue((worktree / "README.md").is_file())
        self.assertEqual(git(self.repo, "config", "--get", "branch.af/test-001.merge"),
                         git(self.repo, "symbolic-ref", "HEAD"))
        self.assertFalse((self.repo / ".git/.af/worktrees/test-001.json").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.checkout_sha)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        removed = self.run_af("worktree", "remove", "--repo", str(self.repo),
                              "--flow-id", "test-001")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "af/test-001"), "")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.checkout_sha)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_remove_allows_flow_branch_tip_already_merged_into_base(self):
        flow_id = "merged-flow"
        created = self.run_af("worktree", "create", "--repo", str(self.repo),
                              "--flow-id", flow_id)
        worktree = Path(json.loads(created.stdout)["path"])
        (worktree / "merged.txt").write_text("already in base\n")
        git(worktree, "add", "merged.txt")
        git(worktree, "commit", "-qm", "flow change")
        git(self.repo, "merge", "--ff-only", f"af/{flow_id}")
        removed = self.run_af("worktree", "remove", "--repo", str(self.repo), "--flow-id", flow_id)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", f"af/{flow_id}"), "")

    def test_remove_ignores_gitignored_files_after_flow_branch_is_merged(self):
        flow_id = "ignored-build"
        created = self.run_af("worktree", "create", "--repo", str(self.repo),
                              "--flow-id", flow_id)
        worktree = Path(json.loads(created.stdout)["path"])
        (worktree / ".gitignore").write_text("ignored-output/\n")
        git(worktree, "add", ".gitignore")
        git(worktree, "commit", "-qm", "ignore generated output")
        git(self.repo, "merge", "--ff-only", f"af/{flow_id}")
        output = worktree / "ignored-output" / "bundle.js"
        output.parent.mkdir()
        output.write_text("generated\n")

        removed = self.run_af("worktree", "remove", "--repo", str(self.repo), "--flow-id", flow_id)

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", f"af/{flow_id}"), "")

    def test_remove_refuses_tracked_changes_and_untracked_nonignored_files(self):
        flow_id = "dirty-flow"
        created = self.run_af("worktree", "create", "--repo", str(self.repo),
                              "--flow-id", flow_id)
        worktree = Path(json.loads(created.stdout)["path"])
        (worktree / "README.md").write_text("modified\n")

        tracked = self.run_af("worktree", "remove", "--repo", str(self.repo), "--flow-id", flow_id)

        self.assertNotEqual(tracked.returncode, 0)
        self.assertIn("tracked changes", tracked.stderr)
        self.assertTrue(worktree.exists())
        (worktree / "README.md").write_text("fixture\n")
        (worktree / "untracked.txt").write_text("untracked\n")

        untracked = self.run_af("worktree", "remove", "--repo", str(self.repo), "--flow-id", flow_id)

        self.assertNotEqual(untracked.returncode, 0)
        self.assertIn("untracked non-ignored files", untracked.stderr)
        self.assertTrue(worktree.exists())

    def app_worktree(self, name="app"):
        path = self.base / f"{name}-session"
        git(self.repo, "worktree", "add", "-q", "-b", f"claude/{name}", str(path), "HEAD")
        return path

    def reuse(self, worktree, flow_id):
        return self.run_af("worktree", "create", "--repo", str(worktree), "--flow-id", flow_id,
                           "--reuse-current")

    def commit_in(self, worktree, name):
        (worktree / name).write_text("change\n")
        git(worktree, "add", name)
        git(worktree, "commit", "-qm", f"add {name}")
        return git(worktree, "rev-parse", "HEAD")

    def squash_merge(self, flow_id):
        git(self.repo, "merge", "--squash", f"af/{flow_id}")
        git(self.repo, "commit", "-qm", f"squash {flow_id}")

    def gh_reports(self, state, head):
        self.env["FAKE_GH_JSON"] = json.dumps({"state": state, "headRefOid": head})

    def remove(self, flow_id, *extra):
        return self.run_af("worktree", "remove", "--repo", str(self.repo), "--flow-id", flow_id, *extra)

    def test_reuse_current_switches_the_linked_worktree_to_the_flow_branch(self):
        app = self.app_worktree()
        head = git(app, "rev-parse", "HEAD")
        created = self.reuse(app, "reuse-001")
        self.assertEqual(created.returncode, 0, created.stderr)
        record = json.loads(created.stdout)
        self.assertEqual(record["path"], str(app.resolve()))
        self.assertEqual(record["base_sha"], head)
        self.assertTrue(record["reused"])
        self.assertEqual(record["original_branch"], "claude/app")
        self.assertEqual(git(app, "branch", "--show-current"), "af/reuse-001")
        self.assertFalse((self.base / "project-reuse-001").exists())
        self.assertFalse((self.base / "app-session-reuse-001").exists())

        (self.repo / "af.config.json").write_text("{}\n")
        started = self.run_af("state", "start", "--repo", str(app), "--flow-id", "reuse-001",
                              "--base-sha", head, "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)
        state = json.loads(Path(json.loads(started.stdout)["state_path"]).read_text())
        self.assertTrue(state["reused"])
        self.assertEqual(state["original_branch"], "claude/app")

    def test_reuse_current_refuses_primary_checkout_and_dirty_worktrees(self):
        primary = self.reuse(self.repo, "reuse-primary")
        self.assertNotEqual(primary.returncode, 0)
        self.assertIn("linked worktree", primary.stderr)
        self.assertEqual(git(self.repo, "branch", "--list", "af/reuse-primary"), "")
        app = self.app_worktree()
        (app / "scratch.txt").write_text("not mine\n")
        dirty = self.reuse(app, "reuse-dirty")
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("clean", dirty.stderr)
        self.assertEqual(git(app, "branch", "--show-current"), "claude/app")

    def test_reuse_current_refuses_a_worktree_held_by_another_active_flow(self):
        app = self.app_worktree()
        self.assertEqual(self.reuse(app, "reuse-a").returncode, 0)
        (self.repo / "af.config.json").write_text("{}\n")
        started = self.run_af("state", "start", "--repo", str(app), "--flow-id", "reuse-a",
                              "--base-sha", git(app, "rev-parse", "HEAD"), "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)
        on_flow = self.reuse(app, "reuse-b")
        self.assertNotEqual(on_flow.returncode, 0)
        self.assertIn("active flow", on_flow.stderr)
        git(app, "switch", "-q", "claude/app")
        switched_away = self.reuse(app, "reuse-b")
        self.assertNotEqual(switched_away.returncode, 0)
        self.assertIn("active flow reuse-a", switched_away.stderr)

    def test_remove_accepts_squash_merged_pull_request_with_matching_head(self):
        created = self.run_af("worktree", "create", "--repo", str(self.repo), "--flow-id", "squash")
        worktree = Path(json.loads(created.stdout)["path"])
        head = self.commit_in(worktree, "feature.txt")
        self.squash_merge("squash")
        self.gh_reports("MERGED", head)
        removed = self.remove("squash")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "af/squash"), "")
        self.assertIn("pr view af/squash --json state,headRefOid", self.gh_log.read_text())

    def add_origin(self):
        origin = self.base / "origin.git"
        git(self.base, "init", "-q", "--bare", str(origin))
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", git(self.repo, "branch", "--show-current"))
        return origin

    def merged_flow_with_remote_branch(self, flow_id, push=True):
        worktree = Path(json.loads(self.run_af("worktree", "create", "--repo", str(self.repo),
                                               "--flow-id", flow_id).stdout)["path"])
        head = self.commit_in(worktree, f"{flow_id}.txt")
        if push:
            git(worktree, "push", "-q", "origin", f"af/{flow_id}")
        self.squash_merge(flow_id)
        self.gh_reports("MERGED", head)
        return worktree, head

    def remote_heads(self, origin):
        return git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/af/")

    def test_remove_deletes_the_remote_branch_when_it_sits_at_the_merged_tip(self):
        origin = self.add_origin()
        worktree, _ = self.merged_flow_with_remote_branch("remote-gone")
        self.assertEqual(self.remote_heads(origin), "af/remote-gone")

        removed = self.remove("remote-gone")

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(json.loads(removed.stdout)["remote_branch"], "deleted")
        self.assertEqual(self.remote_heads(origin), "")
        self.assertFalse(worktree.exists())

    def test_remove_reports_absent_remote_branch_and_missing_remote(self):
        origin = self.add_origin()
        self.merged_flow_with_remote_branch("remote-absent", push=False)
        removed = self.remove("remote-absent")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(json.loads(removed.stdout)["remote_branch"], "absent")

        git(self.repo, "remote", "remove", "origin")
        self.merged_flow_with_remote_branch("no-remote", push=False)
        removed = self.remove("no-remote")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(json.loads(removed.stdout)["remote_branch"], "no remote")

    def test_remove_keeps_a_remote_branch_at_another_sha_or_that_cannot_be_deleted(self):
        origin = self.add_origin()
        self.merged_flow_with_remote_branch("remote-moved", push=False)
        git(self.repo, "push", "-q", "origin", f"{self.checkout_sha}:refs/heads/af/remote-moved")
        removed = self.remove("remote-moved")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(json.loads(removed.stdout)["remote_branch"].startswith("kept: "))
        self.assertEqual(git(origin, "rev-parse", "refs/heads/af/remote-moved"), self.checkout_sha)

        self.merged_flow_with_remote_branch("remote-protected")
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho protected >&2\nexit 1\n")
        hook.chmod(0o755)
        removed = self.remove("remote-protected")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(json.loads(removed.stdout)["remote_branch"].startswith("kept: "))
        self.assertIn("af/remote-protected", self.remote_heads(origin))

    def test_pushed_upstream_of_its_own_branch_is_not_a_merge_proof(self):
        origin = self.add_origin()
        for remote in ("origin", "mirror", "custom"):
            with self.subTest(remote=remote):
                flow_id = f"pushed-{remote}"
                if remote == "custom":
                    # The tracking ref name no longer ends with the branch name.
                    git(self.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/review-*")
                    remote = "origin"
                elif remote != "origin":
                    git(self.repo, "remote", "add", remote, str(origin))
                worktree = Path(json.loads(self.run_af("worktree", "create", "--repo", str(self.repo),
                                                       "--flow-id", flow_id).stdout)["path"])
                head = self.commit_in(worktree, f"{flow_id}.txt")
                git(worktree, "push", "-q", "-u", remote, f"af/{flow_id}")
                self.gh_reports("OPEN", head)

                removed = self.remove(flow_id)

                self.assertNotEqual(removed.returncode, 0)
                self.assertIn("not merged", removed.stderr)
                self.assertTrue(worktree.exists())
                self.assertEqual(git(self.repo, "branch", "--list", f"af/{flow_id}"), f"+ af/{flow_id}")
                self.assertIn(f"af/{flow_id}", self.remote_heads(origin))

    def test_extra_tracking_values_cannot_hide_the_own_branch_upstream(self):
        origin = self.add_origin()
        worktree = Path(json.loads(self.run_af("worktree", "create", "--repo", str(self.repo),
                                               "--flow-id", "multi-track").stdout)["path"])
        head = self.commit_in(worktree, "multi.txt")
        git(worktree, "push", "-q", "-u", "origin", "af/multi-track")
        # --get would return only this last value; @{upstream} uses the first.
        git(worktree, "config", "--add", "branch.af/multi-track.merge", "refs/heads/main")
        self.gh_reports("OPEN", head)

        removed = self.remove("multi-track")

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("not merged", removed.stderr)
        self.assertTrue(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "af/multi-track"), "+ af/multi-track")
        self.assertIn("af/multi-track", self.remote_heads(origin))

    def test_remove_keeps_a_remote_branch_that_advances_between_the_check_and_the_delete(self):
        origin = self.add_origin()
        worktree, head = self.merged_flow_with_remote_branch("remote-race")
        self.commit_in(worktree, "late.txt")
        late = git(worktree, "rev-parse", "HEAD")
        git(worktree, "push", "-q", "origin", f"{late}:refs/heads/late-work")
        git(worktree, "reset", "-q", "--hard", head)
        # PATH wrapper: right after ls-remote answers, someone pushes to af/remote-race.
        real_git = shutil.which("git")
        (self.bin / "git").write_text(
            "#!/bin/sh\n"
            f"{real_git} \"$@\"; code=$?\n"
            "for arg in \"$@\"; do [ \"$arg\" = ls-remote ] && "
            f"{real_git} -C {origin} update-ref refs/heads/af/remote-race {late}; done\n"
            "exit $code\n")
        (self.bin / "git").chmod(0o755)

        removed = self.remove("remote-race")

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(json.loads(removed.stdout)["remote_branch"].startswith("kept: "))
        self.assertEqual(git(origin, "rev-parse", "refs/heads/af/remote-race"), late)

    def test_remove_does_not_delete_a_tip_that_moves_after_the_merge_proof(self):
        origin = self.add_origin()
        worktree, head = self.merged_flow_with_remote_branch("proof-race")
        # B has A's tree, so the worktree stays clean while the branch moves to it.
        git(worktree, "commit", "-q", "--allow-empty", "-m", "late")
        late = git(worktree, "rev-parse", "HEAD")
        git(worktree, "push", "-q", "origin", f"{late}:refs/heads/late-work")
        git(worktree, "reset", "-q", "--hard", head)
        real_git = shutil.which("git")
        # The fake gh proves A merged, but advances af/proof-race to B (locally and on origin) first.
        (self.bin / "gh").write_text(
            "#!/bin/sh\n"
            f"{real_git} -C {self.repo} update-ref refs/heads/af/proof-race {late}\n"
            f"{real_git} -C {origin} update-ref refs/heads/af/proof-race {late}\n"
            f"printf '%s' '{json.dumps({'state': 'MERGED', 'headRefOid': head})}'\n")

        removed = self.remove("proof-race")

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("moved after its merge was proven", removed.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/af/proof-race"), late)
        self.assertEqual(git(origin, "rev-parse", "refs/heads/af/proof-race"), late)

    def test_force_remove_never_touches_the_remote_branch(self):
        origin = self.add_origin()
        worktree = Path(json.loads(self.run_af("worktree", "create", "--repo", str(self.repo),
                                               "--flow-id", "remote-force").stdout)["path"])
        self.commit_in(worktree, "feature.txt")
        git(worktree, "push", "-q", "origin", "af/remote-force")

        removed = self.remove("remote-force", "--force")

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(json.loads(removed.stdout)["remote_branch"].startswith("kept: "))
        self.assertEqual(self.remote_heads(origin), "af/remote-force")

    def test_remove_fails_closed_unless_github_proves_the_same_head_merged(self):
        created = self.run_af("worktree", "create", "--repo", str(self.repo), "--flow-id", "unproven")
        worktree = Path(json.loads(created.stdout)["path"])
        head = self.commit_in(worktree, "feature.txt")
        self.squash_merge("unproven")
        cases = [("OPEN", head, 0), ("MERGED", "0" * 40, 0), ("MERGED", head, 1)]
        for state, reported, status in cases:
            with self.subTest(state=state, reported=reported, status=status):
                self.gh_reports(state, reported)
                self.env["FAKE_GH_EXIT"] = str(status)
                removed = self.remove("unproven")
                self.assertNotEqual(removed.returncode, 0)
                self.assertIn("not merged", removed.stderr)
                self.assertTrue(worktree.exists())
        (self.bin / "gh").unlink()
        tools = self.base / "no-gh"
        tools.mkdir()
        for name in ("bash", "python3", "git", "dirname", "env"):
            (tools / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(tools)
        missing = self.remove("unproven")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("not merged", missing.stderr)
        self.assertTrue(worktree.exists())

    def test_remove_reused_flow_keeps_the_worktree_and_restores_its_branch(self):
        app = self.app_worktree()
        self.assertEqual(self.reuse(app, "reuse-done").returncode, 0)
        head = self.commit_in(app, "feature.txt")
        self.squash_merge("reuse-done")
        self.gh_reports("MERGED", head)
        removed = self.remove("reuse-done")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(app.is_dir())
        self.assertIn(str(app.resolve()), git(self.repo, "worktree", "list"))
        self.assertEqual(git(app, "branch", "--show-current"), "claude/app")
        self.assertEqual(git(self.repo, "branch", "--list", "af/reuse-done"), "")

    def set_origin_head(self, name="trunk"):
        git(self.repo, "remote", "add", "origin", "https://example.invalid/project.git")
        git(self.repo, "update-ref", f"refs/remotes/origin/{name}", "HEAD")
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{name}")

    def test_reuse_current_bases_the_flow_branch_on_the_origin_default_branch(self):
        self.set_origin_head()
        git(self.repo, "branch", "other", "HEAD")
        app = self.app_worktree()
        git(app, "branch", "--set-upstream-to", "other")
        self.assertEqual(self.reuse(app, "reuse-base").returncode, 0)
        self.assertEqual(git(app, "rev-parse", "--symbolic-full-name", "af/reuse-base@{upstream}"),
                         "refs/remotes/origin/trunk")

    def test_remove_reused_flow_merged_into_origin_default_branch_needs_no_force(self):
        self.set_origin_head()
        app = self.app_worktree()
        self.assertEqual(self.reuse(app, "reuse-ff").returncode, 0)
        tip = self.commit_in(app, "feature.txt")
        git(self.repo, "update-ref", "refs/remotes/origin/trunk", tip)
        removed = self.remove("reuse-ff")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(app.is_dir())
        self.assertEqual(git(app, "branch", "--show-current"), "claude/app")
        self.assertEqual(git(self.repo, "branch", "--list", "af/reuse-ff"), "")

    def test_reuse_current_without_origin_head_keeps_the_original_branch_upstream(self):
        git(self.repo, "remote", "add", "origin", "https://example.invalid/project.git")
        git(self.repo, "branch", "other", "HEAD")
        tracked = self.app_worktree("tracked")
        git(tracked, "branch", "--set-upstream-to", "other")
        self.assertEqual(self.reuse(tracked, "reuse-old").returncode, 0)
        self.assertEqual(git(tracked, "rev-parse", "--symbolic-full-name", "af/reuse-old@{upstream}"),
                         "refs/heads/other")
        bare = self.app_worktree("bare")
        self.assertEqual(self.reuse(bare, "reuse-none").returncode, 0)
        self.assertEqual(git(bare, "for-each-ref", "--format=%(upstream)", "refs/heads/af/reuse-none"), "")

    def test_force_remove_of_reused_flow_never_discards_worktree_changes(self):
        app = self.app_worktree()
        self.assertEqual(self.reuse(app, "reuse-dirty").returncode, 0)
        (app / "work.txt").write_text("unsaved\n")
        removed = self.remove("reuse-dirty", "--force")
        self.assertNotEqual(removed.returncode, 0)
        self.assertTrue((app / "work.txt").is_file())
        self.assertEqual(git(app, "branch", "--show-current"), "af/reuse-dirty")

    def start_sibling(self, flow_id):
        created = self.run_af("worktree", "create", "--repo", str(self.repo), "--flow-id", flow_id)
        worktree = Path(json.loads(created.stdout)["path"])
        (self.repo / "af.config.json").write_text("{}\n")
        started = self.run_af("state", "start", "--repo", str(worktree), "--flow-id", flow_id,
                              "--base-sha", self.checkout_sha, "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)
        return worktree, Path(json.loads(started.stdout)["state_path"])

    def test_remove_fails_closed_when_record_and_branch_marker_disagree_on_reuse(self):
        worktree, state_path = self.start_sibling("lost-marker")
        record = json.loads(state_path.read_text())
        record.update({"reused": True, "original_branch": "claude/app"})
        state_path.write_text(json.dumps(record))
        removed = self.remove("lost-marker", "--force")
        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("disagree", removed.stderr)
        self.assertTrue(worktree.is_dir())
        self.assertEqual(git(worktree, "branch", "--show-current"), "af/lost-marker")

        record.pop("reused"); record.pop("original_branch")
        state_path.write_text(json.dumps(record))
        git(self.repo, "config", "branch.af/lost-marker.afReusedFrom", "claude/app")
        removed = self.remove("lost-marker", "--force")
        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("disagree", removed.stderr)
        self.assertTrue(worktree.is_dir())


if __name__ == "__main__":
    unittest.main()
