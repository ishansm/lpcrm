import os
import json
from dotenv import load_dotenv

load_dotenv(override=True)

# --- API Keys (from .env or environment) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "3345d6a4e0dc817eb9ece2f97e21ba0c")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "3345d6a4e0dc807c872dd574d98c9d2d")

# --- Models ---
EXTRACTION_MODEL = "claude-sonnet-4-20250514"   # Used for extraction passes
MODEL = "claude-sonnet-4-20250514"              # Fine for rationale generation

# --- GP Profile ---
_profile_path = os.path.join(os.path.dirname(__file__), "gp_profile.json")
with open(_profile_path) as f:
    GP_PROFILE = json.load(f)

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
