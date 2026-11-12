"""Check Git, configured Matt skills, optional CLIs, and setup docs."""

import json
import os
from pathlib import Path
import shutil
import subprocess

from config import global_high_risk_patterns, load_config

REQUIRED_SKILLS = ("grill-with-docs", "to-spec", "to-tickets", "implement-spec", "implement", "tdd",
                   "code-review", "diagnosing-bugs", "pr", "retro")
OPTIONAL_TOOLS = ("pi", "codex", "claude", "gitleaks")

def skill_directories(config, repo):
    home = Path(os.environ.get("HOME", str(Path.home())))
    values = config.get("skill_dirs", [home / ".claude/skills", home / ".agents/skills", repo / "skills"])
    paths = [Path(value).expanduser() for value in values]
    return [path.resolve() if path.is_absolute() else (repo / path).resolve() for path in paths]

def check(args):
    git_available = shutil.which("git") is not None
    result = subprocess.run(["git", "-C", str(args.repo), "rev-parse", "--show-toplevel"],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) if git_available else None
    repo = Path(result.stdout.strip()).resolve() if result and result.returncode == 0 else None
    try:
        baseline = global_high_risk_patterns(os.environ)
        baseline_count = len(baseline) if baseline is not None else 0
    except RuntimeError:
        baseline_count = None
    try:
        config, config_path = load_config(args.config, cwd=repo or Path(args.repo))
        config_report = {"valid": True, "path": str(config_path), "error": None}
        effective_count = len(config.get("high_risk_paths", []))
        owner_count = len(config.get("owner_approval_paths", []))
    except (OSError, RuntimeError, ValueError) as exc:
        config = {}
        config_report = {"valid": False, "path": None, "error": str(exc)}
        effective_count = owner_count = None

    directories = skill_directories(config, repo or Path(args.repo))
    found = [name for name in REQUIRED_SKILLS
             if any((path / name / "SKILL.md").is_file() for path in directories)]
    missing = [name for name in REQUIRED_SKILLS if name not in found]
    docs = [name for name in ("docs/agents/issue-tracker.md", "docs/agents/domain.md")
            if repo and not (repo / name).is_file()]
    report = {"ok": bool(git_available and repo and config_report["valid"] and not missing),
              "git": {"available": git_available, "repo": str(repo) if repo else None},
              "skills": {"required": list(REQUIRED_SKILLS), "found": found, "missing": missing},
              "repository_setup": {"missing_files": docs,
                                   "warnings": [f"missing {', '.join(docs)}; run /setup-matt-pocock-skills once in this repository"]
                                   if docs else []},
              "optional_tools": {name: shutil.which(name) is not None for name in OPTIONAL_TOOLS},
              "high_risk": {"baseline_count": baseline_count,
                            "effective_count": effective_count,
                            "warnings": ["high_risk_paths is empty: the merge gate auto-merges every path"]
                            if effective_count == 0 else []},
              "owner_approval": {"effective_count": owner_count,
                                 "warnings": ["owner_approval_paths is empty: payment diffs merge without the owner's approval"]
                                 if owner_count == 0 else []},
              "config": config_report}
    return report

def add_doctor_parser(commands):
    parser = commands.add_parser("doctor", help="check af prerequisites")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--config")

def run_doctor(args):
    report = check(args)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1
