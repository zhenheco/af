# af-lite

af-lite chains [Matt Pocock's skills](https://github.com/mattpocock/skills) in a sibling Git worktree or the current linked worktree. It is POSIX bash plus Python 3 standard library; the upstream skills and worker CLIs remain separately installed.

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

Inside a clean linked worktree that will host a single writer, such as a Claude app worktree session, use `af worktree create --repo <current worktree> --flow-id <id> --reuse-current` instead: it switches that worktree to `af/<id>` from its HEAD, records the original branch, sets the base (upstream) of `af/<id>` to origin's default branch (`refs/remotes/origin/HEAD`, else the original branch's upstream, else none), and refuses the primary checkout, a dirty worktree, or one already held by another active flow. Use sibling worktrees for parallel writers.

Use the worktree path and `base_sha` returned by creation. Config resolves in this order: `--config`, `$AF_CONFIG`, `<primary checkout>/af.config.json`, `~/.config/af/af.config.json`. Any resolved config path inside a worktree on an `af/*` branch, including the primary checkout, is refused. `state start` records the resolved config path and the SHA-256 of the effective config after global `roles` are inherited only when the project omits the entire `roles` key (a project `roles` map replaces the whole global map; there are no per-role overlays), and global `high_risk_paths` and `owner_approval_paths` are merged in; `state advance`/`resume`, `select --flow-id`, `dispatch`, and `gate test`/`commit`/`decide`/`merge` use that path and fail closed if the effective config has changed.

Advance one step before running it. Run Matt skills in the host as appropriate; dispatch can use installed `pi`, `codex`, or `claude` CLIs or return a `host` handoff. Run focused tests and host commit explicit paths before review. Use `state add-steps` to insert ordered ticket steps. Run full tests and reviews before `gate decide`. Push `af/<id>` (without `-u`), open the PR, then run `af gate merge`, the only af command that merges. `blocked` and `owner` never merge. After `auto`, run `gh pr merge <n> --disable-auto` before any new push to `af/<id>`. After `queued`, do not push to `af/<id>`: `gh pr merge --disable-auto` (gh 2.101) reports success for a queued PR without dequeuing it. If a change is needed, dequeue first with `gh api graphql -f query='mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } } }' -f id=<PR node id>` and confirm with `gh api graphql -f query='query($id: ID!) { node(id: $id) { ... on PullRequest { isInMergeQueue } } }' -f id=<PR node id>` that `isInMergeQueue` is false (the PR node id comes from `gh pr view af/<id> --json id`). The new head then goes through tests, reviews, `gate decide` and `gate merge` again; do not turn on Auto-fix for an af PR while auto-merge is enabled.

```sh
af state advance --repo /path/to/repository-example-001 --flow-id example-001
af gate test --repo /path/to/repository-example-001 --flow-id example-001 --suite focused
af gate commit --repo /path/to/repository-example-001 --flow-id example-001 --message "implement focused change" --paths src/file.py tests/test_file.py
af gate decide --repo /path/to/repository-example-001 --flow-id example-001 --tests pass --reviews pass
af gate merge --repo /path/to/repository-example-001 --flow-id example-001 --tests pass --reviews pass [--owner-approved]
```

Configured worker candidates are tried in order; missing `pi`, `codex`, and `claude` CLIs are skipped, and `host` candidates are always available. Review roles require a different company whenever a configured candidate from another company exists. Same-company review is allowed only when every configured candidate for that role has the writer's company. If independent reviewers all stop for quota, dispatch returns `waiting_for_quota` with exit 75. Dispatch never fails over after a worker changes HEAD or the worktree status, including ignored files. `AF_NO_HOST=1` makes `host` candidates unavailable so a non-Claude host, or a host without quota for the configured host model, falls through to the next `pi`/`codex`/`claude` candidate.

`dispatch` takes `--worktree`, with `--repo` accepted as an alias for consistency with the other commands; passing both with different values is an error. It always uses the config recorded by `state start` and rejects `--config`.

`gate commit` requires branch `af/<flow-id>`, accepts explicit changed paths only, rejects absolute paths, `..` path components, absolute symlink targets, and symlinks that resolve outside the worktree, then scans staged changes with `gitleaks protect --staged` or a secret-pattern scan of added lines. Relative symlink targets may contain `..` when they resolve inside the worktree. It restores the prior index if a check refuses the commit. `gate decide` requires passing tests and reviews, a clean worktree, and a recorded base that is on origin's default branch, then returns one of four decisions: `blocked` (exit 2), `owner` (exit 3: the diff touches `owner_approval_paths`, so the owner must approve before merging; the non-zero status keeps a script that checks only the exit status from merging a payment diff), `ask` (exit 0: high-risk paths or `auto_merge: false`), or `merge` (exit 0). Precedence is `blocked`, then `owner`, then `ask`, then `merge`. Path matches use Git ignore patterns on committed changes from the recorded base through the flow branch, and the JSON lists them as `high_risk_paths` and `owner_approval_paths`. The base check applies only when the worktree has an `origin` remote and `refs/remotes/origin/HEAD` resolves; it uses local refs only (no network) and blocks with `base_sha is not on origin's default branch ...` when `base_sha` is not an ancestor, because the PR would then carry unpushed commits.

`gate merge --repo <W> --flow-id <id> --tests pass|fail|pending --reviews pass|fail|pending [--owner-approved]` recomputes the same decision, then stops at the first failing step: `blocked` exits 2 and `owner` without `--owner-approved` exits 3, both without calling `gh`. Otherwise it reads the PR with `gh pr view af/<id> --json id,number,url,state,headRefOid,baseRefName,statusCheckRollup`; the PR must be `OPEN` at exactly the local tip of `af/<id>`, or the result is `blocked` (push the tested tip and open the PR first). A failing check (`FAILURE`, `TIMED_OUT`, `CANCELLED`, `ACTION_REQUIRED`, `STARTUP_FAILURE`, `STALE`, or a status context in `FAILURE`/`ERROR`) blocks with `CI is failing: <names>`. A pending check (or an empty rollup while the tip has files under `.github/workflows/`) first reads the base branch's required checks (`gh api repos/{owner}/{repo}/branches/<base>` protection `contexts` and `checks[].context`, plus `gh api repos/{owner}/{repo}/rules/branches/<base> --paginate --slurp` `required_status_checks` rules, all pages), because GitHub auto-merge waits only for required checks. When every pending check is required it enables GitHub auto-merge through the `enablePullRequestAutoMerge` GraphQL mutation (squash, `expectedHeadOid` = tip), which never merges immediately: result `{"merge": "auto"}`, exit 0. The result is `{"merge": "waiting_for_ci"}` with the reason, exit 4, nothing enabled or merged, when any pending check is not required (`GitHub auto-merge would not wait for these non-required checks: <names>`), when the same check name appears more than once in the rollup and one of them is pending (`gh pr view` does not say which app produced a check, so a pending check could hide behind a required name) (`the same check name appears more than once and one is pending: <names>`), when the required-check responses have an unexpected shape, when the rollup is empty with workflows and no check is required (`CI has not registered checks and none are required`), when the required checks cannot be read (fail safe), or when auto-merge cannot be enabled (for example the repository disallows it; the `gh` error). With no checks and no workflows, or only passed, neutral or skipped checks, it runs `gh pr merge <number> --squash --match-head-commit <tip>`: after a successful merge call it asks `gh pr view af/<id> --json state` again: `MERGED` gives `{"merge": "merged"}`, anything else (a merge queue) gives `{"merge": "queued"}`, both exit 0; a refused merge is `blocked` with the `gh` error, exit 2. Exit statuses: 0 merged, auto or queued, 2 blocked, 3 owner, 4 waiting_for_ci. af never polls CI.

Worktree removal refuses tracked changes and untracked non-ignored files. A branch counts as merged when it is an ancestor of its recorded base (an upstream that is the branch's own remote copy, as `git push -u` creates, never counts), or when `gh pr view af/<id> --json state,headRefOid` reports `MERGED` at the local branch tip (squash merges); if `gh` is missing or the query fails, removal is refused. `--force` overrides both checks for sibling worktrees. A reused worktree is never removed: af switches it back to its original branch and deletes `af/<id>`, and refuses a dirty reused worktree even with `--force`. After a removal backed by a merge proof, when an `origin` remote exists and `git ls-remote --heads origin af/<id>` shows the branch at exactly the local tip that was just deleted, af also deletes it with `git push --force-with-lease=refs/heads/af/<id>:<tip> origin :refs/heads/af/<id>`, so a push that lands after the check survives (for repositories without `delete_branch_on_merge`); the result JSON gains `remote_branch`: `deleted`, `absent`, `kept: <reason>` (a different SHA, a failed delete, or a `--force` removal, which never touches the remote), or `no remote`. This step never raises. `af cleanup --worktrees` (off by default) applies the same removal to completed flows, reports skipped flows with reasons, and keeps their records for a later retry. Cleanup archives or deletes only completed or stopped flow records and their logs after the configured ages.

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
| `roles` | Ordered candidates for each role. Each candidate uses `via` (`pi`, `codex`, `claude`, or `host`) and `company`; Pi uses `provider` plus `model`, Codex uses `profile`, Claude Code CLI uses `model`, and host uses `model`. A host candidate may also set `agent` to `claude` with company `Anthropic`, or `codex` with company `OpenAI`; af validates this agent/company pair case-insensitively. Model names are opaque: the operator checks runtime availability and agent suitability, which af does not infer from the model. Omitting `agent` remains valid and dispatch returns `null`. Pi also accepts optional `tools` and `env_allow` candidate settings; Claude accepts the same, with `tools` passed as `--tools ... --permission-mode acceptEdits --strict-mcp-config`. Claude also accepts `base_url` plus `api_key_command` (both required together, `https://` only), which point the Claude CLI at another vendor's Anthropic-compatible endpoint: af then starts `claude --bare` (which skips user and project settings and hooks), runs the command for the key, passes it as `ANTHROPIC_API_KEY` with `ANTHROPIC_BASE_URL`, and removes `ANTHROPIC_AUTH_TOKEN`; a failed or empty key command fails the attempt without starting the CLI. |
| `quota_stop.exit_statuses` | Worker exit statuses treated as quota stops; default: `[75]`. |
| `quota_stop.patterns` | Configured case-insensitive regexes match all worker stderr after removing the prompt; none are enabled by default. Built-in patterns always match only the last 20 non-empty stderr lines after prompt removal (`usage limit`, `quota will reset`, `rate limit exceeded`, `429 too many requests`, `insufficient_quota`, `quota exceeded`). |
| `quota_retry_minutes` | Delay to report when all candidates stop for quota; default: `15`. |
| `worker_timeout_minutes` | Worker timeout; default: `60`. |
| `test_timeout_minutes` | Test command timeout; default: `30`. |
| `stale_after_minutes` | Age after which a running step may be resumed; default: `30`. |
| `skill_dirs` | Directories containing Matt skill folders; default: `~/.claude/skills`, `~/.agents/skills`, and `<repo>/skills`. |
| `auto_merge` | Whether an otherwise clear gate decision may return `merge`; default: `true`. |
| `high_risk_paths` | Effective Git ignore patterns: the optional global baseline followed by this project's entries, checked against committed paths from base to flow branch. |
| `owner_approval_paths` | Optional. Git ignore patterns (same matcher as `high_risk_paths`) for payment code. A diff touching them makes `gate decide` return `owner` (exit 3) and `gate merge` refuse without `--owner-approved`. Inherited from the global config exactly like `high_risk_paths`, and only when the key exists in the global or project config, so existing flows keep their config digest. |
| `tests.focused`, `tests.full` | Shell command strings run in the flow worktree. |
| `record_retention.archive_after_days` | Age before eligible records are archived; default: `30`; `null` disables archival. |
| `record_retention.archive_dir` | Archive root; relative paths resolve under `<git-common-dir>/.af/`; `null` disables archival. |
| `record_retention.delete_after_days` | Age before eligible records and logs are deleted; default: `180`; `null` disables deletion. |

### Global role defaults

When a project config omits `roles`, af-lite inherits the role map from `$HOME/.config/af/af.config.json`. If the project config contains `roles`, its entire map is used, including an empty map; role entries are never merged individually. Other global fields are not inherited. If the selected config has no requested role, role-using commands such as `af select` fail with `role not configured: <role>`. An invalid global role map used as the fallback fails closed as an invalid global config.

### Global high-risk baseline

The optional baseline lives at `$HOME/.config/af/af.config.json` under `high_risk_paths` and `owner_approval_paths`; copy the [reference baseline](docs/high-risk-baseline.example.json). `owner_approval_paths` is merged the same way as `high_risk_paths` below. For this field, if the file or field is absent, project high-risk behavior is unchanged. Global patterns come first and project patterns follow, with duplicates kept at their last occurrence; Git ignore rules let the last matching pattern decide, so dropping earlier duplicates never changes which paths match. A later project `!` pattern can cancel a file or basename match. Git tests confirm `!scripts/ci-local.sh` cancels `scripts/ci*`, but it cannot re-include that file when the parent directory pattern `scripts/` is excluded.

An invalid global JSON file, a non-string-array `high_risk_paths` or `owner_approval_paths`, a broken symlink (including a symlink loop), a directory at the config path, an unreadable config file, or an unreadable or broken-symlink ancestor directory between `$HOME` and the config file makes `load_config` fail closed; symlinks resolving to valid files are supported. `gate decide` returns `blocked` and exits 2. `af doctor` reports `high_risk.baseline_count`, `effective_count`, and `warnings`; each count is `null` when the corresponding config cannot be loaded. Only a successfully loaded but empty effective list adds `high_risk_paths is empty: the merge gate auto-merges every path`; this warning does not change `ok` or the exit code. It likewise reports `owner_approval: {effective_count, warnings}` and, for an empty effective list, warns `owner_approval_paths is empty: payment diffs merge without the owner's approval`.

On Pi candidates, `tools` is a non-empty list passed to Pi with MCP disabled. Without a shell tool, the worker cannot run `wrangler`, `gh`, `git`, or `curl`; Pi's `write` and `edit` tools are not path-confined. On Claude candidates, `tools` removes the shell tool (the CLI is started with `--tools`, `--permission-mode acceptEdits` and `--strict-mcp-config`), but af passes no flag that turns off user or project settings or hooks, so hooks and settings `env` entries still apply, and the file tools are not confined to the worktree. `env_allow` may be empty; it keeps the standard runtime, locale, proxy, and CA variables plus listed names that are set in the parent. It removes tokens from the environment, but not credential files on disk or Pi's own credential file.

`host` candidates mean the current host starts its own fresh subagent, so when the host differs from the one the config's `host` models belong to (for example, Pi or Codex with Claude models), use a config whose `host` entries match that host, or list `pi`/`codex`/`claude` reviewer candidates instead. An omitted `agent` keeps the existing native-host/CLI route; do not guess an Orca agent from the model.

## Orca coordinator handoff

Run the coordinator in the exact flow worktree (`$AF_WORKTREE` must be the same current W). Set the flow, task, review, writer-company, and spec-file variables for that flow; `$AF_SPEC` must be outside W. Keep each dispatch's JSON visible:

```sh
af dispatch --worktree "$AF_WORKTREE" --flow-id "$AF_FLOW_ID" \
  --role writer --prompt "$AF_TASK"
```

Only a `via: "host"` result is an Orca handoff. Copy that result's exact `prompt` text into `$AF_SPEC` outside W; set `$AF_AGENT` and `$AF_MODEL` from the same result's explicit, non-null metadata, and confirm its `worktree` is the same W. Then run the official worker start:

```sh
orca orchestration worker-start --spec-file "$AF_SPEC" \
  --worktree current --agent "$AF_AGENT" --model "$AF_MODEL" --json
```

The installed Orca 1.4.253 `agent-context` and `worker-start` help confirms these `worker-start` options. Agent/company pairing is validated by af; model names and availability are opaque and require operator/runtime checking. Agent-level launch, auto-release, and nesting continue to use existing Orca settings; this handoff adds no role registry. A CLI result without `via: "host"` has already run: do not start an Orca worker or treat it as a missing-handoff failure/retry. For a nonzero, quota, or timeout result, show the returned JSON and log evidence, stop and reconcile, and do not blindly retry. af's configured CLI tools, environment, timeout, and quota/failover gates remain in force.

After `worker-start` returns, record its run and dispatch identifiers and END TURN. A Run does not schedule later work; the coordinator handles ready tasks when it wakes. Do not poll or watch for completion, use `--from` to impersonate another pane, or retry while ownership/result is unknown. Reconcile an ambiguous start against that same work using `orca orchestration worker-show --dispatch <dispatch-id>` or `orca orchestration worker-list --run <run-id>`; the installed Orca 1.4.253 help confirms these lookup flags.

On a genuine `worker_done` wake, verify the task, dispatch identifier, and actual result, then read HEAD and status in the same W and compare them with the recorded pre-start state:

```sh
git -C "$AF_WORKTREE" rev-parse HEAD
git -C "$AF_WORKTREE" status --short
```

Inspect the changed paths and diff. A writer must leave only task-scoped changes uncommitted; a reviewer stays read-only. If a worker committed or changed out-of-scope files, stop and reconcile manually; do not normalize commits automatically. `worker_done` is not a test, review, or gate pass. Follow the existing focused test and host commit steps before dispatching an independent reviewer; run full tests, push/PR, and merge only with the actual evidence required by the sections above. Advance or complete the flow only after recording real results.

After the writer's focused test and host commit, dispatch the independent review as a separate phase, passing the writer's company so af can enforce reviewer independence. If this returns a host handoff, use the same prompt/spec and `worker-start` steps above; a reviewer remains read-only:

```sh
af dispatch --worktree "$AF_WORKTREE" --flow-id "$AF_FLOW_ID" \
  --role small_review --writer-company "$AF_WRITER_COMPANY" \
  --prompt "$AF_REVIEW_TASK"
```

A native smoke observed a two-stage read-only handoff and wake; it does not establish the af/Claude/release chain. The offline tests cover the af dispatch handoff JSON only and do not execute this coordinator recipe.

Run `bash tests/run.sh` for the offline suite.

## Credits and license

af-lite builds on [Matt Pocock's skills](https://github.com/mattpocock/skills), used under their MIT license. af-lite is also distributed under the MIT license; see [LICENSE](LICENSE).
