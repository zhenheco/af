"""Select configured workers in order and enforce reviewer independence."""

import json
import os
import shutil
import urllib.request

from config import load_config, load_recorded_config
from state import read_record, state_path

REVIEW_ROLES = {"small_review", "big_review"}

DEFAULT_QUOTA_URL = "http://127.0.0.1:3425/v1/magpie/quotas"

def fetch_quotas():
    url = os.environ.get("AF_QUOTA_URL") or DEFAULT_QUOTA_URL
    with urllib.request.urlopen(url, timeout=2) as response:
        data = json.load(response)["data"]
    if not isinstance(data, list): raise ValueError("quotas data is not a list")
    return data

def over_ceiling(candidate, quotas, ceiling):
    """Return (window name, used) of the first window at or over ceiling, else None; None if provider absent."""
    entries = [e for e in quotas if isinstance(e, dict) and e.get("provider") == candidate["quota_provider"]]
    if not entries: return "missing"
    wanted = candidate.get("quota_windows")
    for entry in entries:
        for window in entry.get("windows") or []:
            if not isinstance(window, dict) or window.get("unlimited"): continue
            used = window.get("used")
            if wanted is not None and window.get("name") not in wanted: continue
            if isinstance(used, (int, float)) and not isinstance(used, bool) and used >= ceiling:
                return window.get("name"), used
    return None

def apply_quota_ceiling(result, ceiling):
    checked = [c for c in result["candidates"] if c.get("quota_provider")]
    if not checked: return
    try:
        quotas = fetch_quotas()
    except Exception as exc:  # fail open: a broken usage feed must not block dispatch
        result["quota_check"] = f"quota fetch failed, ceiling not enforced: {exc}"
        return
    skipped, missing = [], []
    for candidate in checked:
        verdict = over_ceiling(candidate, quotas, ceiling)
        if verdict == "missing": missing.append(candidate["quota_provider"])
        elif verdict: skipped.append({"candidate": candidate, "window": verdict[0], "used": verdict[1]})
    if missing:
        result["quota_check"] = f"no quota data for provider(s) {sorted(set(missing))}; not skipped"
    if not skipped: return
    dropped = [item["candidate"] for item in skipped]
    result["quota_skipped"] = skipped
    result["candidates"] = [c for c in result["candidates"] if not any(c is d for d in dropped)]
    result["selected"] = result["candidates"][0] if result["candidates"] else None
    if not result["selected"]:
        result["quota_ceiling_reached"] = True
        result["error"] = f"all candidates are at or over quota_ceiling_percent ({ceiling})"

def choose(role, config, writer_company=None):
    roles = config.get("roles", {})
    if role not in roles: raise RuntimeError(f"role not configured: {role}")
    configured = roles[role]
    reviewer = role in REVIEW_ROLES
    independent = []
    if reviewer:
        if not writer_company: raise RuntimeError("reviewer selection requires --writer-company")
        writer = writer_company.strip().casefold()
        if not writer or writer == "unknown":
            raise RuntimeError("writer company must be known for reviewer selection")
        independent = [item for item in configured
                       if item["company"].strip().casefold() not in ("unknown", writer)]
        if not independent and not (configured and all(
                item["company"].strip().casefold() == writer for item in configured)):
            raise RuntimeError(f"reviewer independence unavailable for role {role}: no known distinct company")
    candidates = independent if reviewer and independent else configured
    no_host = os.environ.get("AF_NO_HOST") == "1"
    available = [item for item in candidates if
                 (item["via"] != "host" or not no_host) and
                 (item["via"] == "host" or shutil.which(item["via"]))]
    result = {"role": role, "candidates": available,
              "selected": available[0] if available else None,
              "review": "independent" if independent else "same-company" if reviewer else None}
    if not available: result["error"] = "no configured worker CLI is available"
    elif config.get("quota_ceiling_percent") is not None:
        apply_quota_ceiling(result, config["quota_ceiling_percent"])
    return result

def add_select_parser(commands):
    parser = commands.add_parser("select", help="select ordered usable workers")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--flow-id")
    parser.add_argument("--role", required=True)
    parser.add_argument("--config")
    parser.add_argument("--writer-company")

def run_select(args):
    if args.flow_id:
        config = load_recorded_config(read_record(state_path(args.repo, args.flow_id)), args.repo)
    else:
        config, _ = load_config(args.config, cwd=args.repo)
    result = choose(args.role, config, args.writer_company)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["selected"] else 2
