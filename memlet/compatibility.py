"""Read-only recognition of data and managed text written before 9.0.

These are format identifiers, never executable aliases or environment fallbacks.
New writes use current identifiers; existing content stays in its original store.
"""
from pathlib import Path
from contextlib import closing
import sqlite3

# One old directory constant: existence-only notice; never traverse its contents.
LEGACY_STORE_DIRECTORY = ".prism-memory"
# On-disk database filename used by 8.x; explicit store selection must retain its data.
LEGACY_DATABASE_FILENAME = "prism_memory.sqlite3"
# Managed rule delimiters must remain recognizable for replacement/removal in user files.
LEGACY_HARNESS_MARKERS = ("<!-- PRISM_MEMORY_HARNESS_BEGIN -->", "<!-- PRISM_MEMORY_HARNESS_END -->")
# Commands previously written into user settings must be removable, not executable aliases.
LEGACY_HOOK_PROGRAMS = ("ieum-memory", "prism-memory", "deltaharness")
# Persisted context wrappers must still strip safely and be excluded from transcript re-import.
LEGACY_CONTEXT_TAG = "prism-memory-context"
# Signed 8.x capsules must be verified using their original schema and rendering bytes.
LEGACY_HANDOVER_SCHEMA = "routlet-memlet-handover/v1"
LEGACY_HANDOVER_TITLE = "## Routlet × Memlet Verified Handover Capsule"


def existing_store_notice(root):
    """Notice only for an empty default store; do not open or enumerate the old one."""
    new_root = (Path.home() / ".memlet-store").resolve()
    if Path(root).expanduser().resolve() != new_root:
        return None
    old_root = Path.home() / LEGACY_STORE_DIRECTORY
    if not old_root.is_dir():
        return None
    if new_root.is_dir():
        # Store initialization may have created empty tables; task metadata alone
        # does not mean the user has saved any memory. Inspect only the new store.
        for filename in ("work_tasks.sqlite3", "work.sqlite3", "memlet.sqlite3", "state/work.sqlite3", LEGACY_DATABASE_FILENAME):
            db = new_root / filename
            if not db.is_file():
                continue
            try:
                with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table in ("memory_items", "memory_skills"):
                        if table in tables and conn.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone():
                            return None
            except sqlite3.Error:
                return None  # Never label an unreadable store empty.
    return (f"An existing store is at {old_root}; its contents have not been read or migrated. "
            f"Use --root '{old_root}' or MEMLET_HOME='{old_root}' to select it explicitly.")
