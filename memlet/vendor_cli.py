"""Audited, explicit-only subscription CLI calls. No tools, project, MCP or persistence.

Codex exec (even read-only) still offers file/shell tools. agy plan/sandbox is not
no-tools. Their installed help was inspected for 9.1; both fail closed. Adding a
vendor requires an audited no-tools mode, not a prompt asking it to behave.
"""
import contextlib
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

VENDORS = ("claude", "codex", "gemini")
DEFAULT_MODELS = {"claude": "haiku", "codex": "gpt-5-mini", "gemini": "gemini-2.5-flash"}
_PAID_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
             "ANTHROPIC_VERTEX_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
_SETTINGS_RISK_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper")
_SETTINGS_RISK_ENV = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "OPENAI_", "GOOGLE_", "GEMINI_", "AZURE_")


def _runner_env():
    env = {k: v for k, v in os.environ.items() if k not in _PAID_ENV
           and not k.startswith(_SETTINGS_RISK_ENV)
           and k not in ("NODE_OPTIONS", "BASH_ENV", "ENV", "PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES")}
    env["MEMLET_DREAMING"] = "1"
    return env


def _claude_argv(model="claude-sonnet-5-5"):
    return ["claude", "-p", "--model", model, "--safe-mode", "--tools", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence"]


def _settings_risks(config_dir):
    from memlet.memlet_wiki import read_regular_text
    if not isinstance(config_dir, str) or not config_dir:
        return ["config directory unknown"]
    risks = []
    for name in ("settings.json", "settings.local.json"):
        p = Path(config_dir) / name
        try:
            raw = read_regular_text(p, 262144) if p.exists() or p.is_symlink() else "{}"
            cfg = json.loads(raw) if raw is not None else None
            if not isinstance(cfg, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            risks.append(name + " unreadable")
            continue
        risks += [name + ":" + k for k in _SETTINGS_RISK_KEYS if cfg.get(k)]
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        risks += [name + ":env." + k for k in env if k.startswith(_SETTINGS_RISK_ENV) or k in _PAID_ENV]
    return risks


def check_vendor(vendor, *, config=None, timeout=60):
    def result(ok, reason):
        return {"ok": ok, "vendor": vendor, "reason": reason, "isolation": "tool-free"}
    if vendor != "claude":
        reason = ("Codex read-only sandbox restricts shell commands, but not all apps/browser/computer-use tools; no documented disable-all mode"
                  if vendor == "codex" else "agy --sandbox documents terminal restrictions, not a tool-free mode")
        return result(False, vendor + ": no verified tool-free, subscription-only mode; refusing: " + reason)
    try:
        with tempfile.TemporaryDirectory(prefix="memlet-auth-") as empty:
            r = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True,
                               timeout=max(.01, min(60, timeout)), env=_runner_env(), cwd=empty)
        if len(r.stdout or "") > 262144:
            return result(False, "authentication response exceeds bound")
        st = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return result(False, "could not confirm subscription authentication")
    if r.returncode != 0 or not isinstance(st, dict):
        return result(False, "refusing: claude auth status failed")
    if not (st.get("loggedIn") and st.get("authMethod") == "claude.ai" and st.get("apiProvider") == "firstParty"
            and st.get("subscriptionType")):
        return result(False, "refusing: Claude is not authenticated by a claude.ai subscription")
    risks = _settings_risks(st.get("configDirectory"))
    if risks:
        return result(False, "refusing: settings could select API-key or third-party auth: " + ", ".join(risks))
    return result(True, "claude.ai subscription")


def _capture(argv, data, timeout, limit, cwd):
    """Bound both pipes while draining them; kill the process group on excess/timeout."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=cwd, env=_runner_env(), start_new_session=True) as p:
        selector = selectors.DefaultSelector()
        output, errors, sent = bytearray(), bytearray(), 0
        complete = False
        try:
            for pipe in (p.stdin, p.stdout, p.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(p.stdin, selectors.EVENT_WRITE)
            selector.register(p.stdout, selectors.EVENT_READ)
            selector.register(p.stderr, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("vendor timeout")
                for key, _ in selector.select(min(left, 0.1)):
                    pipe = key.fileobj
                    if pipe is p.stdin:
                        if sent < len(data):
                            try:
                                sent += os.write(pipe.fileno(), data[sent:sent + 8192])
                            except BrokenPipeError:
                                sent = len(data)
                        if sent == len(data):
                            selector.unregister(pipe)
                            pipe.close()
                    else:
                        chunk = os.read(pipe.fileno(), 8192)
                        if not chunk:
                            selector.unregister(pipe)
                        else:
                            (output if pipe is p.stdout else errors).extend(chunk)
                            if len(output) + len(errors) > limit:
                                raise ValueError("vendor output exceeds byte limit")
            rc = p.wait(timeout=max(0.01, deadline - time.monotonic()))
            text = output.decode("utf-8", "strict")
            complete = True
            return rc, text
        finally:
            selector.close()
            if not complete or p.poll() is None:
                # A launcher can exit before its child. Kill its whole private group even then.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait()


def run_vendor(vendor, prompt, *, stdin_text="", timeout=120, max_output_bytes=262144, runner=None, model=None, config=None):
    model = model or DEFAULT_MODELS.get(vendor, "")
    result = {"ok": False, "vendor": vendor, "model": model, "text": "", "error": "", "isolation": "tool-free"}
    if vendor not in VENDORS or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", model):
        return dict(result, error="unknown vendor or invalid model")
    if not isinstance(prompt, str) or not isinstance(stdin_text, str) or len(prompt.encode()) > 65536 or len(stdin_text.encode()) > 1048576:
        return dict(result, error="vendor input exceeds bound")
    timeout, limit = max(0.01, min(float(timeout), 1800)), max(1, min(int(max_output_bytes), 1048576))
    started = time.monotonic()
    if runner is None:
        checked = check_vendor(vendor, config=config, timeout=timeout)
        if not checked["ok"]:
            return dict(result, error=checked["reason"])
    timeout -= time.monotonic() - started
    if timeout <= 0:
        return dict(result, error="vendor authentication exhausted timeout")
    try:
        if runner is not None:  # explicit test seam, never exposed through CLI/config/MCP
            text = runner(vendor, prompt, stdin_text=stdin_text, model=model, timeout=timeout)
            rc = 0
        else:
            with tempfile.TemporaryDirectory(prefix="memlet-vendor-") as empty:
                rc, text = _capture(_claude_argv(model) + [prompt], stdin_text.encode("utf-8"), timeout, limit, empty)
        if not isinstance(text, str) or len(text.encode("utf-8")) > limit:
            return dict(result, error="vendor output exceeds byte limit or is not text")
        if rc:
            return dict(result, error="vendor exited %d" % rc)
        return dict(result, ok=True, text=text)
    except (OSError, ValueError, TypeError, TimeoutError, subprocess.SubprocessError):
        return dict(result, error="vendor failed, timed out, or exceeded output limit")
