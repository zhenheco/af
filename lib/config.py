"""Resolve and validate the supported af-lite config fields."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess

FIELDS = {"roles", "quota_stop", "quota_retry_minutes",
          "worker_timeout_minutes", "test_timeout_minutes", "stale_after_minutes",
          "skill_dirs", "auto_merge", "high_risk_paths", "owner_approval_paths", "tests", "record_retention"}

# Global lists of gitignore-style globs that every project config inherits.
PATH_BASELINES = ("high_risk_paths", "owner_approval_paths")

def primary_checkout_root(cwd):
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.returncode: return None
    common = Path(result.stdout.strip()).resolve()
    return common.parent if common.name == ".git" else None

def validate_config_location(path):
    directory = path.parent
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    if not any((parent / ".git").exists() for parent in (directory, *directory.parents)):
        return path
    try:
        result = subprocess.run(["git", "-C", str(directory), "rev-parse", "--path-format=absolute",
                                 "--abbrev-ref", "HEAD"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError(f"cannot validate config location without git: {path}") from exc
    # Any worktree on af/*, primary included, is writer-reachable, so its config is untrusted.
    if result.returncode == 0 and result.stdout.strip().startswith("af/"):
        raise RuntimeError(f"config path is inside a worktree on af/* branch: {path}")
    return path

def global_config_path(env):
    return (Path(env.get("HOME", str(Path.home()))) / ".config/af/af.config.json").resolve()

def resolve_config_path(explicit=None, cwd=None, env=None):
    env = os.environ if env is None else env
    selected = explicit or env.get("AF_CONFIG")
    if selected:
        path = Path(selected).expanduser().resolve()
    else:
        root = primary_checkout_root(Path.cwd() if cwd is None else Path(cwd))
        local = root / "af.config.json" if root else None
        path = local.resolve() if local and local.is_file() else global_config_path(env)
    return validate_config_location(path)

def read_global_config(env):
    home = Path(env.get("HOME", str(Path.home())))
    raw = home / ".config/af/af.config.json"
    try:
        os.lstat(raw)
    except (FileNotFoundError, NotADirectoryError):
        parent = raw.parent
        while parent != home and parent != parent.parent:
            try:
                details = os.lstat(parent)
            except (FileNotFoundError, NotADirectoryError):
                parent = parent.parent
                continue
            except OSError as exc:
                raise RuntimeError(f"invalid global config {raw}: {exc}") from exc
            if stat.S_ISLNK(details.st_mode):
                try:
                    os.stat(parent)
                except (FileNotFoundError, NotADirectoryError) as exc:
                    raise RuntimeError(
                        f"invalid global config {raw}: broken symlink ancestor {parent}") from exc
                except OSError as exc:
                    raise RuntimeError(f"invalid global config {raw}: {exc}") from exc
            parent = parent.parent
        return None
    except OSError as exc:
        raise RuntimeError(f"invalid global config {raw}: {exc}") from exc

    path = raw
    try:
        path = raw.resolve()
        if not path.is_file(): raise OSError("not a readable regular file")
        config = json.loads(path.read_text())
        if not isinstance(config, dict): raise ValueError("config has invalid fields or type")
        for field in PATH_BASELINES:
            if field in config:
                require_strings(config[field], field)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"invalid global config {path}: {exc}") from exc
    return config

def global_high_risk_patterns(env):
    config = read_global_config(env)
    return None if config is None else config.get("high_risk_paths")

def load_config(explicit=None, cwd=None, env=None, require=True):
    env = os.environ if env is None else env
    path = resolve_config_path(explicit, cwd, env)
    is_global_config = path == global_config_path(env)
    if is_global_config:
        config = read_global_config(env)
        if config is None:
            if require or explicit or env.get("AF_CONFIG"):
                raise RuntimeError(f"config file not found: {path}")
            return {}, path
        validate_config(config)
        return config, path
    if not path.is_file():
        if require or explicit or env.get("AF_CONFIG"):
            raise RuntimeError(f"config file not found: {path}")
        return {}, path
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid config {path}: {exc}") from exc
    validate_config(config)
    global_config = read_global_config(env)
    if "roles" not in config and global_config is not None and "roles" in global_config:
        try:
            validate_config({"roles": global_config["roles"]})
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid global config {global_config_path(env)}: {exc}") from exc
        config["roles"] = global_config["roles"]
    for field in PATH_BASELINES:
        baseline = None if global_config is None else global_config.get(field)
        if baseline is not None:
            combined = [*baseline, *config.get(field, [])]
            # Git ignore lets the last matching pattern decide, so dedupe keeps the last occurrence.
            config[field] = list(dict.fromkeys(reversed(combined)))[::-1]
    return config, path

def config_digest(config):
    # Hash the merged result so inherited global roles and high_risk_paths are covered too.
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

def load_recorded_config(record, cwd):
    if not record.get("config_path"):
        raise RuntimeError(f"flow {record.get('flow_id')} has no recorded config path")
    config, path = load_config(record["config_path"], cwd=cwd)
    expected = record.get("config_sha256")
    # Records written before config hashing carry no digest and keep loading as before.
    if expected is not None and config_digest(config) != expected:
        raise RuntimeError(f"config changed since flow start: {path}")
    return config

def require_object(value, name, allowed=None):
    if not isinstance(value, dict) or allowed is not None and set(value) - allowed:
        raise ValueError(f"{name} has invalid fields or type")
    return value

def require_strings(value, name):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")

def require_number(value, name, minimum, integer=False):
    kind = int if integer else (int, float)
    if value is None:
        return
    if (not isinstance(value, kind) or isinstance(value, bool) or value <= minimum
            or not integer and not math.isfinite(value)):
        qualifier = "nonnegative" if minimum < 0 else "positive"
        raise ValueError(f"{name} must be a {qualifier} {'integer' if integer else 'number'}")

def validate_config(config):
    require_object(config, "config", FIELDS)
    roles = require_object(config.get("roles", {}), "roles")
    settings = {"pi": {"provider", "model"}, "codex": {"profile"}, "claude": {"model"}, "host": {"model"}}
    for role, candidates in roles.items():
        if not isinstance(role, str) or not isinstance(candidates, list):
            raise ValueError("roles must map role names to candidate arrays")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError(f"role {role} candidate must be an object")
            via = candidate.get("via")
            required = settings.get(via)
            allowed = required | {"via", "company"} if required is not None else set()
            if via in ("pi", "claude"):
                allowed |= {"tools", "env_allow"}
            if via == "claude":
                allowed |= {"base_url", "api_key_command"}
            if via == "host":
                allowed.add("agent")
            if required is None or set(candidate) - allowed:
                raise ValueError(f"role {role} candidate must use pi, codex, claude, or host settings")
            if any(not isinstance(candidate.get(key), str) or not candidate[key] for key in required):
                raise ValueError(f"role {role} candidate is missing CLI settings")
            if not isinstance(candidate.get("company"), str) or not candidate["company"].strip():
                raise ValueError(f"role {role} candidate needs a company")
            if "agent" in candidate:
                agent = candidate["agent"]
                if not isinstance(agent, str) or agent not in ("claude", "codex"):
                    raise ValueError(f"role {role} host agent must be claude or codex")
                company = "Anthropic" if agent == "claude" else "OpenAI"
                if candidate["company"].casefold() != company.casefold():
                    raise ValueError(f"role {role} host agent {agent} does not match company {candidate['company']}")
            if via in ("pi", "claude") and "tools" in candidate:
                tools = candidate["tools"]
                if (not isinstance(tools, list) or not tools
                        or any(not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_*:-]+", tool)
                               for tool in tools)):
                    raise ValueError(f"role {role} candidate tools must be a nonempty array of valid tool names")
            if via in ("pi", "claude") and "env_allow" in candidate:
                env_allow = candidate["env_allow"]
                if (not isinstance(env_allow, list)
                        or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                                for name in env_allow)):
                    raise ValueError(f"role {role} candidate env_allow must be an array of valid environment names")
            if via == "claude":
                has_base_url = "base_url" in candidate
                has_api_key_command = "api_key_command" in candidate
                if has_base_url != has_api_key_command:
                    raise ValueError(f"role {role} candidate base_url and api_key_command must appear together")
                if has_base_url:
                    base_url = candidate["base_url"]
                    if not isinstance(base_url, str) or not base_url.startswith("https://"):
                        raise ValueError(f"role {role} candidate base_url must be an https:// URL")
                    api_key_command = candidate["api_key_command"]
                    if not isinstance(api_key_command, str) or not api_key_command:
                        raise ValueError(f"role {role} candidate api_key_command must be a nonempty string")

    for field in ("skill_dirs", *PATH_BASELINES):
        if field in config:
            require_strings(config[field], field)
    if "auto_merge" in config and not isinstance(config["auto_merge"], bool):
        raise ValueError("auto_merge must be a boolean")

    for field in ("worker_timeout_minutes", "test_timeout_minutes"):
        require_number(config.get(field), field, 0)
    for field in ("stale_after_minutes", "quota_retry_minutes"):
        require_number(config.get(field), field, 0, integer=True)

    quota = require_object(config.get("quota_stop", {}), "quota_stop", {"exit_statuses", "patterns"})
    statuses = quota.get("exit_statuses", [75])
    if not isinstance(statuses, list) or any(not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 255
                                             for code in statuses):
        raise ValueError("quota_stop.exit_statuses must be integers from 0-255")
    patterns = quota.get("patterns", [])
    require_strings(patterns, "quota_stop.patterns")
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid quota pattern {pattern!r}: {exc}") from exc

    tests = require_object(config.get("tests", {}), "tests", {"focused", "full"})
    if any(not isinstance(command, str) or not command for command in tests.values()):
        raise ValueError("test commands must be nonempty strings")
    retention = require_object(config.get("record_retention", {}), "record_retention",
                               {"archive_after_days", "archive_dir", "delete_after_days"})
    for field in ("archive_after_days", "delete_after_days"):
        require_number(retention.get(field), f"record_retention.{field}", -1, integer=True)
    archive, delete = retention.get("archive_after_days", 30), retention.get("delete_after_days", 180)
    if archive is not None and delete is not None and delete < archive:
        raise ValueError("delete_after_days must not precede archive_after_days")
    if retention.get("archive_dir") is not None and not isinstance(retention["archive_dir"], str):
        raise ValueError("record_retention.archive_dir must be a path or null")
