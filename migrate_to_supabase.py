"""One-time migration of local Response/*.json and ReadList/read_list.json into Supabase.

Usage:
    ./.venv/bin/python migrate_to_supabase.py

Requires SUPABASE_DB_URL to be set in .env (see .env.example). Safe to re-run;
sessions and Read List articles are upserted by their existing id/url.
"""
from __future__ import annotations

import json
from pathlib import Path

import storage

RESPONSE_DIR = storage.BASE_DIR / "Response"
READ_LIST_PATH = storage.BASE_DIR / "ReadList" / "read_list.json"


def migrate_sessions() -> int:
    count = 0
    for path in sorted(RESPONSE_DIR.glob("*.json")):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  Skipping {path.name}: {exc}")
            continue
        storage.save_session_record(session)
        count += 1
        print(f"  Migrated session {session.get('session_id', path.stem)}")
    return count


def migrate_read_list() -> int:
    if not READ_LIST_PATH.exists():
        return 0
    try:
        items = json.loads(READ_LIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  Skipping Read List: {exc}")
        return 0
    count = 0
    for item in items:
        item = dict(item)
        item.setdefault("id", storage.new_read_item_id())
        storage.insert_read_item(item)
        count += 1
        print(f"  Migrated article: {item.get('title', item.get('url'))}")
    return count


def main() -> int:
    if not storage.SUPABASE_DB_URL:
        print("SUPABASE_DB_URL is not set. Add it to .env before running this script.")
        return 1
    print("Ensuring Supabase schema...")
    storage.ensure_schema()
    print("Migrating exam sessions...")
    session_count = migrate_sessions()
    print("Migrating Read List...")
    read_count = migrate_read_list()
    print(f"Done. Migrated {session_count} session(s) and {read_count} Read List item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
