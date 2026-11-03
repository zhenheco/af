"""Select configured workers in order and enforce reviewer independence."""

import json
import os
import shutil

from config import load_config, load_recorded_config
from state import read_record, state_path

REVIEW_ROLES = {"small_review", "big_review"}

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
