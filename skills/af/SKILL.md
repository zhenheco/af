---
name: af
description: Chain Matt Pocock skills (af) in a Git worktree behind test, review, and commit gates. Use for any authorized repository change request (a fix, feature, or edit in any repo, including an empty Git repo or an Orca linked worktree) even when the user never mentions af, and to start a flow, dispatch writers and reviewers, run repair rounds and the merge decision, or clean up flows.
---

# af

Use af to run an ordered Matt Pocock skill flow. The skills and worker CLIs are installed separately. af creates the sibling worktree or reuses the current linked worktree, records steps, selects optional `pi`, `codex`, or `claude` workers or a host handoff, runs configured tests, scans host commits, and checks the final decision.

The older AutoFlow tool has been retired; `af` is not AutoFlow.

Precondition: no writer (`wf-implementer` or any CLI) and no reviewer starts without evidence valid for this flow: an `af state status --repo <W> --flow-id <id>` readback whose worktree, base/HEAD, and config digest match, plus a successful `af select` and `af dispatch` for this flow and role. A new flow gets there with `af doctor`, `af worktree create`, and `af state start`. A resumed flow (even in a new session) reuses its existing worktree and state: run `af worktree create` or `af state start` only when they do not exist yet, never to recreate them. A fresh `af select` and `af dispatch` is still required before each new writer or reviewer launch. If af is unavailable or fails, stop and report instead of bypassing it. The user does not need to say af.

Shell variables in the commands below: `R` is the repository (or the current worktree when reusing it), `P` is the primary checkout, the repository root that holds `.git` (the same as `R` unless `R` is a linked worktree), `W` is the flow worktree path that `worktree create` returns, `S` is a scratch directory outside `W` (for example `S="$(mktemp -d)"`) holding dispatch output, prompt files, and the round log, because a dirty `W` blocks `gate decide` and changes dispatch's before/after comparison. Quote every path: they may contain spaces.

## 1. Start a flow

1. Check prerequisites and the repository:

   ```sh
   af doctor --repo "$R"
   ```

   Doctor checks the ten flow skills: `grill-with-docs`, `to-spec`, `to-tickets`, `implement-spec`, `implement`, `tdd`, `code-review`, `diagnosing-bugs`, `pr`, and `retro`. It warns if `docs/agents/issue-tracker.md` or `docs/agents/domain.md` is missing; it reports `high_risk.baseline_count`, `effective_count`, and `warnings`, setting `baseline_count` to `null` when the global baseline is invalid, `effective_count` to `null` when the project config or global baseline is invalid, and adding a warning for zero `effective_count` without changing `ok`. It reports `owner_approval.effective_count` and `warnings` the same way; an empty `owner_approval_paths` warns that payment diffs merge without the owner's approval.

   Done when `ok` is true and every warning is read.

2. Sweep finished flows before creating a new one:

   ```sh
   af cleanup --repo "$R" --worktrees
   git -C "$R" fetch --prune origin
   ```

   The sweep removes completed flows, and closes then removes active flows whose merge is proven (an active flow only on a MERGED pull request, with its worktree clean and at the merged tip; cleanup only closes the record, the normal removal does the deleting); read `worktrees.closed` and `worktrees.skipped` and report both to the owner. It never touches dirty or unmerged flows. Skip the fetch when the repository has no `origin`.

3. Create a unique flow ID. Keep the returned worktree `path` (this is `W`) and `base_sha`.

   In a Claude app worktree session, or any clean linked worktree that will host a single writer, reuse it. This switches it to `af/<id>` from its current HEAD, records the original branch, and sets the base of `af/<id>` to origin's default branch (`refs/remotes/origin/HEAD`, else the original branch's upstream, else none):

   ```sh
   af worktree create --repo "$R" --flow-id <id> --reuse-current
   ```

   Create a sibling worktree instead when two or more writers run in parallel, when the host keeps editing its own worktree while the writer runs, or when the current worktree has uncommitted changes. `--reuse-current` refuses the primary checkout, a dirty worktree, and a worktree already held by another active flow:

   ```sh
   af worktree create --repo "$R" --flow-id <id>
   ```

4. Start the flow from `W` (on branch `af/<id>`) using the chosen ordered skills:

   ```sh
   af state start --repo "$W" --flow-id <id> --base-sha <base_sha> --chain <skill> [<skill> ...]
   ```

   A new native Orca Claude flow must start with its prepared private config copy (see "Native Orca host" in Step 2):

   ```sh
   af state start --repo "$W" --flow-id <id> --base-sha <base_sha> --config "<private copy>" --chain <skill> [<skill> ...]
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

Claude Code adapter (every host except a new native Orca Claude flow, below): when dispatch returns `via: host`, start a fresh subagent with the selected `model` using the `Agent` tool, set `worktree` as its working directory, and pass the returned `prompt`.

Native Orca host: a new flow in a native Orca Claude chat uses a native worker, never the `Agent` tool, and only when the dispatch `agent` matches a known host company pair. Load the version-matched `orca-cli` and `orchestration` skills, use `ORCA_CLI_COMMAND`, and use only official `orca` help and guides (no private RPC, database, or controller). Authenticated native session path (a legacy coordinator field is not the proof): your own actor is trusted `ORCA_STRUCTURED_SESSION=1`, your own nonempty `ORCA_AGENT_SESSION_ID`, and your own worktree/root/host from `orca worktree show --worktree <exact selector>` (`orca worktree current` can fail on a symlinked selector path). If `ORCA_TERMINAL_HANDLE` identifies a worker, or identity is missing, untrusted, or unresolved, STOP. For the first handoff of a flow, a NEW Run: (a) the initial `orca orchestration run-current --json` must show no Run, or the bound Run must be this chat's own retained Run for an af flow whose `af state status` is `complete` (retained creation record and objective naming that flow; verified on Orca 1.4.256, 2026-11-12: `run-create` then created and bound a new Run, `legacy: 0`, no `next` warning); in that second case go straight to (b). Any other existing Run is foreign, so STOP and never borrow it. (b) `orca orchestration run-create --objective "<flow goal>" --json --full`: record your own new ID; the objective must match, `legacy` must be 0, with no `next` warning and no caller fencing, else STOP. (c) In the same context `run-current --json` must return exactly that ID; only if the binding is empty, run at most one `orca orchestration run-use --id <that new ID> --json` and reread; a different nonempty ID, a failed `run-use`, or a reread that still does not match means STOP. (d) A null `coordinator_handle` (or other legacy terminal coordinator field) is not applicable to the authenticated session: never ownership proof and never an automatic failure. A non-null coordinator identity that conflicts with your own actor, a foreign Run, non-zero `legacy`, missing trusted context, a wrong ID, or unresolved identity means STOP. (e) `run-current` alone is never enough: ownership = same-context creation + objective + own identity/workspace + binding readback + later exact worker receipt + the matching actual `worker_done`. `worker-show` exposes no public caller-proof field, so those stay unknown and are never claimed. A non-structured (terminal/legacy) coordinator must still match its actual coordinator field in the full Run record; never synthesize handles, never use `--from`, `--terminal`, `--takeover-legacy`, forged ORCA env, private RPC, or a database, and never invent field names, caller addresses, or flags. A launch receipt is extra evidence when available, never a required input or manual user step. For a LATER handoff in the same flow (next ticket, repair writer, host reviewer), reuse the retained own Run instead of the NEW-Run steps: `run-current --json` must equal the retained own Run ID, the retained creation/objective/identity/workspace evidence and the previous worker's retained receipt plus its matching successful `worker_done` must exist, then `worker-start --run <that same ID>` with exact `W` and the same receipt validation before ending the turn. An unknown, different, or unretained existing Run, a missing prior receipt or completion, or a failed or mismatched prior `worker_done` means STOP; this adds no `run-use` path beyond the empty-binding rule above. Never use `--from` to borrow another chat. Orca creates the linked `W`; `af worktree create --reuse-current` takes over that same clean `W` (only if no flow worktree exists yet).

Config prep is mandatory and automatic for EVERY new native Orca Claude flow, before `af state start`. af has no command that prints the effective config, so resolve it the way af does (`--config`, `$AF_CONFIG`, `<primary checkout>/af.config.json`, then `$HOME/.config/af/af.config.json`; a project `roles` map replaces the global one whole) and write a fixed private copy outside `W` and any `af/*` worktree. Keep every role map, candidate order, limit, risk path, and valid project test command; add `agent` (`claude` for Anthropic, `codex` for OpenAI) only to host candidates with that explicit company pair, never guessed. Start with `af state start ... --config <copy>` (example below). In a new empty canary repo, pin the user-authorized real unittest `tests.focused` and `tests.full` commands in that same copy before start (other fields unchanged); do not keep a nonexistent `scripts/ci.sh`. Never change the global config or an active flow's config, and add no helper, controller, registry, scheduler, or manual switch. If prep cannot annotate a host candidate the flow may select (no known explicit company pair), stop fail-closed before `af state start` and report; never start the flow with that candidate. Only an existing pinned flow whose fixed config predates this rule and gives `agent: null` keeps its original config and the old host path (fresh `wf-implementer` via the `Agent` tool above), which is explicitly not a native Orca handoff and never applies to a new native flow.

The native Task or Dispatch preamble identifies a dispatched worker, not intrinsically a writer: the af role and the returned prompt decide writer versus read-only reviewer, and neither may start host handoff, `af select`, `af dispatch`, or `worker-start`.

Only a successful `af dispatch` result with `via: host` whose `agent`, company, and `worktree` match is the native handoff. A new native flow whose `af dispatch` returns `via: host` with `agent: null`, or an `agent`, company, or `worktree` that does not match, stops fail-closed with that evidence: do not start `wf-implementer` or `Agent`, do not invent an agent, and do not redispatch blindly. Save its exact `prompt` as a spec file outside `W`, record `W`'s HEAD, `git status --short --ignored`, and the config digest, then:

```sh
orca orchestration run-current --json   # step (a) of the Native Orca host paragraph: empty, or this chat's own Run for a completed af flow; anything else STOP
orca orchestration run-create --objective "<flow goal>" --json --full
orca orchestration run-current --json   # must equal the new ID; only an EMPTY binding allows one run-use --id "<new ID>" --json and a reread; a different nonempty ID, a failed command, or a still-mismatched reread -> STOP
orca orchestration worker-start --spec-file "$SPEC" --run "<run-id>" --worktree "<exact W selector>" \
  --agent "<agent>" --model "<model>" --json --full
orca orchestration worker-show --dispatch "<dispatch-id>" --json --full
```

For the first handoff of the flow, complete the authenticated session path from the Native Orca host paragraph (step (a) initial `run-current`, `run-create` record, `run-current` readback equal to the new ID) before `worker-start`; a later handoff in the same flow skips `run-create` and reuses the retained own Run (the shell block above is the first-handoff sequence); a null legacy coordinator field is not a failure. After `worker-start`, before ending the turn, validate Run, Task, Dispatch, `W`, and host from the `worker-start` receipt and/or `worker-show`, and retain the receipt. The next af phase requires the matching `worker_done` (sender/assignee, Run, Task, Dispatch, payload status) plus that receipt.

A restricted CLI candidate (including a CLI reviewer af chose) stays an af CLI dispatch with its read-only adapter; it is never converted to a native worker. After `worker-start`, end the turn: the actual `worker_done` wakes the coordinator, which verifies the real diff, tests, and identity in `W` for the matching Run, Task, and Dispatch, then advances af state, review, and gate automatically from Step 3 onward. No manual "continue", polling loop, or scheduler; a Run does not schedule work. Failure, quota, login, or unknown output fails closed with no blind redispatch or API fallback. A local-only flow stops truthfully at its real local gate; never fake a PR, merge, or deploy.

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

Shared local services: a separate worktree does not isolate external resources. Concurrent `af gate test` runs from different worktrees collided on one shared local Postgres (global schemas). Take a lock around every gate test that touches the shared service, in every worktree; a repository whose gate command touches no shared local service does not need the lock. A check-then-start (`pgrep`, then run) is racy, since two flows can both see nothing running. The subshell scopes the trap, releases the lock on success, failure, and INT/TERM/HUP, and, as the block's last command, leaves the gate test's exit status as the block's status (works in bash and zsh, with or without `set -e`). The lock directory holds a `holder` file (pid, flow id, worktree, UTC start); the pid comes from `sh -c 'echo $PPID'` because `$$` inside `( ... )` is the parent shell's pid. A waiter prints the holder to stderr each round and never removes or modifies the lock directory: two waiters could both read the same dead pid, and one would then delete the lock the other had just taken. A stale lock (dead holder, or no `holder` file, e.g. after `kill -9` or a holder from an older block) is only reported, with the command to remove it by hand (`rm -rf "$L"`); the waiter keeps waiting until you do (`flock` is an alternative where installed):

```sh
(
  L="${TMPDIR:-/tmp}/af-gate.lock"
  until mkdir "$L" 2>/dev/null; do
    if read -r p w < "$L/holder" 2>/dev/null; then
      if kill -0 "$p" 2>/dev/null; then echo "af-gate.lock held by pid $p: $w" >&2
      else echo "af-gate.lock holder pid $p is gone; remove the stale lock by hand: rm -rf \"$L\"" >&2; fi
    else echo "af-gate.lock held by an unknown holder (no holder file); if stale: rm -rf \"$L\"" >&2; fi
    sleep 30
  done
  trap 'rm -f "$L/holder"; rmdir "$L"' EXIT
  trap 'exit 130' INT TERM HUP
  sh -c 'echo "$PPID $1 $2 $(date -u +%Y-%m-%dT%H:%M:%SZ)"' sh <id> "$W" > "$L/holder"
  while pgrep -f "[s]cripts/ci.sh|[p]npm run ci" >/dev/null; do sleep 30; done
  s=0
  af gate test --repo "$W" --flow-id <id> --suite full || s=$?
  exit "$s"
)
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

The host decides the tier before the review step (definitions in `rules/hard-rules.md` rule 5, owner 2026-11-12). A diff matching `high_risk_paths` is HIGH and cannot be downgraded; the host may only raise a tier. An unclear tier is MEDIUM. The tier chooses the step:

- **HIGH**: two parallel `big_review`/`small_review` dispatches (standards, behavior) with `--writer-company` set to the author's company, as below. At the milestone the two executions may both be Codex `sol`; cross-company means different from the author's company.
- **MEDIUM**: ONE `small_review` dispatch with `--writer-company`, with a single prompt covering standards and behavior; run it once before merge. af's selection picks the reviewer and it may be another company. Outside af a MEDIUM reviewer may be a fresh-context subagent of the writer's company; inside an af flow a same-company reviewer is not available until af supports a review role for it.
- **LOW**: no dispatch. The host reads the `base_sha...HEAD` diff and records that in the round log.

`--reviews pass` in `gate decide`/`gate merge` means the tier's requirement above was met.

`code-review` in a chain is the name of the flow step, not the skill: inside an af flow the host does not invoke the `code-review` skill, whose own two sub-agents would bypass af dispatch. Review is only the tier-driven `af dispatch` above. Put this severity bar in every review prompt: Mark BLOCKING only what gives a wrong result for a realistic repository state, breaks CI, or loses data; everything else is NON-BLOCKING. Against `base_sha...HEAD`: select a reviewer, dispatch, and read the report. Reviewers come only from `af select`/`af dispatch` with `--writer-company` set to the author's company, never a self-picked CLI such as Gemini (selection rules are in Reference).

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

Parallel reviews: in af versions after e62b82f, each attempt gets its own log files (pid plus a random token, created exclusively), so two read-only review dispatches for the same flow may run in parallel:

```sh
af dispatch --worktree "$W" --flow-id <id> --role <review-role> --writer-company <writer-company> --prompt "$(cat "$S/review-standards.md")" > "$S/review-1.json" & p1=$!
af dispatch --worktree "$W" --flow-id <id> --role <review-role> --writer-company <writer-company> --prompt "$(cat "$S/review-behavior.md")" > "$S/review-2.json" & p2=$!
s1=0; wait "$p1" || s1=$?
s2=0; wait "$p2" || s2=$?
echo "review statuses: $s1 $s2"
```

A bare `wait` returns 0 and hides a failed review, and a bare `wait "$p1"; s1=$?` exits under `set -e` on a failure before `s1=$?` runs (hence `|| s1=$?`). Check each status before reading its report: 0 ok, 75 `waiting_for_quota` (wait `retry_after_minutes`, redispatch that review), 124 timeout, anything else a failure (read the JSON and its `.err.log`, then redispatch).

Older af (e62b82f and before) gave two simultaneous dispatches the same log path and their reports interleaved: on those versions, or when unsure of the version, run reviews one after another. A review never runs alongside a writer dispatch (Step 2).

Done when every report is read and its findings are listed.

## 5. Repair rounds

Collect the findings and fix them in one writer run. A MEDIUM or LOW repair round needs focused tests and no repeated review; MEDIUM's final repair diff is still reviewed by one `small_review` dispatch before the gate decision.

HIGH changes (including configured high-risk paths) need two reviews on every round and the full suite. Loop until both reviews return no blocking findings, one writer per round:

1. Collect every finding from both reviews into ONE writer brief and dispatch one writer (Step 2). Writer fixes test-first.
2. Run focused tests, then commit with `af gate commit` (Step 3).
3. Run the full gate (`--suite full`) on the committed SHA, every round, and record that SHA.
4. Dispatch the two reviews (Step 4) against the exact new diff range. The review prompt names `<prev-reviewed-sha>..HEAD` for the repair plus `base_sha...HEAD` for context, lists the findings being fixed, and asks for each "Fixed at the root cause? Does the new test fail without the fix?" plus "any new defect in this diff?". Example saved in `$S/round-N-review.md`:

   ```text
   Review <prev-reviewed-sha>..HEAD (this repair), with <base_sha>...HEAD for context.
   Findings being fixed:
   1. <finding>
   For each: fixed at the root cause? Does the new test fail without the fix?
   Also: any new defect in this diff?
   Mark BLOCKING only what gives a wrong result for a realistic repository state, breaks CI, or loses data; everything else is NON-BLOCKING.
   ```

5. Log the round (round number, findings, commit SHA with its full-suite result) in `$S/rounds.md`, so later rounds do not reopen settled items.

Before the gate decision, review any final repair diff (MEDIUM: one `small_review` dispatch; LOW: the host reads it).

Done when the required reviews of the latest round (two for HIGH, one for MEDIUM, none for LOW) return no blocking findings and the round log shows every finding closed.

## 6. Decide, PR, merge, clean up

The full suite must pass on the HEAD you decide on. A passing full-suite run on that same HEAD (for example the last high-risk round's committed SHA in `$S/rounds.md`) is reused; run it again only when HEAD differs from that SHA:

```sh
af gate test --repo "$W" --flow-id <id> --suite full
af gate decide --repo "$W" --flow-id <id> --tests pass --reviews pass
```

| Decision | Exit | What to do |
|---|---|---|
| `merge` | 0 | Continue. |
| `ask` | 0 | Continue. The owner approved high-risk and `auto_merge: false` merges on 2026-11-06. High-risk paths still get two reviews per round and the full suite every round, as in section 5. |
| `owner` | 3 | The diff touches payment code (`owner_approval_paths`). Push and open the PR, then stop and ask the owner. After their OK, run `af gate merge` with `--owner-approved`. |
| `blocked` | 2 | Fix the cause (failed tests or reviews, dirty worktree, config error, a base that is not on origin) and decide again. Never merge. |

For `merge` and `ask`, steps 1-3 and the clean-up are mandatory and run in the same turn, without asking; the only stop reasons are in `rules/authorization-scope.md`:

1. Push the branch: `git -C "$W" push origin "af/<id>"`. Do not use `-u`: af never treats a branch as merged because it matches its own remote copy.
2. Open the PR with the `pr` skill (`gh pr create --base <default> --head af/<id>`). In the Claude desktop app, bind it with the `ccd_pr` tool `bind_pr`. Do not poll CI in a loop and do not schedule CI checks or wake-ups; the only allowed wait is the one defined under "The CI wait" below.
3. Merge through the gate, the only af command that merges:

   ```sh
   af gate merge --repo "$W" --flow-id <id> --tests pass --reviews pass
   ```

   After the owner OKs an `owner` decision, run the same command with `--owner-approved`:

   ```sh
   af gate merge --repo "$W" --flow-id <id> --tests pass --reviews pass --owner-approved
   ```

`gate merge` prints a `merge` field and exits:

| Result | Exit | What to do |
|---|---|---|
| `merged` | 0 | Clean up now. |
| `auto` | 0 | Auto-merge is enabled; this does not prove the PR merged. Follow "After `auto` or `queued`" below. Read the rule below before pushing anything else to `af/<id>`. |
| `queued` | 0 | The request was accepted but GitHub has not merged yet (merge queue); this does not prove the PR merged. Follow "After `auto` or `queued`" below. Do not push to `af/<id>`; read the rule below. |
| `waiting_for_ci` | 4 | Nothing was enabled or merged. `gate merge` enters the waiting branch when a check is pending or unregistered (`gate.py:321`); the JSON `reasons` say which (a pending check is not required, a pending check shares its name with another, CI has not registered checks, or auto-merge could not be enabled). Do "The CI wait" below, then re-run `af gate merge` and handle whatever it returns by this table (it can still return `blocked`, `queued`, `auto` or `waiting_for_ci`). The CI wait is already used by then: for `auto` or `queued` read the PR with `gh pr view` instead of waiting again, and a second `waiting_for_ci` is reported to the owner, not waited on again. Report to the owner when a check fails. A reason that the required checks could not be read (`gate.py:331-333`) is not about pending checks: a re-run does not fix it, so report it to the owner at once. |
| blocked or `owner` | 2 or 3 | Stop. The JSON `reasons` say why (missing PR at the tested tip, failing CI, merge refused, owner approval missing). |

**The CI wait** (the only wait, used by `waiting_for_ci` and by "After `auto` or `queued`"): run `gh pr checks <n> --watch --fail-fast` as one background command with a 30-minute time limit set through the host's background-command timeout (macOS has no `timeout` binary; do not require one). The wait happens at most once per flow. No polling loop, no scheduled wake-ups, no second wait. When it ends for any reason, including the limit, read `gh pr view <n> --json state,mergedAt,mergeStateStatus`. If the limit was hit and the PR is not merged, leave the flow `active` and report to the owner which checks are still pending.

After `auto` or `queued`, the PR merges on its own, so do not let new commits reach it unreviewed. After `auto`, run `gh pr merge <n> --disable-auto` before any new push to `af/<id>`. After `queued`, do not push to `af/<id>`: `gh pr merge --disable-auto` (gh 2.101) reports success for a queued PR without dequeuing it. If a change is needed, dequeue first with `gh api graphql -f query='mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } } }' -f id=<PR node id>` and confirm with `gh api graphql -f query='query($id: ID!) { node(id: $id) { ... on PullRequest { isInMergeQueue } } }' -f id=<PR node id>` that `isInMergeQueue` is false (the PR node id comes from `gh pr view af/<id> --json id`). The new head then goes through tests, both reviews, `gate decide` and `gate merge` again (a repair round, or a fix the owner asks for). Auto-fix in the Claude desktop app pushes to the PR on its own, so do not turn it on for an af PR while auto-merge is enabled.

After `auto` or `queued`, the flow is not complete until the PR is actually merged. If "The CI wait" has not been used in this flow yet, do it; if it was already used (for example after `waiting_for_ci`), do not wait again and read `gh pr view <n> --json state,mergedAt,mergeStateStatus` directly. Then: `MERGED`: clean up and complete the flow in that same turn. Anything else: leave the flow `active` and report to the owner the pending checks and what is blocking (failing check, queue, auto-merge not firing).

Clean up once the PR is merged (`merged` from `gate merge`, or `MERGED` after `auto`/`queued`). After `merged`, af has already removed a non-reused flow's worktree (`gate.py:355-361`), so `W` may be gone: run every later command from the primary checkout `P` (`--repo "$P"`), never `"$W"`. A reused worktree still exists and either path works there.

1. Complete the remaining steps from `P` with `af state advance --repo "$P" --flow-id <id>` where a step is still pending, then `af state complete --repo "$P" --flow-id <id> --result completed` (the `retro` step is in section 7). Required in the same turn; an `active` flow is swept by `af cleanup --worktrees` only when its merge is proven by a MERGED pull request (at its tip, or for its head branch when the branch is already gone), and otherwise is never touched.
2. From the primary checkout `P`, remove the flow worktree and branches: `af worktree remove --repo "$P" --flow-id <id>`, or sweep with `af cleanup --repo "$P" --worktrees`. With a merge proof it also deletes `origin/af/<id>` when the remote still has it at the merged tip. If removal reports it was already done by `gate merge`, that is fine; if it refuses, report why and leave it for the next sweep.
3. In `P`, when it is clean and on the default branch, run `git -C "$P" pull --ff-only`. If that fails because local main has other unpushed commits, report it; never rebase or reset. Then `git -C "$P" fetch --prune origin`.

Stacked PRs (PR B based on PR A's branch):

- Merge A with a merge commit when B is stacked on it: squash rewrites A's commits, so B then shows A's changes again and may conflict.
- After A merges, retarget B with `gh pr edit <B> --base main` (GitHub retargets automatically only when A's branch is deleted on merge), then let CI re-run.
- If A was squashed anyway, rebase B with `git rebase --onto origin/main <A's old tip> af/<B-id>`.
- af's own cleanup: `af worktree remove` treats a merge-commit merge as merged by ancestry and a squash merge only via `gh pr view` `MERGED` with matching `headRefOid`.

Done when the PR is merged (confirmed by `gate merge` `merged`, or `gh pr view` `MERGED` after `auto`/`queued`), the worktree and branches are removed, and the flow status is `complete` (`af state status --repo "$P"`). A flow waiting for the owner's approval (`owner`) is not done: it is stopped, waiting for the owner, and stays `active`.

## 7. Retro

The `retro` skill has `disable-model-invocation: true`, so the Skill tool refuses it inside a flow. The host completes the step by reading the `retro` `SKILL.md` from the skill directory `doctor` searched (the config's `skill_dirs`; default `~/.claude/skills`, `~/.agents/skills`, `<repo>/skills`; for Claude Code usually `~/.claude/skills/retro/SKILL.md`) with the Read tool and following its steps against the flow's session, then:

```sh
af state advance --repo "$P" --flow-id <id>   # only if the step is still pending
af state complete --repo "$P" --flow-id <id> --result completed
```

Use `P`, not `W`: after `merged` the flow worktree is already removed (section 6); a reused worktree still exists and either path works. If retro proposals need edits, they go through a writer like any other change.

## Reference

### Dispatch

Dispatch passes a fixed writer or read-only reviewer role preamble and the prompt to the worker on stdin. stdout and stderr are logged under `<git-common-dir>/.af/logs/<id>/`, one file pair per attempt; use the returned `log_paths` to follow output. A timeout kills the process group. Failover occurs only when HEAD and `git status --porcelain=v1 -z --ignored` are unchanged. If a worker creates commits, dispatch soft-resets them to the starting commit and reports their SHAs.

`dispatch` takes `--worktree`, with `--repo` accepted as an alias for consistency with the other commands; passing both with different values is an error. It always uses the config recorded by `state start` and rejects `--config`.

### Reviewer selection

Reviewer selection prefers a different company when the role has any configured candidate from another company. Same-company review happens only when every candidate for that role has the writer's company; the shipped config has no such role. If all independent candidates stop for quota, dispatch returns `waiting_for_quota` and exits 75; wait `retry_after_minutes` and retry. A missing `pi`, `codex`, or `claude` CLI is skipped; `host` candidates are always available. `AF_NO_HOST=1` makes `host` candidates unavailable so a non-Claude host, or a host without quota for the configured host model, falls through to the next `pi`/`codex`/`claude` candidate.

`host` candidates mean the current host starts its own fresh subagent, so when the host differs from the one the config's `host` models belong to (for example, Pi or Codex with Claude models), use a config whose `host` entries match that host, or list `pi`/`codex`/`claude` reviewer candidates instead.

### Config

Config resolves from `--config`, `$AF_CONFIG`, the primary checkout's `af.config.json`, then `~/.config/af/af.config.json`. Any resolved config path inside a worktree on an `af/*` branch, including the primary checkout, is refused. `state start` records the resolved config path and the SHA-256 of the effective config, including inherited global `roles`, `high_risk_paths` and `owner_approval_paths`; `state advance`/`resume`, `select --flow-id`, `dispatch`, and `gate test`/`commit`/`decide`/`merge` fail closed with `config changed since flow start` if it changes during the flow.

When a project config is selected, it inherits `roles` from `$HOME/.config/af/af.config.json` only when the project omits the `roles` key; a project `roles` map replaces it entirely, with no per-role merge. An invalid inherited role map fails closed as an invalid global config, and selecting a role absent from the resolved map reports `role not configured: <role>`. The global `high_risk_paths` and `owner_approval_paths` baselines are prepended to the project's lists; duplicates keep their last occurrence. Other global fields are not inherited.

Candidate options (`tools`, `env_allow`, `base_url` plus `api_key_command`) are in [`CANDIDATES.md`](CANDIDATES.md) next to this file; read it when writing or reviewing a role's candidates.

### Status and cleanup

```sh
af state status --repo "$W" --flow-id <id>
af state stop --repo "$W" --flow-id <id>
af worktree remove --repo "$R" --flow-id <id>
af cleanup --repo "$R"
af cleanup --repo "$R" --worktrees
```

Worktree removal refuses tracked changes and untracked non-ignored files. It accepts the branch as merged when it is an ancestor of the recorded base, or, for squash merges, when `gh pr view af/<id>` reports `MERGED` with `headRefOid` equal to the local branch tip (an upstream that is the branch's own remote copy, as `push -u` creates, is never a proof); a missing `gh` or failed query refuses removal. Use `--force` only after reviewing the work. With a merge proof, removal also deletes `origin/af/<id>` when the remote branch sits at exactly the local tip (for repositories without `delete_branch_on_merge`); the result reports `remote_branch` (`deleted`, `absent`, `kept: <reason>`, or `no remote`), and `--force` never touches the remote. `af gate merge` is the only af command that merges (see section 6). A reused flow never removes the worktree: removal switches it back to the original branch and deletes `af/<id>`, and refuses a dirty worktree even with `--force`. `cleanup --worktrees` applies the same removal to completed flows, lists the ones it skipped with the reason, and keeps their records for a later retry. Cleanup archives or deletes only completed or stopped records and their logs according to `record_retention`.
