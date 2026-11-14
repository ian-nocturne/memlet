"""Read evidence variables, independent of optional reader module imports."""

from contextvars import ContextVar

DREAM_READ = ContextVar("memlet_dream_read", default=None)
WIKI_READ = ContextVar("memlet_wiki_read", default=None)
WORK_COUNTERS = ContextVar("memlet_work_counters", default=None)
TRUST_PROJECTIONS = ContextVar("memlet_trust_proj_sink", default=None)
# Only automatically installed evidence is disposable; explicit snapshots remain caller-owned.
INSTALLED_READ = ContextVar("memlet_installed_read", default=None)
