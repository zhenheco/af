---
name: af
description: Chain Matt Pocock skills (af-lite) in a Git worktree behind test, review, and commit gates. Use to start a flow, dispatch writers and reviewers, run repair rounds and the merge decision, or clean up flows.
---

# af

Use af-lite to run an ordered Matt Pocock skill flow. The skills and worker CLIs are installed separately. af-lite creates the sibling worktree or reuses the current linked worktree, records steps, selects optional `pi`, `codex`, or `claude` workers or a host handoff, runs configured tests, scans host commits, and checks the final decision.

Shell variables in the commands below: `R` is the repository (or the current worktree when reusing it), `W` is the flow worktree path that `worktree create` returns, `S` is a scratch directory outside `W` (for example `S="$(mktemp -d)"`) holding dispatch output, prompt files, and the round log, because a dirty `W` blocks `gate decide` and changes dispatch's before/after comparison. Quote every path: they may contain spaces.

## 1. Start a flow

1. Check prerequisites and the repository:

   ```sh
   af doctor --repo "$R"
   ```

   Doctor checks the ten flow skills: `grill-with-docs`, `to-spec`, `to-tickets`, `implement-spec`, `implement`, `tdd`, `code-review`, `diagnosing-bugs`, `pr`, and `retro`. It warns if `docs/agents/issue-tracker.md` or `docs/agents/domain.md` is missing; it reports `high_risk.baseline_count`, `effective_count`, and `warnings`, setting `baseline_count` to `null` when the global baseline is invalid, `effective_count` to `null` when the project config or global baseline is invalid, and adding a warning for zero `effective_count` without changing `ok`.

   Done when `ok` is true and every warning is read.

2. Create a unique flow ID. Keep the returned worktree `path` (this is `W`) and `base_sha`.

   In a Claude app worktree session, or any clean linked worktree that will host a single writer, reuse it. This switches it to `af/<id>` from its current HEAD, records the original branch, and sets the base of `af/<id>` to origin's default branch (`refs/remotes/origin/HEAD`, else the original branch's upstream, else none):

   ```sh
   af worktree create --repo "$R" --flow-id <id> --reuse-current
   ```

   Create a sibling worktree instead when two or more writers run in parallel, when the host keeps editing its own worktree while the writer runs, or when the current worktree has uncommitted changes. `--reuse-current` refuses the primary checkout, a dirty worktree, and a worktree already held by another active flow:

   ```sh
   af worktree create --repo "$R" --flow-id <id>
   ```

3. Start the flow from `W` (on branch `af/<id>`) using the chosen ordered skills:

   ```sh
   af state start --repo "$W" --flow-id <id> --base-sha <base_sha> --chain <skill> [<skill> ...]
   ```

   Suggested chains: feature `grill-with-docs to-spec to-tickets code-review pr retro`; bug `diagnosing-bugs tdd code-review pr retro`; small `tdd code-review pr retro` or `implement code-review pr retro`.

   Done when `state start` has recorded the flow (check with `af state status --repo "$W" --flow-id <id>`).

## 2. Run each step

Advance each step before running it. Follow interactive skills in the host. A writer can be dispatched when a configured candidate is available. Supply `--writer-company` when selecting or dispatching reviewers.

```sh
af state advance --repo "$W" --flow-id <id>
af select --repo "$W" --flow-id <id> --role writer
af dispatch --worktree "$W" --flow-id <id> --role writer --prompt "<task>" > "$S/writer-1.json"
```

Run a writer dispatch alone. Dispatch compares HEAD and `git status` before and after, soft-resets worker commits, and refuses failover when the worktree changed, all of which assume it is the only worker in that worktree. Never run a writer dispatch at the same time as any other dispatch, or while the host edits `W`.

When a prompt lives in a file, quote the substitution: `--prompt "$(cat "$S/prompt.md")"`.

Claude Code adapter: when dispatch returns `via: host`, start a fresh subagent with the selected `model` using the `Agent` tool, set `worktree` as its working directory, and pass the returned `prompt`.

For a ticket flow, insert one step per ticket after `to-tickets`, then run them in order:

```sh
af state add-steps --repo "$W" --flow-id <id> --after to-tickets --steps implement-spec:T1 implement-spec:T2
```

Done when the writer has returned and its changes sit uncommitted in `W`.

## 3. Test and commit each writer step

Run focused tests after each writer step:

```sh
af gate test --repo "$W" --flow-id <id> --suite focused
```

Shared local services: a separate worktree does not isolate external resources. Concurrent `af gate test` runs from different worktrees collided on one shared local Postgres (global schemas). Take a lock around every gate test that touches the shared service, in every worktree. A check-then-start (`pgrep`, then run) is racy, since two flows can both see nothing running. The subshell scopes the trap, releases the lock on success, failure, and INT/TERM/HUP, and returns the gate test's exit status (works in bash and zsh, with or without `set -e`; after `kill -9`, remove the stale lock directory by hand; `flock` is an alternative where installed):

```sh
(
  L="${TMPDIR:-/tmp}/af-gate.lock"
  until mkdir "$L" 2>/dev/null; do sleep 30; done
  trap 'rmdir "$L"' EXIT
  trap 'exit 130' INT TERM HUP
  while pgrep -f "[s]cripts/ci.sh|[p]npm run ci" >/dev/null; do sleep 30; done
  s=0
  af gate test --repo "$W" --flow-id <id> --suite full || s=$?
  exit "$s"
)
echo "gate test status: $?"
```

Runs started outside af (a bare `scripts/ci.sh`) do not take the lock, so the `pgrep` loop inside the lock waits for them too; adapt its pattern to the project's CI command. The `[s]` and `[p]` brackets keep `pgrep -f` from matching the shell whose command line contains the script text.

Run long gate tests in the background (`&` then `wait`, or the host's background-command feature) and read the result when they finish.

The host commits only explicit changed paths, on branch `af/<id>`, and only the step's own files. List the paths first, then commit with the same pipeline. NUL-delimited `xargs -0` works in bash and zsh and with spaces in paths; passing an unquoted `$paths` does not, because zsh does not word-split it and `--paths $paths` becomes one argument.

```sh
{ git -C "$W" diff --no-renames --name-only -z HEAD; git -C "$W" ls-files --others --exclude-standard -z; } \
  | tr '\0' '\n'

{ git -C "$W" diff --no-renames --name-only -z HEAD; git -C "$W" ls-files --others --exclude-standard -z; } \
  | xargs -0 af gate commit --repo "$W" --flow-id <id> --message "<step summary>" --paths

af state complete --repo "$W" --flow-id <id> --result completed
```

Paths are relative to `W`. When the list holds files outside the step, pass the step's files by name (`--paths <file> [<file> ...]`).

Done when the commit exists on `af/<id>` and the step is marked completed.

## 4. Review

After implementation, run `code-review` against `base_sha...HEAD`: select a reviewer, dispatch, and read the report. Supply `--writer-company` so the reviewer comes from another company (selection rules are in Reference).

```sh
af select --repo "$W" --flow-id <id> --role small_review --writer-company <writer-company>
af dispatch --worktree "$W" --flow-id <id> --role small_review --writer-company <writer-company> \
  --prompt "Review the flow changes." > "$S/review-1.json"
```

Read the report. The dispatch JSON on stdout is metadata, not the review. The report text is the file at the JSON's `report_path` (equal to `attempts[-1].log_paths.stdout`); the `.err.log` holds the worker transcript and can be MBs. Redirect each dispatch's JSON to its own file, then:

```sh
cat "$(jq -r '.report_path // .attempts[-1].log_paths.stdout' "$S/review-1.json")"
# python3 equivalent
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(open(d.get("report_path") or d["attempts"][-1]["log_paths"]["stdout"]).read())' "$S/review-1.json"
```

Check `via` first. When `via` is `host`, the fresh subagent's reply is the report, and a `report_path` (if present) belongs to an earlier failed CLI attempt (a host handoff after quota failover carries those attempts), not to the review.

Parallel reviews: in af-lite versions after e62b82f, each attempt gets its own log files (pid plus a random token, created exclusively), so two read-only review dispatches for the same flow may run in parallel:

```sh
af dispatch --worktree "$W" --flow-id <id> --role <review-role> --writer-company <writer-company> --prompt "$(cat "$S/review-standards.md")" > "$S/review-1.json" & p1=$!
af dispatch --worktree "$W" --flow-id <id> --role <review-role> --writer-company <writer-company> --prompt "$(cat "$S/review-behavior.md")" > "$S/review-2.json" & p2=$!
wait "$p1"; s1=$?
wait "$p2"; s2=$?
echo "review statuses: $s1 $s2"
```

A bare `wait` returns 0 and hides a failed review, so check each status before reading its report: 0 ok, 75 `waiting_for_quota` (wait `retry_after_minutes`, redispatch that review), 124 timeout, anything else a failure (read the JSON and its `.err.log`, then redispatch).

Older af-lite (e62b82f and before) gave two simultaneous dispatches the same log path and their reports interleaved: on those versions, or when unsure of the version, run reviews one after another. A review never runs alongside a writer dispatch (Step 2).

Done when every report is read and its findings are listed.

## 5. Repair rounds

Collect the findings and fix them in one writer run. An ordinary repair round needs focused tests and no repeated review.

Changes matching configured high-risk paths need two reviews on every round and the full suite. Loop until both reviews return no blocking findings, one writer per round:

1. Collect every finding from both reviews into ONE writer brief and dispatch one writer (Step 2). Writer fixes test-first.
2. Run the full gate (`--suite full`) on every round, not only focused.
3. Commit with `af gate commit` (Step 3).
4. Dispatch the two reviews (Step 4) against the exact new diff range. The review prompt names `<prev-reviewed-sha>..HEAD` for the repair plus `base_sha...HEAD` for context, lists the findings being fixed, and asks for each "Fixed at the root cause? Does the new test fail without the fix?" plus "any new defect in this diff?". Example saved in `$S/round-N-review.md`:

   ```text
   Review <prev-reviewed-sha>..HEAD (this repair), with <base_sha>...HEAD for context.
   Findings being fixed:
   1. <finding>
   For each: fixed at the root cause? Does the new test fail without the fix?
   Also: any new defect in this diff?
   ```

5. Log the round (round number, findings, commit SHA, full-suite pass on that SHA) in `$S/rounds.md`, so later rounds do not reopen settled items.

Before the gate decision, review any final repair diff.

Done when both reviews of the latest round return no blocking findings and the round log shows every finding closed.

## 6. Decide, PR, merge

The full suite must pass on the HEAD you decide on. A passing full-suite run on that same HEAD (for example the last high-risk round, noted by SHA in `$S/rounds.md`) is reused; run it again only when HEAD moved:

```sh
af gate test --repo "$W" --flow-id <id> --suite full
af gate decide --repo "$W" --flow-id <id> --tests pass --reviews pass
```

A high-risk path or disabled `auto_merge` returns `ask`; missing config, failed checks, or a dirty worktree returns `blocked`. `gate decide` exits 2 for every `blocked` decision and 0 for `merge` or `ask`.

Continue to the project's normal PR process when the decision is `merge`, or when the owner OKs an `ask`. Push and open the PR only with the owner's OK. In the Claude desktop app, bind the PR with the `ccd_pr` tools and offer Auto-fix instead of polling CI.

Stacked PRs (PR B based on PR A's branch):

- Merge A with a merge commit when B is stacked on it: squash rewrites A's commits, so B then shows A's changes again and may conflict.
- After A merges, retarget B with `gh pr edit <B> --base main` (GitHub retargets automatically only when A's branch is deleted on merge), then let CI re-run.
- If A was squashed anyway, rebase B with `git rebase --onto origin/main <A's old tip> af/<B-id>`.
- af's own cleanup: `af worktree remove` treats a merge-commit merge as merged by ancestry and a squash merge only via `gh pr view` `MERGED` with matching `headRefOid`.

Done when the decision is recorded and, for `merge` or an owner-approved `ask`, the PR is open.

## 7. Retro

The `retro` skill has `disable-model-invocation: true`, so the Skill tool refuses it inside a flow. The host completes the step by reading the `retro` `SKILL.md` from the skill directory `doctor` searched (the config's `skill_dirs`; default `~/.claude/skills`, `~/.agents/skills`, `<repo>/skills`; for Claude Code usually `~/.claude/skills/retro/SKILL.md`) with the Read tool and following its steps against the flow's session, then:

```sh
af state complete --repo "$W" --flow-id <id> --result completed
```

If retro proposals need edits, they go through a writer like any other change.

## Reference

### Dispatch

Dispatch passes a fixed writer or read-only reviewer role preamble and the prompt to the worker on stdin. stdout and stderr are logged under `<git-common-dir>/.af/logs/<id>/`, one file pair per attempt; use the returned `log_paths` to follow output. A timeout kills the process group. Failover occurs only when HEAD and `git status --porcelain=v1 -z --ignored` are unchanged. If a worker creates commits, dispatch soft-resets them to the starting commit and reports their SHAs.

`dispatch` takes `--worktree`, with `--repo` accepted as an alias for consistency with the other commands; passing both with different values is an error. It always uses the config recorded by `state start` and rejects `--config`.

### Reviewer selection

Reviewer selection prefers a different company when the role has any configured candidate from another company. Same-company review is allowed only when every candidate for that role has the writer's company. If all independent candidates stop for quota, dispatch returns `waiting_for_quota` and exits 75; wait `retry_after_minutes` and retry. A missing `pi`, `codex`, or `claude` CLI is skipped; `host` candidates are always available. `AF_NO_HOST=1` makes `host` candidates unavailable so a non-Claude host, or a host without quota for the configured host model, falls through to the next `pi`/`codex`/`claude` candidate.

`host` candidates mean the current host starts its own fresh subagent, so when the host differs from the one the config's `host` models belong to (for example, Pi or Codex with Claude models), use a config whose `host` entries match that host, or list `pi`/`codex`/`claude` reviewer candidates instead.

### Config

Config resolves from `--config`, `$AF_CONFIG`, the primary checkout's `af.config.json`, then `~/.config/af/af.config.json`. Any resolved config path inside a worktree on an `af/*` branch, including the primary checkout, is refused. `state start` records the resolved config path and the SHA-256 of the effective config, including inherited global `roles` and `high_risk_paths`; `state advance`/`resume`, `select --flow-id`, `dispatch`, and `gate test`/`commit`/`decide` fail closed with `config changed since flow start` if it changes during the flow.

When a project config is selected, it inherits `roles` from `$HOME/.config/af/af.config.json` only when the project omits the `roles` key; a project `roles` map replaces it entirely, with no per-role merge. An invalid inherited role map fails closed as an invalid global config, and selecting a role absent from the resolved map reports `role not configured: <role>`. The global `high_risk_paths` baseline is prepended to the project's list; duplicates keep their last occurrence. Other global fields are not inherited.

Candidate options (`tools`, `env_allow`, `base_url` plus `api_key_command`) are in [`CANDIDATES.md`](CANDIDATES.md) next to this file; read it when writing or reviewing a role's candidates.

### Status and cleanup

```sh
af state status --repo "$W" --flow-id <id>
af state stop --repo "$W" --flow-id <id>
af worktree remove --repo "$R" --flow-id <id>
af cleanup --repo "$R"
af cleanup --repo "$R" --worktrees
```

Worktree removal refuses tracked changes and untracked non-ignored files. It accepts the branch as merged when it is an ancestor of the recorded base, or, for squash merges, when `gh pr view af/<id>` reports `MERGED` with `headRefOid` equal to the local branch tip; a missing `gh` or failed query refuses removal. Use `--force` only after reviewing the work. A reused flow never removes the worktree: removal switches it back to the original branch and deletes `af/<id>`, and refuses a dirty worktree even with `--force`. `cleanup --worktrees` applies the same removal to completed flows, lists the ones it skipped with the reason, and keeps their records for a later retry. Cleanup archives or deletes only completed or stopped records and their logs according to `record_retention`.
