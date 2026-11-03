"""Archive and delete old completed or stopped flows and their logs."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil

from config import load_config
from state import flow_records_directory
from worktree import common_dir, git, primary_checkout, remove_flow, validate_flow_id

def record_age(record, now):
    try:
        updated = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return now - updated.astimezone(timezone.utc)

def archive_location(repo, retention):
    configured = retention.get("archive_dir")
    if not configured: return None
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = common_dir(repo) / ".af" / path
    digest = hashlib.sha256(str(common_dir(repo)).encode()).hexdigest()[:12]
    name = f"{Path(repo).resolve().name}-{digest}"
    return path.resolve() / name

def cleanup_records(repo, config, keep=()):
    now = datetime.now(timezone.utc)
    retention = config.get("record_retention", {})
    archive_days = retention.get("archive_after_days", 30)
    delete_days = retention.get("delete_after_days", 180)
    flows = flow_records_directory(repo)
    logs = common_dir(repo) / ".af" / "logs"
    archive = archive_location(repo, retention)
    moved, deleted = [], []
    sources = [(path, False) for path in sorted(flows.glob("*.json"))]
    if archive and archive.is_dir():
        sources.extend((path, True) for path in sorted(archive.glob("*.json")))

    for path, archived in sources:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        try:
            validate_flow_id(path.stem)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("flow_id") != path.stem:
            continue
        age = record_age(record, now)
        if age is None or record.get("status") not in ("complete", "stopped") or path.stem in keep:
            continue

        log_path = (archive / f"{path.stem}.logs" if archived else logs / path.stem)
        if delete_days is not None and age >= timedelta(days=delete_days):
            if log_path.is_dir(): shutil.rmtree(log_path)
            path.unlink()
            deleted.append(path.stem)
            continue
        if archived or archive is None or archive_days is None or age < timedelta(days=archive_days):
            continue

        archive.mkdir(parents=True, exist_ok=True)
        destination = archive / path.name
        if destination.exists():
            raise RuntimeError(f"archive record already exists: {destination}")
        saved_logs = archive / f"{path.stem}.logs"
        if log_path.exists():
            shutil.move(str(log_path), saved_logs)
        destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        path.unlink()
        moved.append(path.stem)
    return {"moved": moved, "deleted": deleted}

def cleanup_worktrees(repo):
    removed, skipped = [], []
    for path in sorted(flow_records_directory(repo).glob("*.json")):
        try:
            record = json.loads(path.read_text())
            validate_flow_id(path.stem)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("status") != "complete":
            continue
        if record.get("flow_id") != path.stem:
            skipped.append({"flow_id": path.stem, "reason": "flow record id does not match its filename"})
            continue
        lookup = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/af/{path.stem}", check=False)
        # Exit 1 means the branch is gone because an earlier run removed it; other failures are errors.
        if lookup.returncode == 1:
            continue
        if lookup.returncode:
            skipped.append({"flow_id": path.stem, "reason": lookup.stderr.strip() or "cannot read flow branch"})
            continue
        try:
            remove_flow(repo, path.stem)
            removed.append(path.stem)
        except RuntimeError as exc:
            skipped.append({"flow_id": path.stem, "reason": str(exc)})
    return {"removed": removed, "skipped": skipped}

def add_cleanup_parser(commands):
    parser = commands.add_parser("cleanup", help="archive and expire completed flows")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config")
    parser.add_argument("--worktrees", action="store_true",
                        help="also remove worktrees and branches of completed flows proven merged")

def run_cleanup(args):
    config, _ = load_config(args.config, cwd=args.repo, require=bool(args.config))
    if args.worktrees:
        # Work from the primary checkout: --repo may be a flow worktree that this run removes.
        args.repo = str(primary_checkout(args.repo))
    worktrees = cleanup_worktrees(args.repo) if args.worktrees else None
    # Keep records of skipped flows so a later run can retry their removal.
    kept = [item["flow_id"] for item in worktrees["skipped"]] if worktrees else ()
    result = cleanup_records(args.repo, config, kept)
    if worktrees is not None:
        result["worktrees"] = worktrees
    print(json.dumps(result, ensure_ascii=False))
