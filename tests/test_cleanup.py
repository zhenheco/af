import json
import hashlib
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class CleanupCommandTests(unittest.TestCase):
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
        self.common_dir = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.flows = self.common_dir / ".af" / "flows"
        self.flows.mkdir(parents=True)
        self.config = self.base / "af.config.json"
        self.config.write_text(json.dumps({"record_retention": {
            "archive_after_days": 30, "archive_dir": "archive", "delete_after_days": 180
        }}))

    def tearDown(self):
        self.temp.cleanup()

    def write_record(self, flow_id, age_days):
        created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        path = self.flows / f"{flow_id}.json"
        path.write_text(json.dumps({"version": 1, "flow_id": flow_id, "created_at": created,
                                    "updated_at": created, "status": "complete", "steps": []}))
        return path

    def write_logs(self, flow_id, base):
        folder = base / flow_id
        folder.mkdir(parents=True)
        out = folder / "tdd-1-20261015T000000.000000Z.out.log"
        err = folder / "tdd-1-20261015T000000.000000Z.err.log"
        out.write_text("worker output\n")
        err.write_text("worker error\n")
        return out, err

    def run_af(self, *args):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], cwd=self.repo, env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_archives_after_threshold_and_deletes_after_retention(self):
        archiveable = self.write_record("archive-me", 40)
        expired = self.write_record("delete-me", 181)
        recent = self.write_record("keep-me", 5)
        repo_key = f"project-{hashlib.sha256(str(self.common_dir.resolve()).encode()).hexdigest()[:12]}"
        already_archived = self.common_dir / ".af" / "archive" / repo_key / "delete-archived.json"
        already_archived.parent.mkdir(parents=True)
        expired_at = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        already_archived.write_text(json.dumps({"version": 1, "flow_id": "delete-archived",
                                                "status": "complete", "steps": [],
                                                "created_at": expired_at, "updated_at": expired_at}))
        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        archive = self.common_dir / ".af" / "archive" / repo_key / "archive-me.json"
        self.assertEqual(record["moved"], ["archive-me"])
        self.assertEqual(set(record["deleted"]), {"delete-me", "delete-archived"})
        self.assertTrue(archive.is_file())
        self.assertFalse(archiveable.exists())
        self.assertFalse(expired.exists())
        self.assertFalse(already_archived.exists())
        self.assertTrue(recent.is_file())

    def test_no_archive_directory_keeps_records_until_delete_age(self):
        old_config = self.base / "no-archive.json"
        old_config.write_text(json.dumps({"record_retention": {
            "archive_after_days": 10, "archive_dir": None, "delete_after_days": 90
        }}))
        keep = self.write_record("keep-unarchived", 40)
        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(old_config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["moved"], [])
        self.assertTrue(keep.is_file())

    def test_cleanup_never_archives_or_deletes_an_expired_active_record(self):
        active = self.write_record("expired-active", 200)
        record = json.loads(active.read_text())
        record["status"] = "active"
        active.write_text(json.dumps(record))
        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertNotIn("expired-active", report["moved"])
        self.assertNotIn("expired-active", report["deleted"])
        self.assertNotIn("skipped", report)
        self.assertTrue(active.is_file())

    def test_cleanup_archives_and_deletes_flow_logs_with_their_records(self):
        archiveable = self.write_record("archive-with-logs", 40)
        archived_logs = self.write_logs("archive-with-logs", self.common_dir / ".af" / "logs")
        expired = self.write_record("delete-with-logs", 181)
        expired_logs = self.write_logs("delete-with-logs", self.common_dir / ".af" / "logs")

        active = self.write_record("active-with-logs", 181)
        active_logs = self.write_logs("active-with-logs", self.common_dir / ".af" / "logs")
        record = json.loads(active.read_text())
        record["status"] = "active"
        active.write_text(json.dumps(record))

        result = self.run_af("cleanup", "--repo", self.repo, "--config", self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        key = f"project-{hashlib.sha256(str(self.common_dir.resolve()).encode()).hexdigest()[:12]}"
        archive = self.common_dir / ".af" / "archive" / key
        self.assertTrue((archive / "archive-with-logs.json").is_file())
        saved_logs = archive / "archive-with-logs.logs"
        self.assertTrue((saved_logs / archived_logs[0].name).is_file())
        self.assertFalse((self.common_dir / ".af" / "logs" / "archive-with-logs").exists())
        self.assertFalse(expired.exists())
        self.assertFalse((self.common_dir / ".af" / "logs" / "delete-with-logs").exists())
        self.assertTrue(active.exists())
        self.assertTrue((self.common_dir / ".af" / "logs" / "active-with-logs" /
                         active_logs[0].name).is_file())
    def flow_worktree(self, flow_id, status, age_days=1):
        created = self.run_af("worktree", "create", "--repo", str(self.repo), "--flow-id", flow_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree = Path(json.loads(created.stdout)["path"])
        (worktree / f"{flow_id}.txt").write_text("change\n")
        git(worktree, "add", ".")
        git(worktree, "commit", "-qm", flow_id)
        record = json.loads(self.write_record(flow_id, age_days).read_text())
        record.update({"status": status, "worktree_path": str(worktree)})
        (self.flows / f"{flow_id}.json").write_text(json.dumps(record))
        return worktree

    def fake_merged_gh(self, *branches):
        bin_dir = self.base / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "gh").write_text("#!/bin/sh\n"
                                    "case \" $FAKE_GH_MERGED \" in *\" $3 \"*) state=MERGED ;; *) state=OPEN ;; esac\n"
                                    "printf '{\"state\":\"%s\",\"headRefOid\":\"%s\"}' \"$state\" "
                                    "\"$(git rev-parse \"refs/heads/$3\")\"\n")
        (bin_dir / "gh").chmod(0o755)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env['PATH']}"
        self.env["FAKE_GH_MERGED"] = " ".join(branches)
        return bin_dir

    def cleanup_worktrees(self, repo=None):
        result = self.run_af("cleanup", "--repo", str(repo or self.repo), "--config", str(self.config),
                             "--worktrees")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_worktrees_flag_removes_only_completed_flows_proven_merged(self):
        self.fake_merged_gh("af/merged-done")
        merged = self.flow_worktree("merged-done", "complete")
        open_pr = self.flow_worktree("open-done", "complete")
        active = self.flow_worktree("open-active", "active")

        plain = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertNotIn("worktrees", json.loads(plain.stdout))
        self.assertTrue(merged.exists())

        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config),
                             "--worktrees")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)["worktrees"]
        self.assertEqual(report["removed"], ["merged-done"])
        self.assertEqual([item["flow_id"] for item in report["skipped"]], ["open-done"])
        self.assertIn("not merged", report["skipped"][0]["reason"])
        self.assertFalse(merged.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "af/merged-done"), "")
        self.assertTrue(open_pr.exists())
        self.assertTrue(active.exists())

        again = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config),
                            "--worktrees")
        self.assertEqual(json.loads(again.stdout)["worktrees"]["removed"], [])

    def test_worktrees_flag_deletes_the_remote_branch_of_merged_flows_only(self):
        origin = self.base / "origin.git"
        git(self.base, "init", "-q", "--bare", str(origin))
        git(self.repo, "remote", "add", "origin", str(origin))
        self.fake_merged_gh("af/merged-remote")
        merged = self.flow_worktree("merged-remote", "complete")
        open_pr = self.flow_worktree("open-remote", "complete")
        for worktree, flow_id in ((merged, "merged-remote"), (open_pr, "open-remote")):
            git(worktree, "push", "-q", "origin", f"af/{flow_id}")

        report = self.cleanup_worktrees()["worktrees"]

        self.assertEqual(report["removed"], ["merged-remote"])
        self.assertEqual([item["flow_id"] for item in report["skipped"]], ["open-remote"])
        self.assertEqual(git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/af/"),
                         "af/open-remote")

    def test_worktrees_flag_skips_a_pushed_unmerged_flow_and_keeps_its_remote_branch(self):
        origin = self.base / "origin.git"
        git(self.base, "init", "-q", "--bare", str(origin))
        git(self.repo, "remote", "add", "origin", str(origin))
        self.fake_merged_gh()
        worktree = self.flow_worktree("pushed-open", "complete")
        git(worktree, "push", "-q", "-u", "origin", "af/pushed-open")

        report = self.cleanup_worktrees()["worktrees"]

        self.assertEqual(report["removed"], [])
        self.assertEqual([item["flow_id"] for item in report["skipped"]], ["pushed-open"])
        self.assertTrue(worktree.exists())
        self.assertEqual(git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/af/"),
                         "af/pushed-open")

    def test_worktrees_flag_keeps_expired_records_of_skipped_flows_for_retry(self):
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "gh").chmod(0o755)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env['PATH']}"
        worktree = self.flow_worktree("offline", "complete", age_days=200)
        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config),
                             "--worktrees")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([item["flow_id"] for item in report["worktrees"]["skipped"]], ["offline"])
        self.assertNotIn("offline", report["deleted"])
        self.assertTrue((self.flows / "offline.json").is_file())
        self.assertTrue(worktree.exists())

    def test_worktrees_flag_skips_records_whose_content_names_another_flow(self):
        self.fake_merged_gh("af/in-use")
        worktree = self.flow_worktree("in-use", "complete", age_days=200)
        record = json.loads((self.flows / "in-use.json").read_text())
        record["flow_id"] = "someone-else"
        (self.flows / "in-use.json").write_text(json.dumps(record))
        report = self.cleanup_worktrees()
        self.assertEqual(report["worktrees"]["removed"], [])
        self.assertEqual([item["flow_id"] for item in report["worktrees"]["skipped"]], ["in-use"])
        self.assertTrue(worktree.exists())
        self.assertTrue((self.flows / "in-use.json").is_file())

    def test_worktrees_flag_skips_flows_whose_branch_lookup_errors(self):
        bin_dir = self.fake_merged_gh("af/ref-error")
        worktree = self.flow_worktree("ref-error", "complete", age_days=200)
        real_git = shutil.which("git")
        (bin_dir / "git").write_text("#!/bin/sh\n"
                                     "for arg in \"$@\"; do [ \"$arg\" = show-ref ] && exit 128; done\n"
                                     f"exec {real_git} \"$@\"\n")
        (bin_dir / "git").chmod(0o755)
        report = self.cleanup_worktrees()
        self.assertEqual([item["flow_id"] for item in report["worktrees"]["skipped"]], ["ref-error"])
        self.assertTrue((self.flows / "ref-error.json").is_file())
        self.assertTrue(worktree.exists())

    def test_worktrees_flag_works_when_run_from_the_removed_flow_worktree(self):
        self.fake_merged_gh("af/from-inside")
        worktree = self.flow_worktree("from-inside", "complete")
        report = self.cleanup_worktrees(repo=worktree)
        self.assertEqual(report["worktrees"]["removed"], ["from-inside"])
        self.assertFalse(worktree.exists())

    def test_worktrees_flag_keeps_skipped_records_past_the_archive_threshold(self):
        self.fake_merged_gh()
        self.flow_worktree("open-archive", "complete", age_days=40)
        report = self.cleanup_worktrees()
        self.assertEqual([item["flow_id"] for item in report["worktrees"]["skipped"]], ["open-archive"])
        self.assertNotIn("open-archive", report["moved"])
        self.assertTrue((self.flows / "open-archive.json").is_file())

    def record_status(self, flow_id):
        return json.loads((self.flows / f"{flow_id}.json").read_text())

    def test_worktrees_flag_closes_and_removes_an_active_flow_proven_merged(self):
        self.fake_merged_gh("af/merged-active")
        worktree = self.flow_worktree("merged-active", "active")
        report = self.cleanup_worktrees()["worktrees"]
        self.assertEqual(report["closed"], ["merged-active"])
        self.assertEqual(report["removed"], ["merged-active"])
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "af/merged-active"), "")
        record = self.record_status("merged-active")
        self.assertEqual(record["status"], "complete")
        self.assertIn("closed_by_cleanup_at", record)

    def test_worktrees_flag_leaves_an_unmerged_active_flow_untouched(self):
        self.fake_merged_gh()
        worktree = self.flow_worktree("open-active", "active")
        before = (self.flows / "open-active.json").read_text()
        report = self.cleanup_worktrees()["worktrees"]
        self.assertEqual(report["closed"], [])
        self.assertEqual(report["removed"], [])
        self.assertTrue(worktree.exists())
        self.assertIn("af/open-active", git(self.repo, "branch", "--list", "af/open-active"))
        self.assertEqual((self.flows / "open-active.json").read_text(), before)

    def fresh_active_flow(self, flow_id):
        created = self.run_af("worktree", "create", "--repo", str(self.repo), "--flow-id", flow_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree = Path(json.loads(created.stdout)["path"])
        record = json.loads(self.write_record(flow_id, 1).read_text())
        record.update({"status": "active", "worktree_path": str(worktree)})
        (self.flows / f"{flow_id}.json").write_text(json.dumps(record))
        return worktree

    def assert_flow_untouched(self, flow_id, worktree, before):
        report = self.cleanup_worktrees()["worktrees"]
        self.assertEqual((report["closed"], report["removed"], report["skipped"]), ([], [], []))
        self.assertTrue(worktree.exists())
        self.assertIn(f"af/{flow_id}", git(self.repo, "branch", "--list", f"af/{flow_id}"))
        self.assertEqual((self.flows / f"{flow_id}.json").read_text(), before)

    def test_worktrees_flag_leaves_a_just_started_active_flow_untouched(self):
        self.fake_merged_gh()
        worktree = self.fresh_active_flow("just-started")
        self.assert_flow_untouched("just-started", worktree, (self.flows / "just-started.json").read_text())

    def test_worktrees_flag_leaves_an_active_flow_fast_forwarded_locally_without_a_merged_pr(self):
        self.fake_merged_gh()
        worktree = self.flow_worktree("ff-local", "active")
        git(self.repo, "merge", "--ff-only", "af/ff-local")
        self.assert_flow_untouched("ff-local", worktree, (self.flows / "ff-local.json").read_text())

    def test_worktrees_flag_reports_a_merged_active_flow_that_cannot_be_removed(self):
        self.fake_merged_gh("af/merged-dirty")
        worktree = self.flow_worktree("merged-dirty", "active")
        (worktree / "scratch.txt").write_text("uncommitted\n")
        report = self.cleanup_worktrees()["worktrees"]
        self.assertEqual((report["closed"], report["removed"]), ([], []))
        self.assertEqual([item["flow_id"] for item in report["skipped"]], ["merged-dirty"])
        self.assertTrue(worktree.exists())
        self.assertEqual(self.record_status("merged-dirty")["status"], "active")

    def gone_branch_gh(self, state, head):
        bin_dir = self.base / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "gh").write_text("#!/bin/sh\n"
                                    f"printf '{{\"state\":\"{state}\",\"headRefName\":\"{head}\"}}'\n")
        (bin_dir / "gh").chmod(0o755)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env['PATH']}"

    def active_flow_with_branch_gone(self, flow_id):
        self.write_record(flow_id, 1)
        record = self.record_status(flow_id)
        record["status"] = "active"
        (self.flows / f"{flow_id}.json").write_text(json.dumps(record))

    def test_worktrees_flag_closes_an_active_flow_whose_branch_is_gone_when_the_pr_merged(self):
        self.gone_branch_gh("MERGED", "af/gone-merged")
        self.active_flow_with_branch_gone("gone-merged")
        report = self.cleanup_worktrees()["worktrees"]
        self.assertEqual(report["closed"], ["gone-merged"])
        self.assertEqual(self.record_status("gone-merged")["status"], "complete")

    def test_worktrees_flag_keeps_an_active_flow_whose_pr_lookup_does_not_prove_a_merge(self):
        for flow_id, state, head in (("gone-open", "OPEN", "af/gone-open"),
                                     ("gone-closed", "CLOSED", "af/gone-closed"),
                                     ("gone-other", "MERGED", "af/another-head")):
            self.gone_branch_gh(state, head)
            self.active_flow_with_branch_gone(flow_id)
            self.assertEqual(self.cleanup_worktrees()["worktrees"]["closed"], [])
            self.assertEqual(self.record_status(flow_id)["status"], "active")
        (self.base / "bin" / "gh").write_text("#!/bin/sh\nexit 1\n")
        self.active_flow_with_branch_gone("gone-error")
        self.assertEqual(self.cleanup_worktrees()["worktrees"]["closed"], [])
        self.assertEqual(self.record_status("gone-error")["status"], "active")


if __name__ == "__main__":
    unittest.main()
