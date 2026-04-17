"""
GP profile management.

Stores multiple GP opportunity profiles in gp_profiles/, with _active.json
pointing at the currently active one. New profiles are created from pasted
free-text by asking Claude to structure them into the canonical schema.

Usage:
  python3 gp.py
"""

import json
import os
import re
import subprocess
import sys
import anthropic
from config import MODEL, ANTHROPIC_API_KEY


GP_DIR = os.path.join(os.path.dirname(__file__), "gp_profiles")
ACTIVE_POINTER = os.path.join(GP_DIR, "_active.json")


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _ensure_dir():
    os.makedirs(GP_DIR, exist_ok=True)


def list_slugs():
    _ensure_dir()
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(GP_DIR)
        if f.endswith(".json") and not f.startswith("_")
    )


def load_active():
    if not os.path.exists(ACTIVE_POINTER):
        return None
    with open(ACTIVE_POINTER) as f:
        return json.load(f).get("active")


def set_active(slug):
    _ensure_dir()
    with open(ACTIVE_POINTER, "w") as f:
        json.dump({"active": slug}, f, indent=2)


def load_profile(slug):
    path = os.path.join(GP_DIR, f"{slug}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_profile(slug, profile):
    _ensure_dir()
    path = os.path.join(GP_DIR, f"{slug}.json")
    with open(path, "w") as f:
        json.dump(profile, f, indent=2)
    return path


def slugify(name):
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "gp"


# ---------------------------------------------------------------------------
# Claude structuring
# ---------------------------------------------------------------------------

STRUCTURE_PROMPT = """You are structuring a GP investment opportunity into a canonical JSON profile. The user pasted this description:

{text}

Extract and return a JSON object with these exact fields:
- name — short fund name (string)
- fund_size — e.g., "$20M" (string)
- stage — e.g., "pre-seed/seed", "Series A" (string)
- geography — primary geography, comma-separated (string)
- manager_type — e.g., "first-time fund manager", "second-time manager" (string)
- sectors — list of strings
- broader_geo — list of adjacent/secondary geographies (strings)
- lp_product_category — one-sentence categorization for LP matching (string)
- key_traits — 2-3 bullet points capturing the fund's differentiated thesis (list of strings)

Rules:
- If a field isn't present in the text, make a reasonable inference and flag it with a prefix [inferred] in the value, OR leave it as "unknown" for strings / [] for lists if no reasonable inference is possible.
- Don't invent sectors or geographies not supported by the text.
- Output ONLY valid JSON. No markdown, no commentary."""


def structure_with_claude(text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        temperature=0,
        messages=[{"role": "user", "content": STRUCTURE_PROMPT.format(text=text)}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        lines = [l for l in raw.split("\n") if not l.strip().startswith("```")]
        raw = "\n".join(lines)
    return json.loads(raw)


def print_profile(profile):
    """Print profile, highlighting [inferred] lines with a prefix marker."""
    for key, val in profile.items():
        is_inferred = (
            (isinstance(val, str) and "[inferred]" in val)
            or (isinstance(val, list) and any("[inferred]" in str(v) for v in val))
        )
        marker = "  ⚠ " if is_inferred else "    "
        if isinstance(val, list):
            print(f"{marker}{key}: {json.dumps(val)}")
        else:
            print(f"{marker}{key}: {json.dumps(val)}")


# ---------------------------------------------------------------------------
# Commands (shared with chatbot /gp)
# ---------------------------------------------------------------------------

def cmd_list():
    active = load_active()
    slugs = list_slugs()
    if not slugs:
        print("No GP profiles found.")
        return
    for s in slugs:
        marker = "* " if s == active else "  "
        prof = load_profile(s) or {}
        print(f"{marker}{s:25s}  {prof.get('name', '?')}")


def cmd_show(slug):
    if not slug:
        print("Usage: show <name>")
        return
    prof = load_profile(slug)
    if not prof:
        print(f"No profile named '{slug}'.")
        return
    print_profile(prof)


def cmd_switch(slug, offer_rerun=True):
    if not slug:
        print("Usage: switch <name>")
        return
    prof = load_profile(slug)
    if not prof:
        print(f"No profile named '{slug}'.")
        return
    prev = load_active()
    set_active(slug)
    print(f"Active GP is now: {slug} ({prof.get('name', '?')})")
    if offer_rerun and prev and prev != slug:
        print(f"\nThe pipeline was last run against: {prev}")
        print(f"Your output/ files for '{slug}' may be stale or missing.")
        ans = input("Do you want to re-run the pipeline now? (y/n/later): ").strip().lower()
        if ans == "y":
            print("\nRunning python3 main.py ...\n")
            subprocess.run([sys.executable, "main.py"])
        else:
            print(f"\u26a0 Reminder: query.py will read stale/old data for '{slug}' "
                  f"until you run 'python3 main.py'.")


def cmd_delete(slug):
    if not slug:
        print("Usage: delete <name>")
        return
    if not load_profile(slug):
        print(f"No profile named '{slug}'.")
        return
    if slug == load_active():
        print(f"'{slug}' is the active GP. Switch to another GP first.")
        return
    ans = input(f"Delete '{slug}'? This cannot be undone. (y/n): ").strip().lower()
    if ans != "y":
        print("Cancelled.")
        return
    os.remove(os.path.join(GP_DIR, f"{slug}.json"))
    print(f"Deleted {slug}.")


def collect_paste(prompt="Paste a description of the GP (sector, geography, stage, manager type, fund size, thesis).\nWhen done, type END on a blank line.\n"):
    print(prompt)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def cmd_add():
    text = collect_paste()
    if not text:
        print("No input. Cancelled.")
        return

    print("\nProcessing with Claude...\n")
    try:
        profile = structure_with_claude(text)
    except Exception as e:
        print(f"Structuring failed: {e}")
        return

    print("Here's the structured profile:")
    print_profile(profile)

    suggested = slugify(profile.get("name", "gp"))
    ans = input(f"\nSave this as? (suggested: {suggested}, or type your own name / 'cancel'): ").strip()
    if ans.lower() == "cancel" or ans.lower() == "":
        if ans == "":
            slug = suggested
        else:
            print("Cancelled.")
            return
    else:
        slug = slugify(ans) if ans else suggested

    if load_profile(slug):
        overwrite = input(f"'{slug}' already exists. Overwrite? (y/n): ").strip().lower()
        if overwrite != "y":
            print("Cancelled.")
            return

    save_profile(slug, profile)
    print(f"Saved to gp_profiles/{slug}.json")

    make_active = input("Make this the active GP now? (y/n): ").strip().lower()
    if make_active == "y":
        cmd_switch(slug, offer_rerun=True)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

HELP = """Commands:
  list              List all GP profiles
  show <name>       Show a GP profile's fields
  add               Add a new GP from pasted text
  switch <name>     Switch the active GP
  delete <name>     Delete a GP profile (asks confirmation)
  exit              Quit
"""


def main():
    _ensure_dir()
    active = load_active()
    print("GP Management")
    print(f"  Current active GP: {active or '(none)'}\n")
    print(HELP)

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        parts = raw.split(maxsplit=1)
        verb = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if verb in ("exit", "quit"):
            break
        elif verb == "help":
            print(HELP)
        elif verb == "list":
            cmd_list()
        elif verb == "show":
            cmd_show(arg)
        elif verb == "add":
            cmd_add()
        elif verb == "switch":
            cmd_switch(arg)
        elif verb == "delete":
            cmd_delete(arg)
        else:
            print("Unknown command. Type 'help'.")
        print()


if __name__ == "__main__":
    main()
