"""Opt-in automatic loop: `memlet init --hooks` (installer) and the hidden runtime `memlet admin hook <event>`.

Installer
    Merges four Claude Code hook entries (SessionStart, PostToolUse on Write|Edit, Stop, PreCompact) into
    `<repo>/.claude/settings.local.json` (scope `local`, personal) or `<repo>/.claude/settings.json` (scope `project`,
    shared). It never replaces the file: every other key and every foreign hook stays where it was. A Memlet entry is
    recognised by its command (`... admin hook <event>`), and an install record is kept under `hooks.installed` in
    `.memlet/config.json`, so a second run changes nothing and `--hooks-remove` removes exactly Memlet's entries.
    Malformed settings, symlinked `.claude` / settings files and non-object JSON are refused without any write. A
    backup `<name>.bak-<UTC timestamp>` is written before the first change to a file whose bytes Memlet did not
    write itself; writes are atomic. `--git-hooks` additionally writes `pre-commit` (warn only) and `post-merge`
    (one-line re-check) into the hooks directory git uses (`core.hooksPath` honoured); a hook file Memlet did not
    write is never touched.

Runtime
    Reads the hook payload (JSON) on stdin and writes at most one JSON object on stdout. It never blocks or breaks a
    session: every error, a timeout, malformed stdin or a directory without `.memlet/` ends with exit 0 and no output
    (one line goes to the size-capped `.memlet/hooks.log`, never transcript text). No network, no model call, no
    `.env` read; lessons go to the PENDING queue only (nothing is approved or promoted by a hook).
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HOOK_PROTOCOL = 1
# (Claude Code event, runtime event, timeout seconds, matcher)
CLAUDE_EVENTS: Tuple[Tuple[str, str, int, Optional[str]], ...] = (
    ("SessionStart", "session-start", 10, None),
    ("PostToolUse", "post-tool-use", 10, "Write|Edit"),
    ("Stop", "stop", 20, None),
    ("PreCompact", "pre-compact", 20, None),
)
RUNTIME_EVENTS = tuple(e[1] for e in CLAUDE_EVENTS)
GIT_HOOKS: Tuple[Tuple[str, str], ...] = (("pre-commit", "git-pre-commit"), ("post-merge", "git-post-merge"))
GIT_EVENTS = tuple(g[1] for g in GIT_HOOKS)
SCOPES = {"local": ".claude/settings.local.json", "project": ".claude/settings.json"}
PROGRAM_NAMES = ("memlet", "ieum-memory", "prism-memory", "deltaharness")
GIT_MARKER = "# memlet-managed-hook v1"
# Appended to every written command: whatever the installed `memlet` does (an older release without this entry exits
# 2 from argparse, which a Stop hook would treat as "block"), the shell reports success to Claude Code.
NEVER_FAIL_SUFFIX = " || true"
CONFIG_KEY = "hooks"

DEFAULT_SESSION_TOKENS = 400
DEFAULT_MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
MAX_STDIN_BYTES = 16 * 1024 * 1024
# Self-imposed deadlines, below the timeouts written into settings (10 s / 20 s) and below git's patience.
DEADLINES = {"session-start": 7.0, "post-tool-use": 7.0, "stop": 16.0, "pre-compact": 16.0,
             "git-pre-commit": 20.0, "git-post-merge": 20.0}
LOG_REL = ".memlet/hooks.log"
LOG_CAP_BYTES = 64 * 1024
_EVENT_NAMES = {e[1]: e[0] for e in CLAUDE_EVENTS}
_FLAG_STATES = ("stale", "compatible-unverified")
_MAX_LISTED = 8


class Refused(Exception):
    """The installer cannot proceed safely; nothing has been written."""


# ---------------------------------------------------------------------------------------------------------------
# small helpers


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _umask_mode(base: int) -> int:
    old = os.umask(0)
    os.umask(old)
    return base & ~old


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Temp file in the same directory, fsync, chmod, replace. Refuses to replace a symlink."""
    if path.is_symlink():
        raise Refused(f"refusing to write through a symlink: {path}")
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".memlet-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _strict_json(raw: bytes) -> Any:
    """JSON the way Claude Code reads it: UTF-8 without BOM, no NaN/Infinity, no duplicate keys (a merge would
    silently drop one of them)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("the file starts with a UTF-8 byte-order mark")
    text = raw.decode("utf-8")

    def pairs(items):
        out: Dict[str, Any] = {}
        for k, v in items:
            if k in out:
                raise ValueError(f"duplicate key {k!r}")
            out[k] = v
        return out

    def constant(name):
        raise ValueError(f"non-standard JSON constant {name}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _dump_settings(doc: Dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _same_doc(a: Any, b: Any) -> bool:
    """Equal including key order (dict equality ignores order)."""
    return json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def _git(root: Path, args: List[str], timeout: float = 10.0) -> Optional[str]:
    try:
        r = subprocess.run(["git"] + args, cwd=str(root), capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "surrogateescape")


def default_cli_path() -> Path:
    """`memory_cli.py` sits next to the `prism_memory` package, both in a checkout and in an installed wheel."""
    return Path(__file__).resolve().parent.parent / "memory_cli.py"


def memlet_hook_event(hook: Any) -> Optional[str]:
    """The runtime event of a hook entry Memlet wrote, else None. Recognised forms (as written by the installer):
    `memlet admin hook <event>` and `<abs interpreter> <abs .../memory_cli.py> admin hook <event>`, optionally
    followed by `|| true`."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return None
    cmd = hook.get("command")
    if not isinstance(cmd, str) or "admin hook" not in cmd:
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if tokens[-2:] == ["||", "true"]:
        tokens = tokens[:-2]
    if len(tokens) < 4 or tokens[-3:-1] != ["admin", "hook"] or tokens[-1] not in RUNTIME_EVENTS:
        return None
    head = tokens[:-3]
    if len(head) == 1 and head[0] in PROGRAM_NAMES:
        return tokens[-1]
    if (len(head) == 2 and os.path.isabs(head[0]) and os.path.isabs(head[1])
            and os.path.basename(head[1]) == "memory_cli.py"):
        return tokens[-1]
    return None


# Variables a launcher may export for this process only; Claude Code / git will not have them when they run the hook.
_LAUNCHER_ENV = ("PYTHONHOME", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")


def probe_command(prefix: str, cwd: Path) -> bool:
    """Run `<prefix> admin hook probe` the way Claude Code runs a hook (through /bin/sh, without this process's
    launcher variables) and check that it answers {"memletHook": HOOK_PROTOCOL}. Proves the quoting and the
    interpreter before anything is written."""
    env = {k: v for k, v in os.environ.items() if k not in _LAUNCHER_ENV}
    line = f"{prefix} admin hook probe"
    argv = ["/bin/sh", "-c", line] if os.path.exists("/bin/sh") else shlex.split(line)
    try:
        r = subprocess.run(argv, cwd=str(cwd), input=b"", capture_output=True, timeout=30, env=env)
        doc = json.loads(r.stdout.decode("utf-8", "replace").strip() or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return r.returncode == 0 and isinstance(doc, dict) and doc.get("memletHook") == HOOK_PROTOCOL


def resolve_command(scope: str, root: Path, *, which: Callable[[str], Optional[str]] = shutil.which,
                    interpreter: Optional[str] = None, cli_path: Optional[str] = None) -> Dict[str, str]:
    """The command prefix written before `admin hook <event>`; every candidate is probed before it is chosen.

    project: always the portable `memlet` (the file is shared), which must be on PATH and support the hook entry.
    local: `memlet` when that works, otherwise an absolute interpreter and the absolute memory_cli.py, shell-quoted
    (this interpreter first, then `python3` from PATH)."""
    exe = which("memlet")
    works = bool(exe) and probe_command("memlet", root)
    if scope == "project":
        if not exe:
            raise Refused("--hooks-scope project writes the portable command `memlet admin hook <event>` into the shared "
                          ".claude/settings.json, but `memlet` is not on PATH. Install memlet so `memlet` is on PATH "
                          "for everyone using this repository, or use --hooks-scope local.")
        if not works:
            raise Refused(f"`memlet` on PATH ({exe}) does not answer `memlet admin hook probe`; upgrade it before "
                          "writing the shared project hooks, or use --hooks-scope local.")
        return {"prefix": "memlet", "form": "portable", "detail": f"memlet on PATH: {exe}"}
    if works:
        return {"prefix": "memlet", "form": "portable", "detail": f"memlet on PATH: {exe}"}
    cli = Path(cli_path) if cli_path else default_cli_path()
    if not cli.is_absolute() or not cli.is_file():
        raise Refused(f"cannot find memory_cli.py at {cli}; put `memlet` on PATH and retry")
    candidates: List[str] = []
    for c in ([interpreter] if interpreter else [sys.executable, which("python3")]):
        if c and os.path.isabs(c) and c not in candidates:
            candidates.append(c)
    why = "memlet is not on PATH" if not exe else f"memlet on PATH ({exe}) does not answer the hook probe"
    for interp in candidates:
        prefix = shlex.quote(interp) + " " + shlex.quote(str(cli))
        if probe_command(prefix, root):
            return {"prefix": prefix, "form": "absolute",
                    "detail": why + f"; using {interp} and memory_cli.py by absolute path"}
    raise Refused(why + "; and no interpreter (" + ", ".join(candidates or ["none found"]) + ") could run "
                  f"{cli} outside this shell's environment. Put `memlet` on PATH and retry.")


def hook_command(prefix: str, event: str) -> str:
    return f"{prefix} admin hook {event}{NEVER_FAIL_SUFFIX}"


# ---------------------------------------------------------------------------------------------------------------
# .memlet/config.json install record


def _read_config(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    p = root / ".memlet" / "config.json"
    if p.is_symlink():
        return None, "refusing a symlinked .memlet/config.json"
    if not p.is_file():
        return None, None
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f".memlet/config.json is unreadable: {exc}"
    return (cfg, None) if isinstance(cfg, dict) else (None, ".memlet/config.json is not an object")


def _write_config(root: Path, cfg: Dict[str, Any]) -> None:
    p = root / ".memlet" / "config.json"
    data = (json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    mode = stat.S_IMODE(p.stat().st_mode) if p.is_file() else _umask_mode(0o666)
    _atomic_write(p, data, mode)


def _installed(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    hooks = (cfg or {}).get(CONFIG_KEY)
    inst = hooks.get("installed") if isinstance(hooks, dict) else None
    return inst if isinstance(inst, dict) else {}


def _record(cfg: Optional[Dict[str, Any]], kind: str, key: Optional[str] = None) -> Dict[str, Any]:
    inst = _installed(cfg).get(kind)
    if key is not None:
        inst = inst.get(key) if isinstance(inst, dict) else None
    return inst if isinstance(inst, dict) else {}


def _set_record(cfg: Dict[str, Any], kind: str, key: Optional[str], value: Optional[Dict[str, Any]]) -> None:
    """Set or (value None) delete one record, dropping empty containers this module created."""
    hooks = cfg.get(CONFIG_KEY)
    if not isinstance(hooks, dict):
        if value is None:
            return
        hooks = cfg[CONFIG_KEY] = {}
    inst = hooks.get("installed")
    if not isinstance(inst, dict):
        if value is None:
            return
        inst = hooks["installed"] = {}
    if key is None:
        if value is None:
            inst.pop(kind, None)
        else:
            inst[kind] = value
    else:
        sub = inst.get(kind)
        if not isinstance(sub, dict):
            if value is None:
                return
            sub = inst[kind] = {}
        if value is None:
            sub.pop(key, None)
            if not sub:
                inst.pop(kind, None)
        else:
            sub[key] = value
    if not inst:
        hooks.pop("installed", None)
    if not hooks:
        cfg.pop(CONFIG_KEY, None)


# ---------------------------------------------------------------------------------------------------------------
# Claude Code settings: plan, then apply


def _load_settings(root: Path, rel: str) -> Tuple[Path, Optional[bytes], Dict[str, Any]]:
    claude_dir = root / ".claude"
    if claude_dir.is_symlink():
        raise Refused(".claude is a symlink; refusing to write through it (nothing changed)")
    if claude_dir.exists() and not claude_dir.is_dir():
        raise Refused(".claude exists but is not a directory (nothing changed)")
    path = root / rel
    if path.is_symlink():
        raise Refused(f"{rel} is a symlink; refusing to write through it (nothing changed)")
    if not path.exists():
        return path, None, {}
    if not path.is_file():
        raise Refused(f"{rel} is not a regular file (nothing changed)")
    raw = path.read_bytes()
    try:
        doc = _strict_json(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Refused(f"{rel} is not valid JSON ({exc}); nothing changed. A broken settings file disables every "
                      "setting in it, so Memlet never writes one: fix the file first.")
    if not isinstance(doc, dict):
        raise Refused(f"{rel} must hold a JSON object, found {type(doc).__name__}; nothing changed")
    return path, raw, doc


def _settings_backups(path: Path, rec: Dict[str, Any]) -> List[Path]:
    seen, out = set(), []
    for item in rec.get("backups") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            p = path.parent / Path(item["path"]).name
            if p not in seen:
                seen.add(p)
                out.append(p)
    for p in sorted(path.parent.glob(path.name + ".bak-*"))[:50]:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return [p for p in out if p.is_file() and not p.is_symlink()]


def plan_claude(root: Path, scope: str, *, remove: bool, prefix: Optional[str],
                cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: what installing (or removing) Memlet's entries would do. Raises Refused."""
    rel = SCOPES[scope]
    path, raw, doc = _load_settings(root, rel)
    rec = _record(cfg, "claude", scope)
    hooks_val = doc.get("hooks")
    if "hooks" in doc and not isinstance(hooks_val, dict):
        if remove:
            return {"path": rel, "op": "unchanged", "before": raw, "after": raw, "record": None, "events": []}
        raise Refused(f"{rel}: `hooks` must be a JSON object, found {type(hooks_val).__name__}; nothing changed")
    new = copy.deepcopy(doc)
    created_events: List[str] = []
    touched: List[str] = []
    if not remove:
        for event, _cli, _t, _m in CLAUDE_EVENTS:
            if event in (hooks_val or {}) and not isinstance(hooks_val[event], list):
                raise Refused(f"{rel}: `hooks.{event}` must be a JSON array, found "
                              f"{type(hooks_val[event]).__name__}; nothing changed")
        created_hooks_key = "hooks" not in new
        hooks = new.setdefault("hooks", {})
        for event, cli_event, timeout, matcher in CLAUDE_EVENTS:
            command = hook_command(prefix or "memlet", cli_event)
            if event not in hooks:
                hooks[event] = []
                created_events.append(event)
            arr = hooks[event]
            found = [(g, i) for g in arr if isinstance(g, dict) and isinstance(g.get("hooks"), list)
                     for i, h in enumerate(g["hooks"]) if memlet_hook_event(h) == cli_event]
            if found:
                group, idx = found[0]
                if group["hooks"][idx].get("command") != command:
                    group["hooks"][idx]["command"] = command  # keep the user's timeout / extra fields
                    touched.append(event)
                for g, i in reversed(found[1:]):  # duplicates of the same Memlet entry
                    del g["hooks"][i]
                    if not g["hooks"]:
                        arr[:] = [x for x in arr if x is not g]
                    touched.append(event)
            else:
                entry: Dict[str, Any] = {"type": "command", "command": command, "timeout": timeout}
                group = {"matcher": matcher, "hooks": [entry]} if matcher else {"hooks": [entry]}
                arr.append(group)
                touched.append(event)
        if not touched:
            new = doc  # byte-identical: nothing to do
        prior_events = [e for e in rec.get("createdEvents") or [] if isinstance(e, str)]
        record = {
            "settings": rel,
            # the form only: an absolute prefix would put this machine's paths into a file teams may commit
            "commandForm": "portable" if prefix in PROGRAM_NAMES else "absolute",
            "createdFile": bool(rec.get("createdFile")) if rec else raw is None,
            "createdDir": bool(rec.get("createdDir")) if rec else not (root / ".claude").exists(),
            "createdHooksKey": bool(rec.get("createdHooksKey")) if rec else created_hooks_key,
            "createdEvents": sorted(set(prior_events) | set(created_events)),
            "backups": list(rec.get("backups") or []),
        }
        if rec.get("writtenSha256"):
            record["writtenSha256"] = rec["writtenSha256"]
    else:
        hooks = new.get("hooks")
        removed_any = False
        if isinstance(hooks, dict):
            for event in list(hooks):
                arr = hooks[event]
                if not isinstance(arr, list):
                    continue
                emptied_by_us = False
                for g in list(arr):
                    if not (isinstance(g, dict) and isinstance(g.get("hooks"), list)):
                        continue
                    mine = [h for h in g["hooks"] if memlet_hook_event(h)]
                    if not mine:
                        continue
                    removed_any = True
                    touched.append(event)
                    g["hooks"] = [h for h in g["hooks"] if not memlet_hook_event(h)]
                    if not g["hooks"]:
                        arr[:] = [x for x in arr if x is not g]
                        emptied_by_us = True
                created = event in (rec.get("createdEvents") or []) if rec else True
                if not arr and emptied_by_us and created:
                    del hooks[event]
            created_key = bool(rec.get("createdHooksKey")) if rec else True
            if not hooks and removed_any and created_key:
                del new["hooks"]
        if not removed_any:
            new = doc
        record = None
    plan: Dict[str, Any] = {"path": rel, "before": raw, "record": record, "events": sorted(set(touched))}
    if new is doc:
        plan.update(op="unchanged", after=raw)
        return plan
    after: Optional[bytes] = _dump_settings(new)
    op = "create" if raw is None else "update"
    if remove:
        restore = next((p for p in _settings_backups(path, rec) if _parsed_equal(p, new)), None)
        if restore is not None:
            after, op = restore.read_bytes(), "restore"
            plan["restoredFrom"] = restore.name
        we_created = bool(rec.get("createdFile")) if rec else not _settings_backups(path, rec)
        if not new and we_created:
            after, op = None, "delete"
    plan.update(op=op, after=after)
    return plan


def _parsed_equal(p: Path, doc: Dict[str, Any]) -> bool:
    try:
        return _same_doc(_strict_json(p.read_bytes()), doc)
    except (OSError, ValueError, UnicodeDecodeError):
        return False


def _diff(rel: str, before: Optional[bytes], after: Optional[bytes]) -> str:
    a = (before or b"").decode("utf-8", "replace").splitlines(keepends=True)
    b = (after or b"").decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, "a/" + rel if before is not None else "/dev/null",
                                        "b/" + rel if after is not None else "/dev/null"))


def needs_backup(before: Optional[bytes], cfg_rec: Dict[str, Any]) -> bool:
    """Back up an existing file unless its bytes are exactly what Memlet last wrote or are already backed up."""
    if before is None or _sha(before) == cfg_rec.get("writtenSha256"):
        return False
    known = {b.get("sha256") for b in cfg_rec.get("backups") or [] if isinstance(b, dict)}
    return _sha(before) not in known


def apply_claude(root: Path, scope: str, plan: Dict[str, Any], cfg_rec: Dict[str, Any]) -> Dict[str, Any]:
    """Write the planned settings change. Returns the new install record (or None when removed)."""
    rel = plan["path"]
    path = root / rel
    actions: List[Dict[str, str]] = []
    record = plan.get("record")
    if plan["op"] == "unchanged":
        actions.append({"op": "unchanged", "path": rel})
        return {"actions": actions, "record": record, "backup": None}
    # Re-check right before writing: a symlink could have appeared, or Claude Code itself may have rewritten the file
    # (it stores "always allow" permissions in settings.local.json); never overwrite a change made since planning.
    _path, current, _doc = _load_settings(root, rel)
    if current != plan["before"]:
        raise Refused(f"{rel} changed while Memlet was preparing the update; nothing was written to it, run the "
                      "command again")
    backup = None
    before = plan["before"]
    if needs_backup(before, cfg_rec):
        backup = path.with_name(f"{path.name}.bak-{_utc_stamp()}")
        n = 0
        while backup.exists() or backup.is_symlink():
            n += 1
            backup = path.with_name(f"{path.name}.bak-{_utc_stamp()}-{n}")
        with open(str(backup), "xb") as fh:
            fh.write(before)
        os.chmod(str(backup), stat.S_IMODE(path.stat().st_mode))
        actions.append({"op": "backup", "path": str(backup.relative_to(root))})
        if record is not None:
            record["backups"] = list(record.get("backups") or []) + [
                {"path": backup.name, "sha256": _sha(before)}]
    if plan["after"] is None:
        path.unlink()
        actions.append({"op": "delete", "path": rel})
        claude_dir = root / ".claude"
        if cfg_rec.get("createdDir") and claude_dir.is_dir() and not any(claude_dir.iterdir()):
            claude_dir.rmdir()
            actions.append({"op": "delete", "path": ".claude/"})
    else:
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
            actions.append({"op": "create", "path": ".claude/"})
        mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else _umask_mode(0o666)
        _atomic_write(path, plan["after"], mode)
        actions.append({"op": plan["op"], "path": rel})
    if record is not None:
        record["writtenSha256"] = _sha(plan["after"] or b"")
    return {"actions": actions, "record": record, "backup": str(backup) if backup else None}


def _gitignore_notice(root: Path, rel: str) -> Optional[str]:
    if _git(root, ["rev-parse", "--is-inside-work-tree"]) is None:
        return None
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=str(root), capture_output=True,
                           timeout=10, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    return (f"`{rel}` is personal and not meant for git, but this repository's ignore rules do not cover it. "
            f"Add `{rel}` to `.gitignore` (Memlet does not edit it for you).")


# ---------------------------------------------------------------------------------------------------------------
# git hooks


def git_hooks_dir(root: Path) -> Tuple[Optional[Path], Optional[str]]:
    """The directory git runs hooks from (`core.hooksPath` honoured), else (None, reason)."""
    if (_git(root, ["rev-parse", "--is-inside-work-tree"]) or "").strip() != "true":
        return None, "not inside a git work tree"
    out = (_git(root, ["rev-parse", "--git-path", "hooks"]) or "").strip()
    if not out:
        return None, "git did not report a hooks directory"
    p = Path(out)
    return (p if p.is_absolute() else root / p), None


def git_hook_script(prefix: str, name: str, event: str) -> str:
    return ("#!/bin/sh\n"
            f"{GIT_MARKER} ({name}): written by `memlet init --hooks --git-hooks`; "
            "`memlet init --hooks-remove --git-hooks` deletes it.\n"
            "# Warns about code-anchored rules touched by this change; it never blocks.\n"
            f"{prefix} admin hook {event} </dev/null{NEVER_FAIL_SUFFIX}\n"
            "exit 0\n")


def _is_memlet_git_hook(p: Path) -> bool:
    if p.is_symlink() or not p.is_file():
        return False
    try:
        head = p.read_bytes()[:4096].decode("utf-8", "replace").splitlines()[:5]
    except OSError:
        return False
    return any(line.startswith(GIT_MARKER) for line in head)


def plan_git(root: Path, prefix: Optional[str], *, remove: bool) -> Dict[str, Any]:
    hooks_dir, why = git_hooks_dir(root)
    plan: Dict[str, Any] = {"hooksDir": str(hooks_dir) if hooks_dir else None, "items": [], "notices": []}
    if hooks_dir is None:
        plan["skipped"] = why
        return plan
    try:
        inside = hooks_dir.resolve().relative_to(root.resolve())
        if inside.parts[:1] != (".git",):
            plan["notices"].append(f"git runs hooks from `{inside}` inside the working tree (core.hooksPath); "
                                   "Memlet's hook files there could be committed.")
    except ValueError:
        pass
    for name, event in GIT_HOOKS:
        p = hooks_dir / name
        item: Dict[str, Any] = {"hook": name, "path": str(p)}
        exists = p.exists() or p.is_symlink()
        mine = _is_memlet_git_hook(p)
        if remove:
            item["op"] = "delete" if mine else ("absent" if not exists else "kept")
            if exists and not mine:
                item["reason"] = "not written by Memlet; left untouched"
        else:
            text = git_hook_script(prefix or "memlet", name, event)
            if exists and not mine:
                item.update(op="skipped", reason="an existing hook not written by Memlet; left untouched (add "
                                                 f"`{prefix or 'memlet'} admin hook {event}` to it yourself)")
            elif mine and p.read_text(encoding="utf-8", errors="replace") == text:
                item["op"] = "unchanged"
            else:
                item.update(op="update" if mine else "create", content=text)
        plan["items"].append(item)
    plan["createDir"] = not hooks_dir.exists()
    return plan


def apply_git(plan: Dict[str, Any]) -> List[Dict[str, str]]:
    actions: List[Dict[str, str]] = []
    if not plan.get("hooksDir"):
        return actions
    d = Path(plan["hooksDir"])
    for item in plan["items"]:
        p = Path(item["path"])
        if item["op"] in ("create", "update"):
            d.mkdir(parents=True, exist_ok=True)
            if p.exists() and not _is_memlet_git_hook(p):  # appeared since planning: never overwrite
                actions.append({"op": "skipped", "path": str(p)})
                continue
            _atomic_write(p, item["content"].encode("utf-8"), 0o755)
            actions.append({"op": item["op"], "path": str(p)})
        elif item["op"] == "delete":
            if _is_memlet_git_hook(p):
                p.unlink()
                actions.append({"op": "delete", "path": str(p)})
        else:
            actions.append({"op": item["op"], "path": str(p)})
    return actions


# ---------------------------------------------------------------------------------------------------------------
# installer entry (`memlet init --hooks` / `--hooks-remove`)


def _render(rep: Dict[str, Any]) -> str:
    title = "Memlet hooks" + (" removal" if rep.get("remove") else "") + (" (dry run)" if rep.get("dryRun") else "")
    lines = [f"## {title}", ""]
    if not rep.get("ok"):
        lines.append(f"- ERROR: {rep.get('error')}")
        return "\n".join(lines) + "\n"
    claude = rep.get("claude") or {}
    lines.append(f"- Claude Code ({rep.get('scope')}): `{claude.get('path')}` → {claude.get('op')}")
    if rep.get("hookCommand"):
        lines.append(f"- Command form: {rep['hookCommand']['form']} — {rep['hookCommand']['detail']}")
        lines.append(f"- Example: `{hook_command(rep['hookCommand']['prefix'], 'session-start')}`")
    for a in rep.get("actions") or []:
        lines.append(f"- {a['op']}: `{a['path']}`")
    git = rep.get("gitHooks")
    if git:
        if git.get("skipped"):
            lines.append(f"- Git hooks SKIPPED: {git['skipped']}")
        for item in git.get("items") or []:
            extra = f" ({item['reason']})" if item.get("reason") else ""
            lines.append(f"- git {item['hook']}: {item['op']} `{item['path']}`{extra}")
    for n in rep.get("notices") or []:
        lines.append(f"- NOTICE: {n}")
    if rep.get("diff"):
        lines += ["", "```diff", rep["diff"].rstrip("\n"), "```"]
    return "\n".join(lines) + "\n"


def hooks_command(repo_path=".", *, remove: bool = False, scope: str = "local", git_hooks: bool = False,
                  dry_run: bool = False, which: Callable[[str], Optional[str]] = shutil.which,
                  interpreter: Optional[str] = None, cli_path: Optional[str] = None,
                  before_apply: Optional[Callable[[], Optional[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Install or remove Memlet's hooks. `before_apply` (used by `init --hooks`) runs after every check passed and
    before the first write; returning a failed report stops the installer without writing."""
    from prism_memory import memlet_wiki as mw
    rep: Dict[str, Any] = {"ok": True, "exitCode": 0, "command": "init --hooks-remove" if remove else "init --hooks",
                           "dryRun": bool(dry_run), "remove": bool(remove), "scope": scope, "notices": []}
    if scope not in SCOPES:
        rep.update(ok=False, exitCode=2, error=f"unknown hooks scope {scope!r} (use local or project)")
        rep["formatted"] = _render(rep)
        return rep
    try:
        root = mw._resolve_root(repo_path)
        cfg, cfg_problem = _read_config(root)
        if cfg_problem:
            rep["notices"].append(cfg_problem + "; the install record is not used")
        cmd = None
        if not remove:
            cmd = resolve_command(scope, root, which=which, interpreter=interpreter, cli_path=cli_path)
            rep["hookCommand"] = cmd
        cplan = plan_claude(root, scope, remove=remove, prefix=cmd["prefix"] if cmd else None, cfg=cfg)
        gplan = None
        if git_hooks:
            git_prefix = cmd["prefix"] if cmd else None
            if cmd and scope == "project":  # git hooks are personal: same choice as the local scope
                git_prefix = resolve_command("local", root, which=which, interpreter=interpreter,
                                             cli_path=cli_path)["prefix"]
            gplan = plan_git(root, git_prefix, remove=remove)
    except Refused as exc:
        rep.update(ok=False, exitCode=2, error=str(exc))
        rep["formatted"] = _render(rep)
        return rep
    rep["repoPath"] = str(root)
    rep["claude"] = {"path": cplan["path"], "op": cplan["op"], "events": cplan["events"]}
    if cplan.get("restoredFrom"):
        rep["claude"]["restoredFrom"] = cplan["restoredFrom"]
    rep["diff"] = _diff(cplan["path"], cplan["before"], cplan["after"]) if cplan["op"] != "unchanged" else ""
    if gplan is not None:
        rep["gitHooks"] = {k: v for k, v in gplan.items() if k != "items"}
        rep["gitHooks"]["items"] = [{k: v for k, v in it.items() if k != "content"} for it in gplan["items"]]
        rep["notices"] += gplan.get("notices") or []
    elif remove:
        hooks_dir, _why = git_hooks_dir(root)
        left = [n for n, _e in GIT_HOOKS if hooks_dir and _is_memlet_git_hook(hooks_dir / n)]
        if left:
            rep["notices"].append("Memlet git hooks remain (" + ", ".join(left) + "); add --git-hooks to remove them")
    if not remove and scope == "local" and cplan["before"] is None:
        note = _gitignore_notice(root, cplan["path"])
        if note:
            rep["notices"].append(note)
    if dry_run:
        changes = cplan["op"] != "unchanged"
        rep["actions"] = []
        if changes and needs_backup(cplan["before"], _record(cfg, "claude", scope)):
            rep["actions"].append({"op": "would-backup", "path": cplan["path"] + ".bak-<UTC timestamp>"})
        rep["actions"].append({"op": "would-" + cplan["op"] if changes else "unchanged", "path": cplan["path"]})
        if gplan is not None:
            rep["actions"] += [{"op": "would-" + it["op"] if it["op"] in ("create", "update", "delete") else it["op"],
                                "path": it["path"]} for it in gplan["items"]]
            changes = changes or any(it["op"] in ("create", "update", "delete") for it in gplan["items"])
        if changes and cfg is not None and not cfg_problem:
            rep["actions"].append({"op": "would-update", "path": ".memlet/config.json"})
        rep["formatted"] = _render(rep)
        return rep
    if before_apply is not None:
        stop = before_apply()
        if stop is not None:
            return stop
        cfg, cfg_problem = _read_config(root)
        try:  # init may have created .memlet/config.json: plan again with the record now available
            cplan = plan_claude(root, scope, remove=remove, prefix=cmd["prefix"] if cmd else None, cfg=cfg)
        except Refused as exc:
            rep.update(ok=False, exitCode=2, error=str(exc))
            rep["formatted"] = _render(rep)
            return rep
    actions: List[Dict[str, str]] = []
    try:
        res = apply_claude(root, scope, cplan, _record(cfg, "claude", scope))
        actions += res["actions"]
        if res["backup"]:
            rep["notices"].append(f"backup of the previous file: `{Path(res['backup']).name}` (not meant for git)")
        git_actions: List[Dict[str, str]] = []
        if gplan is not None:
            git_actions = apply_git(gplan)
            actions += git_actions
        if cfg is not None and not cfg_problem:
            new_cfg = copy.deepcopy(cfg)
            if remove:
                _set_record(new_cfg, "claude", scope, None)
                if gplan is not None:
                    _set_record(new_cfg, "git", None, None)
            else:
                _set_record(new_cfg, "claude", scope, res["record"])
                if gplan is not None and gplan.get("hooksDir"):
                    files = sorted(it["hook"] for it in gplan["items"] if it["op"] in ("create", "update", "unchanged"))
                    _set_record(new_cfg, "git", None, {"files": files} if files else None)
            if new_cfg != cfg:
                _write_config(root, new_cfg)
                actions.append({"op": "update", "path": ".memlet/config.json"})
        elif not remove:
            rep["notices"].append("no readable .memlet/config.json: the install record was not written (run "
                                  "`memlet init` first)")
    except (OSError, Refused) as exc:
        rep.update(ok=False, exitCode=1, error=f"hook installation failed part-way: {exc}")
    rep["actions"] = actions
    rep["formatted"] = _render(rep)
    return rep


def init_hooks_command(repo_path=".", *, remove: bool = False, scope: str = "local", git_hooks: bool = False,
                       dry_run: bool = False, force: bool = False, output_format: str = "json",
                       **kw: Any) -> Dict[str, Any]:
    """`memlet init --hooks`: the normal `init`, then the hooks. `--hooks-remove`: only the removal. Every hook check
    runs before `init` writes anything; `--dry-run` writes nothing at all."""
    from prism_memory.memlet_wiki import init_project
    if remove:
        rep = hooks_command(repo_path, remove=True, scope=scope, git_hooks=git_hooks, dry_run=dry_run, **kw)
        if output_format != "markdown":
            rep.pop("formatted", None)
        return rep
    holder: Dict[str, Any] = {}

    def run_init() -> Optional[Dict[str, Any]]:
        holder["init"] = init_project(repo_path, dry_run=dry_run, force=force, output_format=output_format)
        if not holder["init"].get("ok"):
            holder["init"]["hooks"] = {"ok": False, "error": "init failed; no hook was installed"}
            return holder["init"]
        return None

    if dry_run:
        hooks = hooks_command(repo_path, scope=scope, git_hooks=git_hooks, dry_run=True, **kw)
        if not hooks.get("ok"):
            return _hooks_failure(hooks, output_format)
        stop = run_init()
        if stop is not None:
            return stop
    else:
        hooks = hooks_command(repo_path, scope=scope, git_hooks=git_hooks, before_apply=run_init, **kw)
        if "init" not in holder:
            return _hooks_failure(hooks, output_format)
        if hooks is holder["init"]:
            return hooks
    report = holder["init"]
    report["hooks"] = {k: v for k, v in hooks.items() if k != "formatted"}
    if not hooks.get("ok"):
        report["ok"] = False
        report["exitCode"] = hooks.get("exitCode", 1)
    if output_format == "markdown":
        report["formatted"] = (report.get("formatted") or "").rstrip("\n") + "\n\n" + hooks["formatted"]
    return report


def _hooks_failure(hooks: Dict[str, Any], output_format: str) -> Dict[str, Any]:
    if output_format != "markdown":
        hooks.pop("formatted", None)
    return hooks


# ---------------------------------------------------------------------------------------------------------------
# runtime (`memlet admin hook <event>`)


class _Deadline(BaseException):
    """Raised by the alarm; BaseException so library `except Exception` blocks do not swallow it."""


class _Guard:
    def __init__(self, seconds: float, hard_exit: bool):
        self.start = time.monotonic()
        self.hard_exit = hard_exit
        self.alarm = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
        self.prev = None
        self.backstop: Optional[threading.Timer] = None
        if self.alarm:
            self.prev = signal.signal(signal.SIGALRM, self._on_alarm)
        self.arm(seconds)

    @staticmethod
    def _on_alarm(signum, frame):
        raise _Deadline()

    def arm(self, seconds: float) -> None:
        seconds = max(0.05, seconds)
        if self.alarm:
            signal.setitimer(signal.ITIMER_REAL, seconds)
        if self.hard_exit:  # last resort if the work is stuck where no signal handler can run
            if self.backstop is not None:
                self.backstop.cancel()
            self.backstop = threading.Timer(seconds + 1.5, os._exit, args=(0,))
            self.backstop.daemon = True
            self.backstop.start()

    def close(self) -> None:
        if self.alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.prev if self.prev is not None else signal.SIG_DFL)
        if self.backstop is not None:
            self.backstop.cancel()


def _text(value: Any, limit: int = 4096) -> Optional[str]:
    if isinstance(value, str) and value and len(value) <= limit and "\x00" not in value:
        return value
    return None


def _read_payload(stdin: Any) -> Optional[Dict[str, Any]]:
    """The hook payload, or None when stdin is empty, malformed, oversized or not an object."""
    if stdin is None:
        return None
    with contextlib.suppress(Exception):
        if stdin.isatty():
            return None
    raw = stdin.read(MAX_STDIN_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogateescape")
    if not raw or len(raw) > MAX_STDIN_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _git_toplevel(d: Path) -> Optional[Path]:
    out = _git(d, ["rev-parse", "--show-toplevel"], timeout=5.0)
    return Path(out.strip()).resolve() if out and out.strip() else None


def find_root(payload: Dict[str, Any]) -> Optional[Path]:
    """`cwd` of the payload when it is inside a git work tree, else the process cwd; then the nearest directory (up
    to the work-tree top) holding a real `.memlet/` directory. None when there is none (nothing is created)."""
    start: Optional[Path] = None
    top: Optional[Path] = None
    cwd = _text(payload.get("cwd"))
    if cwd and os.path.isabs(cwd) and Path(cwd).is_dir():
        top = _git_toplevel(Path(cwd))
        if top is not None:
            start = Path(cwd).resolve()
    if start is None:
        start = Path(os.getcwd()).resolve()
        top = _git_toplevel(start)
    d = start
    for _ in range(64):
        m = d / ".memlet"
        if m.is_dir() and not m.is_symlink():
            return d
        if top is None or d == top or d.parent == d:
            return None
        d = d.parent
    return None


def _hook_settings(root: Path) -> Dict[str, Any]:
    cfg, _err = _read_config(root)
    raw = (cfg or {}).get(CONFIG_KEY)
    raw = raw if isinstance(raw, dict) else {}

    def number(key: str, default: float, lo: float, hi: float) -> float:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
            return default
        return v

    return {"tokens": int(number("sessionStartTokenBudget", DEFAULT_SESSION_TOKENS, 50, 4000)),
            "maxTranscriptBytes": int(number("maxTranscriptBytes", DEFAULT_MAX_TRANSCRIPT_BYTES, 1, 1 << 30)),
            "deadline": number("deadlineSeconds", 0.0, 0.1, 60.0)}


def _log(root: Optional[Path], event: str, message: str) -> None:
    """One line in `.memlet/hooks.log` (kept under LOG_CAP_BYTES). Only fixed words and numbers, never payload or
    transcript text. Never creates `.memlet/`; never follows a symlink."""
    if root is None:
        return
    with contextlib.suppress(Exception):
        d = root / ".memlet"
        if not d.is_dir() or d.is_symlink():
            return
        p = root / LOG_REL
        if p.is_symlink():
            return
        safe = re.sub(r"[^A-Za-z0-9 _.,:=/()<>+-]", "?", message)[:240]
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {event} {safe}\n".encode("ascii", "replace")
        if p.is_file() and p.stat().st_size + len(line) > LOG_CAP_BYTES:
            tail = p.read_bytes()[-(LOG_CAP_BYTES // 2):]
            tail = tail[tail.find(b"\n") + 1:] if b"\n" in tail else b""
            _atomic_write(p, tail, 0o600)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(p), flags, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


def _where(exc: BaseException) -> str:
    tb = exc.__traceback__
    frame = None
    while tb is not None:
        frame = tb
        tb = tb.tb_next
    loc = ""
    if frame is not None:
        loc = f" at {os.path.basename(frame.tb_frame.f_code.co_filename)}:{frame.tb_lineno}"
    return f"{type(exc).__name__}{loc}"


def _clean(text: Any, limit: int) -> str:
    from prism_memory.memlet_dream import mask_secrets
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text or ""))).strip()
    s = mask_secrets(s)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _session_start(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from prism_memory import memlet_bridge as mb
    from prism_memory import memlet_wiki as mw
    cap = mw.build_handover(root, output_format="json", write=False, budget=2000, mode="verified")
    active = [a for a in cap.get("activeConstraints") or [] if isinstance(a, dict)]
    health = (cap.get("executionProof") or {}).get("anchorHealth") or {}
    stale = int(health.get("stale") or 0)
    reverify = int(health.get("compatible-unverified") or 0)
    omitted = int((cap.get("omitted") or {}).get("activeConstraints") or 0)
    pend = mb.learn_pending(root)
    pending = int(((pend.get("counts") or {}).get("pendingNodes") or 0)) if pend.get("ok") else 0
    if not active and not stale and not reverify and not pending:
        return None
    head = "Memlet verified rules (code anchors match; source cited):"
    status = (f"Not verified, do not rely on: {stale} stale anchor(s), {reverify} to re-verify, {pending} lesson(s) "
              "pending human approval (`memlet learn --pending`); see `memlet check`.")
    items = [f"- {_clean(a.get('rule'), 160)} ({_clean(a.get('source'), 80)})" for a in active]

    def render(n: int) -> str:
        body = items[:n] if items else ["- (none)"]
        more = len(items) - n + omitted if items else omitted
        tail = [f"- (+{more} more verified: `memlet handover`)"] if more > 0 else []
        return "\n".join([head] + body + tail + ["", status])

    n = len(items)
    text = render(n)
    while n > 0 and mw.estimate_tokens(text) > st["tokens"]:
        n -= 1
        text = render(n)
    if mw.estimate_tokens(text) > st["tokens"]:
        return None  # even the counts do not fit the configured budget
    out: Dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    if stale:
        out["systemMessage"] = f"Memlet: {stale} stale rule anchor(s); run `memlet check` to review."
    return out


def _edited_file(payload: Dict[str, Any], root: Path) -> Optional[Path]:
    ti = payload.get("tool_input")
    tr = payload.get("tool_response")
    fp = _text(ti.get("file_path")) if isinstance(ti, dict) else None
    if fp is None and isinstance(tr, dict):
        fp = _text(tr.get("filePath"))
    if fp is None:
        return None
    p = Path(fp)
    if not p.is_absolute():
        cwd = _text(payload.get("cwd"))
        base = Path(cwd) if cwd and os.path.isabs(cwd) and Path(cwd).is_dir() else root
        p = base / p
    return p


def _flag_lines(affected: List[Dict[str, Any]]) -> List[str]:
    return [f"- {str(a.get('status', '')).upper()} `{_clean(a.get('anchor'), 120)}` ({_clean(a.get('reason'), 60)}): "
            f"\"{_clean(a.get('rule'), 160)}\" — source {_clean(a.get('source'), 80)}" for a in affected[:_MAX_LISTED]]


def _post_tool_use(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from prism_memory import memlet_bridge as mb
    p = _edited_file(payload, root)
    if p is None:
        return None
    res = mb.changed_check(root, files=[str(p)])
    if not isinstance(res, dict) or res.get("exitCode") == 2:
        _log(root, "post-tool-use", "check failed: exitCode=2")
        return None
    flagged = [a for a in res.get("affected") or [] if isinstance(a, dict) and a.get("status") in _FLAG_STATES]
    if not flagged:
        return None
    rel = (res.get("changedFiles") or [p.name])[0]
    stale = sum(1 for a in flagged if a.get("status") == "stale")
    more = len(flagged) - _MAX_LISTED
    text = "\n".join(
        [f"Memlet: the edit to `{_clean(rel, 200)}` affects {len(flagged)} code-anchored rule(s) that are no longer "
         "verified (stale = the anchored code changed; compatible-unverified = re-verify before relying on it):"]
        + _flag_lines(flagged) + ([f"- (+{more} more: `memlet check --changed`)"] if more > 0 else [])
        + ["Re-read each rule at its source and tell the user. If the change is intended, the user decides whether to "
           "re-pin with `memlet check --accept <anchor>`; do not re-pin on your own."])
    first = flagged[0]
    msg = (f"Memlet: {stale} stale / {len(flagged) - stale} re-verify rule(s) after editing {_clean(rel, 80)} "
           f"(e.g. {_clean(first.get('source'), 60)})")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}, "systemMessage": msg}


def _learn(root: Path, payload: Dict[str, Any], st: Dict[str, Any], event: str) -> Optional[Dict[str, Any]]:
    from prism_memory import memlet_bridge as mb
    from prism_memory import memlet_dream as md
    tp = _text(payload.get("transcript_path"))
    if tp is None or not os.path.isabs(tp):
        return None
    p = Path(tp)
    if p.is_symlink() or not p.is_file() or p.suffix != ".jsonl" or p.name.startswith(".env"):
        return None
    if not os.access(str(p), os.R_OK):
        return None
    size = p.stat().st_size
    if size > st["maxTranscriptBytes"]:
        _log(root, event, f"skip: transcript is {size} bytes, over the cap of {st['maxTranscriptBytes']}")
        return None
    previous = md.QUEUE_LOCK_TIMEOUT
    md.QUEUE_LOCK_TIMEOUT = min(float(previous), 3.0)  # never wait long for a concurrent `memlet learn`
    try:
        res = mb.learn_from_transcript(root, str(p))
    finally:
        md.QUEUE_LOCK_TIMEOUT = previous
    if not isinstance(res, dict) or not res.get("ok"):
        code = res.get("exitCode") if isinstance(res, dict) else "?"
        _log(root, event, f"learn failed: exitCode={code}")
        return None
    added = len(res.get("addedNodeIds") or [])
    if not added:
        return None
    return {"systemMessage": f"Memlet: {added} new lesson(s) queued as PENDING (nothing approved); review with "
                             "`memlet learn --pending`."}


def _git_check(root: Path, event: str) -> Optional[str]:
    from prism_memory import memlet_bridge as mb
    if event == "git-pre-commit":
        out = _git(root, ["diff", "--cached", "--name-only", "-z", "--no-renames", "--relative"])
        staged = [f for f in (out or "").split("\0") if f]
        if not staged:
            return None
        res = mb.changed_check(root, files=staged)
        flagged = [a for a in res.get("affected") or [] if a.get("status") in _FLAG_STATES]
        if not flagged:
            return None
        return "\n".join([f"memlet: warning: {len(flagged)} code-anchored rule(s) touched by the staged files are not "
                          "verified (the commit continues):"] + _flag_lines(flagged)
                         + ["memlet: details: `memlet check --changed`"]) + "\n"
    res = mb.changed_check(root, base="ORIG_HEAD")
    if res.get("exitCode") == 2:
        return "memlet: post-merge check skipped (no ORIG_HEAD to compare with)\n"
    affected = res.get("affected") or []
    flagged = [a for a in affected if a.get("status") in _FLAG_STATES]
    stale = sum(1 for a in flagged if a.get("status") == "stale")
    line = (f"memlet: post-merge check: {len(res.get('changedFiles') or [])} changed file(s), {len(affected)} rule(s) "
            f"touched, {stale} stale, {len(flagged) - stale} to re-verify")
    return line + (" — run `memlet check --changed --base ORIG_HEAD`" if flagged else "") + "\n"


def _dispatch(event: str, root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Any:
    if event == "session-start":
        return _session_start(root, payload, st)
    if event == "post-tool-use":
        return _post_tool_use(root, payload, st)
    if event in ("stop", "pre-compact"):
        return _learn(root, payload, st, event)
    return _git_check(root, event)


def run_hook(argv: List[str], *, stdin: Any = None, stdout: Any = None, stderr: Any = None,
             hard_exit: bool = True) -> int:
    """Always returns 0. Writes one JSON object to stdout (Claude Code events) or warning lines to stderr (git
    events), or nothing."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    event = argv[0] if argv and isinstance(argv[0], str) else ""
    if event == "probe" and len(argv) == 1:
        with contextlib.suppress(Exception):
            out.write(json.dumps({"memletHook": HOOK_PROTOCOL}) + "\n")
            out.flush()
        return 0
    if event not in RUNTIME_EVENTS + GIT_EVENTS or len(argv) != 1:
        return 0
    root: Optional[Path] = None
    result: Any = None
    guard: Optional[_Guard] = None
    try:
        guard = _Guard(DEADLINES[event], hard_exit)
        sink = io.StringIO()
        with warnings.catch_warnings(), contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            warnings.simplefilter("ignore")
            if event in GIT_EVENTS:
                payload: Optional[Dict[str, Any]] = {}
            else:
                payload = _read_payload(stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin))
            root = find_root(payload or {})
            if root is not None:
                if payload is None:
                    _log(root, event, "skip: empty, malformed or oversized stdin")
                else:
                    st = _hook_settings(root)
                    if st["deadline"] and st["deadline"] < DEADLINES[event]:
                        guard.arm(st["deadline"] - (time.monotonic() - guard.start))
                    result = _dispatch(event, root, payload, st)
    except _Deadline:
        result = None
        _log(root, event, "timeout: self-imposed deadline reached; no output")
    except BaseException as exc:  # noqa: B036 - a hook must never break the session
        result = None
        _log(root, event, "error: " + _where(exc))
    finally:
        if guard is not None:
            guard.close()
    if result is None:
        return 0
    with contextlib.suppress(Exception):
        if isinstance(result, str):
            err.write(result)
            err.flush()
        else:
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
    return 0
