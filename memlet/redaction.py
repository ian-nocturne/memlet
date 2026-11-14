"""Linear-time secret masking for memory rule text."""
import re

_SECRET_VALUE_PATTERN = (
    r"(?<![A-Za-z0-9])(sk_(?:live|test)_[A-Za-z0-9_-]{8,}|rk_(?:live|test)_[A-Za-z0-9_-]{8,}|whsec_[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{5,})?|"
    r"postgres(?:ql)?://[^\s'\"`\[\]\\]{3,}|mysql://[^\s'\"`\[\]\\]{3,}|mongodb(?:\+srv)?://[^\s'\"`\[\]\\]{3,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{16,}|glpat-[A-Za-z0-9_-]{16,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprse]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|"
    r"npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{30,}|polar_(?:oat|pat|sk)_[A-Za-z0-9_-]{16,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_SECRET_VALUE_RX = re.compile(_SECRET_VALUE_PATTERN)  # reference semantics; never run on untrusted long text


def _secret_value_alternation():
    """`_SECRET_VALUE_PATTERN` in linear time (see `audit.LinearAlternation`). Every alternative except the JWT one
    is a literal prefix followed by one character run with nothing after it, so one regex covers them; the JWT
    alternative (`eyJ` + run + `.` + ...) retried each `eyJ` of a `-eyJ-eyJ...` run against the rest of the run, so
    it gets a run-memoised finder. Same matches as the single regex (differentially tested)."""
    from .audit import LinearAlternation, regex_finder, run_tail_finder
    cls = r"[A-Za-z0-9_-]"
    before_jwt = re.compile(
        r"(?<![A-Za-z0-9])(?:sk_(?:live|test)_[A-Za-z0-9_-]{8,}|rk_(?:live|test)_[A-Za-z0-9_-]{8,}|whsec_[A-Za-z0-9_-]{8,}|"
        r"sk-[A-Za-z0-9_-]{12,})")
    after_jwt = re.compile(
        r"(?<![A-Za-z0-9])(?:postgres(?:ql)?://[^\s'\"`\[\]\\]{3,}|mysql://[^\s'\"`\[\]\\]{3,}|"
        r"mongodb(?:\+srv)?://[^\s'\"`\[\]\\]{3,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{16,}|glpat-[A-Za-z0-9_-]{16,}|"
        r"(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprse]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|"
        r"npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{30,}|polar_(?:oat|pat|sk)_[A-Za-z0-9_-]{16,}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")
    jwt = run_tail_finder(re.compile(r"(?<![A-Za-z0-9])eyJ"), re.compile(cls + "*"), 10,
                          re.compile(r"\." + cls + r"{10,}(?:\." + cls + r"{5,})?"))
    return LinearAlternation(_SECRET_VALUE_PATTERN, [regex_finder(before_jwt), jwt, regex_finder(after_jwt)], _owned=True)


SECRET_VALUE_REDACT_RE = _secret_value_alternation()


def mask_sensitive_text(text: str) -> str:
    """Redact any accidental token or connection string pattern from free text."""
    if not text:
        return ""
    return SECRET_VALUE_REDACT_RE.sub("[REDACTED_SECRET]", str(text))
