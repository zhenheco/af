# af-lite

af-lite chains [Matt Pocock's skills](https://github.com/mattpocock/skills) in a sibling Git worktree. It is POSIX bash plus Python 3 standard library; the upstream skills and worker CLIs remain separately installed.

## Design

Workers are trusted but fallible: they are the owner's coding agents working on the owner's repositories. af-lite protects against mistakes: working outside the flow worktree, committing unintended files, committing secrets, same-company review, merging untested or high-risk changes, and losing work during failover. Deliberately malicious-worker defenses such as hook injection, symlink escapes, FIFO/socket traps, and crafted encodings are out of scope; worker CLI sandboxes and human review cover that threat.

The flow is explicit: create a worktree, record and advance skill steps, select and dispatch an available worker when useful, run configured tests, commit explicit paths through the gate, review, and request a merge decision. Workers receive a fixed role preamble and the task on stdin. A worker commit is soft-reset to its starting commit; quota failover is allowed only when both HEAD and `git status --porcelain=v1 -z --ignored` are unchanged. Worker output is logged under `<git-common-dir>/.af/logs/<flow-id>/`.

## Install and run

Follow [INSTALL.md](INSTALL.md), copy [af.config.example.json](af.config.example.json) to the primary checkout's `af.config.json`, then configure candidate order, tests, skills, and risk paths.

```sh
af doctor --repo /path/to/repository
af worktree create --repo /path/to/repository --flow-id example-001
af state start --repo /path/to/repository-example-001 --flow-id example-001 --base-sha <base-sha> --chain tdd code-review pr retro
```

Use the worktree path and `base_sha` returned by creation. Config resolves in this order: `--config`, `$AF_CONFIG`, `<primary checkout>/af.config.json`, `~/.config/af/af.config.json`. Any resolved config path inside a linked worktree on an `af/*` branch is refused. `state start` records the resolved config path; flow commands use it.

Advance one step before running it. Run Matt skills in the host as appropriate; dispatch can use installed `pi`, `codex`, or `claude` CLIs or return a `host` subagent handoff. Run focused tests and host commit explicit paths before review. Use `state add-steps` to insert ordered ticket steps. Run full tests and reviews before `gate decide`; only a `merge` result allows the project's normal PR process.

```sh
af state advance --repo /path/to/repository-example-001 --flow-id example-001
af gate test --repo /path/to/repository-example-001 --flow-id example-001 --suite focused
af gate commit --repo /path/to/repository-example-001 --flow-id example-001 --message "implement focused change" --paths src/file.py tests/test_file.py
af gate decide --repo /path/to/repository-example-001 --flow-id example-001 --tests pass --reviews pass
```

Configured worker candidates are tried in order; missing `pi`, `codex`, and `claude` CLIs are skipped, and `host` candidates are always available. Review roles require a different company whenever a configured candidate from another company exists. Same-company review is allowed only when every configured candidate for that role has the writer's company. If independent reviewers all stop for quota, dispatch returns `waiting_for_quota` with exit 75. Dispatch never fails over after a worker changes HEAD or the worktree status, including ignored files. `AF_NO_HOST=1` makes `host` candidates unavailable so a non-Claude host, or a host without quota for the configured host model, falls through to the next `pi`/`codex`/`claude` candidate.

`dispatch` takes `--worktree`, with `--repo` accepted as an alias for consistency with the other commands; passing both with different values is an error. It always uses the config recorded by `state start` and rejects `--config`.

`gate commit` requires branch `af/<flow-id>`, accepts explicit changed paths only, rejects absolute paths, `..` path components, absolute symlink targets, and symlinks that resolve outside the worktree, then scans staged changes with `gitleaks protect --staged` or a secret-pattern scan of added lines. Relative symlink targets may contain `..` when they resolve inside the worktree. It restores the prior index if a check refuses the commit. `gate decide` requires passing tests and reviews, a clean worktree, and no high-risk paths; otherwise it blocks or returns `ask`. High-risk matches use Git ignore patterns and check committed changes from the recorded base through the flow branch.

Worktree removal refuses tracked changes, untracked non-ignored files, and branches not merged into their recorded base; `--force` overrides both checks. Cleanup archives or deletes only completed or stopped flow records and their logs after the configured ages.

### Scheduled cleanup

Schedulers do not start in the repository, so pass `--repo` and `--config` explicitly. For cron:

```cron
17 3 * * * /path/to/af-lite/bin/af cleanup --repo /path/to/repository --config /path/to/repository/af.config.json
```

For launchd, add these keys to a LaunchAgent plist:

```xml
<key>ProgramArguments</key>
<array>
  <string>/path/to/af-lite/bin/af</string>
  <string>cleanup</string>
  <string>--repo</string>
  <string>/path/to/repository</string>
  <string>--config</string>
  <string>/path/to/repository/af.config.json</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>17</integer></dict>
```

## Configuration

Unknown config fields are rejected. These are exactly the supported fields:

| Field | Meaning |
|---|---|
| `roles` | Ordered candidates for each role. Each candidate uses `via` (`pi`, `codex`, `claude`, or `host`) and `company`; Pi uses `provider` plus `model`, Codex uses `profile`, Claude Code CLI uses `model`, and host uses `model`. Pi also accepts optional `tools` and `env_allow` candidate settings; Claude accepts the same, with `tools` passed as `--tools ... --permission-mode acceptEdits --strict-mcp-config`. Claude also accepts `base_url` plus `api_key_command` (both required together, `https://` only), which point the Claude CLI at another vendor's Anthropic-compatible endpoint: af then starts `claude --bare` (which skips user and project settings and hooks), runs the command for the key, passes it as `ANTHROPIC_API_KEY` with `ANTHROPIC_BASE_URL`, and removes `ANTHROPIC_AUTH_TOKEN`; a failed or empty key command fails the attempt without starting the CLI. |
| `quota_stop.exit_statuses` | Worker exit statuses treated as quota stops; default: `[75]`. |
| `quota_stop.patterns` | Configured case-insensitive regexes match all worker stderr after removing the prompt; none are enabled by default. Built-in patterns always match only the last 20 non-empty stderr lines after prompt removal (`usage limit`, `quota will reset`, `rate limit exceeded`, `429 too many requests`, `insufficient_quota`, `quota exceeded`). |
| `quota_retry_minutes` | Delay to report when all candidates stop for quota; default: `15`. |
| `worker_timeout_minutes` | Worker timeout; default: `60`. |
| `test_timeout_minutes` | Test command timeout; default: `30`. |
| `stale_after_minutes` | Age after which a running step may be resumed; default: `30`. |
| `skill_dirs` | Directories containing Matt skill folders; default: `~/.claude/skills`, `~/.agents/skills`, and `<repo>/skills`. |
| `auto_merge` | Whether an otherwise clear gate decision may return `merge`; default: `true`. |
| `high_risk_paths` | Effective Git ignore patterns: the optional global baseline followed by this project's entries, checked against committed paths from base to flow branch. |
| `tests.focused`, `tests.full` | Shell command strings run in the flow worktree. |
| `record_retention.archive_after_days` | Age before eligible records are archived; default: `30`; `null` disables archival. |
| `record_retention.archive_dir` | Archive root; relative paths resolve under `<git-common-dir>/.af/`; `null` disables archival. |
| `record_retention.delete_after_days` | Age before eligible records and logs are deleted; default: `180`; `null` disables deletion. |

### Global role defaults

When a project config omits `roles`, af-lite inherits the role map from `$HOME/.config/af/af.config.json`. If the project config contains `roles`, its entire map is used, including an empty map; role entries are never merged individually. Other global fields are not inherited. If the selected config has no requested role, role-using commands such as `af select` fail with `role not configured: <role>`. An invalid global role map used as the fallback fails closed as an invalid global config.

### Global high-risk baseline

The optional baseline lives at `$HOME/.config/af/af.config.json` under `high_risk_paths`; copy the [reference baseline](docs/high-risk-baseline.example.json). For this field, if the file or field is absent, project high-risk behavior is unchanged. Global patterns come first and project patterns follow, with duplicates kept at their last occurrence; Git ignore rules let the last matching pattern decide, so dropping earlier duplicates never changes which paths match. A later project `!` pattern can cancel a file or basename match. Git tests confirm `!scripts/ci-local.sh` cancels `scripts/ci*`, but it cannot re-include that file when the parent directory pattern `scripts/` is excluded.

An invalid global JSON file, a non-string-array `high_risk_paths`, a broken symlink (including a symlink loop), a directory at the config path, an unreadable config file, or an unreadable or broken-symlink ancestor directory between `$HOME` and the config file makes `load_config` fail closed; symlinks resolving to valid files are supported. `gate decide` returns `blocked` and exits 2. `af doctor` reports `high_risk.baseline_count`, `effective_count`, and `warnings`; each count is `null` when the corresponding config cannot be loaded. Only a successfully loaded but empty effective list adds `high_risk_paths is empty: the merge gate auto-merges every path`; this warning does not change `ok` or the exit code.

On Pi candidates, `tools` is a non-empty list passed to Pi with MCP disabled. Without a shell tool, the worker cannot run `wrangler`, `gh`, `git`, or `curl`; Pi's `write` and `edit` tools are not path-confined. On Claude candidates, `tools` removes the shell tool (the CLI is started with `--tools`, `--permission-mode acceptEdits` and `--strict-mcp-config`), but af passes no flag that turns off user or project settings or hooks, so hooks and settings `env` entries still apply, and the file tools are not confined to the worktree. `env_allow` may be empty; it keeps the standard runtime, locale, proxy, and CA variables plus listed names that are set in the parent. It removes tokens from the environment, but not credential files on disk or Pi's own credential file.

`host` candidates mean the current host starts its own fresh subagent, so when the host differs from the one the config's `host` models belong to (for example, Pi or Codex with Claude models), use a config whose `host` entries match that host, or list `pi`/`codex`/`claude` reviewer candidates instead.

Run `bash tests/run.sh` for the offline suite.

## Credits and license

af-lite builds on [Matt Pocock's skills](https://github.com/mattpocock/skills), used under their MIT license. af-lite is also distributed under the MIT license; see [LICENSE](LICENSE).
