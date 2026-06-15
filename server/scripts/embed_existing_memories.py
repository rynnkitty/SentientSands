"""
Phase 4: Embed existing DurableMemories in sentient_sands.db.
Usage: python.exe server/scripts/embed_existing_memories.py [campaign_name]

Reads all rows in durable_memories that have no embedding yet,
encodes them with model2vec (potion-multilingual-128M), and stores
the result in the embedding column + durable_memory_index vec0 table.

Requirements:
  - sentient_sands.db must exist (run migrate_to_sqlite.py first)
  - model2vec + potion-multilingual-128M model must be available
  - sqlite-vec must be installed (pip install sqlite-vec)
"""
import os, sys, json, sqlite3, struct

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR  = os.path.dirname(SCRIPT_DIR)
CAMPAIGNS_DIR = os.path.join(SERVER_DIR, "campaigns")
MODELS_DIR  = os.path.join(SERVER_DIR, "models")

def load_model():
    from model2vec import StaticModel
    local = os.path.join(MODELS_DIR, "potion-multilingual-128M")
    if os.path.isdir(local):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        return StaticModel.from_pretrained(local)
    return StaticModel.from_pretrained("minishlab/potion-multilingual-128M")

def load_vec_extension(conn):
    import sqlite_vec
    path = sqlite_vec.loadable_path()
    conn.enable_load_extension(True)
    conn.load_extension(path)
    conn.enable_load_extension(False)

def embed(model, texts):
    import numpy as np
    v = model.encode(texts)
    v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
    return v.astype("float32")

def run(campaign_name="Default"):
    db_path = os.path.join(CAMPAIGNS_DIR, campaign_name, "sentient_sands.db")
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found at {db_path}")
        print("  Run migrate_to_sqlite.py first.")
        return

    print(f"Loading model2vec model...")
    try:
        model = load_model()
    except Exception as e:
        print(f"ERROR: model load failed: {e}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        load_vec_extension(conn)
    except Exception as e:
        print(f"ERROR: sqlite-vec load failed: {e}")
        conn.close()
        return

    # Ensure vec0 table exists
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS durable_memory_index USING vec0(
            memory_id TEXT,
            embedding FLOAT[256]
        )
    """)
    conn.commit()

    # Fetch rows without embeddings
    rows = conn.execute(
        "SELECT id, npc_id, text FROM durable_memories WHERE embedding IS NULL"
    ).fetchall()

    if not rows:
        print("All memories already have embeddings. Nothing to do.")
        conn.close()
        return

    print(f"Embedding {len(rows)} memories...")
    BATCH = 32
    total = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i+BATCH]
        texts = [r["text"] for r in batch]
        vecs  = embed(model, texts)
        for r, v in zip(batch, vecs):
            blob = v.tobytes()
            conn.execute(
                "UPDATE durable_memories SET embedding=? WHERE id=?",
                (blob, r["id"])
            )
            conn.execute(
                "INSERT OR REPLACE INTO durable_memory_index(memory_id,embedding) VALUES(?,?)",
                (r["id"], blob)
            )
        conn.commit()
        total += len(batch)
        print(f"  {total}/{len(rows)} done...")

    conn.close()
    print(f"\n완료! {total}개 메모리 임베딩 저장됨.")
    print(f"DB: {db_path}")

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "Default"
    run(name)
