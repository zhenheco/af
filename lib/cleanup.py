"""Archive and delete old completed or stopped flows and their logs."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil

from config import load_config
from state import edit_flow, flow_records_directory, now
from worktree import (common_dir, flow_path, git, git_output, merged_branch_pull_request, merged_pull_request,
                      primary_checkout, remove_flow, reused_from, validate_flow_id)

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

def flow_worktree(repo, flow_id):
    """The worktree remove_flow would act on for this flow, or None when none is registered on its branch."""
    branch = f"af/{flow_id}"
    original = reused_from(repo, branch)
    entries = [dict(line.partition(" ")[::2] for line in block.splitlines() if line)
               for block in git_output(repo, "worktree", "list", "--porcelain").split("\n\n")]
    if original is None:
        target = flow_path(Path(repo), flow_id)
        entry = next((item for item in entries if Path(item.get("worktree", "")).resolve() == target.resolve()), None)
    else:
        entry = next((item for item in entries if item.get("branch") == f"refs/heads/{branch}"), None)
        target = Path(entry["worktree"]) if entry else None
    return target if entry and entry.get("branch") == f"refs/heads/{branch}" else None

def worktree_listed(repo, flow_id):
    """True when any registered worktree is the flow's sibling path or sits on its branch."""
    target = flow_path(Path(repo), flow_id).resolve()
    branch = f"refs/heads/af/{flow_id}"
    for block in git_output(repo, "worktree", "list", "--porcelain").split("\n\n"):
        entry = dict(line.partition(" ")[::2] for line in block.splitlines() if line)
        if Path(entry.get("worktree", "")).resolve() == target or entry.get("branch") == branch:
            return True
    return False

def close_active_flow(repo, flow_id, tip=None):
    """Mark a flow proven merged as complete; False when the record or branch changed meanwhile.

    Closing is only a record edit: nothing is deleted here, removal stays with the completed-flow path."""
    with edit_flow(repo, flow_id) as (_, record):
        if record.get("status") != "active": return False
        # Holding the lock, look once more: the branch must still be at the proven tip (or still absent).
        found = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/af/{flow_id}", check=False)
        if tip is None and found.returncode != 1: return False
        if tip is not None and found.stdout.strip() != tip: return False
        record.update({"status": "complete", "closed_by_cleanup_at": now()})
    return True

def cleanup_worktrees(repo):
    removed, skipped, closed = [], [], []
    for path in sorted(flow_records_directory(repo).glob("*.json")):
        try:
            record = json.loads(path.read_text())
            validate_flow_id(path.stem)
        except (OSError, ValueError):
            continue
        # Active flows are closed only on a merge proof; stopped flows keep their old handling.
        status = record.get("status") if isinstance(record, dict) else None
        if status not in ("complete", "active"):
            continue
        if record.get("flow_id") != path.stem:
            skipped.append({"flow_id": path.stem, "reason": "flow record id does not match its filename"})
            continue
        lookup = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/af/{path.stem}", check=False)
        # Exit 1 means the branch is gone because an earlier run removed it; other failures are errors.
        if lookup.returncode == 1:
            if status == "active" and merged_branch_pull_request(repo, f"af/{path.stem}"):
                if worktree_listed(repo, path.stem):
                    skipped.append({"flow_id": path.stem,
                                    "reason": "merged, but its worktree still exists; review it"})
                elif close_active_flow(repo, path.stem):
                    closed.append(path.stem)
            continue
        if lookup.returncode:
            if status == "complete":
                skipped.append({"flow_id": path.stem, "reason": lookup.stderr.strip() or "cannot read flow branch"})
            continue
        if status == "active":
            # Ancestry proves nothing for a live flow (a fresh branch sits at its base): require a MERGED PR at the tip.
            tip = git(repo, "rev-parse", f"refs/heads/af/{path.stem}", check=False).stdout.strip()
            if not tip or not merged_pull_request(repo, f"af/{path.stem}", tip):
                continue
            # Same worktree and clean rule as remove_flow; checked after the proof so a change made meanwhile is seen.
            target = flow_worktree(repo, path.stem)
            if target is None:
                continue
            if git(target, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout:
                skipped.append({"flow_id": path.stem, "reason": "merged, but its worktree has changes; review it"})
                continue
            if git(target, "rev-parse", "HEAD", check=False).stdout.strip() != tip:
                skipped.append({"flow_id": path.stem, "reason": "merged, but its worktree HEAD differs from the merged tip; review it"})
                continue
            if not close_active_flow(repo, path.stem, tip):
                continue
            closed.append(path.stem)
        try:
            remove_flow(repo, path.stem)
        except RuntimeError as exc:
            # A merged flow that cannot be removed (for example a dirty worktree) stays as it is, reported to the owner.
            skipped.append({"flow_id": path.stem, "reason": str(exc)})
            continue
        removed.append(path.stem)
    return {"removed": removed, "skipped": skipped, "closed": closed}

def add_cleanup_parser(commands):
    parser = commands.add_parser("cleanup", help="archive and expire completed flows")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config")
    parser.add_argument("--worktrees", action="store_true",
                        help="also remove worktrees and branches of completed flows proven merged; an active flow with a MERGED pull request and a clean worktree is closed first, then removed the same way")

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
