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

9.2 opt-ins (`init --hooks --jit / --guard / --capsule`; without them every event prints what 9.1.1 printed)
    Flags are recorded per settings scope (`hooks.scopes.<local|project>`) and that scope's commands carry
    `--scope <scope>`. `--jit` adds PreToolUse (`Write|Edit|MultiEdit|NotebookEdit`): the verified memory attached
    to the edited file, as `additionalContext`, which Claude Code delivers with the tool result (after the call
    ran; it informs, it does not prevent). `--guard` (implies `--jit`) answers the first edit of a file with an
    approved BLOCKS lesson or supersession with `deny` (`guard: "ask"`: the person decides; `"off"`: no guard), once
    per (session, agent, file, item set), only in a session first seen at SessionStart `startup` / `clear`, and only
    after the decision reads back from disk. `--capsule` makes PreCompact print instructions that keep the session's
    verified memory for the files it touched, and SessionStart after `compact` / `resume` re-deliver it; when the
    time left is short the 9.1.1 output is printed instead. Memory comes only from `memlet_graph.neighbors`, through
    the gate `_checked_neighbors`; per-session state (ids and digests, never lesson text) is kept under
    `.memlet/sessions/`.
"""

from __future__ import annotations

from memlet.memlet_dream import call_foreign as _call_foreign, _foreign_context
from . import memlet_dream as md

from .vocabulary import (HINT as TIER_HINT)
from .vocabulary import APPROVED as TIER_APPROVED
from .vocabulary import suggested_label
from . import vocabulary as voc

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
# 9.2: installed only with `init --hooks --jit` (or `--guard`); removed again by a reinstall without it.
JIT_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"
OPTIONAL_EVENTS: Tuple[Tuple[str, str, int, Optional[str]], ...] = (
    ("PreToolUse", "pre-tool-use", 10, JIT_MATCHER),
)
ALL_CLAUDE_EVENTS = CLAUDE_EVENTS + OPTIONAL_EVENTS
RUNTIME_EVENTS = tuple(e[1] for e in ALL_CLAUDE_EVENTS)
GIT_HOOKS: Tuple[Tuple[str, str], ...] = (("pre-commit", "git-pre-commit"), ("post-merge", "git-post-merge"))
GIT_EVENTS = tuple(g[1] for g in GIT_HOOKS)
SCOPES = {"local": ".claude/settings.local.json", "project": ".claude/settings.json"}
PROGRAM_NAMES = ("memlet",)
from .compatibility import LEGACY_HOOK_PROGRAMS
GIT_MARKER = "# memlet-managed-hook v2"
# Appended to every written command: whatever the installed `memlet` does (an older release without this entry exits
# 2 from argparse, which a Stop hook would treat as "block"), the shell reports success to Claude Code.
NEVER_FAIL_SUFFIX = " || true"
CONFIG_KEY = "hooks"

DEFAULT_SESSION_TOKENS = 400
DEFAULT_JIT_TOKENS = 400
DEFAULT_CAPSULE_TOKENS = 800
DEFAULT_MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
MAX_STDIN_BYTES = 16 * 1024 * 1024
# Self-imposed deadlines, below the timeouts written into settings (10 s / 20 s) and below git's patience.
# git-pre-commit runs while someone waits for `git commit`: well under two seconds including interpreter start-up;
# on overrun it prints nothing and exits 0.
DEADLINES = {"session-start": 7.0, "post-tool-use": 7.0, "stop": 16.0, "pre-compact": 16.0,
             "git-pre-commit": 1.0, "git-post-merge": 5.0,
             # PreToolUse holds the tool call while it runs: a short deadline, nothing printed on overrun
             "pre-tool-use": 4.0}
LOG_REL = ".memlet/hooks.log"
LOG_CAP_BYTES = 64 * 1024
_EVENT_NAMES = {e[1]: e[0] for e in ALL_CLAUDE_EVENTS}
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
        from memlet import memlet_dream as md
        md._drop_read_caches()
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
        rc, stdout, _ = md._run_owned_git(["git"] + args, cwd=str(root), capture_output=True, timeout=timeout,
                                         stdin=subprocess.DEVNULL, _run=subprocess.run)
    except (OSError, subprocess.SubprocessError):
        return None
    if rc != 0:
        return None
    return stdout.decode("utf-8", "surrogateescape")


def default_cli_path() -> Path:
    """`memory_cli.py` sits next to the `memlet` package, both in a checkout and in an installed wheel."""
    return Path(__file__).resolve().parent.parent / "memory_cli.py"


def memlet_hook_event(hook: Any) -> Optional[str]:
    """The runtime event of a hook entry Memlet wrote, else None. Recognised forms (as written by the installer):
    `memlet admin hook <event>` and `<abs interpreter> <abs .../memory_cli.py> admin hook <event>`, optionally
    preceded by `MEMLET_HOOK_SCOPE=local|project` (9.2: a scope installed with --jit/--guard/--capsule), optionally
    followed by `--scope local|project` (written by 9.2 development builds) and by `|| true`."""
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
    if len(tokens) >= 6 and tokens[-2] == "--scope" and tokens[-1] in SCOPES:
        tokens = tokens[:-2]
    if tokens and _scope_assignment(tokens[0]) is not None:
        tokens = tokens[1:]
    if len(tokens) < 4 or tokens[-3:-1] != ["admin", "hook"] or tokens[-1] not in RUNTIME_EVENTS:
        return None
    head = tokens[:-3]
    if len(head) == 1 and head[0] in PROGRAM_NAMES + LEGACY_HOOK_PROGRAMS:
        return tokens[-1]
    if (len(head) == 2 and os.path.isabs(head[0]) and os.path.isabs(head[1])
            and os.path.basename(head[1]) == "memory_cli.py"):
        return tokens[-1]
    return None


# Variables a launcher may export for this process only; Claude Code / git will not have them when they run the hook.
_LAUNCHER_ENV = ("PYTHONHOME", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")


@md.fresh_read
def probe_command(prefix: str, cwd: Path) -> bool:
    """Run `<prefix> admin hook probe` the way Claude Code runs a hook (through /bin/sh, without this process's
    launcher variables) and check that it answers {"memletHook": HOOK_PROTOCOL}. Proves the quoting and the
    interpreter before anything is written."""
    env = {k: v for k, v in os.environ.items() if k not in _LAUNCHER_ENV}
    line = f"{prefix} admin hook probe"
    argv = ["/bin/sh", "-c", line] if os.path.exists("/bin/sh") else shlex.split(line)
    try:
        rc, out_bytes = _call_foreign(
            lambda: (lambda p: (p.returncode, p.stdout))(
                subprocess.run(argv, cwd=str(cwd), input=b"", capture_output=True, timeout=30, env=env)
            )
        )
        doc = json.loads(out_bytes.decode("utf-8", "replace").strip() or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return rc == 0 and isinstance(doc, dict) and doc.get("memletHook") == HOOK_PROTOCOL


@md.fresh_read
def resolve_command(scope: str, root: Path, *, which: Callable[[str], Optional[str]] = shutil.which,
                    interpreter: Optional[str] = None, cli_path: Optional[str] = None) -> Dict[str, str]:
    """The command prefix written before `admin hook <event>`; every candidate is probed before it is chosen.

    project: always the portable `memlet` (the file is shared), which must be on PATH and support the hook entry.
    local: `memlet` when that works, otherwise an absolute interpreter and the absolute memory_cli.py, shell-quoted
    (this interpreter first, then `python3` from PATH)."""
    exe = _call_foreign(lambda: which("memlet"))
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
    for c in ([interpreter] if interpreter else [sys.executable, _call_foreign(lambda: which("python3"))]):
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


SCOPE_ENV = "MEMLET_HOOK_SCOPE"


def _scope_assignment(token: str) -> Optional[str]:
    """The scope of a leading `MEMLET_HOOK_SCOPE=<scope>` shell assignment, else None."""
    name, sep, value = token.partition("=")
    return value if sep and name == SCOPE_ENV and value in SCOPES else None


def hook_command(prefix: str, event: str, scope: Optional[str] = None) -> str:
    """`scope` only for a scope that needs its own flags (installed with --jit/--guard/--capsule): the shell sets
    MEMLET_HOOK_SCOPE for this command, which a 9.1.1 runtime ignores (it keeps printing its 9.1.1 output). Without
    it the command is byte-for-byte the 9.1.1 one."""
    return (f"{SCOPE_ENV}={scope} " if scope else "") + f"{prefix} admin hook {event}" + NEVER_FAIL_SUFFIX


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
            from memlet import memlet_dream as md
            md._drop_read_caches()
            _call_foreign(lambda: txn.file(path, None, None, data))
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
        from memlet import memlet_dream as md
        md._drop_read_caches()
        _call_foreign(lambda: txn.file(path, expected, prior_mode, data))
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
        _call_foreign(lambda: txn.file(path, expected, prior_mode, None))
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
        _call_foreign(lambda: txn.made_dir(p))


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
    for ev, cli_event, timeout, matcher in ALL_CLAUDE_EVENTS:
        if ev == event:
            return cli_event, matcher, timeout
    return None, None, 0


def _drop_memlet_entries(hooks: Dict[str, Any], event: str, rec: Dict[str, Any]) -> bool:
    """Remove Memlet's entries for `event` (inside the event's install shape) from a parsed `hooks` object; the
    event's array goes too when Memlet emptied it and had created it. True when anything was removed."""
    arr = hooks.get(event)
    cli_event, matcher, _t = _shape_of(event)
    if not isinstance(arr, list) or cli_event is None:
        return False
    removed = emptied_by_us = False
    for g in list(arr):
        if not _in_shape(g, matcher):
            continue
        if not any(memlet_hook_event(h) == cli_event for h in g["hooks"]):
            continue
        removed = True
        g["hooks"] = [h for h in g["hooks"] if memlet_hook_event(h) != cli_event]
        if not g["hooks"]:
            arr[:] = [x for x in arr if x is not g]
            emptied_by_us = True
    created = event in (rec.get("createdEvents") or []) if rec else True
    if not arr and emptied_by_us and created:
        del hooks[event]
    return removed


@md.fresh_read
def plan_claude(root: Path, scope: str, *, remove: bool, prefix: Optional[str],
                cfg: Optional[Dict[str, Any]], events: Optional[Tuple[Tuple[str, str, int, Optional[str]], ...]] = None,
                scoped: bool = False) -> Dict[str, Any]:
    """Pure: what installing (or removing) Memlet's entries would do. Raises Refused.

    Memlet's entry for an event is a recognised command (`memlet_hook_event`) for that event, inside a group with
    the event's matcher (`Write|Edit` for PostToolUse, `Write|Edit|MultiEdit|NotebookEdit` for PreToolUse, none
    otherwise). A Memlet-looking command anywhere else is the user's customisation: left alone on install and on
    removal, and listed in `leftAlone`. `events` (install only): the events to install, default the four 9.1.1
    events; Memlet's entry of an optional event (PreToolUse) that is not requested is removed, so a reinstall without
    `--jit` leaves no PreToolUse entry behind. `scoped`: write the commands with `--scope <scope>` (a scope with 9.2
    flags), else exactly the 9.1.1 commands."""
    events = CLAUDE_EVENTS if events is None else events
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
        for event, _cli, _t, _m in events:
            if event in (hooks_val or {}) and not isinstance(hooks_val[event], list):
                raise Refused(f"{rel}: `hooks.{event}` must be a JSON array, found "
                              f"{type(hooks_val[event]).__name__}; nothing changed")
        created_hooks_key = "hooks" not in new
        hooks = new.setdefault("hooks", {})
        wanted = {e[0] for e in events}
        dropped: List[str] = []
        for event, _cli, _t, _m in OPTIONAL_EVENTS:
            if event not in wanted and _drop_memlet_entries(hooks, event, rec):
                touched.append(event)
                if event not in hooks:
                    dropped.append(event)
        for event, cli_event, timeout, matcher in events:
            command = hook_command(prefix or "memlet", cli_event, scope if scoped else None)
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
            "createdEvents": sorted((set(prior_events) | set(created_events)) - set(dropped)),
            "backups": list(rec.get("backups") or []),
        }
        if rec.get("writtenSha256"):
            record["writtenSha256"] = rec["writtenSha256"]
    else:
        hooks = new.get("hooks")
        removed_any = False
        if isinstance(hooks, dict):
            for event in list(hooks):
                if _drop_memlet_entries(hooks, event, rec):
                    removed_any = True
                    touched.append(event)
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


@md.fresh_read
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
        rc, _, _ = md._run_owned_git(["git", "check-ignore", "-q", "--no-index", rel], cwd=str(root), capture_output=True,
                                    timeout=10, stdin=subprocess.DEVNULL, _run=subprocess.run)
        if rc == 0:
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    return (f"`{rel}` is personal and not meant for git, but this repository's ignore rules do not cover it. "
            f"Add `{rel}` to `.gitignore` (Memlet does not edit it for you).")


# ---------------------------------------------------------------------------------------------------------------
# git hooks


@md.fresh_read
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


@md.fresh_read
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


@md.fresh_read
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
            _call_foreign(lambda: txn.mode(p, prior, data))
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
JIT_NOTICE = ("--jit: a PreToolUse hook (Write|Edit|MultiEdit|NotebookEdit) adds the verified memory attached to the "
              "file being edited. Claude Code delivers it alongside the tool result, after the call ran: it informs, "
              "it does not prevent an edit.")
GUARD_NOTICE = ("--guard: in a session that started fresh (startup or clear), the first edit of a file with an approved "
                "BLOCKS lesson or a replaced instruction is denied once, with those lessons quoted in full; set "
                "hooks.scopes.<scope>.guard to exactly \"deny\", \"ask\" (you decide in the permission prompt) or "
                "\"off\" in .memlet/config.json without reinstalling (any other value means off and is reported).")
CAPSULE_NOTICE = ("--capsule: PreCompact prints instructions asking the compaction summary to keep this session's "
                  "verified memory for the files it touched (Write, Edit, MultiEdit and NotebookEdit; a PreToolUse "
                  "entry sees the last two); when 9.1.1 would have printed a status message there, its text follows "
                  "under a fixed heading. SessionStart re-delivers the memory after compact or resume.")
FLAG_KEYS = ("jit", "guard", "capsule")
SCOPE_KEYS = FLAG_KEYS + ("dream",)


def _scopes(cfg: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    hooks = (cfg or {}).get(CONFIG_KEY)
    scopes = hooks.get("scopes") if isinstance(hooks, dict) else None
    return {k: v for k, v in scopes.items() if k in SCOPES and isinstance(v, dict)} if isinstance(scopes, dict) else {}


GUARD_VALUES = ("deny", "ask", "off")


def _explicit(*, jit: bool, guard: str, capsule: bool, dream: bool) -> Dict[str, Any]:
    return {"jit": bool(jit), "guard": guard, "capsule": bool(capsule), "dream": bool(dream)}


def _plain(rec: Dict[str, Any]) -> bool:
    """A record that needs nothing beyond what a 9.1.1-shaped (unscoped) command can do."""
    return rec.get("jit") is not True and rec.get("capsule") is not True


def _legacy_record(hooks: Dict[str, Any]) -> Dict[str, Any]:
    """What an installed scope without a record reads today: the flat keys (9.1.1 `dream`; `jit` / `guard` /
    `capsule` from 9.2 development builds)."""
    guard = hooks.get("guard")
    return _explicit(jit=hooks.get("jit") is True, guard=guard if guard in GUARD_VALUES else "off",
                     capsule=hooks.get("capsule") is True, dream=hooks.get("dream") is True)


def _installed_shape(root: Path, scope: str) -> Optional[bool]:
    """Read-only: True when the scope's installed Memlet commands select their scope (MEMLET_HOOK_SCOPE or the
    development `--scope`), False when they are 9.1.1-shaped, None when unknown or not installed."""
    try:
        _path, raw, doc = _load_settings(root, SCOPES[scope])
    except Refused:
        return None
    hooks = doc.get("hooks") if raw is not None else None
    found = []
    for arr in (hooks.values() if isinstance(hooks, dict) else []):
        for g in arr if isinstance(arr, list) else []:
            for h in (g.get("hooks") if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []):
                if memlet_hook_event(h):
                    tokens = shlex.split(h["command"])
                    found.append(_scope_assignment(tokens[0]) is not None or "--scope" in tokens)
    return (any(found)) if found else None


def _scope_plan(root: Path, cfg: Optional[Dict[str, Any]], scope: str,
                record: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
    """The `hooks` keys an install (`record`) or removal (`record` None) of `scope` leaves, and whether that scope's
    commands must select their scope. Rules: (1) every other installed scope keeps exactly what it reads today: a
    scope without a record first gets an explicit record of the flat values it reads (migration); (2) the flat
    `jit` / `guard` / `capsule` (9.2 development builds) stay only while another installed scope with 9.1.1-shaped
    commands still relies on them, and then this scope selects its own explicit record; (3) the flat 9.1.1
    `hooks.dream` is on exactly when an installed scope with 9.1.1-shaped commands asked for it; (4) when nothing
    needs a record, the config returns to the exact 9.1.1 shape (no `scopes`). While no record, flat 9.2 key or new
    flag is involved, the result is exactly 9.1.1's: one `hooks.dream` shared by every 9.1.1-shaped install."""
    hooks = (cfg or {}).get(CONFIG_KEY)
    hooks = hooks if isinstance(hooks, dict) else {}
    inst = _installed(cfg).get("claude")
    others = [x for x in (inst if isinstance(inst, dict) else {}) if x != scope and x in SCOPES]
    records = {k: dict(v) for k, v in _scopes(cfg).items() if k in others}
    dev = any(k in hooks for k in FLAG_KEYS)
    legacy = _legacy_record(hooks)
    if not (records or dev or (record is not None and not _plain(record))):
        # nothing of 9.2 is in use: exactly 9.1.1 (one shared `hooks.dream`, set or cleared by this install)
        return {"scopes": {}, "keepDev": False, "dream": bool(record and record.get("dream"))}, False
    for x in others:
        records.setdefault(x, dict(legacy))
    shapes = {x: _installed_shape(root, x) for x in others}
    keep_dev = dev and any(not _plain(records.get(x, legacy)) and shapes[x] is not True for x in others)
    scoped = record is not None and (not _plain(record) or keep_dev)
    if record is not None:
        records[scope] = dict(record)
    dreams = [records.get(x, legacy).get("dream") is True for x in others if shapes[x] is not True]
    if record is not None and not scoped:
        dreams.append(record.get("dream") is True)
    if (not keep_dev and not scoped and all(_plain(r) for r in records.values())
            and all(shapes[x] is not True for x in others)):
        records = {}
    return {"scopes": records, "keepDev": keep_dev, "dream": any(dreams)}, scoped


def _apply_scope_plan(new_cfg: Dict[str, Any], plan: Dict[str, Any]) -> None:
    hooks = new_cfg.get(CONFIG_KEY)
    if not isinstance(hooks, dict):
        if not (plan["scopes"] or plan["dream"]):
            return
        hooks = new_cfg[CONFIG_KEY] = {}
    if plan["scopes"]:
        hooks["scopes"] = plan["scopes"]
    else:
        hooks.pop("scopes", None)
    if not plan["keepDev"]:
        for k in FLAG_KEYS:
            hooks.pop(k, None)
    if plan["dream"]:
        hooks["dream"] = True
    else:
        hooks.pop("dream", None)


def _invalid_guards(cfg: Optional[Dict[str, Any]]) -> List[str]:
    """Notices for every `guard` value that is not exactly "deny", "ask" or "off" (such a value means off)."""
    hooks = (cfg or {}).get(CONFIG_KEY)
    hooks = hooks if isinstance(hooks, dict) else {}
    where = [("hooks.guard", hooks)] + [(f"hooks.scopes.{k}.guard", v) for k, v in sorted(_scopes(cfg).items())]
    return [voc.HOOK_GUARD_INVALID % (key, json.dumps(rec["guard"], ensure_ascii=True)[:60])
            for key, rec in where if "guard" in rec and rec["guard"] not in GUARD_VALUES]


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


@md.fresh_read
def hooks_command(repo_path=".", *, remove: bool = False, scope: str = "local", git_hooks: bool = False,
                  dry_run: bool = False, which: Callable[[str], Optional[str]] = shutil.which,
                  interpreter: Optional[str] = None, cli_path: Optional[str] = None,
                  run_init: Optional[Callable[[], Dict[str, Any]]] = None, dream: bool = False,
                  jit: bool = False, guard: bool = False, capsule: bool = False) -> Dict[str, Any]:
    """Install or remove Memlet's hooks as one transaction. Every destination is checked before the first write;
    when a later write fails, exactly this run's writes are undone (including what `run_init`, the implicit
    `memlet init` of `init --hooks`, created), unless another program changed them meanwhile; whatever had to be
    left is listed in `leftBehind`.

    9.2 opt-ins (install only; a removal or a reinstall of the same scope without them clears them; another scope is
    never changed): recorded under `hooks.scopes.<scope>` (`jit`, `guard` "deny" or a "ask" already set, `capsule`,
    `dream`). `jit` or `capsule` add the PreToolUse entry, and that scope's commands carry `--scope <scope>`."""
    from memlet import memlet_wiki as mw
    rep: Dict[str, Any] = {"ok": True, "exitCode": 0, "command": "init --hooks-remove" if remove else "init --hooks",
                           "dryRun": bool(dry_run), "remove": bool(remove), "scope": scope, "notices": []}

    if dream:
        rep["dream"] = True
        rep["notices"].append("Stop and PreCompact only ingest; no automatic model calls. Run memlet dream explicitly.")
    jit = bool(jit or guard) and not remove
    guard = bool(guard) and not remove
    capsule = bool(capsule) and not remove
    events = CLAUDE_EVENTS + (OPTIONAL_EVENTS if jit or capsule else ())
    for flag, on, note in (("jit", jit, JIT_NOTICE), ("guard", guard, GUARD_NOTICE), ("capsule", capsule, CAPSULE_NOTICE)):
        if on:
            rep[flag] = True
            rep["notices"].append(note)

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
        rep["notices"] += _invalid_guards(cfg)

        def record_for(cfg_now: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if remove:
                return None
            hooks_now = (cfg_now or {}).get(CONFIG_KEY)
            prior = _scopes(cfg_now).get(scope, _legacy_record(hooks_now if isinstance(hooks_now, dict) else {}))
            return _explicit(jit=jit, guard=("ask" if prior.get("guard") == "ask" else "deny") if guard else "off",
                             capsule=capsule, dream=bool(dream))

        plan_cfg, scoped = _scope_plan(root, cfg, scope, record_for(cfg))
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
            nonlocal plan_cfg, scoped
            plan_cfg, scoped = _scope_plan(root, cfg_now, scope, record_for(cfg_now))
            c = plan_claude(root, scope, remove=remove, prefix=cmd["prefix"] if cmd else None, cfg=cfg_now,
                            events=None if remove else events, scoped=scoped)
            g = plan_git(root, git_prefix, remove=remove, cfg=cfg_now) if git_hooks else None
            return c, g

        cplan, gplan = plan_all(cfg)
        git_changes = gplan is not None and any(it["op"] in ("create", "update", "delete", "chmod")
                                                for it in gplan["items"])
        probe_cfg = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
        _apply_scope_plan(probe_cfg, plan_cfg)
        flags_change = isinstance(cfg, dict) and probe_cfg.get(CONFIG_KEY) != cfg.get(CONFIG_KEY)
        problems = _preflight(root, cplan, gplan, init_runs=run_init is not None,
                              record_changes=(cplan["op"] != "unchanged" or git_changes or dream or bool((cfg or {}).get("hooks", {}).get("dream"))
                                              or flags_change)
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
        if (changes or dream or bool((cfg or {}).get("hooks", {}).get("dream")) or flags_change) and cfg is not None \
                and not cfg_problem:
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
                    _call_foreign(lambda: txn.made_tree(memlet, _tree_digest(memlet)))
                elif existed:
                    after = _memlet_files(root)
                    for p in set(before) | set(after):
                        old, new_ = before.get(p, (None, None)), after.get(p, (None, None))
                        if old[0] != new_[0]:
                            _call_foreign(lambda: txn.file(p, old[0], old[1], new_[0]))

            try:
                init_rep = _call_foreign(lambda: run_init())
            finally:
                md._drop_read_caches()
                track_init()
            if not init_rep.get("ok"):
                left = _call_foreign(lambda: txn.rollback())
                md._drop_read_caches()
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
            new_cfg.setdefault("hooks", {})  # as 9.1.1 did (it set or popped `dream` here)
            _apply_scope_plan(new_cfg, plan_cfg)
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
        left = _call_foreign(lambda: txn.rollback())
        md._drop_read_caches()
        rep["leftBehind"] = left
        rep["actions"] = actions
        return fail(1, "hook installation failed and this run's changes were rolled back"
                    + (" (except what is listed in leftBehind)" if left else "") + f": {exc}")
    rep["actions"] = actions
    rep["formatted"] = _render(rep)
    return rep


@md.fresh_read
def init_hooks_command(repo_path=".", *, remove: bool = False, scope: str = "local", git_hooks: bool = False,
                       dry_run: bool = False, force: bool = False, output_format: str = "json",
                       **kw: Any) -> Dict[str, Any]:
    """`memlet init --hooks`: the normal `init`, then the hooks, as one all-or-nothing run (see `hooks_command`).
    `--hooks-remove`: only the removal. `--dry-run` writes nothing at all."""
    from memlet.memlet_wiki import init_project
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
        _call_foreign(lambda: run_init())
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
        self.end = time.monotonic() + seconds
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
        if _call_foreign(lambda: stdin.isatty()):
            return None
    raw = _call_foreign(lambda: stdin.read(MAX_STDIN_BYTES + 1))
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


@md.fresh_read
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


def _hook_settings(root: Path, scope: Optional[str] = None) -> Dict[str, Any]:
    """Settings for one hook run. `scope` (MEMLET_HOOK_SCOPE, or `--scope` from 9.2 development builds; written only
    for a scope that needs its own flags): every flag, `dream` included, from `hooks.scopes.<scope>`; without it
    (every 9.1.1-shaped command), or without that record, the flat keys, where 9.1.1 configs have no `jit` /
    `guard` / `capsule`. A `guard` that is not exactly "deny", "ask" or "off" means off and is reported
    (`guardInvalid`)."""
    cfg, _err = _read_config(root)
    raw = (cfg or {}).get(CONFIG_KEY)
    raw = raw if isinstance(raw, dict) else {}
    flags, where = raw, "hooks.guard"
    if scope is not None and scope in _scopes(cfg):
        flags, where = _scopes(cfg)[scope], f"hooks.scopes.{scope}.guard"

    def number(key: str, default: float, lo: float, hi: float) -> float:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
            return default
        return v

    guard = flags.get("guard")
    invalid = (voc.HOOK_GUARD_INVALID % (where, json.dumps(guard, ensure_ascii=True, default=str)[:60])
               if "guard" in flags and guard not in GUARD_VALUES else None)
    return {"tokens": int(number("sessionStartTokenBudget", DEFAULT_SESSION_TOKENS, 50, 4000)),
            "maxTranscriptBytes": int(number("maxTranscriptBytes", DEFAULT_MAX_TRANSCRIPT_BYTES, 1, 1 << 30)),
            "deadline": number("deadlineSeconds", 0.0, 0.1, 60.0), "dream": flags.get("dream") is True,
            # 9.2 opt-ins: recorded by `init --hooks --jit/--guard/--capsule`; anything malformed means off
            "jit": flags.get("jit") is True,
            "guard": guard if guard in ("deny", "ask") else None, "guardInvalid": invalid,
            "capsule": flags.get("capsule") is True,
            "jitTokens": int(number("jitTokenBudget", DEFAULT_JIT_TOKENS, 50, 1000)),
            "capsuleTokens": int(number("capsuleTokenBudget", DEFAULT_CAPSULE_TOKENS, 100, 2000))}


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
    from memlet.memlet_dream import mask_secrets
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text or ""))).strip()
    s = mask_secrets(s)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _session_start(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from memlet import memlet_dream as md
    with md._enter_read_scope() as state:
        return _session_start_scoped(root, payload, st, _read_state=state)


def _session_start_scoped(root: Path, payload: Dict[str, Any], st: Dict[str, Any], *,
                          _read_state: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    from memlet import memlet_bridge as mb, memlet_dream as md, memlet_wiki as mw
    cap = mw.build_handover(root, output_format="json", write=False, budget=2000, mode="verified",
                            _read_state=_read_state)
    active = [a for a in cap.get("activeConstraints") or [] if isinstance(a, dict)
              and not str(a.get("source", "")).startswith(mb.PROMOTED_PAGE + ":")]
    health = (cap.get("executionProof") or {}).get("anchorHealth") or {}
    stale = int(health.get("stale") or 0)
    reverify = int(health.get("compatible-unverified") or 0)
    omitted = int((cap.get("omitted") or {}).get("activeConstraints") or 0)
    pend = mb.learn_pending(root, _record_review=False, _read_state=_read_state)
    pending: Optional[int] = None
    if isinstance(pend, dict) and pend.get("ok") and isinstance(pend.get("counts"), dict):
        pending = int(pend["counts"].get("pendingNodes") or 0)
    else:  # unknown is not zero: the count is left out, never printed as 0
        _log(root, "session-start", "pending lessons unavailable: queue not readable")
    from memlet.memlet_dreamcycle import dream_status
    status_result = dream_status(root, _read_state=_read_state) if st.get("dream") else {}
    waiting = status_result.get("waiting", 0)
    hints = status_result.get("counts", {}).get(TIER_HINT, 0)
    approved = mb.dream_items(root, _read_state=_read_state)["counts"]["approved"]
    if not active and not stale and not reverify and not pending and not waiting and not hints and not approved:
        return None
    head = "Memlet verified rules (code anchors match; source cited):"
    pending_part = ("" if pending is None else
                    f", {pending} lesson(s) pending human approval (`memlet learn --pending`)")
    status = (f"Not verified, do not rely on: {stale} stale anchor(s), {reverify} to re-verify{pending_part}; "
              "see `memlet check .`.")
    if st.get("dream") and pending is not None:
        status = status.replace(f"{pending} lesson(s) pending human approval (`memlet learn --pending`)",
                                f"{pending} unapproved lesson(s), of which {hints} shown as unreviewed hint(s) (`memlet learn --pending`)")
    if approved:
        status += f" {approved} approved lesson(s); read `memlet recall` or `memlet handover`."
    if waiting:
        status += f" {waiting} session(s) waiting to dream; run `memlet dream --via …`."
    items = [f"- {suggested_label(a)}{_clean(a.get('rule'), 160)} ({_clean(a.get('source'), 80)})" for a in active]

    def render(n: int) -> str:
        body = items[:n] if items else ["- (none)"]
        md._count_work("sessionStartItemRenders", len(body))
        more = len(items) - n + omitted if items else omitted
        tail = [f"- (+{more} more verified: `memlet handover`)"] if more > 0 else []
        return "\n".join([head] + body + tail + ["", status])

    item_toks = [mw.estimate_tokens(it) for it in items]
    md._count_work("sessionStartItemRenders", len(items))
    body_toks = sum(item_toks)
    fixed_toks = mw.estimate_tokens(head) + mw.estimate_tokens(status)
    none_toks = mw.estimate_tokens("- (none)")

    def _toks_at(n: int) -> int:
        b = body_toks if items and n > 0 else (0 if items and n == 0 else none_toks)
        if items and n == 0:
            # render(0) when items is non-empty uses items[:0] == []
            b = 0
        more = len(items) - n + omitted if items else omitted
        t = mw.estimate_tokens(f"- (+{more} more verified: `memlet handover`)") if more > 0 else 0
        return fixed_toks + b + t

    n = len(items)
    while n > 0 and _toks_at(n) > st["tokens"]:
        n -= 1
        body_toks -= item_toks[n]
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
            f"{suggested_label(a)}\"{_clean(a.get('rule'), 160)}\" — source {_clean(a.get('source'), 80)}" for a in affected[:_MAX_LISTED]]


def _post_tool_use(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from memlet import memlet_bridge as mb
    p = _edited_file(payload, root)
    if p is None:
        return None
    res = mb.changed_check(root, files=[str(p)])
    if not isinstance(res, dict) or res.get("exitCode") == 2:
        _log(root, "post-tool-use", "check failed: exitCode=2")
        return None
    flagged = [a for a in res.get("affected") or [] if isinstance(a, dict) and a.get("status") in _FLAG_STATES]
    approved = res.get("approvedLessons", [])
    hints = res.get("hints", [])
    if not flagged and not approved and not hints:
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
    if not flagged:
        text = "Memlet: lessons touching `" + _clean(rel, 200) + "`:"
    if approved:
        text += "\n\n### Approved lessons\n" + "\n".join(mb.render_approved_lesson(i) for i in approved[:_MAX_LISTED])
    if hints:
        text += "\n\n### " + mb.HINT_LABEL + "\n" + mb.HINT_HELP + "\n" + "\n".join(
            "- [hint; " + mb.hint_item_label(i) + "]\n" + mb._lesson_quote_block(i) for i in hints[:_MAX_LISTED])
    if not flagged:
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}
    first = flagged[0]
    msg = (f"Memlet: {stale} stale / {len(flagged) - stale} re-verify rule(s) after editing {_clean(rel, 80)} "
           f"(e.g. {_clean(first.get('source'), 60)})")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}, "systemMessage": msg}


@contextlib.contextmanager
def _without_own_text(mb: Any):
    """Hook-path learning only (Stop / PreCompact): a transcript line (one visible message) that contains a fixed
    text only Memlet's 9.2 hooks print (`vocabulary.HOOK_OWN_TEXT_MARKERS`: a denial, its tail, a JIT head, a
    capsule heading, ...) quotes Memlet, not the person, and becomes an empty line before anything is learned or
    snapshotted. Empty, not deleted: line numbers (pins) and the snapshot's identity stay as 9.1.1 has them, and a
    transcript without such text is read exactly as before."""
    from memlet import memlet_dream as md
    real = mb._transcript_lines

    def filtered(f: Path):  # the readers already blank these lines; this keeps the hook path safe on its own
        lines, stats = real(f)
        return ["" if md.memlet_echo(ln) else ln for ln in lines], stats

    mb._transcript_lines = filtered
    try:
        yield
    finally:
        mb._transcript_lines = real


def _learn(root: Path, payload: Dict[str, Any], st: Dict[str, Any], event: str) -> Optional[Dict[str, Any]]:
    from memlet import memlet_bridge as mb
    from memlet import memlet_dream as md
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
        with _without_own_text(mb):
            if st.get("dream"):
                from memlet.memlet_dreamcycle import dream_ingest
                res = dream_ingest(root, str(p))
            else:
                res = mb.learn_from_transcript(root, str(p))
    finally:
        md.QUEUE_LOCK_TIMEOUT = previous
    if not isinstance(res, dict) or not res.get("ok"):
        code = res.get("exitCode") if isinstance(res, dict) else "?"
        _log(root, event, f"learn failed: exitCode={code}")
        return None
    if st.get("dream"):
        return {"systemMessage": f"Memlet: {res.get('waiting', 0)} session(s) waiting to dream; run `memlet dream --via …`."}
    added = len(res.get("addedNodeIds") or [])
    if not added:
        return None
    return {"systemMessage": f"Memlet: {added} new lesson(s) queued as PENDING (nothing approved); review with "
                             "`memlet learn --pending`."}


def _git_check(root: Path, event: str) -> Optional[str]:
    from memlet import memlet_bridge as mb
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


# ---------------------------------------------------------------------------------------------------------------
# 9.2: memory at the moment of the edit (PreToolUse `--jit` / `--guard`), the compaction capsule (`--capsule`) and
# the per-session state they share. Nothing here calls a model, launches a vendor or writes outside `.memlet/`.
# What a hook shows comes from ONE place, `memlet_graph.neighbors` (through `_neighbors`), and passes the gate
# `_checked_neighbors` first. Every hook output is fixed labels from `vocabulary.py` plus whole items quoted from a
# re-hashable source (an instruction-file line or cited snapshot lines), each quoted line prefixed with `  > `.

JIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
JIT_MAX_CHARS = 2000            # hard cap of the PreToolUse additionalContext (the token budget usually binds first)
GUARD_MAX_CHARS = 8000          # permissionDecisionReason (Claude Code context strings: 10,000)
CAPSULE_MAX_CHARS = 6000        # PreCompact capsule (Claude Code cuts each hook's output at 10,000 characters)
SESSION_START_MAX_CHARS = 9000  # SessionStart additionalContext with the capsule (context strings: 10,000)
ITEM_MAX_CHARS = 16384          # one larger item makes the whole graph answer inadmissible
ITEM_MAX_CITES = 64
GRAPH_BUDGET = 8000             # tokens asked of `neighbors` (its own maximum); hooks trim further, whole items only
CAPSULE_MAX_FILES = 12
CAPSULE_RESERVE_SECONDS = 1.0   # kept back from the hook deadline so the 9.1.1 output is always printed
PRINT_RESERVE_SECONDS = 0.1     # kept back after session bookkeeping for printing the kept 9.1.1 result
SESSION_NOTE_GRACE = 0.5        # at most this, after a 9.1.1 SessionStart timeout, to record the session start
REARM_FLOOR_SECONDS = 0.5       # after 9.2 work, the hook alarm leaves at least this for printing the kept result
_DISPLAY_ORDER = ("blocks", "replaced", "governs", "resolves", "stale", "hints")
_TRIM_RANK = {"hints": 0, "resolves": 1, "stale": 2, "governs": 3, "replaced": 4, "blocks": 4}  # lowest goes first
_GUARD_GROUPS = ("blocks", "replaced")
_RELATIONS = ("SUPERSEDES", "BLOCKS", "RESOLVES", "GOVERNS")
_RULE_STATES = ("active", "compatible-unverified", "stale", "unpinned", "path-exists", "unanchored")
_WHY_REPLACED_RULE = "replaced-rule-text"

SESSIONS_REL = ".memlet/sessions"
SESSION_SCHEMA = "memlet-hook-session/v2"
SESSION_KEYS = ("schema", "createdAt", "updatedAt", "startSource", "mayGuard", "guardDisabled", "recovered",
                "touched", "shown", "guard")
SESSION_SOURCES = ("startup", "clear", "resume", "fork", "compact", "")
SESSION_MAX_BYTES = 64 * 1024
SESSION_MAX_FILES = 64                  # session files kept, newest first (by modification time)
SESSION_MAX_AGE_SECONDS = 14 * 86400    # older session files are deleted (that only ever turns a guard off)
SESSION_MAX_TOUCHED = 48
SESSION_MAX_SHOWN = 256                 # JIT digests; the oldest is forgotten first (the context may then repeat)
SESSION_MAX_GUARD = 128                 # guard decisions; never forgotten: at the bound the session stops guarding
STATE_LOCK_TIMEOUT = 1.0
_SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCOPE_RE = re.compile(r"(?:main|agent:[A-Za-z0-9][A-Za-z0-9_-]{0,127}|agent:h[0-9a-f]{24})\Z")


class _Plain(str):
    """Hook output printed to stdout as plain text (PreCompact instructions), not as JSON."""


# -- the graph, and the gate in front of it ---------------------------------------------------------------------


def _graph_neighbors() -> Optional[Callable[..., Any]]:
    """`memlet.memlet_graph.neighbors`, or None when it cannot be imported (the hooks then say nothing)."""
    try:
        from memlet import memlet_graph
    except Exception:
        return None
    fn = getattr(memlet_graph, "neighbors", None)
    return fn if callable(fn) else None


def _neighbors(root: Path, target: str, *, tiers: Tuple[str, ...] = (TIER_APPROVED,)) -> Optional[Dict[str, Any]]:
    """The ONE call the hooks make into memory: `memlet_graph.neighbors`, then `_checked_neighbors`. None = cannot
    decide (import or call failed, `ok` false, a historical answer, or anything the gate refuses): say nothing,
    never deny."""
    impl = _graph_neighbors()
    if impl is None:
        _log(root, "graph", "memlet_graph unavailable; nothing said")
        return None
    try:
        res = impl(str(root), target, tiers=tuple(tiers), budget=GRAPH_BUDGET)
    except _Deadline:
        raise
    except Exception as exc:
        _log(root, "graph", "neighbors failed: " + _where(exc))
        return None
    checked = _checked_neighbors(res, tuple(tiers), root)
    if checked is None:
        _log(root, "graph", "answer refused by the hook gate; nothing said")
    return checked


def _quote_lines(item: Dict[str, Any]) -> List[str]:
    q = item.get("quoteLines")
    if isinstance(q, list):
        return list(q)
    t = item.get("text")
    return [t] if isinstance(t, str) else []


def _is_str(value: Any, limit: int = 4096) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value


_RULE_WHY = ("anchor-in-file", "anchor-symbol")
_LESSON_WHY = ("lesson-path", "lesson-cites-path")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def _usable_cite(root: Optional[Path], c: Any, *, rule_source: Optional[str]) -> bool:
    """A citation a reader can re-hash: an existing contained regular file (a lesson: under the Dream snapshot; a
    rule: its own source file at its own line), positive integer lines with start <= end, a SHA-256 digest, and not
    marked unverified."""
    from memlet import memlet_bridge as mb
    from memlet import memlet_wiki as mw
    if not isinstance(c, dict) or not _is_str(c.get("file"), 1024):
        return False
    start, end, rel = c.get("start"), c.get("end"), c["file"]
    if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int)
            or not 1 <= start <= end):
        return False
    if not isinstance(c.get("sha256"), str) or not _SHA_RE.match(c["sha256"]) or c.get("verified") is False:
        return False
    if rule_source is None:
        if not rel.startswith(mb.SNAPSHOT_REL + "/"):
            return False
    else:
        path, _sep, line = rule_source.rpartition(":")
        if rel != path or not line.isdigit() or int(line) != start:
            return False
    if root is None:  # a caller without a repository (diagnostics): everything but the file's existence
        return mw._rel_parts(rel) is not None
    return mw.contained_path_problem(root, rel) is None and (root / rel).is_file()


def _admissible(root: Optional[Path], item: Any, group: str, hints_on: bool) -> bool:
    """The gate for one item of a `neighbors` answer: structure AND meaning; a graph answer that disagrees with
    these rules (or with itself) is not acted on.
    - blocks / resolves / governs (live groups): current items only (`current` true; no replaced line role, no
      quarantine, no replacement label, no `replacedBy`, no lesson `status`); blocks: an approved lesson whose
      relation is BLOCKS and listed among its relations; resolves: likewise RESOLVES; governs: likewise GOVERNS, or
      a rule with status `active`, relation GOVERNS and no unknown replacement;
    - replaced: an approved quarantined supersession pair with SUPERSEDES among its relations and at least one
      replaced line, or a rule (relation SUPERSEDES) with `why` "replaced-rule-text" and a non-empty `replacedBy`;
    - stale: a rule (relation GOVERNS, any anchor state) that is not replaced;
    - hints: tier `hint` lessons, only when hints were asked for.
    Every item: string `id` / `kind` / `relation` / `source` / `why` (a value of its kind), relations from the four
    (a rule exactly its one relation), `quoteLines` a list of whole strings with one usable citation each (see
    `_usable_cite`), at most MAX_LESSON_LINES lines and ITEM_MAX_CHARS characters."""
    if not isinstance(item, dict):
        return False
    for key in ("id", "kind", "relation", "source", "why"):
        if not _is_str(item.get(key), 200 if key == "id" else 4096):
            return False
    quotes, cites, text = item.get("quoteLines"), item.get("cites"), item.get("text")
    if (not isinstance(text, str) or not isinstance(quotes, list) or not quotes
            or len(quotes) > voc.MAX_LESSON_LINES or not all(isinstance(q, str) and q for q in quotes)
            or sum(len(q) for q in quotes) + len(text) > ITEM_MAX_CHARS):
        return False
    if not isinstance(cites, list) or len(cites) != len(quotes) or len(cites) > ITEM_MAX_CITES:
        return False
    relations = item.get("relations")
    if (not isinstance(relations, list) or not relations or not all(isinstance(r, str) for r in relations)
            or not set(relations) <= set(_RELATIONS)):
        return False
    kind, tier, relation, why = item["kind"], item.get("tier"), item["relation"], item["why"]
    roles = item.get("lineRoles")
    live = group in ("blocks", "resolves", "governs")
    if live and (item.get("current") is not True or item.get("quarantined") or item.get("replacementLabel")
                 or item.get("replacedBy") or (roles is not None and (not isinstance(roles, list) or "replaced" in roles))):
        return False
    if kind == "rule":
        status, replaced_by = item.get("status"), item.get("replacedBy")
        if (tier is not None or status not in _RULE_STATES or relations != [relation]
                or not all(_usable_cite(root, c, rule_source=item["source"]) for c in cites)):
            return False
        if group == "replaced":
            return (relation == "SUPERSEDES" and why == _WHY_REPLACED_RULE and isinstance(replaced_by, list)
                    and bool(replaced_by) and all(_is_str(x, 200) for x in replaced_by))
        if replaced_by or relation != "GOVERNS" or why not in _RULE_WHY:
            return False
        if group == "governs":
            return status == "active" and item.get("replacementUnknown") is False
        return group == "stale"
    if kind != "lesson" or "status" in item or not all(_usable_cite(root, c, rule_source=None) for c in cites):
        return False
    if group == "hints":
        return (hints_on and tier == TIER_HINT and why in _LESSON_WHY and relation == ",".join(relations)
                and item.get("current") is not False)
    if tier != TIER_APPROVED:
        return False
    if group == "replaced":
        return (item.get("quarantined") is True and relation == "SUPERSEDES" and "SUPERSEDES" in relations
                and why in _LESSON_WHY + (_WHY_REPLACED_RULE,) and isinstance(roles, list)
                and len(roles) == len(quotes) and set(roles) <= {"replaced", "current"} and "replaced" in roles)
    want = {"blocks": "BLOCKS", "resolves": "RESOLVES", "governs": "GOVERNS"}.get(group)
    return want is not None and relation == want and want in relations and why in _LESSON_WHY


def _checked_neighbors(res: Any, tiers: Tuple[str, ...], root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The whole answer or nothing. Refused: a failed or historical answer; a missing or unknown group; any
    inadmissible item; and bookkeeping that is missing, mistyped or inconsistent (`historical` false, `truncated`
    a bool and true whenever a group is incomplete, `incompleteGroups` known group names, `blocksIncomplete` /
    `replacedIncomplete` bools that agree with it, `omitted` counts only for incomplete groups). Absence is never
    read as completeness. `guardComplete` is true only when blocks and replaced are complete."""
    if not isinstance(res, dict) or res.get("ok") is not True or res.get("historical") is not False:
        return None
    raw = res.get("groups")
    if not isinstance(raw, dict) or set(raw) != set(voc.GRAPH_HEADINGS):
        return None
    hints_on = TIER_HINT in tiers
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for g in _DISPLAY_ORDER:
        items = raw[g]
        if not isinstance(items, list) or not all(_admissible(root, it, g, hints_on) for it in items):
            return None
        groups[g] = items
    incomplete, truncated = res.get("incompleteGroups"), res.get("truncated")
    bi, ri, raw_om = res.get("blocksIncomplete"), res.get("replacedIncomplete"), res.get("omitted")
    if (not isinstance(incomplete, list) or not all(isinstance(g, str) and g in raw for g in incomplete)
            or len(set(incomplete)) != len(incomplete) or not isinstance(truncated, bool)
            or not isinstance(bi, bool) or not isinstance(ri, bool)
            or bi != ("blocks" in incomplete) or ri != ("replaced" in incomplete) or (incomplete and not truncated)
            or not isinstance(raw_om, dict) or set(raw_om) - set(raw)):
        return None
    omitted: Dict[str, int] = {}
    for g, n in raw_om.items():
        if isinstance(n, bool) or not isinstance(n, int) or n < 0 or (n and g not in incomplete):
            return None
        if n and (g != "hints" or hints_on):
            omitted[g] = n
    return {"ok": True, "groups": groups, "omitted": omitted, "guardComplete": not bi and not ri}


def _guard_items(groups: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """What may guard: approved BLOCKS lessons and approved supersession pairs (a replaced rule is shown with them,
    it never guards alone; rules, hints and fixes never guard)."""
    return [it for g in _GUARD_GROUPS for it in groups.get(g) or [] if it.get("kind") == "lesson"]


# -- rendering (fixed labels + whole quoted items) --------------------------------------------------------------


def _cite_text(c: Dict[str, Any]) -> str:
    a, b = c.get("start"), c.get("end")
    return _clean(c.get("file"), 300) + f":{a}" + (f"-{b}" if b != a else "")


def _quote(text: Any) -> str:
    """Display sanitisation only (controls, hidden characters, comment markers, secrets); never shortened."""
    from memlet import memlet_bridge as mb
    from memlet.memlet_dream import mask_secrets
    s = mb.safe_lesson_display(str(text))
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)).strip()
    return mask_secrets(s)


def _item_lines(item: Dict[str, Any], group: str) -> List[str]:
    from memlet import memlet_bridge as mb, memlet_dream as md
    md._count_work("hookItemLineRenders", 1)
    quotes = _quote_lines(item)
    roles = item.get("lineRoles") if isinstance(item.get("lineRoles"), list) else []
    if item.get("kind") == "rule":
        source = _clean(item.get("source"), 300)
        if group == "stale":
            head = f"- {_clean(item.get('status'), 30).upper()}: rule at {source}"
        elif group == "replaced":
            head = (f"- {voc.HOOK_RULE_REPLACED} rule at {source} (by "
                    + ", ".join(_clean(x, 60) for x in item.get("replacedBy") or []) + ")")
        else:
            head = f"- {suggested_label(item)}rule at {source}"
    else:
        rels = [r for r in _RELATIONS if r in (item.get("relations") or [item.get("relation")])] or ["LESSON"]
        cites = ", ".join(_cite_text(c) for c in item.get("cites") or [])
        if group == "hints":
            head = f"- [hint; {_clean(mb.hint_item_label(item), 120)}] lesson {_clean(item.get('id'), 60)} (cites {cites})"
        else:
            head = f"- [{', '.join(rels)}] lesson {_clean(item.get('id'), 60)} (approved; cites {cites})"
    lines = [head]
    for i, q in enumerate(quotes):
        role = roles[i] if i < len(roles) else None
        label = voc.HOOK_ROLE_REPLACED if role == "replaced" else voc.HOOK_ROLE_CURRENT if role == "current" else ""
        lines.append("  > " + label + _quote(q))
    return lines


def _pointer(item: Dict[str, Any]) -> str:
    cites = ", ".join(_cite_text(c) for c in item.get("cites") or [])
    return voc.HOOK_POINTER % (_clean(item.get("id"), 60), cites)


def _fits(text: str, tokens: Optional[int], cap: int) -> bool:
    from memlet import memlet_dream as md, memlet_wiki as mw
    md._count_work("hookBlockFitChecks", 1)
    return len(text) <= cap and (tokens is None or mw.estimate_tokens(text) <= tokens)


def _render_block(head: List[str], entries: List[Tuple[Optional[str], str, Dict[str, Any]]], tail: List[str],
                  *, tokens: Optional[int], cap: int, omitted: int = 0) -> Tuple[Optional[str], List[int]]:
    """(text, indices of the entries shown in full). Whole items only: over the budget, items go in `_TRIM_RANK`
    order (hints first, prohibitions and replaced instructions last), within one rank the largest item first (so
    as many whole items as possible remain), then the latest. A dropped approved
    prohibition or supersession keeps one fixed pointer line (its citations and `memlet recall`) when that fits;
    everything else left out is counted. (None, []) when not even head and tail fit."""
    from memlet import memlet_wiki as mw

    n = len(entries)
    rendered_lines = [_item_lines(item, g) for _s, g, item in entries]
    sizes = [sum(len(ln) for ln in lines_i) + max(0, len(lines_i) - 1) for lines_i in rendered_lines]
    pointer_lines = [_pointer(item) if g in _GUARD_GROUPS and item.get("kind") == "lesson" else None
                     for _s, g, item in entries]
    kept_mask = [True] * n
    drop_seq = sorted(range(n), key=lambda i: (_TRIM_RANK[entries[i][1]], -sizes[i], -i))

    est = mw.estimate_tokens
    use_tok = tokens is not None
    head_list = list(head)
    tail_list = list(tail)

    base_lines = len(head_list) + len(tail_list)
    base_chars = sum(len(s) for s in head_list) + sum(len(s) for s in tail_list)
    base_toks = (sum(est(s) for s in head_list) + sum(est(s) for s in tail_list)) if use_tok else 0

    item_line_counts = [len(lines_i) for lines_i in rendered_lines]
    item_char_sums = [sum(len(ln) for ln in lines_i) for lines_i in rendered_lines]
    item_tok_sums = [sum(est(ln) for ln in lines_i) if use_tok else 0 for lines_i in rendered_lines]

    ptr_char_lens = [len(p) if p is not None else 0 for p in pointer_lines]
    ptr_tok_lens = [est(p) if (use_tok and p is not None) else 0 for p in pointer_lines]

    sec_tok_cache: Dict[str, int] = {}
    grp_tok_cache: Dict[str, int] = {}

    def _hdr_contribution(prev_idx: int, idx: int) -> Tuple[int, int, int]:
        sec, g, _ = entries[idx]
        if prev_idx == -1:
            need_sec = True
            need_grp = True
        else:
            p_sec, p_g, _ = entries[prev_idx]
            if sec != p_sec:
                need_sec = True
                need_grp = True
            else:
                need_sec = False
                need_grp = (g != p_g)
        lines_add = 0
        chars_add = 0
        toks_add = 0
        if need_sec and sec:
            lines_add += 1
            chars_add += len(sec)
            if use_tok:
                st = sec_tok_cache.get(sec)
                if st is None:
                    st = est(sec)
                    sec_tok_cache[sec] = st
                toks_add += st
        if need_grp:
            gl = voc.HOOK_GROUP_LABELS[g]
            lines_add += 1
            chars_add += len(gl)
            if use_tok:
                gt = grp_tok_cache.get(g)
                if gt is None:
                    gt = est(gl)
                    grp_tok_cache[g] = gt
                toks_add += gt
        return lines_add, chars_add, toks_add

    prev_link = [i - 1 for i in range(n)]
    next_link = [i + 1 if i + 1 < n else -1 for i in range(n)]

    body_lines = 0
    body_chars = 0
    body_toks = 0
    for i in range(n):
        hl, hc, ht = _hdr_contribution(i - 1, i)
        body_lines += hl + item_line_counts[i]
        body_chars += hc + item_char_sums[i]
        body_toks += ht + item_tok_sums[i]

    dropped_count = 0
    ptr_count = 0
    ptr_chars = 0
    ptr_toks = 0

    def _within_limits() -> bool:
        left = omitted + dropped_count - ptr_count
        tot_lines = base_lines + body_lines + ptr_count + (1 if left > 0 else 0)
        om_line = (voc.HOOK_OMITTED % left) if left > 0 else ""
        tot_chars = base_chars + body_chars + ptr_chars + len(om_line) + (tot_lines - 1 if tot_lines > 0 else 0)
        if tot_chars > cap:
            return False
        if use_tok:
            tot_toks = base_toks + body_toks + ptr_toks + (est(om_line) if left > 0 else 0)
            if tot_toks > tokens:
                return False
        return True

    drop_pos = 0
    while not _within_limits() and drop_pos < n:
        idx = drop_seq[drop_pos]
        drop_pos += 1
        kept_mask[idx] = False
        prv = prev_link[idx]
        nxt = next_link[idx]
        old_hl, old_hc, old_ht = _hdr_contribution(prv, idx)
        if nxt != -1:
            nxt_old_hl, nxt_old_hc, nxt_old_ht = _hdr_contribution(idx, nxt)
            nxt_new_hl, nxt_new_hc, nxt_new_ht = _hdr_contribution(prv, nxt)
            prev_link[nxt] = prv
        else:
            nxt_old_hl = nxt_old_hc = nxt_old_ht = 0
            nxt_new_hl = nxt_new_hc = nxt_new_ht = 0
        if prv != -1:
            next_link[prv] = nxt
        body_lines += nxt_new_hl - old_hl - nxt_old_hl - item_line_counts[idx]
        body_chars += nxt_new_hc - old_hc - nxt_old_hc - item_char_sums[idx]
        body_toks += nxt_new_ht - old_ht - nxt_old_ht - item_tok_sums[idx]
        dropped_count += 1
        if pointer_lines[idx] is not None:
            ptr_count += 1
            ptr_chars += ptr_char_lens[idx]
            ptr_toks += ptr_tok_lens[idx]

    out = list(head_list)
    section = group = object()
    pointers: List[str] = []
    keep: List[int] = []
    for i in range(n):
        if kept_mask[i]:
            keep.append(i)
            sec, g, _item = entries[i]
            if sec != section:
                section, group = sec, object()
                if sec:
                    out.append(sec)
            if g != group:
                group = g
                out.append(voc.HOOK_GROUP_LABELS[g])
            out.extend(rendered_lines[i])
        elif pointer_lines[i] is not None:
            pointers.append(pointer_lines[i])
    left = omitted + dropped_count - len(pointers)
    out.extend(pointers)
    if left > 0:
        out.append(voc.HOOK_OMITTED % left)
    text = "\n".join(out + tail_list)

    if not _fits(text, tokens, cap):  # the pointer lines themselves did not fit: count everything instead
        text = "\n".join(head_list + [voc.HOOK_OMITTED % (omitted + n)] + tail_list) if entries else \
            "\n".join(head_list + tail_list)
        return (text, []) if _fits(text, tokens, cap) else (None, [])
    return text, keep


def _entries(groups: Dict[str, List[Dict[str, Any]]], names: Tuple[str, ...], section: Optional[str] = None,
             seen: Optional[set] = None) -> List[Tuple[Optional[str], str, Dict[str, Any]]]:
    out = []
    for g in _DISPLAY_ORDER:
        if g not in names:
            continue
        for item in groups.get(g) or []:
            key = (g, str(item.get("id")))
            if seen is not None:
                if key in seen:
                    continue
                seen.add(key)
            out.append((section, g, item))
    return out


def _digest(items: List[Tuple[str, Dict[str, Any]]], omitted: Optional[Dict[str, int]] = None) -> str:
    """Identity of an item set: group, id, tier/status, relations, citation hashes and a hash of the quoted lines
    (plus how many items the graph left out). No wall-clock time, no text."""
    rows: List[Any] = [["omitted", sorted((omitted or {}).items())]] if omitted else []
    for g, it in items:
        cites = sorted(str(c.get("sha256") or "") + "@" + _cite_text(c) for c in it.get("cites") or [])
        body = json.dumps([_quote_lines(it), it.get("lineRoles"), it.get("source"), it.get("replacedBy")],
                          sort_keys=True, default=str)
        rows.append([g, str(it.get("id")), str(it.get("tier") or ""), str(it.get("status") or ""),
                     sorted(str(r) for r in it.get("relations") or [it.get("relation")]), cites,
                     hashlib.sha256(body.encode("utf-8", "surrogateescape")).hexdigest()])
    return _sha(json.dumps(sorted(rows), sort_keys=True).encode("utf-8"))


# -- the edited file --------------------------------------------------------------------------------------------


def _path_char_bad(c: str) -> bool:
    import unicodedata
    from memlet import memlet_bridge as mb
    return unicodedata.category(c) in ("Cc", "Cf", "Mn", "Me") or mb._hidden_character(c)


def _repo_file(root: Path, payload: Dict[str, Any]) -> Optional[str]:
    """The key of the file a Write/Edit/MultiEdit/NotebookEdit call names: its real repo-relative path, every
    component spelled as the directory entry on disk (a case-insensitive file system may be handed `Mod.py` for
    `mod.py`). The parent directory is resolved (so /tmp vs /private/tmp, `./` and symlinked ancestors give one
    key); the leaf must be a regular file or not exist yet (a file to be created). None for: a trailing slash, `.`
    or `..` parts, backslashes or hidden/control characters, a missing parent, a directory, a symlink, a path
    outside the repository, inside `.git/` or `.memlet/`, or inside a nested repository or another worktree."""
    from memlet import memlet_wiki as mw
    ti = payload.get("tool_input")
    tr = payload.get("tool_response")
    fp = None
    if isinstance(ti, dict):
        fp = _text(ti.get("file_path")) or _text(ti.get("notebook_path"))
    if fp is None and isinstance(tr, dict):
        fp = _text(tr.get("filePath"))
    if (fp is None or fp.endswith("/") or "\\" in fp or any(_path_char_bad(c) for c in fp)
            or re.search(r"(?:^|/)\.\.(?:/|$)", fp)):  # `./` parts are the same file; `..` is refused
        return None
    p = Path(fp)
    if not p.is_absolute():
        cwd = _text(payload.get("cwd"))
        if not (cwd and os.path.isabs(cwd) and Path(cwd).is_dir()):
            return None
        p = Path(cwd) / p
    try:
        parent = Path(os.path.realpath(str(p.parent)))
        rel_parent = parent.relative_to(root) if parent != root else Path(".")
    except (OSError, ValueError, RuntimeError):
        return None
    if not parent.is_dir():
        return None
    parts: List[str] = []
    cur = root
    wanted = [x for x in rel_parent.parts if x != "."] + [p.name]
    for n, part in enumerate(wanted):
        try:
            names = os.listdir(str(cur))
        except OSError:
            return None
        if part in names:
            name = part
        else:
            same = [x for x in names if x.casefold() == part.casefold()]
            if len(same) == 1 and os.path.lexists(str(cur / part)):
                name = same[0]  # the same directory entry, named in another case
            elif not same and n == len(wanted) - 1:
                name = part  # a file to be created
            else:
                return None
        parts.append(name)
        cur = cur / name
    rel = "/".join(parts)
    if not rel or len(rel) > 1024 or parts[0] in (".git", ".memlet"):
        return None
    leaf = root / rel
    if os.path.lexists(str(leaf)) and (leaf.is_symlink() or not leaf.is_file()):
        return None
    if mw.contained_path_problem(root, rel):
        return None
    for i in range(1, len(parts)):  # a `.git` below the root: a nested repository or another worktree
        if os.path.lexists(str(root.joinpath(*parts[:i], ".git"))):
            return None
    return rel


# -- per-session state ------------------------------------------------------------------------------------------
# Deny-once, kept deliberately simple: a session may deny only when its own state says it was first seen at a
# SessionStart `startup` or `clear`; its guard history is a set of (agent scope, file, item-set digest) that is never
# shortened while the session can still deny; at the bound the session stops guarding (written before answering).
# Anything unexpected in the state means "corrupt": that session never denies again. No decision depends on a clock.


def _now() -> int:
    return int(time.time())


def _session_rel(sid: Any) -> Optional[str]:
    """`.memlet/sessions/s-<id>.json`: the id itself when it is a plain token, else a hash of it (never a path)."""
    if not isinstance(sid, str) or not sid or len(sid) > 4096 or "\x00" in sid:
        return None
    key = sid if _SID_RE.match(sid) else "h" + hashlib.sha256(sid.encode("utf-8", "surrogateescape")).hexdigest()[:40]
    return f"{SESSIONS_REL}/s-{key}.json"


def _scope_of(payload: Dict[str, Any]) -> str:
    """Whose context an item reached: the main thread, or one subagent (`agent_id`)."""
    if payload.get("agent_id") is None:
        return "main"
    aid = payload.get("agent_id")
    aid = aid if isinstance(aid, str) else json.dumps(aid, default=str)
    return "agent:" + (aid if _SID_RE.match(aid) else
                       "h" + hashlib.sha256(aid.encode("utf-8", "surrogateescape")).hexdigest()[:24])


def _new_state(*, source: str = "", may_guard: bool = False, recovered: bool = False) -> Dict[str, Any]:
    now = _now()
    return {"schema": SESSION_SCHEMA, "createdAt": now, "updatedAt": now, "startSource": source,
            "mayGuard": bool(may_guard) and not recovered, "guardDisabled": bool(recovered), "recovered": recovered,
            "touched": [], "shown": [], "guard": []}


def _triples(value: Any, limit: int) -> bool:
    return (isinstance(value, list) and len(value) <= limit and all(
        isinstance(t, list) and len(t) == 3 and isinstance(t[0], str) and _SCOPE_RE.match(t[0])
        and _is_str(t[1], 1024) and isinstance(t[2], str) and _DIGEST_RE.match(t[2]) for t in value))


def _valid_state(doc: Any) -> bool:
    """Field by field; anything missing, extra, mistyped, out of bounds or dated in the future fails."""
    if not isinstance(doc, dict) or set(doc) != set(SESSION_KEYS) or doc["schema"] != SESSION_SCHEMA:
        return False
    now = _now()
    for k in ("createdAt", "updatedAt"):
        v = doc[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > now + 1:
            return False
    if doc["createdAt"] > doc["updatedAt"]:
        return False
    if not all(isinstance(doc[k], bool) for k in ("mayGuard", "guardDisabled", "recovered")):
        return False
    if doc["startSource"] not in SESSION_SOURCES:
        return False
    if doc["mayGuard"] and (doc["startSource"] not in ("startup", "clear") or doc["recovered"]):
        return False
    touched = doc["touched"]
    if (not isinstance(touched, list) or len(touched) > SESSION_MAX_TOUCHED or len(set(map(str, touched))) != len(touched)
            or not all(_is_str(t, 1024) for t in touched)):
        return False
    return _triples(doc["shown"], SESSION_MAX_SHOWN) and _triples(doc["guard"], SESSION_MAX_GUARD)


def _load_state(root: Path, rel: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """(state, 'ok' | 'absent' | 'corrupt'). A symlink, a non-regular file, an oversized or unparsable file or any
    invalid field is 'corrupt'."""
    from memlet import memlet_wiki as mw
    p = root / rel
    if not os.path.lexists(str(p)):
        return None, "absent"
    if mw.managed_path_problem(root, rel):
        return None, "corrupt"
    raw = mw.read_regular_bytes(p, SESSION_MAX_BYTES)
    try:
        doc = json.loads((raw or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, "corrupt"
    return (doc, "ok") if _valid_state(doc) else (None, "corrupt")


def _state_bytes(state: Dict[str, Any]) -> bytes:
    return (json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _save_doc(root: Path, rel: str, data: bytes) -> None:
    from memlet import memlet_wiki as mw
    mw.write_managed_bytes(root, rel, data, new_mode=0o600)


def _fits_state(state: Dict[str, Any]) -> bool:
    """Forget touched files and JIT digests oldest-first until the state fits SESSION_MAX_BYTES; guard decisions are
    never forgotten. False when it still does not fit."""
    state["updatedAt"] = max(_now(), state["createdAt"])
    del state["touched"][:-SESSION_MAX_TOUCHED]
    del state["shown"][:-SESSION_MAX_SHOWN]
    while len(_state_bytes(state)) > SESSION_MAX_BYTES and (state["shown"] or len(state["touched"]) > 1):
        del (state["shown"] if state["shown"] else state["touched"])[0]
    return len(_state_bytes(state)) <= SESSION_MAX_BYTES


def _save_state(root: Path, rel: str, state: Dict[str, Any], *, created: bool) -> None:
    """Bounded write (see `_fits_state`); a state that cannot fit is not written (ValueError), and the caller then
    treats any decision as unrecorded. A state that can no longer deny (corrupt, or guarding disabled) also leaves a
    tombstone for its id. Call with the state lock held."""
    if not _fits_state(state):
        raise ValueError("session state over its size cap")
    _save_doc(root, rel, _state_bytes(state))
    if state["recovered"] or state["guardDisabled"]:
        _add_tombstones(root, [rel])
    if created:
        _prune_sessions(root, keep=rel)


# Tombstones: a bounded set of salted hashes of session ids whose state Memlet pruned or stopped guarding, so a
# later `startup` / `clear` with the same id gets a state that can never deny (an id is never re-armed). Residual:
# only the newest TOMBSTONE_MAX ids are kept, so an id pruned more than that many prunes ago counts as new. While the
# tombstone file is unreadable or malformed no session may deny; the next tombstone write replaces it (a new salt,
# the earlier ids are then forgotten).
TOMBSTONES_REL = SESSIONS_REL + "/tombstones.json"
TOMBSTONE_SCHEMA = "memlet-hook-tombstones/v1"
TOMBSTONE_MAX = 8192
TOMBSTONE_MAX_BYTES = 1024 * 1024
_HEX32_RE = re.compile(r"[0-9a-f]{32}\Z")


def _load_tombstones(root: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    from memlet import memlet_wiki as mw
    p = root / TOMBSTONES_REL
    if not os.path.lexists(str(p)):
        return None, "absent"
    if mw.managed_path_problem(root, TOMBSTONES_REL):
        return None, "corrupt"
    raw = mw.read_regular_bytes(p, TOMBSTONE_MAX_BYTES)
    try:
        doc = json.loads((raw or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, "corrupt"
    if (not isinstance(doc, dict) or set(doc) != {"schema", "salt", "ids"} or doc["schema"] != TOMBSTONE_SCHEMA
            or not isinstance(doc["salt"], str) or not _HEX32_RE.match(doc["salt"]) or not isinstance(doc["ids"], list)
            or len(doc["ids"]) > TOMBSTONE_MAX or not all(isinstance(x, str) and _HEX32_RE.match(x) for x in doc["ids"])):
        return None, "corrupt"
    return doc, "ok"


def _tombstone_key(doc: Dict[str, Any], rel: str) -> str:
    return hashlib.sha256((doc["salt"] + ":" + rel).encode("utf-8", "surrogateescape")).hexdigest()[:32]


def _tombstoned(root: Path, rel: str) -> bool:
    """True when `rel` may not be armed: its id is in the tombstones, or the tombstone file is unusable."""
    doc, status = _load_tombstones(root)
    if status == "absent":
        return False
    return doc is None or _tombstone_key(doc, rel) in doc["ids"]


def _add_tombstones(root: Path, rels: List[str]) -> None:
    doc, status = _load_tombstones(root)
    if doc is None:
        doc = {"schema": TOMBSTONE_SCHEMA, "salt": os.urandom(16).hex(), "ids": []}
    keys = [_tombstone_key(doc, r) for r in rels]
    fresh = [k for k in dict.fromkeys(keys) if k not in doc["ids"]]
    if not fresh and status == "ok":
        return
    doc["ids"] = (doc["ids"] + fresh)[-TOMBSTONE_MAX:]
    _save_doc(root, TOMBSTONES_REL, (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))


def _prune_sessions(root: Path, keep: str) -> None:
    """Delete session files older than SESSION_MAX_AGE_SECONDS, then the oldest beyond SESSION_MAX_FILES. Regular
    `s-*.json` files directly in `.memlet/sessions/` only; never follows a symlink. A deleted session is unknown
    afterwards, and an unknown session never denies; its id is tombstoned first, so a later `startup` / `clear`
    with the same id cannot re-arm it either: pruning can only turn a guard off."""
    from memlet import memlet_wiki as mw
    d = root / SESSIONS_REL
    if mw.managed_path_problem(root, SESSIONS_REL, leaf="dir"):
        return
    found: List[Tuple[float, str]] = []
    with contextlib.suppress(OSError):
        with os.scandir(str(d)) as it:
            for n, entry in enumerate(it):
                if n > 10000:
                    break
                if entry.name.startswith("s-") and entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    with contextlib.suppress(OSError):
                        found.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
    now = time.time()
    found.sort(reverse=True)
    doomed = [f"{SESSIONS_REL}/{name}" for i, (mtime, name) in enumerate(found)
              if f"{SESSIONS_REL}/{name}" != keep and (now - mtime > SESSION_MAX_AGE_SECONDS or i >= SESSION_MAX_FILES)
              and not mw.managed_path_problem(root, f"{SESSIONS_REL}/{name}")]
    if not doomed:
        return
    _add_tombstones(root, doomed)  # first: if this fails, nothing is pruned (a pruned id must never re-arm)
    for rel in doomed:
        with contextlib.suppress(OSError):
            os.unlink(str(root / rel))


@contextlib.contextmanager
def _state_lock(root: Path):
    """One short lock for every session-state change of this repository (the per-queue lock machinery of
    `memlet_dream.queue_lock`, on its own lock file). TimeoutError when another hook holds it too long."""
    from memlet import memlet_dream as md
    with md.queue_lock(root / SESSIONS_REL / "state.lock", timeout=STATE_LOCK_TIMEOUT):
        yield


def _open_state(root: Path, rel: str) -> Tuple[Dict[str, Any], bool]:
    """(state, created). Missing: a new state that may not deny. Corrupt: replaced by a state that may never deny
    (`recovered`, `guardDisabled`); the file is never repaired into one that could."""
    state, status = _load_state(root, rel)
    if state is not None:
        return state, False
    return _new_state(recovered=status == "corrupt"), True


def _touch(state: Dict[str, Any], rel: str) -> None:
    if rel in state["touched"]:
        state["touched"].remove(rel)
    state["touched"].append(rel)


def _record_touch(root: Path, payload: Dict[str, Any]) -> None:
    """The edited file joins the session's touched files (PostToolUse, and PreToolUse with --capsule alone). Run
    through `_best_effort`: it never changes the hook's output."""
    rel = _repo_file(root, payload)
    srel = _session_rel(payload.get("session_id"))
    if rel is None or srel is None:
        return
    with _state_lock(root):
        state, created = _open_state(root, srel)
        _touch(state, rel)
        _save_state(root, srel, state, created=created)


def _note_session_start(root: Path, payload: Dict[str, Any]) -> None:
    """A session first seen at SessionStart `startup` / `clear` may deny, unless its id is tombstoned (pruned or
    disabled before) or the tombstones are unusable; one first seen any other way never does. An existing (or
    unreadable) state is never re-labelled. Run through `_best_effort`."""
    srel = _session_rel(payload.get("session_id"))
    if srel is None:
        return
    source = payload.get("source") if payload.get("source") in SESSION_SOURCES else ""
    with _state_lock(root):
        if os.path.lexists(str(root / srel)):
            return
        may = source in ("startup", "clear") and not _tombstoned(root, srel)
        _save_state(root, srel, _new_state(source=source, may_guard=may), created=True)


# -- PreToolUse -------------------------------------------------------------------------------------------------


def _hints_enabled(root: Path) -> bool:
    from memlet import memlet_bridge as mb
    cfg, _err = _read_config(root)
    return bool(mb.dream_settings(cfg or {})[0])


def _guard_reason(groups: Dict[str, List[Dict[str, Any]]], guarding: List[Dict[str, Any]], mode: str,
                  shown_path: str) -> Tuple[Optional[str], List[Tuple[str, Dict[str, Any]]]]:
    """(reason, the guarding lessons it shows in full). The tail names what was shown in full and, when another
    prohibition only got its pointer line, says so by id; None when no guarding lesson fits whole."""
    gentries = [(None, g, it) for g in _GUARD_GROUPS for it in groups[g]]
    head = (voc.GUARD_DENY_HEAD if mode == "deny" else voc.GUARD_ASK_HEAD) % shown_path
    full_tail = voc.GUARD_DENY_TAIL if mode == "deny" else voc.GUARD_ASK_TAIL
    tail = full_tail
    for _attempt in range(3):  # the tail depends on what fits, and what fits on the tail
        text, full = _render_block([head, voc.HOOK_QUOTE_NOTE], gentries, [tail], tokens=None, cap=GUARD_MAX_CHARS)
        shown = [(gentries[i][1], gentries[i][2]) for i in full if any(gentries[i][2] is it for it in guarding)]
        if text is None or not shown:
            return None, []
        pointed = [it for it in guarding if not any(it is x for _g, x in shown)]
        want = full_tail if not pointed else (voc.GUARD_DENY_PARTIAL_TAIL if mode == "deny" else voc.GUARD_ASK_PARTIAL_TAIL) % (
            ", ".join(_clean(it.get("id"), 60) for _g, it in shown), ", ".join(_clean(it.get("id"), 60) for it in pointed))
        if want == tail:
            return text, shown
        tail = want
    return None, []


def _pre_tool_use(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """`--jit`: the verified memory attached to the file an edit names, as `additionalContext` (Claude Code adds it
    alongside the tool result, i.e. after the call ran), once per session, agent and item set. `--guard`: in a
    session that may deny, the first attempt on a file with an approved BLOCKS lesson or supersession is answered
    with `deny` (or `ask`) quoting those lessons in full, once, and only after the decision is on disk.
    `--capsule` alone: the file is only recorded as touched. Plan mode: nothing."""
    if not (st.get("jit") or st.get("capsule")):
        return None
    if payload.get("hook_event_name") != "PreToolUse":  # not the payload shape whose output fields we know
        return None
    mode = payload.get("permission_mode")
    if mode == "plan" or payload.get("tool_name") not in JIT_TOOLS:
        return None
    rel = _repo_file(root, payload)
    if rel is None:
        return None
    srel = _session_rel(payload.get("session_id"))
    if not st.get("jit"):
        _best_effort(root, st, "pre-tool-use", "touch record", lambda: _record_touch(root, payload),
                     reserve=PRINT_RESERVE_SECONDS)
        return None
    hints = _hints_enabled(root)
    nb = _neighbors(root, rel, tiers=(TIER_APPROVED, TIER_HINT) if hints else (TIER_APPROVED,))
    groups: Dict[str, List[Dict[str, Any]]] = nb["groups"] if nb else {}
    omitted: Dict[str, int] = nb["omitted"] if nb else {}
    shown_names = tuple(g for g in _DISPLAY_ORDER if g != "hints" or hints)
    entries = _entries(groups, shown_names)
    guard_mode = st.get("guard")
    if guard_mode == "ask" and mode in ("bypassPermissions", "dontAsk"):
        guard_mode = "deny"  # no permission prompt would show the reason to a person
    shown_path = _clean(rel, 300)
    reason: Optional[str] = None
    guarding = _guard_items(groups)
    shown_guard: List[Tuple[str, Dict[str, Any]]] = []
    if guard_mode in ("deny", "ask") and nb and nb["guardComplete"] and guarding:
        reason, shown_guard = _guard_reason(groups, guarding, guard_mode, shown_path)
    # the decision covers exactly the prohibitions shown in full: a lesson only pointed to can still be delivered,
    # once, when it later fits
    guard_digest = _digest(shown_guard)
    shown_omitted = {g: n for g, n in omitted.items() if g in shown_names and n}
    jit_digest = _digest([(g, it) for _s, g, it in entries], shown_omitted)
    scope = _scope_of(payload)
    decision: Optional[str] = None
    informs = bool(entries or shown_omitted)  # items, or only the count of what the graph could not include
    show = informs
    if srel is not None:
        try:
            with _state_lock(root):
                state, created = _open_state(root, srel)
                _touch(state, rel)
                triple = [scope, rel, guard_digest]
                pending = False
                if (reason is not None and state["mayGuard"] and not state["guardDisabled"]
                        and not state["recovered"] and triple not in state["guard"] and not _tombstoned(root, srel)):
                    if len(state["guard"]) >= SESSION_MAX_GUARD:
                        state["guardDisabled"] = True  # at the bound: stop guarding, never forget
                    else:
                        state["guard"].append(triple)
                        if _fits_state(state):
                            pending = True
                            show = False
                        else:  # the history would not fit: stop guarding instead of forgetting
                            state["guard"].pop()
                            state["guardDisabled"] = True
                if not pending and informs:
                    jt = [scope, rel, jit_digest]
                    if jt in state["shown"]:
                        show = False
                    else:
                        state["shown"].append(jt)
                _save_state(root, srel, state, created=created)
                if pending:
                    back, status = _load_state(root, srel)  # the decision counts only once it reads back
                    if (status == "ok" and back is not None and triple in back["guard"] and back["mayGuard"]
                            and not back["guardDisabled"]):
                        decision = guard_mode
                    else:
                        show = informs
        except _Deadline:
            raise
        except Exception as exc:  # cannot record: never deny; the context is still informative
            decision = None
            show = informs
            _log(root, "pre-tool-use", "session state unavailable, guard off for this call: " + _where(exc))
    if decision is not None and reason is not None:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                       "permissionDecisionReason": reason}}
    if not show:
        return None
    text, _full = _render_block([voc.JIT_HEAD % shown_path, voc.HOOK_QUOTE_NOTE], entries, [],
                                tokens=st.get("jitTokens") or DEFAULT_JIT_TOKENS, cap=JIT_MAX_CHARS,
                                omitted=sum(omitted.get(g, 0) for g in shown_names))
    if text is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


# -- the compaction capsule -------------------------------------------------------------------------------------


def _remaining(st: Dict[str, Any]) -> Optional[float]:
    g = st.get("_guard")
    return None if g is None else g.end - time.monotonic()


def _capsule(root: Path, payload: Dict[str, Any], st: Dict[str, Any], head: List[str], *, cap: int,
             event: str) -> Optional[str]:
    """This session's verified memory for the files it touched (most recent first, at most CAPSULE_MAX_FILES):
    rules anchored in them, approved lessons attached to them, their stale-anchor warnings. Approved tier only (no
    hints, nothing pending). None when nothing is attached, or when the remaining hook time (after
    CAPSULE_RESERVE_SECONDS for the 9.1.1 output) runs out: the caller then prints its 9.1.1 output unchanged."""
    srel = _session_rel(payload.get("session_id"))
    if srel is None:
        return None
    state, _status = _load_state(root, srel)
    if state is None or not state["touched"]:
        return None
    files = list(reversed(state["touched"]))[:CAPSULE_MAX_FILES]
    entries: List[Tuple[Optional[str], str, Dict[str, Any]]] = []
    omitted = 0
    seen: set = set()
    names = ("blocks", "replaced", "governs", "resolves", "stale")
    for f in files:
        left = _remaining(st)
        if left is not None and left < CAPSULE_RESERVE_SECONDS + 0.2:
            _log(root, event, "capsule out of time; 9.1.1 output only")
            return None
        nb = _neighbors(root, f, tiers=(TIER_APPROVED,))
        if nb is None:
            continue
        entries += _entries(nb["groups"], names, voc.CAPSULE_FILE_HEAD % _clean(f, 300), seen)
        omitted += sum(nb["omitted"].get(g, 0) for g in names)
    if not entries:
        return None
    text, _full = _render_block(head + [voc.HOOK_QUOTE_NOTE], entries, [],
                                tokens=st.get("capsuleTokens") or DEFAULT_CAPSULE_TOKENS, cap=cap, omitted=omitted)
    return text


def _best_effort(root: Path, st: Dict[str, Any], event: str, what: str, fn: Callable[[], Any], *,
                 reserve: float) -> Any:
    """Run 9.2 work (session bookkeeping, the capsule) after the 9.1.1 result is computed and kept: under a soft
    deadline `reserve` seconds before the hook's own (which honours `hooks.deadlineSeconds` and the time already
    spent). Its lock waits, its deadline and its errors stop only this work (logged); None is returned and the
    caller prints its kept 9.1.1 result unchanged."""
    g = st.get("_guard")
    if g is None:  # called directly (tests): no alarm to borrow
        try:
            return fn()
        except Exception as exc:
            _log(root, event, f"{what} skipped: " + _where(exc))
            return None
    end = g.end
    left = end - time.monotonic() - reserve
    if left < 0.05:
        _log(root, event, f"{what} out of time; 9.1.1 output only")
        return None
    result = None
    g.arm(left)
    try:
        try:
            result = fn()
        except _Deadline:
            _log(root, event, f"{what} stopped at its soft deadline; 9.1.1 output only")
        except Exception as exc:
            _log(root, event, f"{what} skipped: " + _where(exc))
        finally:
            # what is left after this is printing the kept result: never less than REARM_FLOOR_SECONDS for it
            g.arm(max(end - time.monotonic(), REARM_FLOOR_SECONDS))
    except _Deadline:  # the soft alarm fired between the work and the re-arm
        g.arm(REARM_FLOOR_SECONDS)
    return result


def _pre_compact(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Any:
    """9.1.1 behaviour first (snapshot / ingest per the installed flags). With --capsule and something to keep, the
    capsule is printed as plain text (Claude Code appends PreCompact stdout to the compaction instructions),
    followed by the 9.1.1 status message as plain text under a fixed heading. Otherwise the 9.1.1 output."""
    result = _learn(root, payload, st, "pre-compact")
    if not st.get("capsule"):
        return result
    text = _best_effort(root, st, "pre-compact", "capsule",
                        lambda: _capsule(root, payload, st, [voc.CAPSULE_COMPACT_HEAD], cap=CAPSULE_MAX_CHARS,
                                         event="pre-compact"), reserve=CAPSULE_RESERVE_SECONDS)
    if not text:
        return result
    message = (result or {}).get("systemMessage") if isinstance(result, dict) else None
    if isinstance(message, str) and message:
        text += "\n\n" + voc.CAPSULE_STATUS_HEAD + "\n" + message
    return _Plain(text)


def _session_start_event(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The 9.1.1 SessionStart output first, kept; then, best effort, the session note and the capsule. If the 9.1.1
    handover itself runs out of time (large repositories), the note still gets a short grace period so the guard
    keeps its start record; the hook then prints nothing, exactly as 9.1.1 does on a timeout."""
    tracking = st.get("jit") or st.get("capsule")
    try:
        out = _session_start(root, payload, st)
    except _Deadline:
        g = st.get("_guard")
        if tracking and g is not None:
            g.arm(min(SESSION_NOTE_GRACE, 0.25 * (st.get("deadline") or DEADLINES["session-start"])))
            _best_effort(root, st, "session-start", "session note", lambda: _note_session_start(root, payload),
                         reserve=0.0)
        raise
    if tracking:
        _best_effort(root, st, "session-start", "session note", lambda: _note_session_start(root, payload),
                     reserve=PRINT_RESERVE_SECONDS)
    out = _with_guard_notice(out, st)
    source = payload.get("source")
    if not st.get("capsule") or source not in ("compact", "resume"):
        return out
    first = voc.CAPSULE_AFTER_COMPACT if source == "compact" else voc.CAPSULE_AFTER_RESUME
    text = _best_effort(root, st, "session-start", "capsule",
                        lambda: _capsule(root, payload, st, [first], cap=SESSION_START_MAX_CHARS // 2,
                                         event="session-start"), reserve=CAPSULE_RESERVE_SECONDS)
    if not text:
        return out
    base = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext")
    if isinstance(base, str) and base:
        text = text + "\n\n" + base
    if len(text) > SESSION_START_MAX_CHARS:
        return out
    new: Dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    if out and out.get("systemMessage"):
        new["systemMessage"] = out["systemMessage"]
    return new


def _post_tool_use_event(root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    out = _post_tool_use(root, payload, st)  # the 9.1.1 result, kept whatever the bookkeeping below does
    if st.get("jit") or st.get("capsule"):
        _best_effort(root, st, "post-tool-use", "touch record", lambda: _record_touch(root, payload),
                     reserve=PRINT_RESERVE_SECONDS)
    return out


def _with_guard_notice(out: Optional[Dict[str, Any]], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An invalid guard value is reported to the person as a status message (only when one is configured)."""
    notice = st.get("guardInvalid")
    if not notice:
        return out
    new = dict(out or {})
    new["systemMessage"] = (new["systemMessage"] + " " + notice) if new.get("systemMessage") else notice
    return new


def _dispatch(event: str, root: Path, payload: Dict[str, Any], st: Dict[str, Any]) -> Any:
    if event == "session-start":
        return _session_start_event(root, payload, st)
    if event == "post-tool-use":
        return _post_tool_use_event(root, payload, st)
    if event == "pre-tool-use":
        return _pre_tool_use(root, payload, st)
    if event == "pre-compact":
        return _pre_compact(root, payload, st)
    if event == "stop":
        return _learn(root, payload, st, event)
    return _git_check(root, event)


@md.fresh_read
def run_hook(argv: List[str], *, stdin: Any = None, stdout: Any = None, stderr: Any = None,
             hard_exit: bool = True) -> int:
    """Always returns 0. Writes one JSON object to stdout (Claude Code events) or warning lines to stderr (git
    events), or nothing."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    event = argv[0] if argv and isinstance(argv[0], str) else ""
    if event == "probe" and len(argv) == 1:
        with contextlib.suppress(Exception):
            _call_foreign(lambda: out.write(json.dumps({"memletHook": HOOK_PROTOCOL}) + "\n"))
            _call_foreign(lambda: out.flush())
        return 0
    if os.environ.get("MEMLET_DREAMING") == "1":
        return 0
    scope: Optional[str] = None
    if len(argv) == 3 and argv[1] == "--scope" and argv[2] in SCOPES and event in RUNTIME_EVENTS:
        scope = argv[2]  # commands written by 9.2 development builds
    elif event not in RUNTIME_EVENTS + GIT_EVENTS or len(argv) != 1:
        return 0
    elif event in RUNTIME_EVENTS and os.environ.get(SCOPE_ENV) in SCOPES:
        scope = os.environ[SCOPE_ENV]
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
                    st = _hook_settings(root, scope)
                    if st["deadline"] and st["deadline"] < DEADLINES[event]:
                        guard.arm(st["deadline"] - (time.monotonic() - guard.start))
                    st["_guard"] = guard  # the capsule borrows the remaining time from it
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
        if isinstance(result, _Plain):
            _call_foreign(lambda: out.write(str(result) + "\n"))
            _call_foreign(lambda: out.flush())
        elif isinstance(result, str):
            _call_foreign(lambda: err.write(result))
            _call_foreign(lambda: err.flush())
        else:
            _call_foreign(lambda: out.write(json.dumps(result, ensure_ascii=False) + "\n"))
            _call_foreign(lambda: out.flush())
    return 0
