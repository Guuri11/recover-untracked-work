---
name: recover-untracked-work
description: "Use when the user reports losing work that was never committed to git — accidental `git clean`, a `lazygit` discard, `git checkout .` wiping new files, an editor 'discard changes' action, or any deletion of untracked files/directories. Reconstructs the lost files from Claude Code's own session logs, which store the exact content of every Write/Edit tool call made while working on the project. Trigger phrases: 'he borrado los untracked', 'perdí mi trabajo', 'lost my changes', 'git clean wiped my files', 'accidentally discarded untracked files'."
---

# Recover untracked work from Claude Code session logs

## When this applies

The user lost files that were **never committed to git** (so `git status`,
`git stash`, `git fsck --lost-found` and the reflog are all dead ends — git
never had a copy). This is recoverable **only if the lost files were
created or edited through Claude Code** on this machine: every session is
logged as JSONL under `~/.claude/projects/<encoded-cwd>/*.jsonl`, and every
`Write`/`Edit` tool call in that log contains the full file content (or the
exact before/after strings) at the moment it happened.

If the work was done by hand, in another editor, or on another machine,
this technique cannot help — say so plainly instead of trying.

## First, rule out easier wins (cheap, do these before touching logs)

1. `git status` — confirm the loss is really untracked files, not staged/tracked ones (those have real recovery paths: reflog, `git fsck --unreachable`, stash).
2. System trash: `~/.local/share/Trash/files/` (Linux), or the OS equivalent.
3. Editor local history (VSCode/Zed sometimes keep unsaved-file snapshots) and `*.swp` vim swap files.
4. Only once these come up empty, move to session-log recovery below.

## The recovery process

### 1. Locate the session logs

Claude Code encodes the working directory as a dash-joined path under
`~/.claude/projects/`. A monorepo worked on from its root AND from
subdirectories produces **multiple** project dirs, all sharing the repo
root's prefix — scan all of them, not just the one matching the exact cwd.

```bash
python3 ~/.claude/skills/recover-untracked-work/scripts/recover.py scan --repo-root /path/to/repo
```

This lists every file ever touched by `Write`/`Edit` in those logs that no
longer exists on disk. **This list is a first pass, not a verdict** — see
step 2.

### 2. Separate real losses from legitimate history (the part that needs judgment)

For each candidate, before restoring it:

- **Check git history**: `git log --all --oneline --full-history -- <path>`. If it has commits, it was tracked and removed intentionally at some point (a real commit, not the accident) — skip it.
- **Check for intentional deletion in the logs themselves**: grep the session files for `Bash` tool calls containing `rm `, `git rm`, or `mv ` mentioning the file's basename — if found, you (Claude, in a prior session) deleted it on purpose as part of a refactor. Skip it.
- **Check if it's superseded**: grep the *current* codebase for references/imports to the file. If nothing imports it and a similarly-named file already exists and does the job, it was probably renamed, not lost.
- Only files that are (a) absent from git history, (b) never deliberately `rm`'d in the logs, and (c) still referenced by (or clearly load-bearing for) the current code are real candidates for restoration.

### 3. Reconstruct

```bash
python3 ~/.claude/skills/recover-untracked-work/scripts/recover.py reconstruct <path> [<path> ...] --write --repo-root /path/to/repo
```

This replays every `Write`/`Edit` event for each path in chronological
order and writes the final state. It refuses to overwrite a file that
already exists, so it's safe to run broadly.

**It will warn you when an `Edit` couldn't be replayed** (its `old_string`
didn't match the replayed state — usually because an auto-formatter, a
different session, or a manual edit touched the real file in a way the log
replay can't see). A warning means the written content is a stale
intermediate version, not the true final one. Don't treat "the script ran
without warnings" as proof of correctness either — always verify with step 4.

### 4. When replay diverges, use `Read` snapshots as ground truth

A replayed `Edit` chain is a simulation; a `Read` tool call's result is a
literal disk snapshot at that exact moment — more trustworthy when they
disagree.

```bash
python3 ~/.claude/skills/recover-untracked-work/scripts/recover.py history <path> --repo-root /path/to/repo
python3 ~/.claude/skills/recover-untracked-work/scripts/recover.py reads <path> --repo-root /path/to/repo
```

`history` shows exactly where the chain broke. `reads` shows real snapshots
around that point. For the last few edits after the most reliable
checkpoint, open the specific skipped `Edit` events (their `old_string`/
`new_string` are still in the log) and apply them by hand with the `Edit`
tool, using surrounding context to place them correctly.

### 5. Verify — don't trust the reconstruction, exercise it

Run whatever this project uses to prove code is correct: typecheck, lint,
test suite. Treat every remaining error as a signal, not noise:

- "Cannot find module X" → X is still missing; scan again, it may have been created in a session outside the date range you first checked (session file mtimes lie about *when the file was created* — always scan the full history, not just "recent" sessions).
- A test expects behavior the reconstructed code doesn't have → the test is
  the spec (this codebase practices TDD) — trust the test, fix the
  implementation to match, using surrounding restored code as a style
  reference.
- An import name or i18n key doesn't exist where expected → check nearby
  already-correct files/locale entries for the real name; a stale
  reconstruction commonly has an old identifier that was later renamed.

Only stop when the project's own quality gate (typecheck + tests, at
minimum) is green.

### 6. Protect the recovered work immediately

Once verified, `git add` the recovered files (with the user's OK) so a
repeat of the same mistake can no longer destroy them — `git clean` and
friends only touch untracked files.

## Key insight to remember

Claude Code session logs are, incidentally, a complete undo history of
every file Claude has ever touched in a project — independent of git. This
works because the logs store literal tool inputs (`Write.content`,
`Edit.old_string`/`new_string`) and literal tool outputs (`Read` results),
not summaries. The technique generalizes beyond "I ran `git clean`" to any
scenario where disk state and Claude's own memory of the project diverge.
