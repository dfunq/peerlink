# peerlink — notes for agents working on this repo

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: layout, tests, rules for changes. This file is the handoff: how a change ships, and the traps that cost retries.

**This repo is public.** Nothing committed may name a real person, company, machine, home folder, ticket key or private project, including in commit messages, PR titles, branch names and release notes. Use made-up names (alex, acme, `/Users/x`). Before every push: `git grep -niE '<the names you know from this machine>'` and `git log -1 --format=%an` (it must be the repo's configured author, not your real name). On macOS, `git grep` and BSD `sed` have no `\b`; use perl for word-boundary edits.

## Shipping a change

1. Branch off `main` (name it after the change, not a ticket key), commit, push, open a PR. `main` has a ruleset: no force push, no deletion. Every PR gets an independent review before merge.
   **Push to this repo, never a fork.** Check `gh api user --jq .login` first. If it isn't the repo owner (`gh repo view --json owner --jq .owner.login`), don't fork and don't open a PR. Write the commit out with `git format-patch -1 -o <folder>` and hand the patch to whoever runs the repo on a machine logged in as the owner. A PR from a fork shows that other account on a public repo.
2. Before merging, check `gh pr view N --json headRepositoryOwner --jq .headRepositoryOwner.login`. If it isn't the repo owner, don't merge: push the commit to a branch here, open a new PR with the same title and body, and close the fork PR as superseded.
3. CI (`.github/workflows/ci.yml`) runs the daemon suite on Ubuntu and macOS (Python 3.9 and 3.13) plus the hooks module. Check for `fail` yourself before merging: `gh pr checks N --watch | tail && gh pr merge` merges even when a check failed, because the pipe hides the exit code.
4. A release bumps the version in all five places listed in CONTRIBUTING.md. In `register.test.tsx` the reload loop's `[[next, true], [current, false]]` must stay one version ahead, so don't blanket-`sed` the old version.
5. Merge, then tag and release on the merge commit:
   ```bash
   git tag -a vX.Y.Z -m vX.Y.Z <merge sha> && git push origin vX.Y.Z
   gh release create vX.Y.Z --target <merge sha> --title X.Y.Z --notes "<what changed for users>"
   ```
6. Install it locally: `command claude plugin marketplace update peerlink` then `command claude plugin update peerlink@peerlink`. The daemon restarts on the new version within ~20 s; agents reload when idle. Check `curl -s localhost:7899/api/health`.

## Tests

- Hooks module: `command claude plugin test plugin` from the repo root. Plain `claude` may be a shell alias with flags, and `claude plugin test` refuses to run through it. `console.log` is hidden there: trace with `$.ui.log` and collect it in an `on('ui.log')` handler. A hook such as `clock.now` registers once, before the first `$` call.
- Daemon: `python3 -m unittest discover -s plugin/daemon`. To run one module, use `discover -s plugin/daemon -k <name>` (running a module directly hits a KeyError in setup). A ship one-liner needs `set -o pipefail`, or a failing suite still ships.
- Scenario tests run the real daemon in its own process with fake agents: `plugin/daemon/rig.py`, `test_scenarios.py`. `Rig(env={"PEERLINK_TEST": "1"})` hands back a web sign-in link; `crash()` is kill -9 plus start; two rigs with different `PEERLINK_HOSTNAME` and a shared token act as two machines.
- A new daemon module beside `peerlinkd.py` must be added to the fingerprint list near the top of `peerlinkd.py`.
- The web UI's JS is a string inside `peerlinkd.py`. Syntax-check it by extracting the `<script>` text and running `node --check`. Click-throughs use Playwright on a rig daemon, in dark mode; save screenshots under the repo (for example `.playwright-mcp/`) and delete them afterwards.
- CI on macOS: arm64 jobs can wait 10+ minutes for a runner, or be cancelled with zero steps. Check the job's step count before calling it a hang, then `gh run rerun <id> --failed`. A real hang fails within 2 minutes with stack traces. GitHub's macOS runners have slow DNS, so anything that blocks on a lookup hangs there first. Known flaky tests: `test_teams` (Py3.9 macOS) and the switching-off case in `test_agents` (Py3.13 macOS); compare with main's run.

## The live daemon: traps

- **Agents replace the daemon.** A daemon whose version matches the installed plugin but whose code differs is replaced by the installed copy within seconds. To live-test repo code, bump the version first so the repo is newer than what's installed.
- After `peerlinkd.py stop`, an agent hook may restart the *old installed* daemon before you `start` yours. Check `/api/health`'s version and the version inside `~/.peerlink/bin/peerlink`; stop and start again if it's stale. `restart` brings back the installed copy, not yours.
- **Don't trigger owner prompts casually.** A key-authenticated web sign-in (`POST /api/web/link`), `restart`, `shutdown` and `rotate` pop a confirmation dialog on the owner's screen and give the agent no link. Agents can't sign in to the live web UI; live web-UI setting changes (including **Install herdr mods** and `herdr_mods_source`) are the owner's to make. Do UI work on a rig daemon.
- For a scratch daemon: `PEERLINK_HOME=<scratch dir> PEERLINK_PORT=7998 python3 plugin/daemon/peerlinkd.py serve`.

## `~/.peerlink` (runtime state)

- `daemon.log`: one line per event (spawns, messages, issues). `grep` it by message id (`m123`), spawn id (`s93`), agent id or name.
- `agents.json`, `config.json`, `tasks.json`, `schedules.json`, `spawns.json`, `rules.json`: state files, written atomically through `write_json` (with `.bak` and `.corrupt` copies). Don't hand-edit them while the daemon runs; it will overwrite you.
- `issues.jsonl`: the Feedback queue (`report_issue`). An issue is closed by appending a line with its id and `"status": "done"` plus a note.
- `messages.jsonl` (+ `.1` rotation), `terminations.jsonl`, `backups/` (what Install herdr mods replaced).
- Several agents can share one folder: never look an agent up by folder alone.

## herdr

- peerlink must never require herdr. The `hc` launcher is used only when herdr is installed and `use_herdr` is on.
- peerlink's own herdr-app panels (`peerlink-spaces`, `peerlink-agents`) are generated from `plugin/daemon/herdr_mod.py`. A new release overwrites any live-patched copy in `~/.config/herdr-app/mods/`. herdr-app keeps the agents-list sort in memory, so editing `shared.json` while it runs has no effect. A `turn.step` hook must be an `async function*`.
- Restart an agent in its herdr pane: SIGTERM its `claude` process, then `hc send <terminal> ...` with `HC_TERMINAL_ID` unset.
- The user's other herdr mods live in [herdr-mods](https://github.com/dfunq/herdr-mods), which has its own release flow (see its CLAUDE.md).

## Git

- In a rebase, `git checkout --theirs`/`--ours` takes a whole file and drops the other side's changes. Resolve only inside the conflict hunks (usually the version lines).
