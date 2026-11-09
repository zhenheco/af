import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from config import config_digest, load_config, validate_config
from gate import high_risk_paths


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.global_config = self.home / ".config" / "af" / "af.config.json"
        self.env = dict(os.environ, HOME=str(self.home))
        self.env.pop("AF_CONFIG", None)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def test_host_agent_accepts_known_matching_company_and_legacy_omission(self):
        validate_config({"roles": {"writer": [{
            "via": "host", "model": "gpt-6-sol", "company": "oPeNaI", "agent": "codex"}]}})
        validate_config({"roles": {"writer": [{
            "via": "host", "model": "claude-sonnet", "company": "Anthropic"}]}})

    def test_host_agent_rejects_unsupported_empty_non_string_and_mismatched_values(self):
        candidates = [
            {"via": "host", "model": "m", "company": "OpenAI", "agent": "cursor"},
            {"via": "host", "model": "m", "company": "OpenAI", "agent": ""},
            {"via": "host", "model": "m", "company": "OpenAI", "agent": None},
            {"via": "host", "model": "m", "company": "OpenAI", "agent": 7},
            {"via": "host", "model": "m", "company": "Anthropic", "agent": "codex"},
            {"via": "host", "model": "m", "company": "OpenAI", "agent": "claude"},
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_config({"roles": {"writer": [candidate]}})

    def test_agent_is_not_allowed_on_cli_candidates(self):
        candidate = {"via": "pi", "provider": "openai", "model": "m",
                     "company": "OpenAI", "agent": "codex"}
        with self.assertRaises(ValueError):
            validate_config({"roles": {"writer": [candidate]}})

    def test_existing_candidate_forms_remain_valid(self):
        validate_config({"roles": {"writer": [
            {"via": "pi", "provider": "openai", "model": "m", "company": "OpenAI",
             "tools": ["read", "edit"], "env_allow": ["OPENAI_API_KEY"]},
            {"via": "codex", "profile": "default", "company": "OpenAI"},
            {"via": "claude", "model": "sonnet", "company": "Anthropic",
             "tools": ["Read", "Edit"], "env_allow": ["ANTHROPIC_API_KEY"],
             "base_url": "https://api.example.invalid", "api_key_command": "printf key"},
            {"via": "host", "model": "claude-sonnet", "company": "Anthropic"},
        ]}})

    def test_project_high_risk_patterns_still_union_with_global_baseline(self):
        self.write_json(self.global_config, {
            "high_risk_paths": ["auth/", "billing/", "shared", "shared"],
            "auto_merge": False,
            "tests": {"full": "false"},
        })
        repo = self.base / "project"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        local = self.write_json(repo / "af.config.json", {
            "high_risk_paths": ["local/", "shared"],
            "auto_merge": True,
            "tests": {"full": "true"},
        })

        config, path = load_config(cwd=repo, env=self.env)
        self.assertEqual(path, local.resolve())
        self.assertEqual(config["high_risk_paths"], ["auth/", "billing/", "local/", "shared"])
        self.assertTrue(config["auto_merge"])
        self.assertEqual(config["tests"], {"full": "true"})

        explicit, explicit_path = load_config(local, env=self.env)
        self.assertEqual(explicit_path, local.resolve())
        self.assertEqual(explicit["high_risk_paths"], config["high_risk_paths"])
        self.assertTrue(explicit["auto_merge"])
        self.assertEqual(explicit["tests"], {"full": "true"})

    def test_project_roles_inherit_global_roles_when_omitted(self):
        global_roles = {
            "writer": [{"via": "host", "model": "global-writer", "company": "OpenAI"}],
            "small_review": [{"via": "host", "model": "global-reviewer", "company": "Anthropic"}],
        }
        self.write_json(self.global_config, {"roles": global_roles})
        local = self.write_json(self.base / "project.json", {"auto_merge": True})

        config, _ = load_config(local, env=self.env)

        self.assertEqual(config["roles"], global_roles)
        self.assertTrue(config["auto_merge"])

    def test_project_roles_replace_global_role_mapping_entirely(self):
        self.write_json(self.global_config, {"roles": {
            "writer": [{"via": "host", "model": "global-writer", "company": "OpenAI"}],
            "small_review": [{"via": "host", "model": "global-reviewer", "company": "Anthropic"}],
        }})
        project_roles = {
            "writer": [{"via": "host", "model": "project-writer", "company": "OpenAI"}],
        }
        local = self.write_json(self.base / "project.json", {"roles": project_roles})

        config, _ = load_config(local, env=self.env)

        self.assertEqual(config["roles"], project_roles)

    def test_empty_project_roles_do_not_inherit_global_roles(self):
        self.write_json(self.global_config, {"roles": {
            "writer": [{"via": "host", "model": "global-writer", "company": "OpenAI"}],
        }})
        local = self.write_json(self.base / "project.json", {"roles": {}})

        config, _ = load_config(local, env=self.env)

        self.assertEqual(config["roles"], {})

    def test_merge_keeps_a_single_copy_of_patterns_duplicated_across_configs(self):
        self.write_json(self.global_config, {"high_risk_paths": ["billing/"]})
        local = self.write_json(self.base / "project.json", {"high_risk_paths": ["billing/"]})

        config, _ = load_config(local, env=self.env)

        self.assertEqual(config["high_risk_paths"], ["billing/"])

    def test_missing_global_file_or_high_risk_key_preserves_project_config(self):
        local = self.write_json(self.base / "project.json", {"auto_merge": True})
        config, _ = load_config(local, env=self.env)
        self.assertEqual(config, {"auto_merge": True})

        self.write_json(self.global_config, {"auto_merge": False, "tests": {"full": "false"}})
        config, _ = load_config(local, env=self.env)
        self.assertEqual(config, {"auto_merge": True})

    def test_missing_global_config_through_non_directory_keeps_project_config(self):
        local = self.write_json(self.base / "project.json", {"high_risk_paths": ["local/"]})
        self.write_json(self.home / ".config", {"not": "a directory"})

        config, _ = load_config(local, env=self.env)

        self.assertEqual(config, {"high_risk_paths": ["local/"]})

    def test_global_config_path_is_loaded_once(self):
        self.write_json(self.global_config, {"high_risk_paths": ["auth/", "auth/"]})
        config, path = load_config(self.global_config, env=self.env)
        self.assertEqual(path, self.global_config.resolve())
        self.assertEqual(config["high_risk_paths"], ["auth/", "auth/"])

    def test_invalid_global_json_or_high_risk_value_fails_closed(self):
        local = self.write_json(self.base / "project.json", {"high_risk_paths": []})
        self.global_config.parent.mkdir(parents=True, exist_ok=True)
        for source in (local, self.global_config):
            for content in ("{", json.dumps({"high_risk_paths": "auth/"}),
                            json.dumps({"high_risk_paths": ["auth/", 1]})):
                with self.subTest(source=source.name, content=content):
                    self.global_config.write_text(content)
                    with self.assertRaises(RuntimeError) as caught:
                        load_config(source, env=self.env)
                    self.assertTrue(str(caught.exception).startswith(
                        f"invalid global config {self.global_config.resolve()}:"))

    def test_invalid_global_roles_fail_closed(self):
        local = self.write_json(self.base / "project.json", {"auto_merge": True})
        self.write_json(self.global_config, {"roles": {
            "writer": [{"via": "host", "model": "missing-company"}],
        }})

        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)

        self.assertTrue(str(caught.exception).startswith(
            f"invalid global config {self.global_config.resolve()}:"))

    def test_non_string_global_role_via_error_includes_global_config_path(self):
        local = self.write_json(self.base / "project.json", {"auto_merge": True})
        self.write_json(self.global_config, {"roles": {
            "writer": [{"via": [], "model": "global-writer", "company": "OpenAI"}],
        }})

        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)

        self.assertIn("invalid global config", str(caught.exception))
        self.assertIn(str(self.global_config.resolve()), str(caught.exception))

    def test_unreadable_global_baseline_fails_closed(self):
        local = self.write_json(self.base / "project.json", {"high_risk_paths": []})
        self.global_config.parent.mkdir(parents=True, exist_ok=True)

        self.global_config.symlink_to(self.base / "missing-target")
        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)
        self.assertTrue(str(caught.exception).startswith(
            f"invalid global config {self.global_config.resolve()}:"))
        self.global_config.unlink()

        self.global_config.mkdir()
        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)
        self.assertTrue(str(caught.exception).startswith(
            f"invalid global config {self.global_config.resolve()}:"))
        self.global_config.rmdir()

        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return  # chmod 000 stays readable for root
        self.write_json(self.global_config, {"high_risk_paths": []})
        self.global_config.chmod(0)
        try:
            with self.assertRaises(RuntimeError) as caught:
                load_config(local, env=self.env)
            self.assertTrue(str(caught.exception).startswith(
                f"invalid global config {self.global_config.resolve()}:"))
        finally:
            self.global_config.chmod(0o600)

    def test_unreadable_global_parent_fails_closed(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read a mode-000 directory")
        local = self.write_json(self.base / "project.json", {"high_risk_paths": []})
        directory = self.global_config.parent
        directory.mkdir(parents=True)
        self.write_json(self.global_config, {"high_risk_paths": ["*"]})
        original_mode = directory.stat().st_mode & 0o777
        self.addCleanup(directory.chmod, original_mode)
        directory.chmod(0)

        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)

        self.assertTrue(str(caught.exception).startswith(f"invalid global config {self.global_config}:"))

    def test_broken_global_parent_symlink_fails_closed(self):
        local = self.write_json(self.base / "project.json", {"high_risk_paths": ["local/"]})
        config_dir = self.home / ".config"
        config_dir.mkdir()
        af_dir = config_dir / "af"
        af_dir.symlink_to(self.base / "unmounted" / "dotfiles" / "af")

        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)

        self.assertTrue(str(caught.exception).startswith(f"invalid global config {self.global_config}:"))
        self.assertIn(f"broken symlink ancestor {af_dir}", str(caught.exception))

    def test_invalid_utf8_configs_fail_with_runtime_error(self):
        local = self.write_json(self.base / "project.json", {"high_risk_paths": []})
        self.global_config.parent.mkdir(parents=True, exist_ok=True)
        self.global_config.write_bytes(b"\xff\xfe invalid utf-8")

        with self.assertRaises(RuntimeError) as caught:
            load_config(self.global_config, env=self.env)
        self.assertTrue(str(caught.exception).startswith(
            f"invalid global config {self.global_config.resolve()}:"))

        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)
        self.assertTrue(str(caught.exception).startswith(
            f"invalid global config {self.global_config.resolve()}:"))

        self.global_config.unlink()
        local.write_bytes(b"\xff\xfe invalid utf-8")
        with self.assertRaises(RuntimeError) as caught:
            load_config(local, env=self.env)
        self.assertTrue(str(caught.exception).startswith(f"invalid config {local.resolve()}:"))
        self.assertFalse(str(caught.exception).startswith("invalid global config"))

    def test_reference_baseline_matches_the_spec_and_git_paths(self):
        expected = """auth/
authn/
authz/
oauth/
session/
sessions/
login/
admin/
audit/
*password*
*credential*
*secret*
*.pem
*.key
.env
.env.*
payment/
payments/
billing/
checkout/
invoice/
invoices/
webhook/
webhooks/
*payment*
*refund*
*invoice*
*webhook*
*checkout*
*reconcil*
*payuni*
*stripe*
*amego*
migrations/
supabase/
prisma/
drizzle/
alembic/
worker/
workers/
functions/
release/
.github/workflows/
.github/actions/
Dockerfile*
docker-compose*.yml
wrangler*.jsonc
wrangler*.toml
fly.toml
vercel.json
netlify.toml
deploy/
scripts/deploy*
scripts/ci*
*.plist
terraform/
*.tf
security/
_headers
crypto.*
crypto/
*.rules
af.config.json""".splitlines()
        baseline_path = ROOT / "docs" / "high-risk-baseline.example.json"
        patterns = json.loads(baseline_path.read_text())["high_risk_paths"]
        self.assertEqual(patterns, expected)
        self.assertEqual(len(patterns), 63)
        validate_config({"high_risk_paths": patterns})

        repo = self.base / "baseline-repo"
        repo.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n")
        git("add", "README.md")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        marked = ["src/auth/login.py", "migrations/0001_init.sql", ".github/workflows/ci.yml",
                  "apps/landing/worker/index.mjs", "apps/x/payment-reconciliation.mjs", ".env.example",
                  "wrangler.production.jsonc"]
        unmarked = ["README.md", "src/components/button.tsx", "src/domain/slip.py"]
        for relative in (*marked, *unmarked):
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("changed\n")
        git("add", "--", *marked, *unmarked)
        git("commit", "-qm", "representative paths")

        self.assertEqual(high_risk_paths(repo, patterns, base), sorted(marked))

    def test_owner_approval_paths_validate_like_high_risk_paths(self):
        validate_config({"owner_approval_paths": ["payment/", "*stripe*"]})
        for value in ("payment/", ["payment/", 1], {"a": "b"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({"owner_approval_paths": value})

    def test_owner_approval_paths_inherit_from_global_with_last_occurrence_dedupe(self):
        self.write_json(self.global_config, {"owner_approval_paths": ["payment/", "shared"]})
        local = self.write_json(self.base / "project.json", {"owner_approval_paths": ["local/", "shared"]})
        config, _ = load_config(local, env=self.env)
        self.assertEqual(config["owner_approval_paths"], ["payment/", "local/", "shared"])

        only_global = self.write_json(self.base / "other.json", {"auto_merge": True})
        config, _ = load_config(only_global, env=self.env)
        self.assertEqual(config["owner_approval_paths"], ["payment/", "shared"])

    def test_invalid_global_owner_approval_paths_fail_closed(self):
        local = self.write_json(self.base / "project.json", {"auto_merge": True})
        for value in ("payment/", ["payment/", 1]):
            with self.subTest(value=value):
                self.write_json(self.global_config, {"owner_approval_paths": value})
                with self.assertRaises(RuntimeError):
                    load_config(local, env=self.env)

    def test_config_without_owner_approval_paths_anywhere_is_unchanged(self):
        raw = {"auto_merge": True, "high_risk_paths": ["auth/"], "tests": {"full": "true"}}
        local = self.write_json(self.base / "project.json", raw)
        self.write_json(self.global_config, {"high_risk_paths": ["billing/"]})
        config, _ = load_config(local, env=self.env)
        self.assertNotIn("owner_approval_paths", config)
        self.assertEqual(config, {**raw, "high_risk_paths": ["billing/", "auth/"]})
        self.assertEqual(config_digest(config), config_digest({**raw, "high_risk_paths": ["billing/", "auth/"]}))


if __name__ == "__main__":
    unittest.main()
