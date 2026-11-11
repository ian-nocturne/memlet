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
MODEL_FAMILIES = {"claude": ("haiku", "sonnet", "opus", "fable")}
DEFAULT_MODELS = {"claude": "haiku", "codex": "gpt-5-mini", "gemini": "gemini-2.5-flash"}
_PAID_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
             "ANTHROPIC_VERTEX_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
_SETTINGS_RISK_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper")
# Settings routing checks retain the brief runner's scope; unrelated AWS_REGION
# or another provider's config is not evidence that Claude will use paid auth.
_SETTINGS_RISK_ENV = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_BEARER_TOKEN")
_RUNNER_ENV_KEYS = frozenset(("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR",
    "CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"))


def valid_model(model):
    return isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}", model) is not None


def model_family(vendor, model):
    tokens = re.split(r"[^a-z0-9]+", str(model).casefold())
    families = [t for t in tokens if t in MODEL_FAMILIES.get(vendor, ())]
    return families[0] if len(families) == 1 else None


def same_model(vendor_a, model_a, vendor_b, model_b):
    """Conservative identity, shared by launch and read-time agreement checks."""
    a, b = str(model_a).casefold(), str(model_b).casefold()
    # An unresolved Claude alias cannot establish model independence, even
    # against another vendor. Vendor-mode agreement remains a separate policy.
    if any(v in MODEL_FAMILIES and model_family(v, m) is None
           for v, m in ((vendor_a, model_a), (vendor_b, model_b))):
        return True
    if a == b:
        return True
    if vendor_a == vendor_b:
        tokens_a, tokens_b = set(re.split(r"[^a-z0-9]+", a)), set(re.split(r"[^a-z0-9]+", b))
        return bool(tokens_a & tokens_b & set(MODEL_FAMILIES.get(vendor_a, ())))
    return False


def static_vendor_refusal(vendor):
    if vendor == "claude":
        return None
    reasons = {
        "codex": "Codex read-only sandbox restricts shell commands, but not all apps/browser/computer-use tools; no documented disable-all mode",
        "gemini": "agy --sandbox documents terminal restrictions, not a tool-free mode",
        "agy": "agy is not a supported vendor spelling; use gemini (which has no verified tool-free mode)",
    }
    reason = reasons.get(vendor, "unknown vendor spelling; expected claude, codex or gemini")
    return str(vendor) + ": no verified tool-free, subscription-only mode; refusing: " + reason


def _runner_env():
    env = {k: v for k, v in os.environ.items() if k in _RUNNER_ENV_KEYS or k.startswith("LC_")}
    env["MEMLET_DREAMING"] = "1"
    return env


def _claude_argv(model="claude-sonnet-5-5"):
    return ["claude", "-p", "--model", model, "--safe-mode", "--tools", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence"]


def launch_plan(vendor, prompt, model=None):
    """The same argv/isolation description for Dream and eval previews; never launches."""
    model = DEFAULT_MODELS.get(vendor, "") if model is None else model
    return {"vendor": vendor, "model": model, "isolation": "tool-free",
            "argv": _claude_argv(model) + [prompt] if vendor == "claude" else None}


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


PLATFORM_REFUSAL = "the vendor runner needs POSIX process groups; not available on this platform"


def _posix_runner_available():
    return os.name == "posix" and all(hasattr(os, name) for name in ("killpg", "set_blocking", "setsid")) and hasattr(signal, "SIGKILL")


def check_vendor(vendor, *, config=None, timeout=60):
    def result(ok, reason):
        return {"ok": ok, "vendor": vendor, "reason": reason, "isolation": "tool-free"}
    if not _posix_runner_available():
        return result(False, PLATFORM_REFUSAL)
    refusal = static_vendor_refusal(vendor)
    if refusal:
        return result(False, refusal)
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
        return result(False, "refusing: `claude auth status` exited %s or returned an invalid object" % r.returncode)
    if not (st.get("loggedIn") and st.get("authMethod") == "claude.ai" and st.get("apiProvider") == "firstParty"
            and st.get("subscriptionType")):
        return result(False, "refusing: Claude is not authenticated by a claude.ai subscription")
    risks = _settings_risks(st.get("configDirectory"))
    if risks:
        return result(False, "refusing: settings could select API-key or third-party auth: " + ", ".join(risks))
    return result(True, "claude.ai subscription (%s)" % st.get("subscriptionType"))


def _capture(argv, data, timeout, limit, cwd):
    """Bound both pipes while draining them; kill the process group on excess/timeout."""
    if not _posix_runner_available():
        raise ValueError(PLATFORM_REFUSAL)
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=cwd, env=_runner_env(), start_new_session=True) as p:
        selector = selectors.DefaultSelector()
        output, errors, sent = bytearray(), bytearray(), 0
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
            return rc, text
        finally:
            selector.close()
            # Normal exit can leave descendants with closed pipes in this private group.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()


def run_vendor(vendor, prompt, *, stdin_text="", timeout=120, max_output_bytes=262144, runner=None, model=None, config=None):
    model = DEFAULT_MODELS.get(vendor, "") if model is None else model
    result = {"ok": False, "vendor": vendor, "model": model, "text": "", "error": "", "isolation": "tool-free"}
    if not _posix_runner_available():
        return dict(result, error=PLATFORM_REFUSAL)
    if vendor not in VENDORS:
        return dict(result, error=static_vendor_refusal(vendor))
    if not valid_model(model):
        return dict(result, error="invalid model id")
    if not isinstance(prompt, str) or prompt.startswith("-") or not isinstance(stdin_text, str) or len(prompt.encode()) > 65536 or len(stdin_text.encode()) > 1048576:
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
