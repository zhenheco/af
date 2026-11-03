"""Store and advance lock-protected flow records."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path

from config import config_digest, load_config, load_recorded_config
from worktree import common_dir, git_output, repository_root, reused_from, validate_flow_id

def flow_records_directory(repo):
    return common_dir(repo) / ".af" / "flows"

def state_path(repo, flow_id):
    validate_flow_id(flow_id)
    return flow_records_directory(repo) / f"{flow_id}.json"

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

@contextmanager
def locked(path):
    lock = path.parent.parent / "state.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield

def write_record(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)

def read_record(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("flow_id") != path.stem:
        raise RuntimeError(f"flow state id does not match record filename: {path.stem}")
    return data

def step_record(skill):
    return {"skill": skill, "status": "pending", "started": None, "result": None}

def emit(data):
    print(json.dumps(data, ensure_ascii=False))

def add_flow_args(parser):
    parser.add_argument("--repo", required=True)
    parser.add_argument("--flow-id", required=True)
    return parser

def add_state_parser(commands):
    parser = commands.add_parser("state", help="record and resume flow steps")
    actions = parser.add_subparsers(dest="state_action", required=True)
    start = add_flow_args(actions.add_parser("start"))
    start.add_argument("--base-sha", required=True)
    start.add_argument("--config")
    start.add_argument("--chain", nargs="+", required=True)

    for action in ("advance", "resume", "status", "stop"):
        add_flow_args(actions.add_parser(action))

    complete = add_flow_args(actions.add_parser("complete"))
    complete.add_argument("--result", default="completed")
    add = add_flow_args(actions.add_parser("add-steps"))
    add.add_argument("--after", required=True)
    add.add_argument("--steps", nargs="+", required=True)

def start_flow(args):
    config, config_path = load_config(args.config, cwd=args.repo)
    root = repository_root(args.repo)
    path = state_path(root, args.flow_id)
    if (root != Path(args.repo).resolve()
            or Path(git_output(root, "rev-parse", "--absolute-git-dir")).resolve() == common_dir(root)
            or git_output(root, "branch", "--show-current") != f"af/{args.flow_id}"):
        raise RuntimeError(f"state start requires the af/{args.flow_id} worktree; pass the worktree path returned by af worktree create")
    with locked(path):
        if path.exists():
            raise RuntimeError(f"flow already exists: {args.flow_id}")
        base = git_output(args.repo, "rev-parse", "--verify", f"{args.base_sha}^{{commit}}")
        timestamp = now()
        original = reused_from(root, f"af/{args.flow_id}")
        data = {"flow_id": args.flow_id, "base_sha": base, "config_path": str(config_path),
                "config_sha256": config_digest(config),
                "worktree_path": str(root), "updated_at": timestamp,
                "status": "active", "current_step": 0,
                "steps": [step_record(name) for name in args.chain]}
        if original is not None:
            data.update({"reused": True, "original_branch": original or None})
        write_record(path, data)
    emit({"flow_id": args.flow_id, "state_path": str(path), "status": "active", "base_sha": base})

def stale(step, minutes):
    started = datetime.fromisoformat(step["started"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - started >= timedelta(minutes=minutes)

def save(path, data):
    data["updated_at"] = now()
    write_record(path, data)

@contextmanager
def edit_flow(repo, flow_id):
    path = state_path(repo, flow_id)
    with locked(path):
        data = read_record(path)
        yield path, data
        save(path, data)

def current_step(data):
    index = data["current_step"]
    if data["status"] != "active" or index >= len(data["steps"]): raise RuntimeError("flow has no pending step")
    return index, data["steps"][index]

def release_if_stale(step, minutes):
    if step["status"] != "running":
        return False
    if not stale(step, minutes):
        raise RuntimeError(f"step {step['skill']} is already running")
    step.update({"status": "pending", "result": "stale runner released"})
    return True

def advance(args):
    with edit_flow(args.repo, args.flow_id) as (_, data):
        index, step = current_step(data)
        release_if_stale(step, args.stale_after_minutes)
        step.update({"status": "running", "started": now(), "result": None})
    emit({"flow_id": args.flow_id, "current_step": index, "step": step})

def resume(args):
    path = state_path(args.repo, args.flow_id)
    with locked(path):
        data = read_record(path)
        index = data["current_step"]
        if data["status"] == "active" and index < len(data["steps"]):
            step = data["steps"][index]
            if release_if_stale(step, args.stale_after_minutes):
                save(path, data)
        next_step = data["steps"][index]["skill"] if index < len(data["steps"]) else None
    emit({"flow_id": args.flow_id, "status": data["status"], "current_step": index, "next_step": next_step})

def complete(args):
    with edit_flow(args.repo, args.flow_id) as (_, data):
        index, step = current_step(data)
        if step["status"] != "running": raise RuntimeError(f"step {step['skill']} is not running")
        step.update({"status": "complete", "result": args.result})
        data["current_step"] = index + 1
        data["status"] = "complete" if data["current_step"] == len(data["steps"]) else "active"
    emit({"flow_id": args.flow_id, "status": data["status"], "step": step})

def status(args):
    emit(read_record(state_path(args.repo, args.flow_id)))

def stop(args):
    with edit_flow(args.repo, args.flow_id) as (_, data):
        if data["status"] == "complete": raise RuntimeError("completed flow cannot be stopped")
        data["status"] = "stopped"
    emit({"flow_id": args.flow_id, "status": "stopped"})

def add_steps(args):
    if any(not name.strip() for name in args.steps):
        raise ValueError("step names must not be empty")
    with edit_flow(args.repo, args.flow_id) as (_, data):
        if data["status"] != "active": raise RuntimeError(f"cannot add steps to a {data['status']} flow")
        names = [step["skill"] for step in data["steps"]]
        if args.after not in names:
            raise RuntimeError(f"step not found in flow: {args.after}")
        duplicates = set(args.steps) & set(names)
        if duplicates:
            raise RuntimeError(f"step already exists: {', '.join(sorted(duplicates))}")
        index = names.index(args.after) + 1
        last_started = max((i for i, step in enumerate(data["steps"])
                            if step["status"] != "pending"), default=-1)
        if index <= last_started:
            raise RuntimeError("steps may only be inserted after the last started flow step")
        data["steps"][index:index] = [step_record(name) for name in args.steps]
    chain = [step["skill"] for step in data["steps"]]
    emit({"flow_id": args.flow_id, "after": args.after, "added": args.steps, "chain": chain})

def flow_config(args):
    return load_recorded_config(read_record(state_path(args.repo, args.flow_id)), args.repo)

def run_state(args):
    if args.state_action in ("advance", "resume"):
        args.stale_after_minutes = flow_config(args).get("stale_after_minutes", 30)
    actions = {"start": start_flow, "advance": advance, "complete": complete,
               "stop": stop, "status": status, "resume": resume, "add-steps": add_steps}
    actions[args.state_action](args)
