import os
import json
from dotenv import load_dotenv

load_dotenv(override=True)

# --- API Keys (from .env or environment) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")

# --- Models ---
EXTRACTION_MODEL = "claude-sonnet-4-20250514"   # Used for extraction passes
MODEL = "claude-sonnet-4-20250514"              # Fine for rationale generation

# --- GP Profile ---
# New layout: gp_profiles/<slug>.json with gp_profiles/_active.json as pointer.
# Fallback to legacy gp_profile.json if the new structure isn't set up.
_here = os.path.dirname(__file__)
_gp_dir = os.path.join(_here, "gp_profiles")
_active_pointer = os.path.join(_gp_dir, "_active.json")
_legacy_path = os.path.join(_here, "gp_profile.json")

ACTIVE_GP_SLUG = None
_profile_path = None
if os.path.exists(_active_pointer):
    with open(_active_pointer) as f:
        ACTIVE_GP_SLUG = json.load(f).get("active")
    if ACTIVE_GP_SLUG:
        candidate = os.path.join(_gp_dir, f"{ACTIVE_GP_SLUG}.json")
        if os.path.exists(candidate):
            _profile_path = candidate

# Fall back to legacy if new layout isn't set up or the active profile file
# is missing. Use "default" slug so cache filenames still work.
if _profile_path is None:
    _profile_path = _legacy_path
if not ACTIVE_GP_SLUG:
    ACTIVE_GP_SLUG = "default"

with open(_profile_path) as f:
    GP_PROFILE = json.load(f)


def _current_slug():
    """Read the currently active slug from gp_profiles/_active.json.
    Falls back to the value at import time, then to 'default'."""
    if os.path.exists(_active_pointer):
        try:
            with open(_active_pointer) as f:
                slug = json.load(f).get("active")
            if slug:
                return slug
        except (json.JSONDecodeError, OSError):
            pass
    return ACTIVE_GP_SLUG or "default"


def output_path(basename):
    """Return output/<stem>_<slug><ext> — per-GP-scoped path.
    Reads the active slug fresh each call so /gp switch + /reload works."""
    stem, ext = os.path.splitext(basename)
    return os.path.join("output", f"{stem}_{_current_slug()}{ext}")


def _migrate_legacy_outputs():
    """One-time rename: if old unscoped output files exist and slug-scoped
    files don't, move them so the user's existing work isn't lost."""
    out_dir = os.path.join(_here, "output")
    if not os.path.isdir(out_dir):
        return
    for fname in ("extracted_profiles.json", "filter_results.json",
                  "scored_results.json", "rationale_results.json"):
        old = os.path.join(out_dir, fname)
        stem, ext = os.path.splitext(fname)
        new = os.path.join(out_dir, f"{stem}_{ACTIVE_GP_SLUG}{ext}")
        if os.path.exists(old) and not os.path.exists(new):
            os.rename(old, new)


_migrate_legacy_outputs()

# --- Scoring Weights ---
WEIGHTS = {
    "intellectual_alignment": 1.75,
    "active_intent": 1.4,
    "demonstrated_behavior": 1.5,
    "sector_alignment": 1.3,
    "geography_match": 1.3,
    "check_size_feasibility": 1.1,
    "relationship_proximity": 1.65,
}

MAX_SCORE = 100  # sum of weights = 10.0, times 10 = 100
