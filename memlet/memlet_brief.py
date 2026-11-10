"""`memlet brief`: an LLM writes a prioritized rule summary once, offline; Memlet verifies every line on each handover.

    memlet brief-build . --via claude      # writes .memlet/wiki/BRIEF.md (subscription CLI only; never runs by itself)
    memlet handover . --mode brief         # carries brief lines whose cited source lines are unchanged

Trust model
  * The model only ever sees the discovered instruction files (never `.env`, memory-note folders or wiki notes), with
    secret-looking values masked, numbered line by line.
  * Every brief line must cite `file:line[-line]` inside those files; uncited lines and citations outside them are
    dropped. Each kept line is pinned to the SHA-256 of the cited source text.
  * At handover time the pins are re-checked deterministically (no model call). A line whose cited source changed
    is quarantined as stale and not carried; the capsule says how many were held back.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from memlet.audit import LazyBlockPattern, LinearAlternation, regex_finder, run_tail_finder

BRIEF_PAGE = ".memlet/wiki/BRIEF.md"
BRIEF_SCHEMA = "memlet-brief/v1"
# Only runners that can be made tool-free and subscription-only are allowed; everything else fails closed.
RUNNERS = ("claude",)
_PAID_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
             "ANTHROPIC_VERTEX_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _runner_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _PAID_ENV}


def _claude_argv() -> List[str]:
    # --safe-mode disables CLAUDE.md, skills, plugins, hooks, MCP and other customizations; --tools "" disables every
    # built-in tool; nothing persisted. Auth is checked under the same environment and cwd policy (check_runner).
    return ["claude", "-p", "--model", "claude-sonnet-5-5", "--safe-mode", "--tools", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence"]


_SETTINGS_RISK_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper")
_SETTINGS_RISK_ENV = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_BEARER_TOKEN")


def _settings_risks(config_dir: Optional[str]) -> List[str]:
    """Names (never values) of settings that could route the call to API-key or third-party billing."""
    if not config_dir:
        return ["config directory unknown"]
    risks = []
    for name in ("settings.json", "settings.local.json"):
        p = Path(config_dir) / name
        try:
            cfg = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (OSError, ValueError):
            risks.append(f"{name} unreadable")
            continue
        risks += [f"{name}:{k}" for k in _SETTINGS_RISK_KEYS if cfg.get(k)]
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        risks += [f"{name}:env.{k}" for k in env if k.startswith(_SETTINGS_RISK_ENV) or k in _PAID_ENV]
    return risks


def check_runner(via: str) -> Tuple[bool, str]:
    """Fail closed unless the runner is tool-free and authenticated by a first-party subscription (never an API key)."""
    if via not in RUNNERS:
        return False, (f"--via {via}: no verified tool-free, subscription-only mode for this CLI; refusing "
                       f"(supported: {', '.join(RUNNERS)})")
    try:
        with tempfile.TemporaryDirectory(prefix="memlet-auth-") as empty:  # same env and cwd policy as generation
            r = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=60,
                               env=_runner_env(), cwd=empty)
        st = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"could not confirm subscription authentication: {exc}"
    if r.returncode != 0:
        return False, f"refusing: `claude auth status` exited {r.returncode}"
    if not (st.get("loggedIn") and st.get("authMethod") == "claude.ai" and st.get("apiProvider") == "firstParty"
            and st.get("subscriptionType")):
        return False, ("refusing: Claude is not authenticated by a claude.ai subscription "
                       f"(authMethod={st.get('authMethod')}, apiProvider={st.get('apiProvider')})")
    risks = _settings_risks(st.get("configDirectory"))
    if risks:
        return False, "refusing: settings could select API-key or third-party auth: " + ", ".join(risks)
    return True, f"claude.ai subscription ({st.get('subscriptionType')})"


_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?P<rule>.+?)\s*\[src:\s*(?P<src>[^\]]+)\]\s*$")
_CITE_RE = re.compile(r"^\s*(?P<file>[^:\s][^:]*?):(?P<a>\d+)(?:\s*-\s*(?P<b>\d+))?\s*$")
def line_lazy_marker(prefix: str, body: str, suffix: str) -> LazyBlockPattern:
    """`<prefix>(?P<body>BODY.*?)<suffix>` on one line (`.` stops at a newline), evaluated in linear time: a line
    full of unclosed openers is scanned once instead of once per opener."""
    rx = re.compile(re.escape(prefix) + "(?P<body>" + body + ".*?)" + re.escape(suffix))
    return LazyBlockPattern(rx.pattern, re.compile(re.escape(prefix) + body), re.compile(re.escape(suffix)), suffix,
                            dotall=False, full=rx)


_PIN_RE = line_lazy_marker("<!-- memlet:brief ", "", " -->")
_BRIEF_META_RE = line_lazy_marker("<!-- memlet:brief-meta ", r"\{", "} -->")
_PIN_BODY_RE = re.compile(r"rank=\d+ pins=(?P<pins>\S+:\d+-\d+=[0-9a-f]{64}(?: \S+:\d+-\d+=[0-9a-f]{64})*)")
# Secret masking: one linear-time implementation (`_mask`) used by every brief / index / recall / handover display.
# Patterns are evaluated by `audit.LazyBlockPattern` / `audit.LinearAlternation`, which give
# the same matches as the plain regexes without retrying long runs from every start (`'z' * 100000` took seconds).
_PEM_PATTERN = r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----"
_PEM_RE = LazyBlockPattern(_PEM_PATTERN, re.compile(r"-----BEGIN [A-Z0-9 ]+-----"),
                           re.compile(r"-----END [A-Z0-9 ]+-----"), "-----END ")
SECRET_MASK_FAMILIES = (
    "OpenAI/Anthropic sk-", "Stripe sk_live_/sk_test_/rk_live_/rk_test_/whsec_", "GitHub ghp_/gho_/ghu_/ghs_/ghr_/github_pat_",
    "Slack xoxa-/xoxb-/xoxp-/xoxr-/xoxs-/xoxe-/xapp-", "AWS AKIA/ASIA access key IDs", "Google AIza",
    "GitLab glpat-", "npm npm_", "PyPI pypi-", "Polar polar_oat_/polar_pat_/polar_sk_",
    "PostgreSQL/MySQL/MongoDB connection URLs", "JWT", "URL credentials", "PEM blocks",
    "assignments whose name contains KEY/TOKEN/SECRET/PASSWORD/PASSWD/CREDENTIAL/AUTH (8+ non-space value characters)",
)
_EXTRA_SECRET_PATTERN = (
    r"(?:sk-[A-Za-z0-9_\-]{16,}|(?:sk_live_|sk_test_|rk_live_|whsec_)[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[abprs]-[A-Za-z0-9\-]{8,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[A-Za-z0-9_\-]{8,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}|[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@"
    r"|\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTH)[A-Z0-9_]*\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    r"|(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{12,})")


def _url_credentials_finder(text: str):
    """`[A-Za-z][A-Za-z0-9+.\\-]*://[^\\s/:@]+:[^\\s/@]+@` (re.I). The scheme run before a `://` is one class run, so
    the only start that matters for one `://` is the first letter of that run (every start in it shares the same
    continuation); one test per `://` instead of one per letter of a long word."""
    scheme_ch = re.compile(r"[A-Za-z0-9+.\-]", re.I)
    letter = re.compile(r"[A-Za-z]", re.I)
    tail = re.compile(r"://[^\s/:@]+:[^\s/@]+@", re.I)
    state = {"c": -1, "frm": -1, "r": 0, "end": None}

    def next_sep(frm: int) -> int:
        # memoised `text.find("://", frm)`: the last answer stays valid while it is still at or after `frm`
        if 0 <= state["frm"] <= frm and (state["c"] < 0 or state["c"] >= frm) and state.get("found", False):
            return state["c"]
        c = text.find("://", frm)
        state["frm"], state["found"] = frm, True
        if c != state["c"]:
            state.update(c=c, end=None, scanned=False)
        return c

    def find(pos: int):
        c = next_sep(pos + 1)
        while c >= 0:
            if not state.get("scanned"):
                r = c
                while r > 0 and scheme_ch.match(text, r - 1):
                    r -= 1
                m = tail.match(text, c)
                state.update(r=r, end=m.end() if m else None, scanned=True)
            if state["end"] is not None:
                s = max(pos, state["r"])
                while s < c and not letter.match(text, s):
                    s += 1
                if s < c:
                    return s, state["end"]
            c = next_sep(c + 1)
        return None
    return find


def _keyword_word_finder(text: str):
    """`\\b[A-Z0-9_]*(?:KEY|TOKEN|...|AUTH)[A-Z0-9_]*\\s*[:=]\\s*['\\"]?[^\\s'\\"]{8,}` (re.I). Both runs are one word
    of class characters: a start is only possible at a word boundary, i.e. at the start of a word run, and the match
    exists iff that word contains a keyword and the separator/value follow the word. Search for a keyword
    first, then inspect its whole word once; ordinary prose words need no Python-level scan."""
    word = re.compile(r"[A-Z0-9_]+", re.I)
    word_char = re.compile(r"[A-Z0-9_]", re.I)
    keyword = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTH", re.I)
    rest = re.compile(r"\s*[:=]\s*['\"]?[^\s'\"]{8,}")
    wordch = re.compile(r"\w")

    def find(pos: int):
        while True:
            m = keyword.search(text, pos)
            if m is None:
                return None
            p = m.start()
            while p > pos and word_char.match(text, p - 1):
                p -= 1
            e = word.match(text, m.start()).end()
            if not (p > 0 and wordch.match(text, p - 1)):
                t = rest.match(text, e)
                if t:
                    return p, t.end()
            pos = e
    return find


def _extra_secret_alternation():
    cls = r"[A-Za-z0-9_\-]"
    head = re.compile(r"sk-[A-Za-z0-9_\-]{16,}|(?:sk_live_|sk_test_|rk_live_|whsec_)[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[abprs]-[A-Za-z0-9\-]{8,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[A-Za-z0-9_\-]{8,}",
                      re.I)
    jwt = run_tail_finder(re.compile(r"eyJ", re.I), re.compile(cls + "*", re.I), 10,
                          re.compile(r"\." + cls + r"{10,}\." + cls + r"{10,}", re.I))
    named = re.compile(r"(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{12,}", re.I)
    return LinearAlternation(_EXTRA_SECRET_PATTERN, [regex_finder(head), jwt, _url_credentials_finder,
                                                     _keyword_word_finder, regex_finder(named)], re.I)


_EXTRA_SECRET_RE = _extra_secret_alternation()
MAX_BRIEF_LINES = 120


def _mask(text: str) -> str:
    # whole blocks first (a private key spans lines), keeping the line count so citations stay aligned
    text = _PEM_RE.sub(lambda m: "\n".join("[REDACTED_SECRET_BLOCK]" for _ in m.group(0).splitlines()), text)
    from .redaction import mask_sensitive_text
    text = mask_sensitive_text(text)
    return _EXTRA_SECRET_RE.sub("[REDACTED_SECRET]", text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_lines(root: Path) -> Dict[str, List[str]]:
    """Discovered instruction files (snapshot copies excluded), each through the shared contained, size-capped
    reader (`memlet_wiki._source_texts`: per-file and cumulative bounds)."""
    from memlet.memlet_wiki import _source_texts, discover_instruction_files
    return {rel: text.splitlines() for rel, text in _source_texts(root, discover_instruction_files(root, exclude_snapshots=True))}


def _cited_text(files: Dict[str, List[str]], file: str, a: int, b: int) -> Optional[str]:
    lines = files.get(file)
    if lines is None or a < 1 or b < a or b > len(lines) or b - a > 12:
        return None
    return "\n".join(lines[a - 1:b])


def prompt_names(files: Dict[str, List[str]]) -> Dict[str, str]:
    """Name shown to the model -> real path. A path that masking would change (a secret-looking file name) is shown
    as an opaque FILE<n> alias instead, so no raw name leaves the machine; ordinary paths keep their (useful) name.
    Deterministic, so the parser can rebuild the same map."""
    names, n = {}, 0
    for rel in sorted(files):
        if _mask(rel) == rel and rel not in names:
            names[rel] = rel
        else:
            n += 1
            names[f"FILE{n}"] = rel
    return names


def _resolve_cited_file(name: str, files: Dict[str, List[str]]) -> Optional[str]:
    names = prompt_names(files)
    if name in names:
        return names[name]
    return name if name in files else None


def build_prompt(files: Dict[str, List[str]]) -> str:
    body = []
    shown = {v: k for k, v in prompt_names(files).items()}
    for rel, lines in files.items():
        body.append(f"===== FILE {shown[rel]} =====")
        masked = _mask("\n".join(lines)).split("\n")
        body += [f"{i:4d}| {l}" for i, l in enumerate(masked, start=1)]
    return (
        "You compress a project's instruction files into a rule brief for an AI coding agent that will NOT see the "
        "originals. List the rules an agent must know to work correctly and safely: hard constraints (must/never/only), "
        "forbidden or rejected approaches, exact paths/commands/names to use, approvals and process order, reporting "
        "rules. Order lines from most to least important. Keep each rule's conditions (who approves, when, exceptions) "
        "and exact identifiers. One rule per line, at most 30 words, in the language of the source. Do not invent "
        "anything; every line must cite the source lines it comes from.\n"
        "Output ONLY lines in this exact format, nothing else:\n"
        "- <rule> [src: <file>:<line>] or [src: <file>:<first>-<last>]\n"
        f"Write at most {MAX_BRIEF_LINES} lines.\n\n" + "\n".join(body))


def parse_brief(text: str, files: Dict[str, List[str]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Keep only lines whose citation resolves inside the instruction files; pin each to its cited source text."""
    kept, dropped = [], {"format": 0, "citation": 0, "duplicate": 0}
    seen = set()
    for raw in text.splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            dropped["format"] += 1 if raw.strip() else 0
            continue
        cites, ok = [], True
        for part in m.group("src").split(";"):
            c = _CITE_RE.match(part.strip().strip("`"))
            if not c:
                ok = False
                break
            a, b = int(c.group("a")), int(c.group("b") or c.group("a"))
            real = _resolve_cited_file(c.group("file").strip(), files)
            src = _cited_text(files, real, a, b) if real else None
            if src is None:
                ok = False
                break
            cites.append({"file": real, "from": a, "to": b, "sha": _sha(src)})
        # model text never carries page markup: comment delimiters and our citation wrapper are removed
        rule = re.sub(r"<!--|-->|\(`|`\)", " ", m.group("rule"))
        rule = _mask(re.sub(r"\s+", " ", rule).strip())
        if not ok or not cites:
            dropped["citation"] += 1
            continue
        key = rule.lower()
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        kept.append({"rule": rule, "cites": cites})
        if len(kept) >= MAX_BRIEF_LINES:
            break
    return kept, dropped


def _render_page(lines: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    out = ["# Memlet Brief", "",
           "<!-- Generated by `memlet brief-build`; each line is pinned to the instruction lines it cites. "
           "Edit the instructions, not this page; re-run the build to refresh. -->",
           f"<!-- memlet:brief-meta {json.dumps(meta, sort_keys=True)} -->", ""]
    for i, ln in enumerate(lines, start=1):
        src = "; ".join(f"{c['file']}:{c['from']}" + (f"-{c['to']}" if c['to'] != c['from'] else "") for c in ln["cites"])
        pins = " ".join(f"{c['file']}:{c['from']}-{c['to']}={c['sha']}" for c in ln["cites"])
        out.append(f"- {ln['rule']} (`{src}`) <!-- memlet:brief rank={i} pins={pins} -->")
    return "\n".join(out) + "\n"


def build_brief(repo_path=".", *, via: str = "claude", runner=None, write: bool = True, timeout: int = 1500) -> Dict[str, Any]:
    from memlet.memlet_wiki import _resolve_root, _write_generated_page
    root = _resolve_root(repo_path)
    files = _source_lines(root)
    if not files:
        return {"ok": False, "command": "brief", "why": "no instruction files found", "repoPath": str(root)}
    prompt = build_prompt(files)
    if runner is None:
        ok, why = check_runner(via)
        if not ok:
            return {"ok": False, "command": "brief", "why": why, "repoPath": str(root)}
        with tempfile.TemporaryDirectory(prefix="memlet-brief-") as empty:  # no project files within the model's reach
            r = subprocess.run(_claude_argv(), input=prompt, capture_output=True, text=True, timeout=timeout, cwd=empty,
                               env=_runner_env())
        text, rc = r.stdout, r.returncode
    else:
        text, rc = runner(prompt), 0
    lines, dropped = parse_brief(text, files)
    meta = {"schema": BRIEF_SCHEMA, "via": via, "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "inputSha256": _sha(prompt), "files": sorted(files)}
    report = {"ok": bool(lines) and rc == 0, "command": "brief", "repoPath": str(root), "via": via, "returncode": rc,
              "lines": len(lines), "dropped": dropped, "meta": meta}
    if write and lines:
        existing = root / BRIEF_PAGE
        if existing.is_file() and "<!-- memlet:brief-meta " not in existing.read_text(encoding="utf-8", errors="replace"):
            report.update(ok=False, why=f"{BRIEF_PAGE} exists and was not generated by brief-build; left untouched")
            return report
        report["op"] = _write_generated_page(root, BRIEF_PAGE, _render_page(lines, meta), dry_run=False)
    return report


def load_verified_brief(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(fresh, stale, meta) — deterministic re-check of every pin against the current instruction files."""
    from memlet.memlet_wiki import MAX_WIKI_PAGE_BYTES, read_managed_text
    text = read_managed_text(root, BRIEF_PAGE, MAX_WIKI_PAGE_BYTES)  # contained, regular, size-capped
    if text is None:
        return [], [], None
    meta = None
    m = _BRIEF_META_RE.search(text)
    if m:
        try:
            meta = json.loads(m.group("body") + "}")
        except ValueError:
            meta = None
    files = _source_lines(root)
    fresh, stale = [], []
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        if len(fresh) + len(stale) >= MAX_BRIEF_LINES:  # bounded work: a brief never has more lines than this
            break
        marks = list(_PIN_RE.finditer(line))
        if not marks:
            continue
        p = marks[-1]  # the renderer's pin is always last; anything else on the line is not trusted
        body = _PIN_BODY_RE.fullmatch(p.group("body").strip())
        rule_part = line[2:p.start()].rstrip()
        src = re.search(r"\(`([^`]+)`\)\s*$", rule_part)
        rule = rule_part[:src.start()].rstrip() if src else rule_part
        pins = re.findall(r"(\S+?):(\d+)-(\d+)=([0-9a-f]{64})", body.group("pins")) if body else []
        ok = bool(pins) and len(marks) == 1 and "<!--" not in rule and bool(src)
        for f, a, b, sha in pins:
            cur = _cited_text(files, f, int(a), int(b))
            if cur is not None and _sha(cur) == sha:
                continue
            width, lines = int(b) - int(a) + 1, files.get(f) or []
            moved = 1 <= width <= 13 and any(_sha("\n".join(lines[i:i + width])) == sha
                                              for i in range(0, max(0, len(lines) - width + 1)))
            if not moved:  # unchanged text that merely moved (lines inserted above) still verifies
                ok = False
        item = {"rule": rule, "source": src.group(1) if src else ""}
        (fresh if ok else stale).append(item)
    return fresh, stale, meta
