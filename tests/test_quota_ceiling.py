import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from config import validate_config
from selection import choose

W = lambda name, used, **kw: {"name": name, "used": used, "remaining": 100 - used if isinstance(used, (int, float)) else 0, **kw}


class Server:
    """Local fake magpie; `body` is served verbatim, `hits` counts requests."""
    def __init__(self, body, status=200):
        outer = self
        self.hits = 0
        self.body = body
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                raw = outer.body if isinstance(outer.body, bytes) else json.dumps(outer.body).encode()
                self.send_response(status)
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args): pass
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/magpie/quotas"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def cand(via="host", company="OpenAI", name="m", **extra):
    return {"via": via, "model": name, "company": company, **extra}


class ChooseQuotaTests(unittest.TestCase):
    def setUp(self):
        self.servers = []
        patch = mock.patch.dict(os.environ, {}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("AF_NO_HOST", None)

    def serve(self, body, status=200):
        server = Server(body, status)
        self.servers.append(server)
        os.environ["AF_QUOTA_URL"] = server.url
        return server

    def tearDown(self):
        for server in self.servers: server.close()

    def run_choose(self, candidates, ceiling=80, role="writer", writer_company=None):
        config = {"roles": {role: candidates}}
        if ceiling is not None: config["quota_ceiling_percent"] = ceiling
        return choose(role, config, writer_company)

    def pair(self, **first):
        return [cand(name="a", quota_provider="codex", **first), cand(name="b", company="Anthropic")]

    def data(self, *providers): return {"data": list(providers)}

    def test_disabled_makes_no_request_and_matches_old_result(self):
        server = self.serve(self.data({"provider": "codex", "windows": [W("5 hours", 99)]}))
        result = self.run_choose(self.pair(), ceiling=None)
        self.assertEqual(server.hits, 0)
        self.assertEqual(result["selected"]["model"], "a")
        for key in ("quota_skipped", "quota_check", "quota_ceiling_reached"):
            self.assertNotIn(key, result)

    def test_no_candidate_with_quota_provider_makes_no_request(self):
        server = self.serve(self.data())
        result = self.run_choose([cand(name="a"), cand(name="b")])
        self.assertEqual(server.hits, 0)
        self.assertEqual(result["selected"]["model"], "a")

    def test_candidate_over_ceiling_is_skipped_and_next_selected(self):
        server = self.serve(self.data({"provider": "codex", "windows": [W("5 hours", 85), W("7 days", 10)]}))
        result = self.run_choose(self.pair())
        self.assertEqual(server.hits, 1)
        self.assertEqual(result["selected"]["model"], "b")
        self.assertEqual([c["model"] for c in result["candidates"]], ["b"])
        skipped = result["quota_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["candidate"]["model"], "a")
        self.assertEqual(skipped[0]["window"], "5 hours")
        self.assertEqual(skipped[0]["used"], 85)

    def test_exactly_at_threshold_skips_and_below_does_not(self):
        self.serve(self.data({"provider": "codex", "windows": [W("5 hours", 80)]}))
        self.assertEqual(self.run_choose(self.pair())["selected"]["model"], "b")
        self.servers[-1].body = self.data({"provider": "codex", "windows": [W("5 hours", 79.9)]})
        result = self.run_choose(self.pair())
        self.assertEqual(result["selected"]["model"], "a")
        self.assertNotIn("quota_skipped", result)

    def test_quota_windows_limits_which_windows_count(self):
        self.serve(self.data({"provider": "codex", "windows": [
            W("5 hours", 10), W("7 days", 20), W("Image generation", 99, lane="images")]}))
        result = self.run_choose(self.pair(quota_windows=["5 hours", "7 days"]))
        self.assertEqual(result["selected"]["model"], "a")
        result = self.run_choose(self.pair())
        self.assertEqual(result["selected"]["model"], "b")
        self.assertEqual(result["quota_skipped"][0]["window"], "Image generation")

    def test_unlimited_window_is_ignored(self):
        self.serve(self.data({"provider": "codex", "windows": [W("Chat requests", 100, unlimited=True)]}))
        self.assertEqual(self.run_choose(self.pair())["selected"]["model"], "a")

    def test_non_numeric_used_is_ignored(self):
        self.serve(self.data({"provider": "codex", "windows": [W("5 hours", "99"), W("7 days", None), W("x", True)]}))
        self.assertEqual(self.run_choose(self.pair())["selected"]["model"], "a")

    def test_any_account_over_ceiling_skips(self):
        self.serve(self.data({"provider": "codex", "windows": [W("5 hours", 5)]},
                             {"provider": "codex", "windows": [W("5 hours", 90)]}))
        self.assertEqual(self.run_choose(self.pair())["selected"]["model"], "b")

    def test_balance_with_positive_amount_keeps_candidate(self):
        for balance in ("5761.98 credits", "$12.40", "1.2K credits", "¥193.45 · $0.00"):
            self.serve(self.data({"provider": "codex", "kind": "subscription", "balance": balance,
                                  "windows": [W("5 hours", 0), W("7 days", 100)]}))
            result = self.run_choose(self.pair())
            self.assertEqual(result["selected"]["model"], "a", balance)
            self.assertNotIn("quota_skipped", result)

    def test_missing_zero_or_non_string_balance_still_skips(self):
        for extra in ({}, {"balance": "0 credits"}, {"balance": "$0.00"}, {"balance": 5761.98}, {"balance": None}):
            self.serve(self.data({"provider": "codex", "windows": [W("7 days", 100)], **extra}))
            self.assertEqual(self.run_choose(self.pair())["selected"]["model"], "b", extra)

    def test_balance_is_per_account(self):
        self.serve(self.data({"provider": "codex", "balance": "9 credits", "windows": [W("5 hours", 100)]},
                             {"provider": "codex", "windows": [W("5 hours", 100)]}))
        self.assertEqual(self.run_choose(self.pair())["selected"]["model"], "b")

    def test_fetch_failures_fail_open_with_quota_check(self):
        cases = {"bad json": (b"not json", 200), "wrong shape": ({"data": "x"}, 200),
                 "http error": ({"data": []}, 500)}
        for name, (body, status) in cases.items():
            with self.subTest(name):
                self.serve(body, status)
                result = self.run_choose(self.pair())
                self.assertEqual(result["selected"]["model"], "a")
                self.assertTrue(result["quota_check"])
                self.assertNotIn("quota_skipped", result)

    def test_unreachable_server_fails_open(self):
        server = Server({})
        os.environ["AF_QUOTA_URL"] = server.url
        server.close()
        result = self.run_choose(self.pair())
        self.assertEqual(result["selected"]["model"], "a")
        self.assertTrue(result["quota_check"])

    def test_provider_missing_or_without_windows_is_not_skipped(self):
        for providers in ([{"provider": "copilot", "windows": [W("x", 99)]}],
                          [{"provider": "codex", "windows": []}]):
            with self.subTest(providers=providers):
                self.serve(self.data(*providers))
                result = self.run_choose(self.pair())
                self.assertEqual(result["selected"]["model"], "a")
        self.serve(self.data({"provider": "copilot", "windows": []}))
        self.assertTrue(self.run_choose(self.pair())["quota_check"])

    def test_all_candidates_over_ceiling(self):
        self.serve(self.data({"provider": "codex", "windows": [W("5 hours", 95)]}))
        result = self.run_choose([cand(name="a", quota_provider="codex")])
        self.assertIsNone(result["selected"])
        self.assertTrue(result["quota_ceiling_reached"])
        self.assertIn("quota", result["error"])
        self.assertEqual(len(result["quota_skipped"]), 1)

    def test_no_available_candidates_is_not_ceiling_reached(self):
        self.serve(self.data())
        result = self.run_choose([{"via": "no-such-cli-xyz", "company": "OpenAI", "profile": "p",
                                   "quota_provider": "codex"}])
        self.assertIsNone(result["selected"])
        self.assertNotIn("quota_ceiling_reached", result)

    def test_reviewer_company_filter_runs_before_quota_filter(self):
        server = self.serve(self.data({"provider": "codex", "windows": [W("5 hours", 99)]}))
        candidates = [cand(name="same", company="OpenAI", quota_provider="codex"),
                      cand(name="other", company="Anthropic")]
        result = self.run_choose(candidates, role="small_review", writer_company="OpenAI")
        self.assertEqual(result["selected"]["model"], "other")
        self.assertEqual(result["review"], "independent")
        # same-company candidate was already filtered out, so it is never reported as quota-skipped
        self.assertNotIn("quota_skipped", result)
        self.assertEqual(server.hits, 0)


class ConfigValidationTests(unittest.TestCase):
    roles = lambda self, **extra: {"roles": {"writer": [cand(**extra)]}}

    def test_valid(self):
        validate_config({**self.roles(quota_provider="codex", quota_windows=["5 hours"]),
                         "quota_ceiling_percent": 80})
        validate_config({**self.roles(), "quota_ceiling_percent": 100})
        validate_config({**self.roles(), "quota_ceiling_percent": 0.5})

    def test_invalid_ceiling(self):
        for value in (0, -1, 100.1, "80", True, None, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({**self.roles(), "quota_ceiling_percent": value})

    def test_invalid_candidate_fields(self):
        for extra in ({"quota_provider": ""}, {"quota_provider": 3}, {"quota_windows": "5 hours"},
                      {"quota_windows": []}, {"quota_windows": [""]}, {"quota_windows": [1]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_config(self.roles(**extra))

    def test_allowed_on_every_via(self):
        for candidate in ({"via": "pi", "provider": "p", "model": "m"}, {"via": "codex", "profile": "p"},
                          {"via": "claude", "model": "m"}, {"via": "host", "model": "m"}):
            validate_config({"roles": {"writer": [{**candidate, "company": "X", "quota_provider": "codex"}]}})


if __name__ == "__main__":
    unittest.main()
