"""Create and safely remove af flow worktrees: owned siblings or a reused linked worktree."""

import json
import re
import subprocess
from pathlib import Path

FLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode: raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result

def git_output(repo, *args, check=True):
    return git(repo, *args, check=check).stdout.strip()

def repository_root(repo):
    return Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()

def common_dir(repo):
    path = git_output(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(path).resolve()

def primary_checkout(repo):
    common = common_dir(repo)
    if common.name != ".git": raise RuntimeError("af worktrees require a non-bare Git checkout")
    return common.parent

def flow_path(repo, flow_id):
    return repo.parent / f"{repo.name}-{flow_id}"

def validate_flow_id(flow_id):
    if not FLOW_ID_RE.fullmatch(flow_id): raise ValueError("flow id must contain only letters, numbers, dot, dash, or underscore")

def add_worktree_parser(commands):
    parser = commands.add_parser("worktree", help="manage flow-owned worktrees")
    actions = parser.add_subparsers(dest="worktree_action", required=True)
    for name in ("create", "remove"):
        action = actions.add_parser(name)
        action.add_argument("--repo", required=True)
        action.add_argument("--flow-id", required=True)
        if name == "remove":
            action.add_argument("--force", action="store_true")
        else:
            action.add_argument("--reuse-current", action="store_true",
                                help="switch this clean linked worktree to af/<id> instead of adding a sibling")

REUSED_KEY = "afReusedFrom"

def reused_from(repo, branch):
    """Return the branch a reused flow came from ("" if detached), or None for sibling flows."""
    result = git(repo, "config", "--get", f"branch.{branch}.{REUSED_KEY}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None

def active_flow_at(root):
    for path in sorted((common_dir(root) / ".af" / "flows").glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(record, dict) and record.get("status") == "active"
                and Path(record.get("worktree_path", "")).resolve() == root):
            return record.get("flow_id", path.stem)
    return None

def reuse_current(repo_arg, flow_id):
    root = repository_root(repo_arg)
    branch = f"af/{flow_id}"
    if root != Path(repo_arg).resolve() or Path(git_output(root, "rev-parse", "--absolute-git-dir")).resolve() == common_dir(root):
        raise RuntimeError("--reuse-current requires the top level of a linked worktree, not the primary checkout")
    if git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout:
        raise RuntimeError("--reuse-current requires a clean worktree; commit or move other changes first")
    original = git_output(root, "branch", "--show-current")
    holder = active_flow_at(root)
    if original.startswith("af/") or holder:
        raise RuntimeError(f"worktree is held by active flow {holder or original[3:]}; "
                           "create a sibling worktree for parallel writers")
    if git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise RuntimeError(f"branch already exists: {branch}")
    base_sha = git_output(root, "rev-parse", "HEAD")
    # Session branches rarely track anything mergeable, so prefer origin's default branch as the merge base.
    upstream = git_output(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if not upstream or git(root, "show-ref", "--verify", "--quiet", upstream, check=False).returncode != 0:
        upstream = git_output(root, "rev-parse", "--symbolic-full-name", f"{original}@{{upstream}}",
                              check=False) if original else ""
    git(root, "switch", "-q", "-c", branch)
    git(root, "config", f"branch.{branch}.{REUSED_KEY}", original)
    if upstream:
        git(root, "branch", "--set-upstream-to", upstream, branch)
    print(json.dumps({"flow_id": flow_id, "branch": branch, "path": str(root), "base_sha": base_sha,
                      "reused": True, "original_branch": original or None}))

def create(repo_arg, flow_id, reuse=False):
    validate_flow_id(flow_id)
    if reuse:
        return reuse_current(repo_arg, flow_id)
    repo = primary_checkout(repo_arg)
    branch = f"af/{flow_id}"
    target = flow_path(repo, flow_id)
    if target.exists(): raise RuntimeError(f"refusing to overwrite existing path: {target}")
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise RuntimeError(f"branch already exists: {branch}")

    base_ref = git_output(repo, "symbolic-ref", "--quiet", "HEAD", check=False)
    base_sha = git_output(repo, "rev-parse", "HEAD")
    git(repo, "worktree", "add", "-b", branch, str(target), base_sha)

    if base_ref:
        git(repo, "branch", "--set-upstream-to", base_ref, branch)
    print(json.dumps({"flow_id": flow_id, "branch": branch, "path": str(target), "base_sha": base_sha}))

def merged_pull_request(target, branch, tip):
    """True only when GitHub reports this branch's PR merged at exactly the given tip."""
    try:
        result = subprocess.run(["gh", "pr", "view", branch, "--json", "state,headRefOid"], cwd=target,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        report = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return (isinstance(report, dict) and report.get("state") == "MERGED"
            and report.get("headRefOid") == tip)

def merged_branch_pull_request(repo, branch):
    """True only when GitHub reports a MERGED pull request whose head branch is exactly this one."""
    try:
        result = subprocess.run(["gh", "pr", "view", branch, "--json", "state,headRefName"], cwd=repo,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        report = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return (isinstance(report, dict) and report.get("state") == "MERGED"
            and report.get("headRefName") == branch)

def merge_proof(repo, target, branch, tip):
    """Return how the tip is proven merged: "ancestor", "pull-request", or None."""
    # A pushed branch (push -u) tracks a branch of its own name on some remote (or "."); that copy proves
    # nothing about being merged. Read the tracking config: the tracking ref's name depends on fetch refspecs.
    # @{upstream} uses the first value, --get the last: judge every value, and trust none when there are several.
    merges = git(repo, "config", "--get-all", f"branch.{branch}.merge", check=False).stdout.split()
    own_copy = len(merges) > 1 or f"refs/heads/{branch}" in merges
    base = git(repo, "rev-parse", "--verify", f"{branch}@{{upstream}}", check=False)
    if (not own_copy and base.returncode == 0
            and git(repo, "merge-base", "--is-ancestor", tip,
                    base.stdout.strip(), check=False).returncode == 0):
        return "ancestor"
    return "pull-request" if merged_pull_request(target, branch, tip) else None

def delete_remote_branch(repo, branch, tip):
    """Delete origin's copy of a merged flow branch (repos without delete_branch_on_merge); never raises."""
    try:
        if "origin" not in git(repo, "remote").stdout.split(): return "no remote"
        ref = f"refs/heads/{branch}"
        listed = git(repo, "ls-remote", "--heads", "origin", ref, check=False)
        if listed.returncode: return f"kept: {listed.stderr.strip() or 'cannot list origin'}"
        remote = next((line.split("\t")[0] for line in listed.stdout.splitlines() if line.endswith(f"\t{ref}")), None)
        if remote is None: return "absent"
        if remote != tip: return f"kept: origin has {branch} at {remote[:12]}, not the merged tip {tip[:12]}"
        # The lease turns a push that lands after the check above into a rejection, not a lost commit.
        deleted = git(repo, "push", f"--force-with-lease={ref}:{tip}", "origin", f":{ref}", check=False)
        return "deleted" if deleted.returncode == 0 else f"kept: {deleted.stderr.strip() or 'push failed'}"
    except (OSError, RuntimeError) as exc:
        return f"kept: {exc}"

def remove_flow(repo_arg, flow_id, force=False, expected_tip=None):
    """expected_tip binds the removal to a tip already proven by a MERGED pull request; ancestry is not accepted then."""
    validate_flow_id(flow_id)
    repo = primary_checkout(repo_arg)
    branch = f"af/{flow_id}"
    original = reused_from(repo, branch)
    record_path = common_dir(repo) / ".af" / "flows" / f"{flow_id}.json"
    if record_path.exists() and bool(json.loads(record_path.read_text()).get("reused")) != (original is not None):
        # Guessing wrong would run git worktree remove on a session-owned worktree.
        raise RuntimeError(f"flow record and branch config disagree on whether {branch} reused its worktree; "
                           "resolve it by hand")
    output = git_output(repo, "worktree", "list", "--porcelain")
    entries = [dict(line.partition(" ")[::2] for line in block.splitlines() if line)
               for block in output.split("\n\n")]
    if original is None:
        target = flow_path(repo, flow_id)
        entry = next((item for item in entries if Path(item.get("worktree", "")).resolve() == target.resolve()), None)
    else:
        entry = next((item for item in entries if item.get("branch") == f"refs/heads/{branch}"), None)
        target = Path(entry["worktree"]) if entry else None
    if not entry or entry.get("branch") != f"refs/heads/{branch}":
        raise RuntimeError(f"flow worktree not found on {branch}: {target}")

    dirty = git(target, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    if dirty and (not force or original is not None):
        # A reused worktree belongs to the session, so even --force never discards its changes.
        raise RuntimeError("refusing to remove a flow worktree with tracked changes or untracked non-ignored files; review it"
                           + ("" if original is not None else " or use --force"))
    # Read the tip once: the proof, the local delete and the remote lease are all bound to this SHA.
    tip = git_output(repo, "rev-parse", f"refs/heads/{branch}")
    if expected_tip is not None and tip != expected_tip:
        raise RuntimeError(f"{branch} moved after its merge was proven; review it")
    if expected_tip is not None:
        proof = "pull-request" if merged_pull_request(target, branch, tip) else None
    else:
        proof = None if force else merge_proof(repo, target, branch, tip)
    if not force and not proof:
        raise RuntimeError("flow branch is not merged into its base and no merged pull request has the same head; "
                           "use --force after reviewing it")

    if original is None:
        git(repo, "worktree", "remove", *(["--force"] if force else []), str(target))
    else:
        git(target, "switch", "-q", *([original] if original else ["--detach", tip]))
    # Compare-and-delete: fails if the ref moved, so a tip advanced after the proof is never lost.
    if git(repo, "update-ref", "-d", f"refs/heads/{branch}", tip, check=False).returncode:
        raise RuntimeError(f"{branch} moved after its merge was proven; review it")
    git(repo, "config", "--remove-section", f"branch.{branch}", check=False)
    # Only a removal backed by a merge proof may touch the remote.
    remote = "kept: --force removals never touch the remote" if force else delete_remote_branch(repo, branch, tip)
    return {"flow_id": flow_id, "removed": True, "branch": branch, "path": str(target),
            "reused": original is not None, "remote_branch": remote}

def remove(repo_arg, flow_id, force=False):
    print(json.dumps(remove_flow(repo_arg, flow_id, force)))

def run_worktree(args):
    if args.worktree_action == "create":
        create(args.repo, args.flow_id, args.reuse_current)
    else:
        remove(args.repo, args.flow_id, args.force)
