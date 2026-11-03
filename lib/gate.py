"""Test, commit, and merge-decision gates for flow worktrees."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from config import load_recorded_config
from state import add_flow_args, read_record, state_path
from worktree import common_dir, git, repository_root

SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("named-secret", re.compile(r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token)\b\s*[:=]\s*[\"']?[A-Za-z0-9/+_=-]{24,}")),
]

def git_paths(repo, *args):
    output = git(repo, *args).stdout
    return [path for path in output.split("\0") if path]

def high_risk_paths(repo, patterns, base, head="HEAD"):
    changed = git_paths(repo, "diff", "--no-renames", "--name-only", "-z", f"{base}..{head}")
    if not changed or not patterns: return []
    with tempfile.TemporaryDirectory(prefix="af-high-risk-") as temp:
        git(temp, "init", "--template=", "-q")
        exclude = Path(temp) / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("".join(f"{pattern}\n" for pattern in patterns))
        global_excludes = Path(temp) / "global-excludes"
        global_excludes.touch()
        git(temp, "config", "core.excludesFile", str(global_excludes))
        result = subprocess.run(["git", "-C", temp, "check-ignore", "--no-index", "-z", "--stdin"],
                                input="\0".join(changed) + "\0", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "git check-ignore failed")
        return sorted(path for path in result.stdout.split("\0") if path)

def run_test_suite(repo, config, suite):
    command = config.get("tests", {}).get(suite)
    if not command: raise RuntimeError(f"tests.{suite} is not configured")
    timeout = float(config.get("test_timeout_minutes", 30)) * 60
    try:
        result = subprocess.run(command, cwd=repo, shell=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        code, timed_out = result.returncode, False
    except subprocess.TimeoutExpired:
        code, timed_out = 124, True
    return code, {"suite": suite, "command": command, "exit_status": code,
                  "passed": code == 0, "timed_out": timed_out}

def safe_commit_paths(repo, requested):
    root = Path(repo).resolve()
    changed = set(git_paths(root, "diff", "--no-renames", "--name-only", "-z")
                  + git_paths(root, "diff", "--cached", "--no-renames", "--name-only", "-z")
                  + git_paths(root, "ls-files", "--others", "-z"))
    if not requested: raise ValueError("--paths must list at least one changed path")
    result = []
    for raw in requested:
        value = Path(raw)
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise ValueError(f"commit path must stay inside the worktree: {raw}")
        relative = value.as_posix()
        if relative not in changed:
            raise ValueError(f"commit path is not changed: {relative}")
        candidate = root
        for part in value.parts:
            candidate /= part
            if candidate.is_symlink():
                target = Path(os.readlink(candidate))
                if target.is_absolute():
                    raise ValueError(f"unsafe symlink target for commit path: {relative}")
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise ValueError(f"symlink target escapes the worktree: {relative}") from exc
        result.append(relative)
    return list(dict.fromkeys(result))

def index_snapshot(repo):
    value = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    path = Path(value)
    if not path.is_absolute():
        path = (Path(repo) / path).resolve()
    return path, path.read_bytes() if path.exists() else None

def restore_index(snapshot):
    path, content = snapshot
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)

def scan_added_lines(repo):
    patch = git(repo, "--literal-pathspecs", "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv",
                "--unified=0").stdout
    added = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(added):
            raise RuntimeError(f"secret pattern {label} found in added lines")
    return "regex"

def scan_staged(repo):
    executable = shutil.which("gitleaks")
    if not executable: return scan_added_lines(repo)
    result = subprocess.run([executable, "protect", "--staged"], cwd=repo, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("gitleaks found a secret or could not scan the staged changes")
    return "gitleaks"

def commit_paths(repo, flow_id, paths, message):
    expected = f"af/{flow_id}"
    branch = git(repo, "branch", "--show-current").stdout.strip()
    if branch != expected:
        raise RuntimeError(f"gate commit requires branch {expected}")
    snapshot = index_snapshot(repo)
    try:
        chosen = safe_commit_paths(repo, paths)
        result = git(repo, "--literal-pathspecs", "add", "-A", "--", *chosen, check=False)
        if result.returncode: raise RuntimeError(result.stderr.strip() or "git add failed")
        staged = set(git_paths(repo, "diff", "--cached", "--no-renames", "--name-only", "-z"))
        if staged != set(chosen):
            raise RuntimeError("staged paths must match --paths; clear unrelated staged changes first")
        scanner = scan_staged(repo)
        commit = git(repo, "commit", "-m", message, check=False)
        if commit.returncode:
            raise RuntimeError(commit.stderr.strip() or "git commit failed")
        sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        return {"commit": sha, "paths": chosen, "secret_scan": scanner}
    except Exception:
        restore_index(snapshot)
        raise

def flow_context(repo, flow_id):
    record = read_record(state_path(repo, flow_id))
    if not record.get("config_path"):
        raise RuntimeError(f"flow {flow_id} has no recorded config path")
    path = Path(record.get("worktree_path", "")).resolve()
    if not path.is_dir() or Path(git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve() != path:
        raise RuntimeError(f"flow worktree is unavailable: {path}")
    if git(path, "branch", "--show-current").stdout.strip() != f"af/{flow_id}":
        raise RuntimeError(f"flow worktree is not on branch af/{flow_id}")
    if common_dir(path) != common_dir(repo):
        raise RuntimeError(f"flow worktree belongs to another repository: {path}")
    return record, path

def add_gate_parser(commands):
    parser = commands.add_parser("gate", help="run test, commit, and merge gates")
    actions = parser.add_subparsers(dest="gate_action", required=True)
    for name in ("test", "decide"):
        action = add_flow_args(actions.add_parser(name))
        if name == "test":
            action.add_argument("--suite", choices=("focused", "full"), required=True)
        else:
            action.add_argument("--tests", choices=("pass", "fail", "pending"), required=True)
            action.add_argument("--reviews", choices=("pass", "fail", "pending"), required=True)
    commit = add_flow_args(actions.add_parser("commit"))
    commit.add_argument("--paths", nargs="+", required=True)
    commit.add_argument("--message", required=True)

def emit(result, status=0):
    print(json.dumps(result, ensure_ascii=False))
    return status

def run_gate(args):
    root = repository_root(args.repo)
    if args.gate_action == "commit":
        load_recorded_config(read_record(state_path(root, args.flow_id)), root)
        return emit(commit_paths(root, args.flow_id, args.paths, args.message))

    try:
        record, worktree = flow_context(root, args.flow_id)
        config = load_recorded_config(record, root)
    except (RuntimeError, ValueError) as exc:
        if args.gate_action == "decide":
            return emit({"decision": "blocked", "reasons": [f"cannot load required config: {exc}"]}, 2)
        return emit({"suite": args.suite, "passed": False, "timed_out": False, "error": str(exc)}, 2)

    if args.gate_action == "test":
        try:
            code, result = run_test_suite(worktree, config, args.suite)
        except (RuntimeError, ValueError) as exc:
            result = {"suite": args.suite, "passed": False, "timed_out": False, "error": str(exc)}
            code = 2
        return emit(result, code)

    base = record.get("base_sha")
    if git(worktree, "status", "--porcelain=v1", "-z").stdout:
        return emit({"decision": "blocked", "reasons": ["uncommitted changes: run af gate commit first"]}, 2)
    head = f"refs/heads/af/{args.flow_id}"
    try:
        high = high_risk_paths(worktree, config.get("high_risk_paths", []), base, head)
    except (RuntimeError, ValueError) as exc:
        return emit({"decision": "blocked", "reasons": [f"cannot check changed paths: {exc}"]}, 2)
    if args.tests != "pass" or args.reviews != "pass":
        decision, reasons = "blocked", ["tests and reviews must pass"]
    elif high:
        decision, reasons = "ask", ["high-risk paths require owner review"]
    elif not config.get("auto_merge", True):
        decision, reasons = "ask", ["auto_merge is disabled"]
    else:
        decision, reasons = "merge", []
    return emit({"decision": decision, "reasons": reasons, "high_risk_paths": high},
                2 if decision == "blocked" else 0)
