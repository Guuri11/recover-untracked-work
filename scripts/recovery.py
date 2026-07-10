#!/usr/bin/env python3
"""
Recover files lost via `git clean`, an editor "discard untracked", or any
accidental deletion of files that were NEVER committed to git — by mining
Claude Code's own session logs (~/.claude/projects/**/*.jsonl), which store
the exact content of every Write/Edit tool call.

This only works for files that were created/edited through Claude Code on
this machine. It is a mechanical first pass: it does NOT know which
deletions were intentional (a refactor) vs accidental. Always cross-check
candidates with `git log --all --full-history -- <path>` and by grepping the
current codebase for references before trusting a reconstruction.

Usage:
  recover.py scan [--repo-root PATH]
      List files ever Written/Edited under PATH via Claude Code sessions
      that no longer exist on disk.

  recover.py reconstruct <path> [<path> ...] [--write] [--repo-root PATH]
      Replay Write/Edit events chronologically for each path and print (or
      write, with --write) the reconstructed content. Reports any Edit that
      could not be applied (old_string not found in the replayed state) —
      those files need manual review, see `history` and `reads` below.

  recover.py history <path> [--repo-root PATH]
      Print every Write/Edit event for a path in chronological order, with
      session file and whether the replay could apply it. Use this to find
      exactly where a reconstruction diverges from reality.

  recover.py reads <path> [--repo-root PATH]
      Print every Read tool_result for a path — these are real on-disk
      snapshots at specific points in time, and are more trustworthy than a
      replayed Edit chain when the chain has diverged (e.g. because of an
      auto-formatter running between sessions).
"""

import argparse
import json
import glob
import os
import sys


def project_dirs_for_repo(repo_root: str) -> list[str]:
    """Claude Code encodes the cwd as a dash-joined dir name under
    ~/.claude/projects/. A repo worked on from its root AND from
    subdirectories (e.g. a monorepo) produces multiple such dirs, all
    prefixed by the repo root's encoding. Match all of them."""
    home = os.path.expanduser("~")
    base = os.path.join(home, ".claude", "projects")
    repo_root = os.path.abspath(repo_root)
    prefix = repo_root.replace("/", "-")
    if not os.path.isdir(base):
        return []
    return [
        os.path.join(base, name)
        for name in os.listdir(base)
        if name.startswith(prefix) and os.path.isdir(os.path.join(base, name))
    ]


def all_session_files(repo_root: str) -> list[str]:
    files = []
    for d in project_dirs_for_repo(repo_root):
        files.extend(glob.glob(os.path.join(d, "*.jsonl")))
    return files


def iter_events(session_files, names, target_paths=None):
    """Yields (timestamp, session_file, tool_use_id, name, input) for
    matching tool_use blocks, plus builds a tool_use_id -> tool_result map.
    Two passes are needed per file since results follow uses in the log."""
    tool_results = {}
    events = []
    for fp in session_files:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = obj.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                if obj.get("type") == "assistant":
                    for item in content:
                        if not (isinstance(item, dict) and item.get("type") == "tool_use"):
                            continue
                        if item.get("name") not in names:
                            continue
                        inp = item.get("input", {})
                        fpath = inp.get("file_path")
                        if target_paths is not None and fpath not in target_paths:
                            continue
                        events.append((obj.get("timestamp"), fp, item.get("id"), item.get("name"), inp))
                elif obj.get("type") == "user":
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            tool_results[item.get("tool_use_id")] = item
    events.sort(key=lambda e: (e[0] or "", e[1]))
    return events, tool_results


def cmd_scan(repo_root: str):
    session_files = all_session_files(repo_root)
    if not session_files:
        print(f"No Claude Code session logs found for {repo_root!r}", file=sys.stderr)
        return
    events, _ = iter_events(session_files, {"Write", "Edit", "NotebookEdit"})
    repo_root_abs = os.path.abspath(repo_root)
    written = sorted({
        e[4].get("file_path") for e in events
        if e[4].get("file_path", "").startswith(repo_root_abs)
    })
    missing = [p for p in written if p and not os.path.exists(p)]
    print(f"{len(session_files)} session file(s) scanned, {len(written)} path(s) ever touched, "
          f"{len(missing)} missing from disk:\n")
    for p in missing:
        print(p)


def replay(session_files, target_path):
    events, tool_results = iter_events(session_files, {"Write", "Edit"}, {target_path})
    state = None
    skips = []
    timeline = []
    for ts, session, tuid, name, inp in events:
        res = tool_results.get(tuid, {})
        is_error = bool(res.get("is_error"))
        entry = {"ts": ts, "session": os.path.basename(session), "name": name, "error": is_error}
        if is_error:
            timeline.append({**entry, "applied": False, "reason": "tool call itself errored"})
            continue
        if name == "Write":
            state = inp.get("content", "")
            timeline.append({**entry, "applied": True})
            continue
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        replace_all = inp.get("replace_all", False)
        if state is None:
            timeline.append({**entry, "applied": False, "reason": "no prior Write in replay"})
            skips.append(entry)
            continue
        if old and state.count(old) == 0:
            timeline.append({**entry, "applied": False, "reason": f"old_string not found (len={len(old)})"})
            skips.append(entry)
            continue
        state = state.replace(old, new) if replace_all else state.replace(old, new, 1)
        timeline.append({**entry, "applied": True})
    return state, skips, timeline


def cmd_reconstruct(repo_root: str, paths: list[str], write: bool):
    session_files = all_session_files(repo_root)
    for path in paths:
        abspath = os.path.abspath(path)
        content, skips, _ = replay(session_files, abspath)
        if content is None:
            print(f"[{path}] no Write event found — nothing to reconstruct", file=sys.stderr)
            continue
        if skips:
            print(f"[{path}] WARNING: {len(skips)} edit(s) could not be replayed — "
                  f"content may be STALE. Run `history` and `reads` on this path before trusting it.",
                  file=sys.stderr)
            for s in skips:
                print(f"    skipped {s['name']} @ {s['ts']} ({s['session']})", file=sys.stderr)
        if write:
            if os.path.exists(abspath):
                print(f"[{path}] already exists on disk — refusing to overwrite, skipping write", file=sys.stderr)
            else:
                os.makedirs(os.path.dirname(abspath), exist_ok=True)
                with open(abspath, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"[{path}] written ({len(content)} bytes)")
        else:
            print(f"===== {path} =====")
            print(content)


def cmd_history(repo_root: str, path: str):
    session_files = all_session_files(repo_root)
    abspath = os.path.abspath(path)
    _, _, timeline = replay(session_files, abspath)
    if not timeline:
        print(f"No Write/Edit events found for {path}", file=sys.stderr)
        return
    for e in timeline:
        status = "OK" if e["applied"] else f"SKIP ({e.get('reason')})"
        print(f"{e['ts']} | {e['session']} | {e['name']} | {status}")


def cmd_reads(repo_root: str, path: str):
    session_files = all_session_files(repo_root)
    abspath = os.path.abspath(path)
    events, tool_results = iter_events(session_files, {"Read"}, {abspath})
    if not events:
        print(f"No Read events found for {path}", file=sys.stderr)
        return
    for ts, session, tuid, name, inp in events:
        res = tool_results.get(tuid, {})
        content = res.get("content")
        text = ""
        if isinstance(content, list):
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        elif isinstance(content, str):
            text = content
        print(f"===== {ts} | {os.path.basename(session)} | tool_use_id={tuid} =====")
        print(text[:4000])
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan")

    p_reconstruct = sub.add_parser("reconstruct")
    p_reconstruct.add_argument("paths", nargs="+")
    p_reconstruct.add_argument("--write", action="store_true")

    p_history = sub.add_parser("history")
    p_history.add_argument("path")

    p_reads = sub.add_parser("reads")
    p_reads.add_argument("path")

    args = parser.parse_args()

    if args.cmd == "scan":
        cmd_scan(args.repo_root)
    elif args.cmd == "reconstruct":
        cmd_reconstruct(args.repo_root, args.paths, args.write)
    elif args.cmd == "history":
        cmd_history(args.repo_root, args.path)
    elif args.cmd == "reads":
        cmd_reads(args.repo_root, args.path)


if __name__ == "__main__":
    main()
