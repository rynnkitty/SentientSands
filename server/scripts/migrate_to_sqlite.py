"""
Phase 3 Migration Script: JSON history fields -> SQLite DB
Usage: python.exe server/scripts/migrate_to_sqlite.py [campaign_name]

What it does:
  1. Reads each NPC JSON in campaigns/<name>/characters/
  2. Inserts ConversationHistory, Digests, DurableMemories etc. into SQLite DB
  3. Removes those fields from the JSON (profile fields preserved)
  4. Migrates event_history.json -> event_history table

What it does NOT do:
  - Delete any JSON file
  - Modify Personality, Backstory, Race, Faction or any profile field
  - Touch the original SentientSands/ directory

Run AFTER backing up SentientSands_custom/.
"""

import os
import sys
import json
import sqlite3
import glob

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(SCRIPT_DIR)
CAMPAIGNS_DIR = os.path.join(SERVER_DIR, "campaigns")

HISTORY_KEYS = {
    "ConversationHistory", "Digests", "DigestCursorLine",
    "DigestCursorTs", "ArchiveSummary", "DurableMemories"
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    npc_id  TEXT NOT NULL,
    line    TEXT NOT NULL,
    UNIQUE(npc_id, line)
);
CREATE INDEX IF NOT EXISTS idx_ch_npc ON conversation_history(npc_id, id DESC);
CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    npc_id      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    from_ts     TEXT DEFAULT '',
    to_ts       TEXT DEFAULT '',
    created_day INTEGER DEFAULT 0,
    line_count  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dg_npc ON digests(npc_id, id ASC);
CREATE TABLE IF NOT EXISTS archive_summaries (
    npc_id  TEXT PRIMARY KEY,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_cursors (
    npc_id      TEXT PRIMARY KEY,
    cursor_line TEXT DEFAULT '',
    cursor_ts   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS durable_memories (
    id                TEXT PRIMARY KEY,
    npc_id            TEXT NOT NULL,
    text              TEXT NOT NULL,
    keywords          TEXT DEFAULT '[]',
    w                 INTEGER DEFAULT 1,
    score             REAL DEFAULT 1.0,
    created_day       INTEGER DEFAULT 0,
    last_recalled_day INTEGER DEFAULT 0,
    recall_count      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dm_npc ON durable_memories(npc_id);
CREATE TABLE IF NOT EXISTS event_history (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    line TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS npc_last_seen (
    npc_id   TEXT PRIMARY KEY,
    last_day INTEGER DEFAULT 0
);
"""


def migrate(campaign_name="Default"):
    campaign_dir = os.path.join(CAMPAIGNS_DIR, campaign_name)
    characters_dir = os.path.join(campaign_dir, "characters")
    db_path = os.path.join(campaign_dir, "sentient_sands.db")

    if not os.path.isdir(campaign_dir):
        print(f"ERROR: Campaign directory not found: {campaign_dir}")
        return False

    print(f"\n=== Phase 3 Migration: '{campaign_name}' ===")
    print(f"  DB path : {db_path}")
    print(f"  Chars   : {characters_dir}")

    # Init DB
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    print("  DB schema initialized.")

    # Migrate NPC JSON files
    json_files = glob.glob(os.path.join(characters_dir, "*.json"))
    print(f"  Found {len(json_files)} character JSON files.\n")

    migrated = 0
    skipped = 0
    for path in sorted(json_files):
        fname = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  SKIP {fname}: read error ({e})")
            skipped += 1
            continue

        npc_id = data.get("ID") or data.get("Name") or os.path.splitext(fname)[0]

        # ConversationHistory
        history = data.get("ConversationHistory") or []
        for line in history:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_history (npc_id,line) VALUES (?,?)",
                    (npc_id, line)
                )
            except Exception:
                pass

        # Digests
        for dg in (data.get("Digests") or []):
            conn.execute(
                "INSERT INTO digests (npc_id,summary,from_ts,to_ts,created_day,line_count)"
                " VALUES (?,?,?,?,?,?)",
                (npc_id, dg.get("summary",""), dg.get("from_ts",""),
                 dg.get("to_ts",""), dg.get("created_day",0), dg.get("line_count",0))
            )

        # ArchiveSummary
        ar = (data.get("ArchiveSummary") or "").strip()
        if ar:
            conn.execute(
                "INSERT OR REPLACE INTO archive_summaries (npc_id,summary) VALUES (?,?)",
                (npc_id, ar)
            )

        # DigestCursor
        conn.execute(
            "INSERT OR REPLACE INTO digest_cursors (npc_id,cursor_line,cursor_ts) VALUES (?,?,?)",
            (npc_id, data.get("DigestCursorLine",""), data.get("DigestCursorTs",""))
        )

        # DurableMemories
        for m in (data.get("DurableMemories") or []):
            mem_id = m.get("id") or f"dm_migrated_{npc_id}_{m.get('created_day',0)}"
            conn.execute("""
                INSERT OR REPLACE INTO durable_memories
                (id,npc_id,text,keywords,w,score,created_day,last_recalled_day,recall_count)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (mem_id, npc_id,
                  m.get("text",""), json.dumps(m.get("keywords",[])),
                  m.get("w",1), float(m.get("score",1.0)),
                  m.get("created_day",0), m.get("last_recalled_day",0),
                  m.get("recall_count",0)))

        conn.commit()

        # Remove history fields from JSON, keep profile
        changed = any(k in data for k in HISTORY_KEYS)
        if changed:
            profile = {k: v for k, v in data.items() if k not in HISTORY_KEYS}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)

        h_count = len(history)
        print(f"  OK  {fname:<40}  history={h_count:>3}줄  "
              f"digests={len(data.get('Digests') or [])}"
              f"  memories={len(data.get('DurableMemories') or [])}")
        migrated += 1

    # Migrate event_history.json
    ev_path = os.path.join(campaign_dir, "event_history.json")
    if os.path.exists(ev_path):
        try:
            with open(ev_path, "r", encoding="utf-8") as f:
                events = json.load(f)
            for line in events:
                conn.execute(
                    "INSERT OR IGNORE INTO event_history (line) VALUES (?)", (line,)
                )
            conn.commit()
            print(f"\n  event_history.json: {len(events)}개 이벤트 이전 완료")
        except Exception as e:
            print(f"\n  event_history.json 이전 실패: {e}")

    conn.close()

    print(f"\n=== 완료 ===")
    print(f"  이전 성공: {migrated}개 / 스킵: {skipped}개")
    print(f"  DB: {db_path}")
    print(f"  JSON 프로파일 파일은 보존됩니다 (이력 필드만 제거됨).")
    print(f"\n  이제 SentientSands_Config.ini에 StorageBackend=sqlite 설정을 확인하세요.")
    return True


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "Default"
    migrate(name)
