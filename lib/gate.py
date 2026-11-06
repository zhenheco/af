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
    for name in ("test", "decide", "merge"):
        action = add_flow_args(actions.add_parser(name))
        if name == "test":
            action.add_argument("--suite", choices=("focused", "full"), required=True)
        else:
            action.add_argument("--tests", choices=("pass", "fail", "pending"), required=True)
            action.add_argument("--reviews", choices=("pass", "fail", "pending"), required=True)
        if name == "merge":
            action.add_argument("--owner-approved", action="store_true",
                                help="the owner approved merging a diff that touches owner_approval_paths")
    commit = add_flow_args(actions.add_parser("commit"))
    commit.add_argument("--paths", nargs="+", required=True)
    commit.add_argument("--message", required=True)

def emit(result, status=0):
    print(json.dumps(result, ensure_ascii=False))
    return status

OWNER_STATUS = 3
OFF_ORIGIN = ("base_sha is not on origin's default branch: the PR would carry unpushed commits; "
              "run git fetch origin or start the flow from origin's default branch")
DECISION_STATUS = {"merge": 0, "ask": 0, "blocked": 2, "owner": OWNER_STATUS}

def base_off_origin(worktree, base):
    """True when origin's default branch is known locally and base is not part of it (local refs only)."""
    if "origin" not in git(worktree, "remote").stdout.split(): return False
    default = git(worktree, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/HEAD^{commit}", check=False)
    return default.returncode == 0 and git(worktree, "merge-base", "--is-ancestor", str(base),
                                           default.stdout.strip(), check=False).returncode != 0

def evaluate(root, args):
    """Return (decision result, exit status, flow worktree or None); shared by gate decide and gate merge."""
    try:
        record, worktree = flow_context(root, args.flow_id)
        config = load_recorded_config(record, root)
    except (RuntimeError, ValueError) as exc:
        return {"decision": "blocked", "reasons": [f"cannot load required config: {exc}"]}, 2, None

    base = record.get("base_sha")
    if git(worktree, "status", "--porcelain=v1", "-z").stdout:
        return {"decision": "blocked", "reasons": ["uncommitted changes: run af gate commit first"]}, 2, worktree
    head = f"refs/heads/af/{args.flow_id}"
    try:
        high = high_risk_paths(worktree, config.get("high_risk_paths", []), base, head)
        owner = high_risk_paths(worktree, config.get("owner_approval_paths", []), base, head)
    except (RuntimeError, ValueError) as exc:
        return {"decision": "blocked", "reasons": [f"cannot check changed paths: {exc}"]}, 2, worktree
    if args.tests != "pass" or args.reviews != "pass":
        decision, reasons = "blocked", ["tests and reviews must pass"]
    elif base_off_origin(worktree, base):
        decision, reasons = "blocked", [OFF_ORIGIN]
    elif owner:
        decision, reasons = "owner", ["payment paths need the owner's approval before merging"]
    elif high:
        decision, reasons = "ask", ["high-risk paths require owner review"]
    elif not config.get("auto_merge", True):
        decision, reasons = "ask", ["auto_merge is disabled"]
    else:
        decision, reasons = "merge", []
    return ({"decision": decision, "reasons": reasons, "high_risk_paths": high, "owner_approval_paths": owner},
            DECISION_STATUS[decision], worktree)

FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
AUTO_MERGE_MUTATION = ("mutation($id: ID!, $oid: GitObjectID!) { enablePullRequestAutoMerge(input: "
                       "{pullRequestId: $id, mergeMethod: SQUASH, expectedHeadOid: $oid}) { pullRequest { number } } }")

def run_gh(worktree, *args):
    try:
        return subprocess.run(["gh", *args], cwd=worktree, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, "", f"gh failed: {exc}")

def classify_checks(rollup):
    """Return (names of failing checks, names of pending checks)."""
    failing, pending = [], []
    for item in rollup:
        name = item.get("name") or item.get("context") or "unnamed check"
        if "state" in item:
            if item["state"] in ("FAILURE", "ERROR"): failing.append(name)
            elif item["state"] in ("PENDING", "EXPECTED"): pending.append(name)
        elif item.get("conclusion") in FAILING_CONCLUSIONS: failing.append(name)
        elif item.get("status") != "COMPLETED": pending.append(name)
    return failing, pending

def gh_json(worktree, *args):
    result = run_gh(worktree, *args)
    if result.returncode: raise RuntimeError(result.stderr.strip() or f"gh {' '.join(args[:2])} failed")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"gh {' '.join(args[:2])} returned invalid JSON: {exc}") from exc

def required_checks(worktree, base):
    """Names GitHub auto-merge waits for on the base branch (protection plus rulesets); raises when unreadable."""
    if not base: raise RuntimeError("cannot read required checks: the pull request has no base branch")
    branch = gh_json(worktree, "api", f"repos/{{owner}}/{{repo}}/branches/{base}")
    rules = gh_json(worktree, "api", f"repos/{{owner}}/{{repo}}/rules/branches/{base}")
    if not isinstance(branch, dict) or not isinstance(rules, list):
        raise RuntimeError("cannot read required checks: unexpected response shape")
    protection = (branch.get("protection") or {}).get("required_status_checks") or {}
    names = set()
    if protection.get("enforcement_level") != "off":
        names.update(protection.get("contexts") or [])
        names.update(item.get("context") for item in protection.get("checks") or [] if isinstance(item, dict))
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            names.update(item.get("context") for item in (rule.get("parameters") or {}).get("required_status_checks") or [])
    return names - {None}

def merge_flow(root, args):
    result, status, worktree = evaluate(root, args)
    result.setdefault("high_risk_paths", [])
    result.setdefault("owner_approval_paths", [])
    if status == 2 or result["decision"] == "owner" and not args.owner_approved:
        return result, status

    def blocked(reason):
        return {**result, "decision": "blocked", "reasons": [reason]}, 2

    tip = git(worktree, "rev-parse", f"refs/heads/af/{args.flow_id}").stdout.strip()
    need_pr = "push the tested tip and open the PR first"
    view = run_gh(worktree, "pr", "view", f"af/{args.flow_id}", "--json",
                  "id,number,url,state,headRefOid,baseRefName,statusCheckRollup")
    try:
        pr = json.loads(view.stdout) if view.returncode == 0 else None
    except ValueError:
        pr = None
    rollup = pr.get("statusCheckRollup") or [] if isinstance(pr, dict) else None
    if (not isinstance(rollup, list) or not all(isinstance(item, dict) for item in rollup)
            or pr.get("state") != "OPEN" or pr.get("headRefOid") != tip):
        return blocked(f"no open pull request at the tested tip {tip[:12]}: {need_pr}")
    result = {**result, "pr": pr.get("number"), "url": pr.get("url")}

    failing, pending = classify_checks(rollup)
    if failing: return blocked(f"CI is failing: {', '.join(failing)}")
    # An empty rollup with workflow files means CI may not have registered on the PR yet.
    unregistered = not rollup and bool(git(worktree, "ls-tree", "-r", "--name-only", tip, "--",
                                           ".github/workflows/").stdout.strip())
    if pending or unregistered:
        def waiting(reason):
            return {**result, "merge": "waiting_for_ci", "reasons": [reason]}, 4
        # GitHub auto-merge waits for required checks only; any other pending check could be skipped.
        try:
            required = required_checks(worktree, pr.get("baseRefName"))
        except RuntimeError as exc:
            return waiting(str(exc))
        unrequired = [name for name in pending if name not in required]
        if unrequired:
            return waiting(f"GitHub auto-merge would not wait for these non-required checks: {', '.join(unrequired)}")
        if unregistered and not required:
            return waiting("CI has not registered checks and none are required")
        enabled = run_gh(worktree, "api", "graphql", "-f", f"query={AUTO_MERGE_MUTATION}",
                         "-f", f"id={pr.get('id')}", "-f", f"oid={tip}")
        if enabled.returncode:
            return waiting(enabled.stderr.strip() or "could not enable auto-merge")
        return {**result, "merge": "auto"}, 0
    merged = run_gh(worktree, "pr", "merge", str(pr.get("number")), "--squash", "--match-head-commit", tip)
    if merged.returncode:
        return blocked(merged.stderr.strip() or "gh pr merge failed")
    # A merge queue accepts the request without merging yet; only GitHub saying MERGED counts.
    state = run_gh(worktree, "pr", "view", f"af/{args.flow_id}", "--json", "state")
    try:
        done = state.returncode == 0 and json.loads(state.stdout).get("state") == "MERGED"
    except (ValueError, AttributeError):
        done = False
    return {**result, "merge": "merged" if done else "queued"}, 0

def run_gate(args):
    root = repository_root(args.repo)
    if args.gate_action == "commit":
        load_recorded_config(read_record(state_path(root, args.flow_id)), root)
        return emit(commit_paths(root, args.flow_id, args.paths, args.message))

    if args.gate_action == "decide":
        result, status, _ = evaluate(root, args)
        return emit(result, status)
    if args.gate_action == "merge":
        return emit(*merge_flow(root, args))

    try:
        record, worktree = flow_context(root, args.flow_id)
        config = load_recorded_config(record, root)
        code, result = run_test_suite(worktree, config, args.suite)
    except (RuntimeError, ValueError) as exc:
        result = {"suite": args.suite, "passed": False, "timed_out": False, "error": str(exc)}
        code = 2
    return emit(result, code)
