"""
Stage 1: Fetch LP records from the Notion CRM database.

Handles two-layer data:
  - Structured fields: Name, Status, Check Size, Location, Email, URL
  - Unstructured content: page body blocks + embedded child page call notes
"""

from notion_client import Client
import os


def get_notion_client():
    token = os.environ.get("NOTION_API_KEY")
    if not token:
        raise RuntimeError("NOTION_API_KEY environment variable not set")
    return Client(auth=token)


def _get_data_source_id(notion, database_id):
    """Resolve the data source ID for a database (notion-client v3)."""
    db = notion.databases.retrieve(database_id=database_id)
    data_sources = db.get("data_sources", [])
    if not data_sources:
        raise RuntimeError(f"No data sources found for database {database_id}")
    return data_sources[0]["id"]


def _query_all_pages(notion, data_source_id):
    """Paginate through all pages in a data source."""
    results = []
    has_more = True
    start_cursor = None
    while has_more:
        response = notion.data_sources.query(
            data_source_id=data_source_id,
            start_cursor=start_cursor,
        )
        results.extend(response["results"])
        has_more = response.get("has_more", False)
        start_cursor = response.get("next_cursor")
    return results


def fetch_all_lps(database_id):
    """Fetch all LP records from CRM. Returns list of LP dicts."""
    notion = get_notion_client()
    data_source_id = _get_data_source_id(notion, database_id)
    results = _query_all_pages(notion, data_source_id)

    lps = []
    for page in results:
        lp = extract_lp_from_page(notion, page)
        lps.append(lp)
        print(f"  Fetched: {lp['name']} ({len(lp['call_notes'])} chars of notes)")

    return lps


def fetch_lp_by_name(database_id, name):
    """Fetch a single LP by name (case-insensitive substring match).
    Much faster than fetch_all_lps for inspecting one record."""
    notion = get_notion_client()
    data_source_id = _get_data_source_id(notion, database_id)
    results = _query_all_pages(notion, data_source_id)

    for page in results:
        page_name = get_title(page["properties"].get("Name"))
        if name.lower() in page_name.lower():
            lp = extract_lp_from_page(notion, page)
            print(f"  Fetched: {lp['name']} ({len(lp['call_notes'])} chars of notes)")
            return lp

    print(f"  No LP found matching '{name}'")
    return None


def extract_lp_from_page(notion, page):
    """Extract structured fields and call notes from one LP page."""
    props = page["properties"]

    structured = {
        "status": get_select(props.get("Status")),
        "check_size": get_select(props.get("Check Size")),
        "location": get_multi_select(props.get("Location")),
        "email": get_email(props.get("Email")),
        "url": get_url(props.get("URL")),
    }
    name = get_title(props.get("Name"))

    # Fetch page body blocks (top-level content)
    blocks = get_all_blocks(notion, page["id"])
    call_notes = blocks_to_text(blocks, notion=notion)

    # Fetch embedded child pages (nested call notes)
    child_pages = find_child_pages(blocks)
    for child_id, child_title in child_pages:
        child_blocks = get_all_blocks(notion, child_id)
        child_text = blocks_to_text(child_blocks, notion=notion)
        if child_text.strip():
            call_notes += f"\n\n--- {child_title} ---\n{child_text}"

    return {
        "id": page["id"],
        "name": name,
        "structured": structured,
        "call_notes": call_notes,
    }


# --- Block traversal ---


def get_all_blocks(notion, block_id):
    """Fetch all child blocks with pagination."""
    blocks = []
    has_more = True
    start_cursor = None
    while has_more:
        response = notion.blocks.children.list(
            block_id=block_id, start_cursor=start_cursor
        )
        blocks.extend(response["results"])
        has_more = response.get("has_more", False)
        start_cursor = response.get("next_cursor")
    return blocks


def blocks_to_text(blocks, notion=None, depth=0):
    """Convert Notion blocks to plain text, preserving structure.
    Recursively fetches children for nested blocks (sub-bullets, toggles, etc.)."""
    indent = "  " * depth
    lines = []
    for block in blocks:
        btype = block["type"]

        # Standard text blocks
        if btype in (
            "paragraph",
            "bulleted_list_item",
            "numbered_list_item",
            "heading_1",
            "heading_2",
            "heading_3",
            "toggle",
            "quote",
            "callout",
        ):
            rich_text = block[btype].get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text.strip():
                prefix = ""
                if btype == "bulleted_list_item":
                    prefix = "• "
                elif btype == "numbered_list_item":
                    prefix = "- "
                elif btype.startswith("heading_"):
                    level = btype[-1]
                    prefix = "#" * int(level) + " "
                elif btype == "quote":
                    prefix = "> "
                lines.append(indent + prefix + text)

        # To-do blocks
        elif btype == "to_do":
            rich_text = block[btype].get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            checked = block[btype].get("checked", False)
            if text.strip():
                mark = "[x]" if checked else "[ ]"
                lines.append(f"{indent}{mark} {text}")

        # Table rows
        elif btype == "table_row":
            cells = block[btype].get("cells", [])
            row_text = " | ".join(
                "".join(rt.get("plain_text", "") for rt in cell)
                for cell in cells
            )
            if row_text.strip():
                lines.append(indent + row_text)

        # Recurse into children (sub-bullets, toggle content, etc.)
        if block.get("has_children") and btype != "child_page" and notion:
            child_blocks = get_all_blocks(notion, block["id"])
            child_text = blocks_to_text(child_blocks, notion=notion, depth=depth + 1)
            if child_text.strip():
                lines.append(child_text)

    return "\n".join(lines)


def find_child_pages(blocks):
    """Find IDs and titles of child pages embedded in content."""
    children = []
    for b in blocks:
        if b["type"] == "child_page":
            title = b["child_page"].get("title", "Untitled")
            children.append((b["id"], title))
    return children


# --- Property helpers ---


def get_title(prop):
    if not prop or not prop.get("title"):
        return ""
    return "".join(t.get("plain_text", "") for t in prop["title"])


def get_select(prop):
    if not prop or not prop.get("select"):
        return None
    return prop["select"].get("name")


def get_multi_select(prop):
    if not prop or not prop.get("multi_select"):
        return []
    return [ms["name"] for ms in prop["multi_select"]]


def get_email(prop):
    if not prop or not prop.get("email"):
        return ""
    return prop["email"] or ""


def get_url(prop):
    if not prop or not prop.get("url"):
        return ""
    return prop["url"] or ""


# --- Standalone test ---
# Usage:
#   python3 notion_reader.py          # fetch all LPs, print summary
#   python3 notion_reader.py "GEM"    # fetch one LP, print full details

if __name__ == "__main__":
    import sys
    from config import NOTION_DATABASE_ID

    if len(sys.argv) > 1:
        # Single LP mode
        name = sys.argv[1]
        print(f"Fetching LP matching '{name}'...\n")
        lp = fetch_lp_by_name(NOTION_DATABASE_ID, name)
        if lp:
            s = lp["structured"]
            print(f"\n{'='*50}")
            print(f"Name:       {lp['name']}")
            print(f"Status:     {s['status'] or '—'}")
            print(f"Check Size: {s['check_size'] or '—'}")
            print(f"Location:   {', '.join(s['location']) or '—'}")
            print(f"Email:      {s['email'] or '—'}")
            print(f"Notes:      {len(lp['call_notes']):,d} chars")
            print(f"{'='*50}\n")
            print(lp["call_notes"])
    else:
        # All LPs mode
        print("Fetching all LPs from Notion...\n")
        lps = fetch_all_lps(NOTION_DATABASE_ID)

        print(f"\n{'='*50}")
        print(f"Total LPs fetched: {len(lps)}")
        print(f"{'='*50}\n")

        for lp in lps:
            s = lp["structured"]
            notes_len = len(lp["call_notes"])
            print(
                f"  {lp['name']:30s} | "
                f"Status: {s['status'] or '—':18s} | "
                f"Check: {s['check_size'] or '—':10s} | "
                f"Notes: {notes_len:,d} chars"
            )
