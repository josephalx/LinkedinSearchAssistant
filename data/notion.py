"""
notion.py — writes a job as a new row/page into your Notion job-tracker
database (the one you imported from Loop's CSV export).

Reads the database's actual property schema first (via the Notion API)
and builds the write payload dynamically based on each property's real
type — so this works whether "Status"/"Platform" ended up as plain Text
or Select after the CSV import, without hardcoding an assumption either way.

One-time setup:
    python3 -c "import keyring; keyring.set_password('notion', 'api_key', 'your_integration_secret')"
    Share the database with your integration in Notion:
      database -> ... -> Connections -> connect your integration
"""

import os
from datetime import date

import requests
import keyring

NOTION_TOKEN = keyring.get_password("notion", "api_key") or os.environ.get("NOTION_TOKEN")

# Your job-tracker database.
NOTION_DATABASE_ID = "3d9781efa333806e95bfd910f12e1975"

NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"

_schema_cache = None


def _headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_database_schema():
    """Fetches (and caches) the database's actual property types, so the
    write payload always matches reality instead of a guess."""
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache

    resp = requests.get(f"{BASE_URL}/databases/{NOTION_DATABASE_ID}", headers=_headers())
    resp.raise_for_status()
    props = resp.json()["properties"]
    _schema_cache = {name: meta["type"] for name, meta in props.items()}
    return _schema_cache


def _build_property_value(prop_type, value):
    """Wraps a plain value in whatever shape Notion's API expects for a
    given property type."""
    if prop_type == "title":
        return {"title": [{"text": {"content": str(value)}}]}
    if prop_type == "rich_text":
        return {"rich_text": [{"text": {"content": str(value)}}]}
    if prop_type == "select":
        return {"select": {"name": str(value)}}
    if prop_type == "date":
        return {"date": {"start": value}}  # expects "YYYY-MM-DD"
    if prop_type == "url":
        return {"url": value}
    if prop_type == "number":
        return {"number": value}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    raise ValueError(f"Unsupported Notion property type: {prop_type!r} — check the database schema.")


def add_job_to_notion(company, role, url=None):
    """Creates one new row in the Notion database for this job.
    Maps to the same columns as your imported CSV: Company Name, Platform,
    Role, Date Applied, Status."""
    schema = get_database_schema()

    field_values = {
        "Company Name": company,
        "Platform": "Linkedin",
        "Role": role,
        "Date Applied": date.today().isoformat(),
        "Status": "Applied",
    }

    properties = {}
    for field_name, value in field_values.items():
        if field_name not in schema:
            continue  # database doesn't have this column — skip rather than error
        properties[field_name] = _build_property_value(schema[field_name], value)

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }

    resp = requests.post(f"{BASE_URL}/pages", headers=_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()
