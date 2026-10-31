---
name: af
description: Chain Matt Pocock skills in a dedicated Git worktree with explicit test, review, and commit gates.
---

# af

Use af-lite to run an ordered Matt Pocock skill flow. The skills and worker CLIs are installed separately. af-lite creates the sibling worktree, records steps, selects optional `pi`, `codex`, or `claude` workers or a host handoff, runs configured tests, scans host commits, and checks the final decision.

## Start a flow

1. Check prerequisites and the repository:

   ```sh
   af doctor --repo /path/to/repository
   ```

   Doctor checks the ten flow skills: `grill-with-docs`, `to-spec`, `to-tickets`, `implement-spec`, `implement`, `tdd`, `code-review`, `diagnosing-bugs`, `pr`, and `retro`. It warns if `docs/agents/issue-tracker.md` or `docs/agents/domain.md` is missing; it reports `high_risk.baseline_count`, `effective_count`, and `warnings`, setting `baseline_count` to `null` when the global baseline is invalid, `effective_count` to `null` when the project config or global baseline is invalid, and adding a warning for zero `effective_count` without changing `ok`.

2. Create a unique flow ID. Keep the returned sibling worktree path and `base_sha`:

   ```sh
   af worktree create --repo /path/to/repository --flow-id <id>
   ```

3. Start a flow from its worktree using the chosen ordered skills:

   ```sh
   af state start --repo /path/to/repository-<id> --flow-id <id> --base-sha <base_sha> --chain <skill> [<skill> ...]
   ```

   Suggested chains: feature `grill-with-docs to-spec to-tickets code-review pr retro`; bug `diagnosing-bugs tdd code-review pr retro`; small `tdd code-review pr retro` or `implement code-review pr retro`.

## Run ordered skills

Advance each step before running it. Follow interactive skills in the host. A writer can be dispatched when a configured candidate is available. Supply `--writer-company` when selecting or dispatching reviewers.

```sh
af state advance --repo /path/to/repository-<id> --flow-id <id>
af select --repo /path/to/repository-<id> --flow-id <id> --role writer
af dispatch --worktree /path/to/repository-<id> --flow-id <id> --role writer --prompt "<task>"
```

Dispatch passes a fixed writer or read-only reviewer role preamble and the prompt to the worker on stdin. stdout and stderr are logged under `<git-common-dir>/.af/logs/<id>/`; use the returned log paths to follow output. A timeout kills the process group. Failover occurs only when HEAD and `git status --porcelain=v1 -z --ignored` are unchanged. If a worker creates commits, dispatch soft-resets them to the starting commit and reports their SHAs.

`dispatch` takes `--worktree`, with `--repo` accepted as an alias for consistency with the other commands; passing both with different values is an error. It always uses the config recorded by `state start` and rejects `--config`.

Claude Code adapter: when dispatch returns `via: host`, start a fresh subagent with the selected `model` using the `Agent` tool, set `worktree` as its working directory, and pass the returned `prompt`.

For a ticket flow, insert one step per ticket after `to-tickets`, then run them in order:

```sh
af state add-steps --repo /path/to/repository-<id> --flow-id <id> --after to-tickets --steps implement-spec:T1 implement-spec:T2
```

## Test, commit, and review

Run focused tests after each writer step. The host commits only explicit changed paths, on branch `af/<id>`:

```sh
af gate test --repo /path/to/repository-<id> --flow-id <id> --suite focused
af gate commit --repo /path/to/repository-<id> --flow-id <id> --message "<step summary>" --paths <changed-file> [<changed-file> ...]
af state complete --repo /path/to/repository-<id> --flow-id <id> --result completed
```

After implementation, run `code-review` against `base_sha...HEAD`. Collect findings and fix them in one writer run; ordinary repair rounds need focused tests but no repeated review. Changes matching configured high-risk paths need two reviews on every round. Run the full test suite and review any final repair diff before the gate decision.

Reviewer selection prefers a different company when the role has any configured candidate from another company. Same-company review is allowed only when every candidate for that role has the writer's company. If all independent candidates stop for quota, dispatch returns `waiting_for_quota` and exits 75; wait `retry_after_minutes` and retry. A missing `pi`, `codex`, or `claude` CLI is skipped; `host` candidates are always available. `AF_NO_HOST=1` makes `host` candidates unavailable so a non-Claude host, or a host without quota for the configured host model, falls through to the next `pi`/`codex`/`claude` candidate.

`host` candidates mean the current host starts its own fresh subagent, so when the host differs from the one the config's `host` models belong to (for example, Pi or Codex with Claude models), use a config whose `host` entries match that host, or list `pi`/`codex`/`claude` reviewer candidates instead.

```sh
af select --repo /path/to/repository-<id> --flow-id <id> --role small_review --writer-company <writer-company>
af dispatch --worktree /path/to/repository-<id> --flow-id <id> --role small_review --writer-company <writer-company> --prompt "Review the flow changes."
```

```sh
af gate test --repo /path/to/repository-<id> --flow-id <id> --suite full
af gate decide --repo /path/to/repository-<id> --flow-id <id> --tests pass --reviews pass
```

Only continue to the project's normal PR process when the decision is `merge`. A high-risk path or disabled `auto_merge` returns `ask`; missing config, failed checks, or a dirty worktree returns `blocked`.

`gate decide` exits 2 for every `blocked` decision and 0 for `merge` or `ask`.

## Config and cleanup

Config resolves from `--config`, `$AF_CONFIG`, the primary checkout's `af.config.json`, then `~/.config/af/af.config.json`. Any resolved config path inside a linked worktree on an `af/*` branch is refused. `state start` records the resolved config path.
When a project config is selected, it inherits `roles` from `$HOME/.config/af/af.config.json` only when the project omits the `roles` key; a project `roles` map replaces it entirely, with no per-role merge. An invalid inherited role map fails closed as an invalid global config, and selecting a role absent from the resolved map reports `role not configured: <role>`. The global `high_risk_paths` baseline is prepended to the project's list; duplicates keep their last occurrence. Other global fields are not inherited.

Pi candidates may set `tools` to a non-empty list passed to Pi with MCP disabled. Without a shell tool, the worker cannot run `wrangler`, `gh`, `git`, or `curl`; Pi's `write` and `edit` tools are not path-confined. On Claude candidates, `tools` removes the shell tool (the CLI is started with `--tools`, `--permission-mode acceptEdits` and `--strict-mcp-config`), but af passes no flag that turns off user or project settings or hooks, so hooks and settings `env` entries still apply, and the file tools are not confined to the worktree. `env_allow` may be empty and keeps standard runtime, locale, proxy, and CA variables plus listed names set in the parent. It removes tokens from the environment, but not credential files on disk or Pi's own credential file. Claude Code CLI candidates use `model` and accept the same optional `tools` (passed as `--tools ... --permission-mode acceptEdits --strict-mcp-config`) and `env_allow`. Claude candidates also accept `base_url` plus `api_key_command` (both required together, `https://` only), which point the Claude CLI at another vendor's Anthropic-compatible endpoint: af then starts `claude --bare` (which skips user and project settings and hooks), runs the command for the key, passes it as `ANTHROPIC_API_KEY` with `ANTHROPIC_BASE_URL`, and removes `ANTHROPIC_AUTH_TOKEN`; a failed or empty key command fails the attempt without starting the CLI.

```sh
af state status --repo /path/to/repository-<id> --flow-id <id>
af state stop --repo /path/to/repository-<id> --flow-id <id>
af worktree remove --repo /path/to/repository --flow-id <id>
af cleanup --repo /path/to/repository
```

Worktree removal refuses tracked changes, untracked non-ignored files, and branches not merged into the recorded base; use `--force` only after reviewing the work. Cleanup archives or deletes only completed or stopped records and their logs according to `record_retention`.
