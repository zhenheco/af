"""Run workers with logged output and quota-only safe failover."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess

from config import load_recorded_config
from selection import REVIEW_ROLES, choose
from state import read_record, state_path
from worktree import common_dir, git, validate_flow_id

WRITER_ROLE = ("ROLE: You are the WRITER, started non-interactively for this worktree. Implement directly here; "
               "do not dispatch another writer; do not ask the user; leave changes uncommitted; never push.")
REVIEWER_ROLE = ("ROLE: You are the REVIEWER, started non-interactively for this worktree. Review the requested "
                 "changes only; do not edit files; do not commit; do not push; do not ask the user.")
BUILTIN_QUOTA_PATTERNS = ("usage limit", "quota will reset", "rate limit exceeded",
                          "429 too many requests", "insufficient_quota", "quota exceeded")

def validate_worktree(path, flow_id):
    root = Path(git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != Path(path).resolve(): raise RuntimeError("dispatch requires the worktree root")
    validate_flow_id(flow_id)
    branch = git(path, "branch", "--show-current").stdout.strip()
    if branch != f"af/{flow_id}":
        raise RuntimeError(f"dispatch requires flow worktree branch af/{flow_id}")
    return root

def worker_prompt(role, config, prompt):
    preamble = REVIEWER_ROLE if role in REVIEW_ROLES else WRITER_ROLE
    return f"{preamble}\n\n{prompt}"

def create_log_paths(root, flow_id, role, attempt):
    folder = common_dir(root) / ".af" / "logs" / flow_id
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", role).strip(".-") or "worker"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    stem = f"{name}-{attempt}-{stamp}"
    return {"stdout": str(folder / f"{stem}.out.log"),
            "stderr": str(folder / f"{stem}.err.log")}

def resolve_api_key(candidate, worktree, environment, paths):
    try:
        result = subprocess.run(candidate["api_key_command"], shell=True, cwd=worktree,
                                capture_output=True, text=True, env=environment, timeout=60)
        status, key = result.returncode, result.stdout.strip() if result.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        status, key = "timeout", ""
    if key:
        return key, None
    text = f"api_key_command failed (exit {status})\n"
    Path(paths["stdout"]).touch()
    Path(paths["stderr"]).write_text(text)
    return None, (1, text, False)

def invoke(candidate, worktree, prompt, timeout, paths):
    environment = None
    if candidate["via"] in ("pi", "claude") and "env_allow" in candidate:
        allowed_environment = {
            "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
            *candidate["env_allow"],
        }
        environment = {name: value for name, value in os.environ.items()
                       if name in allowed_environment}
    if candidate["via"] == "pi":
        command = ["pi", "--print", "--provider", candidate["provider"], "--model", candidate["model"]]
        if "tools" in candidate:
            command.extend(["--tools", ",".join(candidate["tools"]), "--no-mcp"])
    elif candidate["via"] == "claude":
        command = ["claude", "-p", "--model", candidate["model"]]
        if "base_url" in candidate:
            key, failure = resolve_api_key(candidate, worktree, environment, paths)
            if failure:
                return failure
            command.insert(1, "--bare")
            environment = dict(environment) if environment is not None else dict(os.environ)
            environment["ANTHROPIC_BASE_URL"] = candidate["base_url"]
            environment["ANTHROPIC_API_KEY"] = key
            environment.pop("ANTHROPIC_AUTH_TOKEN", None)
        if "tools" in candidate:
            command.extend(["--tools", ",".join(candidate["tools"]),
                            "--permission-mode", "acceptEdits", "--strict-mcp-config"])
    else:
        command = ["codex", "exec", "-p", candidate["profile"], "-s", "workspace-write",
                   "-C", str(worktree), "-"]

    with Path(paths["stdout"]).open("w") as stdout, Path(paths["stderr"]).open("w") as stderr:
        process = subprocess.Popen(command, cwd=worktree, stdin=subprocess.PIPE, text=True,
                                   stdout=stdout, stderr=stderr, start_new_session=True,
                                   env=environment)
        timed_out = False
        try:
            process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            signal_group(process.pid, signal.SIGKILL)
        process.communicate()

    stderr_text = Path(paths["stderr"]).read_text(errors="replace")
    return (124 if timed_out else process.returncode), stderr_text, timed_out

def signal_group(process_id, signal_number):
    try:
        os.killpg(process_id, signal_number)
    except ProcessLookupError:
        pass

def quota_stop(exit_status, stderr, config, prompt):
    settings = config.get("quota_stop", {})
    if exit_status in settings.get("exit_statuses", [75]):
        return True
    stderr = stderr.replace(prompt, "")
    lines = [line for line in stderr.splitlines() if line.strip()]
    builtin_stderr = "\n".join(lines[-20:])
    builtins_match = any(re.search(pattern, builtin_stderr, re.IGNORECASE)
                         for pattern in BUILTIN_QUOTA_PATTERNS)
    return builtins_match or any(re.search(pattern, stderr, re.IGNORECASE)
                                 for pattern in settings.get("patterns", []))

def head_and_status(worktree):
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    status = git(worktree, "status", "--porcelain=v1", "-z", "--ignored").stdout
    return head, status

def emit(output, status):
    print(json.dumps(output, ensure_ascii=False))
    return status

def normalize_worker_commits(worktree, start, current):
    if start == current: return []
    commits = git(worktree, "rev-list", "--reverse", f"{start}..{current}").stdout.splitlines()
    git(worktree, "reset", "--soft", start)
    return commits or [current]

def add_dispatch_parser(commands):
    parser = commands.add_parser("dispatch", help="run one configured worker")
    parser.add_argument("--worktree")
    parser.add_argument("--repo")
    parser.add_argument("--config", help=argparse.SUPPRESS)
    parser.add_argument("--flow-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--writer-company")

def run_dispatch(args):
    if args.config is not None:
        raise RuntimeError("dispatch does not accept --config; it uses the config recorded by `af state start` for this flow")
    if args.worktree is not None and args.repo is not None:
        if Path(args.worktree).resolve() != Path(args.repo).resolve():
            raise RuntimeError(f"dispatch got both --worktree and --repo with different values ({args.worktree} vs {args.repo}); pass only one")
        worktree_arg = args.worktree
    elif args.worktree is not None:
        worktree_arg = args.worktree
    elif args.repo is not None:
        worktree_arg = args.repo
    else:
        raise RuntimeError("dispatch requires --worktree (or its alias --repo)")

    worktree = validate_worktree(worktree_arg, args.flow_id)
    record = read_record(state_path(worktree, args.flow_id))
    config = load_recorded_config(record, worktree)

    selection = choose(args.role, config, args.writer_company)
    if not selection["selected"]:
        return emit({"status": "error", "attempts": [], **selection}, 2)

    prompt = worker_prompt(args.role, config, args.prompt)
    candidates = list(selection["candidates"])
    attempts = []
    normalized = []
    timeout = float(config.get("worker_timeout_minutes", 60)) * 60

    while candidates:
        candidate = candidates.pop(0)
        if candidate["via"] == "host":
            return emit({"via": "host", "role": args.role, "model": candidate["model"],
                         "company": candidate["company"], "worktree": str(worktree),
                         "prompt": prompt,
                         "instructions": "start a fresh subagent with this model in this worktree and give it the prompt",
                         "attempts": attempts}, 0)
        start, before = head_and_status(worktree)
        paths = create_log_paths(worktree, args.flow_id, args.role, len(attempts) + 1)
        exit_status, stderr, timed_out = invoke(candidate, worktree, prompt, timeout, paths)
        current, after = head_and_status(worktree)
        new_commits = normalize_worker_commits(worktree, start, current)
        normalized.extend(new_commits)
        changed = current != start or after != before
        attempt = {"via": candidate["via"], "company": candidate["company"],
                   "exit_status": exit_status, "timed_out": timed_out,
                   "log_paths": paths}
        attempts.append(attempt)

        output = {"role": args.role, "attempts": attempts, "review": selection["review"],
                  "worker_commits_normalized": normalized}
        if timed_out:
            output.update({"timed_out": True, "worktree_changed": changed})
            return emit(output, 124)
        if exit_status == 0: return emit(output, 0)
        if not quota_stop(exit_status, stderr, config, prompt):
            return emit(output, exit_status or 1)
        if changed:
            output.update({"status": "blocked",
                           "message": "quota stop followed by worktree changes; refusing failover",
                           "worktree_changed": True})
            return emit(output, 76)

    return emit({"status": "waiting_for_quota", "role": args.role,
                 "retry_after_minutes": config.get("quota_retry_minutes", 15),
                 "attempts": attempts, "review": selection["review"],
                 "worker_commits_normalized": normalized}, 75)
