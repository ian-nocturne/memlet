"""Opt-in automatic loop: `memlet init --hooks` (installer) and the hidden runtime `memlet admin hook <event>`.

Installer
    Merges four Claude Code hook entries (SessionStart, PostToolUse on Write|Edit, Stop, PreCompact) into
    `<repo>/.claude/settings.local.json` (scope `local`, personal) or `<repo>/.claude/settings.json` (scope `project`,
    shared). It never replaces the file: every other key and every foreign hook stays where it was. A Memlet entry is
    recognised by its command (`... admin hook <event>`), and an install record is kept under `hooks.installed` in
    `.memlet/config.json`, so a second run changes nothing and `--hooks-remove` removes exactly Memlet's entries.
    Malformed settings, symlinked `.claude` / settings files and non-object JSON are refused without any write. A
    backup `<name>.bak-<UTC timestamp>` is written before the first change to a file whose bytes Memlet did not
    write itself. A run is all-or-nothing: every destination is checked before the first write, and a later failure
    undoes exactly this run's writes (including what the implicit `init` created) unless another program changed
    them meanwhile, which is reported instead. Each replace is compare-and-swap: the new bytes are written and
    fsynced first, then the target is re-read and compared right before the rename, and an in-place edit made in
    the remaining gap is detected and put back. A program that itself *renames* a new file over the target inside
    that last gap cannot be excluded without a lock both sides honour; Memlet does not claim that guarantee.
    `--git-hooks` additionally writes `pre-commit` (warn only) and `post-merge` (one-line re-check) into the hooks
    directory git uses (`core.hooksPath` honoured). A git hook is Memlet's only when it is an unmodified generated
    script (exact marker line with the SHA-256 of its body) AND listed with those bytes in the install record;
    anything else is reported and never overwritten or deleted.

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
import sqlite3
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
GIT_MARKER = "# memlet-managed-hook v2"
# Appended to every written command: whatever the installed `memlet` does (an older release without this entry exits
# 2 from argparse, which a Stop hook would treat as "block"), the shell reports success to Claude Code.
NEVER_FAIL_SUFFIX = " || true"
CONFIG_KEY = "hooks"

DEFAULT_SESSION_TOKENS = 400
DEFAULT_MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
MAX_STDIN_BYTES = 16 * 1024 * 1024
# Self-imposed deadlines, below the timeouts written into settings (10 s / 20 s) and below git's patience.
# git-pre-commit runs while someone waits for `git commit`: well under two seconds including interpreter start-up;
# on overrun it prints nothing and exits 0.
DEADLINES = {"session-start": 7.0, "post-tool-use": 7.0, "stop": 16.0, "pre-compact": 16.0,
             "git-pre-commit": 1.0, "git-post-merge": 5.0}
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
# transactional writes: compare-and-swap replace, rollback of exactly this run's writes


def _read_regular(path: Path, limit: Optional[int] = None) -> Optional[bytes]:
    """Bytes of a regular file (None when absent); never follows a symlink, never opens a FIFO or device."""
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{path} is not a regular file")
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{path} is not a regular file")
        chunks, left = [], (limit if limit is not None else -1)
        while True:
            chunk = os.read(fd, 1 << 20 if left < 0 else min(1 << 20, left))
            if not chunk:
                break
            chunks.append(chunk)
            if left >= 0:
                left -= len(chunk)
                if left <= 0:
                    break
        return b"".join(chunks)
    finally:
        os.close(fd)


def _mode_of(path: Path) -> Optional[int]:
    try:
        return stat.S_IMODE(os.lstat(str(path)).st_mode)
    except FileNotFoundError:
        return None


class _Txn:
    """The writes of one install/removal run, so a later failure undoes exactly them (newest first). A path whose
    bytes changed after Memlet wrote it is left alone and reported: an intervening edit is never overwritten."""

    def __init__(self) -> None:
        self.steps: List[Tuple[str, Path, Any, Any, Any]] = []

    def file(self, path: Path, prior: Optional[bytes], prior_mode: Optional[int], now: Optional[bytes]) -> None:
        self.steps.append(("file", path, prior, prior_mode, None if now is None else _sha(now)))

    def mode(self, path: Path, prior_mode: int, content: bytes) -> None:
        self.steps.append(("mode", path, prior_mode, _sha(content), None))

    def made_dir(self, path: Path) -> None:
        self.steps.append(("dir", path, None, None, None))

    def made_tree(self, path: Path, digest: Dict[str, Any]) -> None:
        self.steps.append(("tree", path, digest, None, None))

    def rollback(self) -> List[str]:
        left: List[str] = []
        for kind, path, a, b, c in reversed(self.steps):
            try:
                if kind == "file":
                    prior, prior_mode, written = a, b, c
                    try:
                        cur = _read_regular(path)
                    except Refused:
                        cur = b"\0not-regular"
                    if (cur is None and written is None) or (cur is not None and written and _sha(cur) == written):
                        if prior is None:
                            if cur is not None:
                                os.unlink(str(path))
                        else:
                            _atomic_write(path, prior, prior_mode if prior_mode is not None else 0o644)
                    else:
                        left.append(f"{path}: changed by another writer after Memlet wrote it; left as it is now")
                elif kind == "mode":
                    cur = _read_regular(path)
                    if cur is not None and _sha(cur) == b:
                        os.chmod(str(path), a)
                    else:
                        left.append(f"{path}: changed after Memlet restored its executable bit; left as it is now")
                elif kind == "dir":
                    if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                        path.rmdir()
                    elif path.exists():
                        left.append(f"{path}/: created by this run but no longer empty; left in place")
                elif kind == "tree":
                    if _tree_digest(path) == a:
                        shutil.rmtree(str(path))
                    elif path.exists():
                        left.append(f"{path}/: created by this run's `init` but changed since; left in place")
            except (OSError, Refused) as exc:
                left.append(f"{path}: could not be rolled back ({exc})")
        self.steps = []
        return left


def _tree_digest(path: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not path.is_dir() or path.is_symlink():
        return out
    for dirpath, dirnames, filenames in os.walk(str(path)):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(path))
            if p.is_symlink() or not p.is_file():
                out[rel] = "dir" if p.is_dir() and not p.is_symlink() else "other"
            else:
                out[rel] = _sha(p.read_bytes())
    return out


_INIT_FILES_GLOBS = ("*", "wiki/**/*")


def _memlet_files(root: Path) -> Dict[Path, Tuple[Optional[bytes], Optional[int]]]:
    """Files `init` may write inside an existing `.memlet/` (config, ledger, wiki pages), with bytes and mode."""
    m = root / ".memlet"
    out: Dict[Path, Tuple[Optional[bytes], Optional[int]]] = {}
    if not m.is_dir() or m.is_symlink():
        return out
    for pat in _INIT_FILES_GLOBS:
        for p in m.glob(pat):
            if p.is_file() and not p.is_symlink() and (pat != "*" or p.suffix != ".log"):
                out[p] = (p.read_bytes(), _mode_of(p))
    return out


def _cas_write(path: Path, data: bytes, mode: int, expected: Optional[bytes], txn: _Txn, what: str) -> None:
    """Replace `path` with `data` only if it still holds `expected` (None: only if it is still absent).

    Order: the new bytes are written and fsynced to a temp file first; then the current file is hard-linked to a
    recovery name (keeping its inode), re-read and compared; then the temp file is renamed over it. A writer that
    rewrites the file in place inside the remaining gap writes into the recovery inode: that is detected after the
    rename and their bytes are put back. A writer that itself *replaces* the file (rename) inside the last
    check-to-rename gap cannot be excluded without a lock both sides honour: Memlet does not claim that guarantee."""
    if path.is_symlink():
        raise Refused(f"refusing to write through a symlink: {path}")
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".memlet-", dir=str(path.parent))
    recovery: Optional[Path] = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        prior_mode = _mode_of(path)
        if expected is None:
            try:
                os.link(tmp, str(path))  # atomic create-if-absent
            except FileExistsError:
                raise Refused(f"{what} appeared while Memlet was preparing it; nothing was written to it")
            except OSError:  # no hard links here: the closest check-then-rename
                if path.exists() or path.is_symlink():
                    raise Refused(f"{what} appeared while Memlet was preparing it; nothing was written to it")
                os.replace(tmp, str(path))
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            txn.file(path, None, None, data)
            return
        recovery = path.with_name(f"{path.name}.memlet-recovery-{_utc_stamp()}")
        try:
            os.link(str(path), str(recovery))
            linked = True
        except FileNotFoundError:
            raise Refused(f"{what} was removed while Memlet was preparing the update; nothing was written to it")
        except OSError:  # no hard links on this file system: keep a byte copy instead
            current = _read_regular(path)
            with open(str(recovery), "xb") as fh:
                fh.write(current or b"")
            linked = False
        current = _read_regular(path)
        same_inode = (not linked) or (os.lstat(str(path)).st_ino == os.lstat(str(recovery)).st_ino)
        if current != expected or not same_inode:
            raise Refused(f"{what} changed while Memlet was preparing the update; nothing was written to it, run the "
                          "command again")
        os.replace(tmp, str(path))
        txn.file(path, expected, prior_mode, data)
        late = _read_regular(recovery) if linked else expected
        if late != expected:  # rewritten in place between the check and the rename: their bytes win
            if _read_regular(path) == data:
                _atomic_write(path, late, prior_mode if prior_mode is not None else mode)
                txn.steps.pop()
                raise Refused(f"{what} was edited by another program during the update; its edit was put back and "
                              "nothing was installed in it")
            keep = recovery
            recovery = None
            raise Refused(f"{what} was edited by another program during the update; that edit is preserved in "
                          f"{keep.name}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        if recovery is not None:
            with contextlib.suppress(OSError):
                os.unlink(str(recovery))


def _cas_delete(path: Path, expected: bytes, txn: _Txn, what: str) -> None:
    """Delete `path` only if it still holds `expected`; an in-place edit inside the gap is restored."""
    recovery = path.with_name(f"{path.name}.memlet-recovery-{_utc_stamp()}")
    prior_mode = _mode_of(path)
    try:
        os.link(str(path), str(recovery))
    except FileNotFoundError:
        raise Refused(f"{what} was removed by another program; nothing to delete")
    try:
        if _read_regular(path) != expected or os.lstat(str(path)).st_ino != os.lstat(str(recovery)).st_ino:
            raise Refused(f"{what} changed while Memlet was preparing its removal; it was left as it is")
        os.unlink(str(path))
        txn.file(path, expected, prior_mode, None)
        late = _read_regular(recovery)
        if late != expected:
            if not path.exists():
                os.link(str(recovery), str(path))
            txn.steps.pop()
            raise Refused(f"{what} was edited by another program during its removal; the edit was kept")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(str(recovery))


def _make_dirs(d: Path, txn: _Txn) -> None:
    missing = []
    cur = d
    while not cur.exists() and not cur.is_symlink():
        missing.append(cur)
        cur = cur.parent
    for p in reversed(missing):
        p.mkdir()
        txn.made_dir(p)


def _writable(d: Path) -> Optional[str]:
    """None when files can be created and renamed in `d` (or in the nearest existing ancestor that will hold it)."""
    cur = d
    while not cur.exists() and not cur.is_symlink():
        cur = cur.parent
    if cur.is_symlink() or not cur.is_dir():
        return f"{cur} is not a directory"
    if not os.access(str(cur), os.W_OK | os.X_OK):
        return f"{cur} is not writable"
    return None


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
    try:
        raw = _read_regular(path)
    except Refused:
        raise Refused(f"{rel} is not a regular file (nothing changed)")
    try:
        doc = _strict_json(raw or b"")
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise Refused(f"{rel} is not valid JSON ({exc}); nothing changed. A broken settings file disables every "
                      "setting in it, so Memlet never writes one: fix the file first.")
    if not isinstance(doc, dict):
        raise Refused(f"{rel} must hold a JSON object, found {type(doc).__name__}; nothing changed")
    return path, raw, doc


def _settings_style(raw: Optional[bytes]) -> Dict[str, Any]:
    """Indentation, line ending, final newline and separators of the existing file, so a merged file keeps them."""
    if raw is None:
        return {"indent": 2, "nl": "\n", "final": True, "lead": "", "sep": None, "ascii": False}
    text = raw.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    lead = text[:len(text) - len(text.lstrip())]
    body = text.strip()
    style: Dict[str, Any] = {"nl": nl, "final": text.endswith("\n"), "lead": lead, "sep": None, "indent": None,
                             "ascii": bool(re.search(r"\\u[0-9a-fA-F]{4}", text)) and all(ord(c) < 128 for c in text)}
    if "\n" in body:
        m = re.search(r"\n([ \t]+)\S", body)
        style["indent"] = ("\t" if m.group(1).startswith("\t") else len(m.group(1))) if m else 2
    else:  # a one-line file stays one line
        style["sep"] = ("," + (" " if re.search(r",\s", body) else ""), ":" + (" " if re.search(r'":\s', body) else ""))
    return style


def _dump_settings(doc: Dict[str, Any], style: Optional[Dict[str, Any]] = None) -> bytes:
    style = style or _settings_style(None)
    if style["sep"] is not None:
        text = json.dumps(doc, ensure_ascii=style["ascii"], separators=style["sep"])
    else:
        text = json.dumps(doc, ensure_ascii=style["ascii"], indent=style["indent"])
    text = style["lead"] + text.replace("\n", style["nl"]) + (style["nl"] if style["final"] else "")
    return text.encode("utf-8")


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


def _in_shape(group: Any, matcher: Optional[str]) -> bool:
    """Is this hook group where Memlet installs its entry for the event (same matcher)?"""
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return False
    if matcher is None:
        return group.get("matcher") in (None, "", "*")
    return group.get("matcher") == matcher


def _shape_of(event: str) -> Tuple[Optional[str], Optional[str], int]:
    for ev, cli_event, timeout, matcher in CLAUDE_EVENTS:
        if ev == event:
            return cli_event, matcher, timeout
    return None, None, 0


def plan_claude(root: Path, scope: str, *, remove: bool, prefix: Optional[str],
                cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: what installing (or removing) Memlet's entries would do. Raises Refused.

    Memlet's entry for an event is a recognised command (`memlet_hook_event`) for that event, inside a group with
    the event's matcher (`Write|Edit` for PostToolUse, none otherwise). A Memlet-looking command anywhere else is the
    user's customisation: left alone on install and on removal, and listed in `leftAlone`."""
    rel = SCOPES[scope]
    path, raw, doc = _load_settings(root, rel)
    rec = _record(cfg, "claude", scope)
    hooks_val = doc.get("hooks")
    left_alone: List[str] = []
    if isinstance(hooks_val, dict):
        for event, arr in hooks_val.items():
            want_cli, matcher, _t = _shape_of(event)
            for g in arr if isinstance(arr, list) else []:
                if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                    continue
                for h in g["hooks"]:
                    ev = memlet_hook_event(h)
                    if ev and not (ev == want_cli and _in_shape(g, matcher)):
                        left_alone.append(f"hooks.{event}" + (f" (matcher {g.get('matcher')!r})" if "matcher" in g
                                                              else "") + f": `{h.get('command')}`")
    if "hooks" in doc and not isinstance(hooks_val, dict):
        if remove:
            return {"path": rel, "op": "unchanged", "before": raw, "after": raw, "record": None, "events": [],
                    "leftAlone": left_alone}
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
            found = [(g, i) for g in arr if _in_shape(g, matcher)
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
            new = doc  # nothing to do: the file is not touched at all
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
                cli_event, matcher, _t = _shape_of(event)
                if not isinstance(arr, list) or cli_event is None:
                    continue
                emptied_by_us = False
                for g in list(arr):
                    if not _in_shape(g, matcher):
                        continue
                    mine = [h for h in g["hooks"] if memlet_hook_event(h) == cli_event]
                    if not mine:
                        continue
                    removed_any = True
                    touched.append(event)
                    g["hooks"] = [h for h in g["hooks"] if memlet_hook_event(h) != cli_event]
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
    plan: Dict[str, Any] = {"path": rel, "before": raw, "record": record, "events": sorted(set(touched)),
                            "leftAlone": left_alone}
    if new is doc:
        plan.update(op="unchanged", after=raw)
        return plan
    after: Optional[bytes] = _dump_settings(new, _settings_style(raw))
    op = "create" if raw is None else "update"
    if remove:
        restore = next((p for p in _settings_backups(path, rec) if _parsed_equal(p, new)), None)
        if restore is not None:
            after, op = _read_regular(restore), "restore"
            plan["restoredFrom"] = restore.name
        we_created = bool(rec.get("createdFile")) if rec else not _settings_backups(path, rec)
        if not new and we_created:
            after, op = None, "delete"
    plan.update(op=op, after=after)
    return plan


def _parsed_equal(p: Path, doc: Dict[str, Any]) -> bool:
    try:
        return _same_doc(_strict_json(_read_regular(p, 64 << 20) or b""), doc)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError, Refused):
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


def apply_claude(root: Path, scope: str, plan: Dict[str, Any], cfg_rec: Dict[str, Any],
                 txn: Optional[_Txn] = None) -> Dict[str, Any]:
    """Write the planned settings change (compare-and-swap against the planned bytes, see `_cas_write`). Returns
    the new install record (None when removed). Every write is registered in `txn` for rollback (without a `txn`,
    a failure rolls back this call's own writes before re-raising)."""
    if txn is None:
        own = _Txn()
        try:
            return apply_claude(root, scope, plan, cfg_rec, own)
        except BaseException:
            own.rollback()
            raise
    rel = plan["path"]
    path = root / rel
    actions: List[Dict[str, str]] = []
    record = plan.get("record")
    if plan["op"] == "unchanged":
        actions.append({"op": "unchanged", "path": rel})
        return {"actions": actions, "record": record, "backup": None}
    _load_settings(root, rel)  # symlink / type checks again right before writing
    backup = None
    before = plan["before"]
    if needs_backup(before, cfg_rec):
        backup = path.with_name(f"{path.name}.bak-{_utc_stamp()}")
        mode = _mode_of(path) or 0o600
        _cas_write(backup, before, mode, None, txn, f"backup {backup.name}")
        actions.append({"op": "backup", "path": str(backup.relative_to(root))})
        if record is not None:
            record["backups"] = list(record.get("backups") or []) + [{"path": backup.name, "sha256": _sha(before)}]
    if plan["after"] is None:
        _cas_delete(path, before, txn, rel)
        actions.append({"op": "delete", "path": rel})
        claude_dir = root / ".claude"
        if cfg_rec.get("createdDir") and claude_dir.is_dir() and not any(claude_dir.iterdir()):
            claude_dir.rmdir()
            actions.append({"op": "delete", "path": ".claude/"})
    else:
        if not path.parent.exists():
            _make_dirs(path.parent, txn)
            actions.append({"op": "create", "path": ".claude/"})
        mode = _mode_of(path) if before is not None else None
        _cas_write(path, plan["after"], mode if mode is not None else _umask_mode(0o666), before, txn, rel)
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


_GIT_MARKER_RE = re.compile(r"^# memlet-managed-hook v2 (?P<name>[a-z-]{1,32}) sha256=(?P<digest>[0-9a-f]{64})$")
_GIT_HOOK_MAX_BYTES = 64 * 1024


def git_hook_script(prefix: str, name: str, event: str) -> str:
    """Line 1 shebang, line 2 the exact marker with the SHA-256 of everything after it."""
    body = ("# Written by `memlet init --hooks --git-hooks`; `memlet init --hooks-remove --git-hooks` deletes it. "
            "Edit it and it becomes yours.\n"
            "# Warns about code-anchored rules touched by this change; it never blocks.\n"
            f"{prefix} admin hook {event} </dev/null{NEVER_FAIL_SUFFIX}\n"
            "exit 0\n")
    return f"#!/bin/sh\n{GIT_MARKER} {name} sha256={hashlib.sha256(body.encode('utf-8')).hexdigest()}\n{body}"


def _git_hook_state(p: Path, name: str, rec_files: Dict[str, Any]) -> Tuple[str, Optional[bytes]]:
    """('absent' | 'owned' | 'foreign', bytes). Owned = an unmodified script Memlet generated (exact marker line
    whose SHA-256 matches the rest) AND listed with exactly these bytes in the install record. A file that merely
    contains the marker text, an edited script or an unrecorded copy is foreign."""
    if not p.exists() and not p.is_symlink():
        return "absent", None
    if p.is_symlink() or not p.is_file():
        return "foreign", None
    try:
        if p.stat().st_size > _GIT_HOOK_MAX_BYTES:
            return "foreign", None
        data = _read_regular(p, _GIT_HOOK_MAX_BYTES + 1)
    except (OSError, Refused):
        return "foreign", None
    if data is None:
        return "absent", None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "foreign", data
    parts = text.split("\n", 2)
    if len(parts) < 3 or parts[0] != "#!/bin/sh":
        return "foreign", data
    m = _GIT_MARKER_RE.match(parts[1])
    if not m or m.group("name") != name or hashlib.sha256(parts[2].encode("utf-8")).hexdigest() != m.group("digest"):
        return "foreign", data
    if rec_files.get(name) != _sha(data):
        return "foreign", data
    return "owned", data


def _git_record_files(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    files = _record(cfg, "git").get("files")
    return files if isinstance(files, dict) else {}


def plan_git(root: Path, prefix: Optional[str], *, remove: bool, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
    rec_files = _git_record_files(cfg)
    for name, event in GIT_HOOKS:
        p = hooks_dir / name
        item: Dict[str, Any] = {"hook": name, "path": str(p)}
        state, data = _git_hook_state(p, name, rec_files)
        item["before"] = data
        if remove:
            item["op"] = {"owned": "delete", "absent": "absent"}.get(state, "kept")
            if state == "foreign":
                item["reason"] = "not a Memlet-written hook recorded in this repository's install record; left untouched"
        else:
            text = git_hook_script(prefix or "memlet", name, event)
            if state == "foreign":
                item.update(op="skipped", reason="an existing hook that Memlet did not write (or that was edited "
                                                 "since); left untouched (add "
                                                 f"`{prefix or 'memlet'} admin hook {event}` to it yourself)")
            elif state == "owned" and data == text.encode("utf-8"):
                mode = _mode_of(p) or 0
                if mode & 0o100:
                    item["op"] = "unchanged"
                else:
                    item.update(op="chmod", reason="was not executable; the executable bit is restored")
            else:
                item.update(op="update" if state == "owned" else "create", content=text)
        plan["items"].append(item)
    return plan


def apply_git(plan: Dict[str, Any], txn: Optional[_Txn] = None) -> List[Dict[str, str]]:
    txn = txn if txn is not None else _Txn()
    actions: List[Dict[str, str]] = []
    if not plan.get("hooksDir"):
        return actions
    d = Path(plan["hooksDir"])
    for item in plan["items"]:
        p = Path(item["path"])
        if item["op"] in ("create", "update"):
            if not d.exists():
                _make_dirs(d, txn)
            _cas_write(p, item["content"].encode("utf-8"), 0o755, item.get("before"), txn, f"git hook {p}")
            actions.append({"op": item["op"], "path": str(p)})
        elif item["op"] == "chmod":
            data = _read_regular(p, _GIT_HOOK_MAX_BYTES + 1)
            if data != item.get("before"):
                raise Refused(f"git hook {p} changed while Memlet was preparing it; left as it is")
            prior = _mode_of(p) or 0o644
            os.chmod(str(p), prior | 0o755)
            txn.mode(p, prior, data)
            actions.append({"op": "chmod", "path": str(p)})
        elif item["op"] == "delete":
            _cas_delete(p, item["before"], txn, f"git hook {p}")
            actions.append({"op": "delete", "path": str(p)})
        else:
            actions.append({"op": item["op"], "path": str(p)})
    return actions


# ---------------------------------------------------------------------------------------------------------------
# installer entry (`memlet init --hooks` / `--hooks-remove`)


FORMAT_NOTICE = ("the merged settings keep the file's indentation, line endings and final newline, but other "
                 "whitespace inside it may be normalised; the exact previous bytes are in the backup")


def _render(rep: Dict[str, Any]) -> str:
    title = "Memlet hooks" + (" removal" if rep.get("remove") else "") + (" (dry run)" if rep.get("dryRun") else "")
    lines = [f"## {title}", ""]
    if not rep.get("ok"):
        lines.append(f"- ERROR: {rep.get('error')}")
        for item in rep.get("leftBehind") or []:
            lines.append(f"- LEFT BEHIND: {item}")
        return "\n".join(lines) + "\n"
    claude = rep.get("claude") or {}
    lines.append(f"- Claude Code ({rep.get('scope')}): `{claude.get('path')}` → {claude.get('op')}")
    if rep.get("hookCommand"):
        lines.append(f"- Command form: {rep['hookCommand']['form']} — {rep['hookCommand']['detail']}")
        lines.append(f"- Example: `{hook_command(rep['hookCommand']['prefix'], 'session-start')}`")
    for a in rep.get("actions") or []:
        lines.append(f"- {a['op']}: `{a['path']}`")
    for item in claude.get("leftAlone") or []:
        lines.append(f"- LEFT ALONE (not Memlet's install shape): {item}")
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


def _preflight(root: Path, cplan: Dict[str, Any], gplan: Optional[Dict[str, Any]], *, init_runs: bool,
               record_changes: bool) -> List[str]:
    """Every destination this run may write, checked before the first write (rollback covers what this misses)."""
    problems: List[str] = []

    def check(d: Path, why: str) -> None:
        err = _writable(d)
        if err:
            problems.append(f"{why}: {err}")

    if cplan["op"] != "unchanged":
        check((root / cplan["path"]).parent, f"{cplan['path']} (and its backup)")
    if gplan is not None and gplan.get("hooksDir"):
        if any(it["op"] in ("create", "update", "delete") for it in gplan["items"]):
            check(Path(gplan["hooksDir"]), "git hooks")
    memlet = root / ".memlet"
    if init_runs or record_changes:
        if memlet.is_symlink():
            problems.append(".memlet is a symlink")
        elif not memlet.exists():
            if init_runs:
                check(root, ".memlet/ (created by init)")
        else:
            check(memlet, ".memlet/ (config and install record)")
            if init_runs:
                for sub in ("wiki",):
                    if (memlet / sub).is_dir():
                        check(memlet / sub, f".memlet/{sub}/")
                for f in ("config.json", "ledger.sqlite3", "wiki/INDEX.md"):
                    p = memlet / f
                    if p.is_file() and not os.access(str(p), os.W_OK):
                        problems.append(f".memlet/{f} is not writable")
    return problems


def hooks_command(repo_path=".", *, remove: bool = False, scope: str = "local", git_hooks: bool = False,
                  dry_run: bool = False, which: Callable[[str], Optional[str]] = shutil.which,
                  interpreter: Optional[str] = None, cli_path: Optional[str] = None,
                  run_init: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Install or remove Memlet's hooks as one transaction. Every destination is checked before the first write;
    when a later write fails, exactly this run's writes are undone (including what `run_init`, the implicit
    `memlet init` of `init --hooks`, created), unless another program changed them meanwhile; whatever had to be
    left is listed in `leftBehind`."""
    from prism_memory import memlet_wiki as mw
    rep: Dict[str, Any] = {"ok": True, "exitCode": 0, "command": "init --hooks-remove" if remove else "init --hooks",
                           "dryRun": bool(dry_run), "remove": bool(remove), "scope": scope, "notices": []}

    def fail(code: int, error: str) -> Dict[str, Any]:
        rep.update(ok=False, exitCode=code, error=error)
        rep["formatted"] = _render(rep)
        return rep

    if scope not in SCOPES:
        return fail(2, f"unknown hooks scope {scope!r} (use local or project)")
    try:
        root = mw._resolve_root(repo_path)
        cfg, cfg_problem = _read_config(root)
        if cfg_problem:
            rep["notices"].append(cfg_problem + "; the install record is not used")
        cmd = None
        git_prefix = None
        if not remove:
            cmd = resolve_command(scope, root, which=which, interpreter=interpreter, cli_path=cli_path)
            rep["hookCommand"] = cmd
            git_prefix = cmd["prefix"]
            if git_hooks and scope == "project":  # git hooks are personal: same choice as the local scope
                git_prefix = resolve_command("local", root, which=which, interpreter=interpreter,
                                             cli_path=cli_path)["prefix"]

        def plan_all(cfg_now: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
            c = plan_claude(root, scope, remove=remove, prefix=cmd["prefix"] if cmd else None, cfg=cfg_now)
            g = plan_git(root, git_prefix, remove=remove, cfg=cfg_now) if git_hooks else None
            return c, g

        cplan, gplan = plan_all(cfg)
        git_changes = gplan is not None and any(it["op"] in ("create", "update", "delete", "chmod")
                                                for it in gplan["items"])
        problems = _preflight(root, cplan, gplan, init_runs=run_init is not None,
                              record_changes=(cplan["op"] != "unchanged" or git_changes)
                              and (cfg is not None or run_init is not None))
        if problems:
            raise Refused("cannot install safely, nothing was changed: " + "; ".join(problems))
    except Refused as exc:
        return fail(2, str(exc))
    rep["repoPath"] = str(root)

    def describe(c: Dict[str, Any], g: Optional[Dict[str, Any]]) -> None:
        rep["claude"] = {"path": c["path"], "op": c["op"], "events": c["events"], "leftAlone": c["leftAlone"]}
        if c.get("restoredFrom"):
            rep["claude"]["restoredFrom"] = c["restoredFrom"]
        rep["diff"] = _diff(c["path"], c["before"], c["after"]) if c["op"] != "unchanged" else ""
        if g is not None:
            rep["gitHooks"] = {k: v for k, v in g.items() if k not in ("items", "notices")}
            rep["gitHooks"]["items"] = [{k: v for k, v in it.items() if k not in ("content", "before")}
                                        for it in g["items"]]

    describe(cplan, gplan)
    if gplan is not None:
        rep["notices"] += gplan.get("notices") or []
    elif remove:
        hooks_dir, _why = git_hooks_dir(root)
        recorded = _git_record_files(cfg)
        left = [n for n, _e in GIT_HOOKS if hooks_dir and _git_hook_state(hooks_dir / n, n, recorded)[0] == "owned"]
        if left:
            rep["notices"].append("Memlet git hooks remain (" + ", ".join(left) + "); add --git-hooks to remove them")
    if not remove and cplan["op"] in ("create", "update"):
        if scope == "local" and cplan["before"] is None:
            note = _gitignore_notice(root, cplan["path"])
            if note:
                rep["notices"].append(note)
        if cplan["before"] is not None:
            rep["notices"].append(FORMAT_NOTICE)
    if dry_run:
        changes = cplan["op"] != "unchanged"
        rep["actions"] = []
        if changes and needs_backup(cplan["before"], _record(cfg, "claude", scope)):
            rep["actions"].append({"op": "would-backup", "path": cplan["path"] + ".bak-<UTC timestamp>"})
        rep["actions"].append({"op": "would-" + cplan["op"] if changes else "unchanged", "path": cplan["path"]})
        if gplan is not None:
            rep["actions"] += [{"op": "would-" + it["op"] if it["op"] in ("create", "update", "delete", "chmod")
                                else it["op"], "path": it["path"]} for it in gplan["items"]]
            changes = changes or git_changes
        if changes and cfg is not None and not cfg_problem:
            rep["actions"].append({"op": "would-update", "path": ".memlet/config.json"})
        rep["formatted"] = _render(rep)
        return rep

    txn = _Txn()
    actions: List[Dict[str, str]] = []
    try:
        if run_init is not None:
            memlet = root / ".memlet"
            existed = memlet.exists()
            before = _memlet_files(root)

            def track_init() -> None:  # whatever init wrote becomes part of this run's transaction
                if not existed and memlet.is_dir() and not memlet.is_symlink():
                    txn.made_tree(memlet, _tree_digest(memlet))
                elif existed:
                    after = _memlet_files(root)
                    for p in set(before) | set(after):
                        old, new_ = before.get(p, (None, None)), after.get(p, (None, None))
                        if old[0] != new_[0]:
                            txn.file(p, old[0], old[1], new_[0])

            try:
                init_rep = run_init()
            finally:
                track_init()
            if not init_rep.get("ok"):
                left = txn.rollback()
                rep["init"] = init_rep
                return fail(int(init_rep.get("exitCode") or 1), "init failed; no hook was installed"
                            + ("" if not left else "; left behind: " + "; ".join(left)))
            cfg, cfg_problem = _read_config(root)  # init created or refreshed the record's home
            cplan, gplan = plan_all(cfg)
            describe(cplan, gplan)
        res = apply_claude(root, scope, cplan, _record(cfg, "claude", scope), txn)
        actions += res["actions"]
        if res["backup"]:
            rep["notices"].append(f"backup of the previous file: `{Path(res['backup']).name}` (not meant for git)")
        if gplan is not None:
            actions += apply_git(gplan, txn)
        if cfg is not None and not cfg_problem:
            new_cfg = copy.deepcopy(cfg)
            if remove:
                _set_record(new_cfg, "claude", scope, None)
                if gplan is not None:
                    remaining = {n: h for n, h in _git_record_files(cfg).items()
                                 if not any(it["hook"] == n and it["op"] == "delete" for it in gplan["items"])}
                    _set_record(new_cfg, "git", None, {"files": remaining} if remaining else None)
            else:
                _set_record(new_cfg, "claude", scope, res["record"])
                if gplan is not None and gplan.get("hooksDir"):
                    files = dict(_git_record_files(cfg))
                    for it in gplan["items"]:
                        p = Path(it["path"])
                        if it["op"] in ("create", "update", "unchanged", "chmod"):
                            data = _read_regular(p, _GIT_HOOK_MAX_BYTES + 1)
                            if data is not None:
                                files[it["hook"]] = _sha(data)
                    _set_record(new_cfg, "git", None, {"files": files} if files else None)
            if new_cfg != cfg:
                cfg_path = root / ".memlet" / "config.json"
                current = _read_regular(cfg_path)
                data = (json.dumps(new_cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
                if current is None or json.loads(current.decode("utf-8")) != cfg:
                    raise Refused(".memlet/config.json changed while hooks were being installed")
                _cas_write(cfg_path, data, _mode_of(cfg_path) or 0o644, current, txn, ".memlet/config.json")
                actions.append({"op": "update", "path": ".memlet/config.json"})
        elif not remove:
            raise Refused("no readable .memlet/config.json for the install record (run `memlet init` first)")
    except Exception as exc:  # any failure: undo this run's writes, then report (never a half-installed state)
        left = txn.rollback()
        rep["leftBehind"] = left
        rep["actions"] = actions
        return fail(1, "hook installation failed and this run's changes were rolled back"
                    + (" (except what is listed in leftBehind)" if left else "") + f": {exc}")
    rep["actions"] = actions
    rep["formatted"] = _render(rep)
    return rep


def init_hooks_command(repo_path=".", *, remove: bool = False, scope: str = "local", git_hooks: bool = False,
                       dry_run: bool = False, force: bool = False, output_format: str = "json",
                       **kw: Any) -> Dict[str, Any]:
    """`memlet init --hooks`: the normal `init`, then the hooks, as one all-or-nothing run (see `hooks_command`).
    `--hooks-remove`: only the removal. `--dry-run` writes nothing at all."""
    from prism_memory.memlet_wiki import init_project
    if remove:
        rep = hooks_command(repo_path, remove=True, scope=scope, git_hooks=git_hooks, dry_run=dry_run, **kw)
        if output_format != "markdown":
            rep.pop("formatted", None)
        return rep
    holder: Dict[str, Any] = {}

    def run_init() -> Dict[str, Any]:
        holder["init"] = init_project(repo_path, dry_run=dry_run, force=force, output_format=output_format)
        return holder["init"]

    if dry_run:
        hooks = hooks_command(repo_path, scope=scope, git_hooks=git_hooks, dry_run=True, **kw)
        if not hooks.get("ok"):
            return _hooks_failure(hooks, output_format)
        run_init()
        if not holder["init"].get("ok"):
            return holder["init"]
    else:
        hooks = hooks_command(repo_path, scope=scope, git_hooks=git_hooks, run_init=run_init, **kw)
        if "init" not in holder:
            return _hooks_failure(hooks, output_format)
        if not holder["init"].get("ok"):
            holder["init"]["hooks"] = {k: v for k, v in hooks.items() if k not in ("formatted", "init")}
            return holder["init"]
    report = holder["init"]
    if not hooks.get("ok"):  # init's own writes were rolled back together with the hooks
        hooks.pop("init", None)
        return _hooks_failure(hooks, output_format)
    report["hooks"] = {k: v for k, v in hooks.items() if k != "formatted"}
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
    transcript text. Never creates `.memlet/`; never follows a symlink; never opens anything but a regular file; a
    rotation reads only the kept tail."""
    if root is None:
        return
    with contextlib.suppress(Exception):
        d = root / ".memlet"
        if not d.is_dir() or d.is_symlink():
            return
        p = root / LOG_REL
        if p.is_symlink() or (p.exists() and not p.is_file()):
            return
        safe = re.sub(r"[^A-Za-z0-9 _.,:=/()<>+-]", "?", message)[:240]
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {event} {safe}\n".encode("ascii", "replace")
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(str(p), flags, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return
            if st.st_size + len(line) > LOG_CAP_BYTES:
                keep = LOG_CAP_BYTES // 2
                os.lseek(fd, max(0, st.st_size - keep), os.SEEK_SET)
                tail = os.read(fd, keep)
                tail = tail[tail.find(b"\n") + 1:] if b"\n" in tail else b""
                _atomic_write(p, tail + line, 0o600)
                return
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
    pending: Optional[int] = None
    if isinstance(pend, dict) and pend.get("ok") and isinstance(pend.get("counts"), dict):
        pending = int(pend["counts"].get("pendingNodes") or 0)
    else:  # unknown is not zero: the count is left out, never printed as 0
        _log(root, "session-start", "pending lessons unavailable: queue not readable")
    if not active and not stale and not reverify and not pending:
        return None
    head = "Memlet verified rules (code anchors match; source cited):"
    pending_part = ("" if pending is None else
                    f", {pending} lesson(s) pending human approval (`memlet learn --pending`)")
    status = (f"Not verified, do not rely on: {stale} stale anchor(s), {reverify} to re-verify{pending_part}; "
              "see `memlet check .`.")
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
        out["systemMessage"] = f"Memlet: {stale} stale rule anchor(s); run `memlet check .` to review."
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
    if p.suffix != ".jsonl" or p.name.startswith(".env"):
        return None
    problem = mb.transcript_symlink_problem(p)  # same rule as the MCP tool
    if problem:
        _log(root, event, "skip: " + problem)
        return None
    if not p.is_file():
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
                         + ["memlet: details: `memlet check .`"]) + "\n"
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
