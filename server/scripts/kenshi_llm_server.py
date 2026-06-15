import os
import ctypes
import json
import logging
import subprocess
import signal
import requests
import re
import time
import threading
import random
import configparser
import queue
from flask import Flask, request, jsonify
import sys
import logging.handlers
import traceback
import collections
import difflib
import sqlite3

# --- PATH DEFINITIONS (The absolute source of truth) ---
SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
KENSHI_SERVER_DIR = os.path.dirname(SCRIPT_DIR)
KENSHI_MOD_DIR = os.path.dirname(KENSHI_SERVER_DIR)
KENSHI_ROOT = os.path.dirname(os.path.dirname(KENSHI_MOD_DIR))

# Explicitly add script dir to path for imports
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from save_reader import build_world_index

# --- CORE GLOBALS & CONFIG PATHS ---
def resolve_mod_file(filename):
    """
    Helper to find a file in the mod directory.
    Normally files are in KENSHI_MOD_DIR (the root of the mod).
    During development they might be in a 'SentientSands_Mod' subdirectory.
    """
    # 1. Primary: Mod Root (Deployed state)
    path = os.path.join(KENSHI_MOD_DIR, filename)
    if os.path.exists(path):
        return path
        
    # 2. Secondary: Development Subfolder
    dev_path = os.path.join(KENSHI_MOD_DIR, "SentientSands_Mod", filename)
    if os.path.exists(dev_path):
        return dev_path
        
    # 3. Tertiary: Sibling project folder (Source layout)
    alt_path = os.path.join(os.path.dirname(KENSHI_MOD_DIR), "SentientSands_Mod", filename)
    if os.path.exists(alt_path):
        return alt_path

    return path

INI_PATH = resolve_mod_file("SentientSands_Config.ini")
MODELS_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "models.json")
PROVIDERS_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "providers.json")
NAMES_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "names.json")
GENERIC_NAMES_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "generic_names.json")
LOCALIZATION_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "localization.json")

MODELS_CONFIG = {}
PROVIDERS_CONFIG = {}
NAMES_CONFIG = {}
GENERIC_CONFIG = {}
CURRENT_MODEL_KEY = "player2-default" # Default
ACTIVE_CAMPAIGN = "Default"      # Default

CAMPAIGNS_DIR = os.path.join(KENSHI_SERVER_DIR, "campaigns")
TEMPLATES_DIR = os.path.join(KENSHI_SERVER_DIR, "templates")
CHARACTERS_DIR = os.path.join(KENSHI_SERVER_DIR, "characters") # Initial fallback

EVENT_HISTORY = []
PROFILES_IN_PROGRESS = set()
PROGRESS_LOCK = threading.Lock()
LIVE_CONTEXTS = {}
PLAYER_CONTEXT = {}
LAST_NPC_NAME = None
PLAYER2_SESSION_KEY = None
EVENT_THROTTLE = {} 
THROTTLE_LOCK = threading.Lock()
LAST_STATE_LOG = {} # { "NPCName|etype": "last_msg" }
STATE_LOCK = threading.Lock()

# --- Phase 2: mid-term memory Digest worker state ---
# Single background worker + queue keeps LLM digest calls serialized so chat
# latency is never impacted and calls can never stampede (report 1-② design).
DIGEST_QUEUE = queue.Queue()
DIGESTS_IN_PROGRESS = set()   # storage_ids currently queued/being digested
DIGEST_LOCK = threading.Lock()
DIGEST_LAST_RUN = {}          # storage_id -> wall-clock time of last digest attempt (throttle)
SYNTHESIS_STATUS = {"elapsed": 0, "interval": 60}

# Phase 1: max lines stored and displayed in ConversationHistory.
# Lowered from 250 to reduce file size and I/O over long playthroughs.
HISTORY_MAX_LINES = 100

MAJOR_FACTIONS = [
    "The Holy Nation", "United Cities", "Shek Kingdom",
    "Traders Guild", "Slave Traders", "Western Hive",
    "Anti-Slavers", "Flotsam Ninjas", "Mongrel", "The Hub",
    "Hounds", "Deadcat", "Black Desert City"
]

ANIMAL_RACES = [
    "Bonedog", "Boneyard Wolf", "Garru", "Beak Thing", "Gorillo",
    "Landbat", "Goat", "Bull", "Leviathan", "Blood Spider", "Skin Spider",
    "Cave Crawler", "Crab", "Raptor", "Darkfinger", "Thrasher", "Cleaner",
    "Crimper", "Skimmer", "Beeler", "Bat", "Spider", "Wolf"
]

def get_config_radii():
    settings = load_settings()
    # Use radii from settings if present, otherwise fall back to defaults
    r = float(settings.get('radiant_range', 100.0))
    t = float(settings.get('talk_radius', 100.0))
    y = float(settings.get('yell_radius', 200.0))
    return r, t, y

# --- Phase 0: Lightweight prompt instrumentation ---
# Rough token estimate based on string length only. English: ~4 chars/token.
# Korean (Hangul): ~2 chars/token — BPE tokenizers use 2-3 tokens per syllable.
# When Korean chars exceed 30% of content, applies the stricter divisor.
# Zero tokenizer dependency, negligible performance impact.
def estimate_tokens(text):
    if not text:
        return 0
    korean_chars = sum(1 for c in text if '가' <= c <= '힣')
    if korean_chars / max(len(text), 1) > 0.3:
        return max(1, len(text) // 2)
    return max(1, len(text) // 4)

def format_token_breakdown(sections):
    """Formats a {section_name: text_or_int} dict into a 'k≈Ntk' summary string."""
    parts = []
    for k, v in sections.items():
        t = v if isinstance(v, int) else estimate_tokens(v)
        parts.append(f"{k}≈{t}")
    return " | ".join(parts)

def sanitize_llm_text(text):
    if not text: return ""
    # Replace common unicode/smart characters that Kenshi's engine might choke on
    replacements = {
        '\u2018': "'", '\u2019': "'", # Smart single quotes
        '\u201c': '"', '\u201d': '"', # Smart double quotes
        '\u2013': '-', '\u2014': '-', # En/Em dashes
        '\u2026': '...',             # Ellipsis
        '\u00a0': ' ',                # Non-breaking space
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    
    # Standardize line endings
    text = text.replace('\r\n', '\n')
    text = text.replace('\\n', '\n')  # Catch literal escaped newlines
    text = text.replace('\\r', '')    # Catch literal escaped carriage returns
    return text

# Setup logging
_log_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
_log_dir = os.path.join(SCRIPT_DIR, "..", "logs")
if not os.path.exists(_log_dir):
    try:
        os.makedirs(_log_dir)
    except:
        pass

# 1. Main Server Log (Circular/Limited)
_log_file = os.path.join(_log_dir, "server.log")
# 2. Comprehensive Debug Log (Last ~500 entries)
_debug_file = os.path.join(KENSHI_SERVER_DIR, "debug.log")

try:
    # server.log: 512KB limit, 3 backups
    _file_handler = logging.handlers.RotatingFileHandler(_log_file, maxBytes=512*1024, backupCount=3, encoding='utf-8')
    _file_handler.setFormatter(_log_fmt)
    
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(_log_fmt)
    
    # debug.log: 1MB limit, 1 backup
    _debug_handler = logging.handlers.RotatingFileHandler(_debug_file, maxBytes=1024*1024, backupCount=1, encoding='utf-8')
    _debug_handler.setFormatter(_log_fmt)
    _debug_handler.setLevel(logging.DEBUG)

    # Global config
    logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler, _debug_handler])
    
    # Specialized logger for high-volume telemetry (prompts, raw data)
    # This prevents server.log from becoming a wall of text.
    debug_logger = logging.getLogger('kenshi_debug')
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.addHandler(_debug_handler)
    debug_logger.propagate = False # Do not double-log to root handlers

except Exception as e:
    # Fallback to stream only if file handler fails
    logging.basicConfig(level=logging.INFO)
    logging.error(f"Failed to initialize file logging: {e}")

# Silence noise
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Kill any existing process on port 5000 before starting
def kill_old_servers():
    try:
        # Windows specific: find processes on port 5000
        result = subprocess.run(
            ['netstat', '-aon'], capture_output=True, text=True, shell=True
        )
        for line in result.stdout.splitlines():
            if ':5000' in line and 'LISTENING' in line:
                parts = line.strip().split()
                pid = int(parts[-1])
                # Never kill ourselves
                if pid > 0 and pid != os.getpid():
                    logging.info(f"Terminating old server process (PID {pid}) on port 5000...")
                    # Force kill to ensure it's gone
                    subprocess.run(['taskkill', '/F', '/PID', str(pid)], 
                                 capture_output=True, shell=True)
                    time.sleep(1) # Give it a moment to clear the port
    except Exception as e:
        logging.warning(f"Port cleanup diagnostic: {e}")

kill_old_servers()

app = Flask(__name__)
# --- JSON response encoding (Korean name pool support) ---
# NOTE: On Flask >= 2.3 the legacy JSON_AS_ASCII config is ignored; the effective
# switch is app.json.ensure_ascii (default True -> \uXXXX escapes).
# We send RAW UTF-8 instead, for both the HTTP JSON path and the named pipe path:
#  * The DLL has no known JSON library signature (likely a minimal parser);
#    \uXXXX escape decoding is unverified, while raw UTF-8 pass-through is the
#    behaviour every parser supports (the pipe path already ships raw UTF-8 and works).
#  * For pure-ASCII content the output bytes are IDENTICAL either way, so existing
#    English names/saves see zero change (regression-verified).
# If in-game text ever shows mojibake, flip ensure_ascii back to True here.
try:
    app.json.ensure_ascii = False          # Flask >= 2.3 / 3.x
except AttributeError:
    app.config['JSON_AS_ASCII'] = False    # legacy Flask fallback

@app.errorhandler(Exception)
def handle_exception(e):
    # Log the full stack trace for any unhandled exception in Flask routes
    logging.error(f"UNHANDLED SERVER EXCEPTION: {str(e)}")
    debug_logger.error(f"UNHANDLED SERVER EXCEPTION STACK:\n{traceback.format_exc()}")
    # Truncate request data if possible for the debug log
    try:
        if request.json:
            debug_logger.debug(f"Offending Request JSON: {json.dumps(request.json, indent=2)}")
    except:
        pass
    return jsonify({"error": str(e), "status": "error"}), 500

# 3. Load Configurations
def load_configs():
    global MODELS_CONFIG, PROVIDERS_CONFIG, NAMES_CONFIG
    logging.info("Checking configurations...")
    
    # Create config dir if missing
    config_dir = os.path.join(KENSHI_SERVER_DIR, "config")
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)

    if os.path.exists(MODELS_PATH):
        try:
            with open(MODELS_PATH, "r") as f:
                MODELS_CONFIG = json.load(f)
            logging.info(f"Loaded {len(MODELS_CONFIG)} models.")
        except Exception as e:
            logging.error(f"Failed to load models.json: {e}")
            
    if os.path.exists(PROVIDERS_PATH):
        try:
            with open(PROVIDERS_PATH, "r") as f:
                PROVIDERS_CONFIG = json.load(f)
            logging.info(f"Loaded {len(PROVIDERS_CONFIG)} providers.")
        except Exception as e:
            logging.error(f"Failed to load providers.json: {e}")

    if os.path.exists(NAMES_PATH):
        try:
            # Explicit utf-8-sig: name pool may contain non-ASCII (Korean) names.
            # Locale default (cp949) would break or reject raw UTF-8 content.
            with open(NAMES_PATH, "r", encoding="utf-8-sig") as f:
                NAMES_CONFIG = json.load(f)
            logging.info(f"Loaded {len(NAMES_CONFIG)} gender pools from names.json.")
        except Exception as e:
            logging.error(f"Failed to load names.json: {e}")

    if os.path.exists(GENERIC_NAMES_PATH):
        try:
            global GENERIC_CONFIG
            # Explicit utf-8-sig (locale-independent). Content itself must stay
            # English — the DLL matches these against original in-game names.
            with open(GENERIC_NAMES_PATH, "r", encoding="utf-8-sig") as f:
                GENERIC_CONFIG = json.load(f)
            logging.info(f"Loaded {len(GENERIC_CONFIG.get('prefixes', []))} generic prefixes from generic_names.json.")
        except Exception as e:
            logging.error(f"Failed to load generic_names.json: {e}")

    global LOCALIZATION_CONFIG
    LOCALIZATION_CONFIG = {}
    if os.path.exists(LOCALIZATION_PATH):
        try:
            with open(LOCALIZATION_PATH, "r", encoding="utf-8") as f:
                LOCALIZATION_CONFIG = json.load(f)
            logging.info(f"Loaded {len(LOCALIZATION_CONFIG)} language localizations.")
        except Exception as e:
            logging.error(f"Failed to load localization.json: {e}")

# Event History Persistence
GLOBAL_EVENT_COUNTER = 0

# --- CAMPAIGN MANAGEMENT ---
def get_campaign_dir():
    if not os.path.exists(CAMPAIGNS_DIR):
        os.makedirs(CAMPAIGNS_DIR)
        logging.info(f"Created base campaigns directory: {CAMPAIGNS_DIR}")
        
    cdir = os.path.join(CAMPAIGNS_DIR, ACTIVE_CAMPAIGN)
    if not os.path.exists(cdir):
        os.makedirs(cdir)
        logging.info(f"Created campaign directory: {cdir}")
        # Automatically seed new campaigns created during startup/init
        ensure_campaign_seeded(cdir)
    return cdir

def ensure_campaign_seeded(cdir):
    """Populates a campaign directory with default templates and folders."""
    try:
        if not os.path.exists(os.path.join(cdir, "characters")):
            os.makedirs(os.path.join(cdir, "characters"))
            
        # Copy essential personal files to campaigns by default. 
        # All other templates (rules, lore, etc.) remain global in TEMPLATES_DIR.
        for component in ["character_bio.txt", "player_faction_description.txt"]:
            src = os.path.join(TEMPLATES_DIR, component)
            dst = os.path.join(cdir, component)
            if os.path.exists(src) and not os.path.exists(dst):
                import shutil
                shutil.copy2(src, dst)
                logging.info(f"CAMPAIGN: Seeded '{os.path.basename(cdir)}' with {component}")
            
        # Ensure world_events.txt exists (Campaign-Specific History)
        ev_path = os.path.join(cdir, "world_events.txt")
        if not os.path.exists(ev_path):
            with open(ev_path, "w", encoding="utf-8") as f:
                f.write("# Dynamic rumors generated for this campaign\n")
    except Exception as e:
        logging.error(f"Failed to seed campaign directory {cdir}: {e}")

def migrate_to_campaigns():
    """Moves legacy data to campaigns/Default if not already migrated."""
    try:
        if not os.path.exists(CAMPAIGNS_DIR):
            os.makedirs(CAMPAIGNS_DIR)
            
        default_dir = os.path.join(CAMPAIGNS_DIR, "Default")
        is_new_default = not os.path.exists(default_dir)
        
        if is_new_default:
            os.makedirs(default_dir)
            logging.info("MIGRATION: Created Default campaign folder")
            
        import shutil
        # 1. Characters
        old_chars = os.path.join(KENSHI_SERVER_DIR, "characters")
        new_chars = os.path.join(default_dir, "characters")
        if os.path.exists(old_chars) and not os.path.exists(new_chars):
            try:
                shutil.move(old_chars, new_chars)
                logging.info("MIGRATION: Moved legacy characters to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (Characters): {e}")
            
        # 2. Registry
        old_reg = os.path.join(KENSHI_MOD_DIR, "kenshi_ai_registry")
        if not os.path.exists(old_reg):
            old_reg = os.path.join(KENSHI_MOD_DIR, "sentient_sands_registry")
        
        new_reg = os.path.join(default_dir, "sentient_sands_registry")
        if os.path.exists(old_reg) and not os.path.exists(new_reg):
            try:
                shutil.move(old_reg, new_reg)
                logging.info("MIGRATION: Moved legacy registry to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (Registry): {e}")

        # 3. World Events / Rumors
        old_events = os.path.join(KENSHI_SERVER_DIR, "world_events.txt")
        new_events = os.path.join(default_dir, "world_events.txt")
        if os.path.exists(old_events) and not os.path.exists(new_events):
            try:
                shutil.move(old_events, new_events)
                logging.info("MIGRATION: Moved legacy world_events.txt to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (World Events): {e}")

        # 4. Global Event History
        old_hist = os.path.join(KENSHI_SERVER_DIR, "event_history.json")
        new_hist = os.path.join(default_dir, "event_history.json")
        if os.path.exists(old_hist) and not os.path.exists(new_hist):
            try:
                shutil.move(old_hist, new_hist)
                logging.info("MIGRATION: Moved legacy event_history.json to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (History): {e}")

        # Ensure templates exist in Default (always check this during migration)
        ensure_campaign_seeded(default_dir)
            
    except Exception as e:
        logging.error(f"MIGRATION: Critical failure in migration logic: {e}")

def load_campaign_config():
    """Initializes paths based on the active campaign."""
    global CHARACTERS_DIR, EVENT_HISTORY
    try:
        cdir = get_campaign_dir()
        
        # 1. Update Directories
        CHARACTERS_DIR = os.path.join(cdir, "characters")
        if not os.path.exists(CHARACTERS_DIR): 
            os.makedirs(CHARACTERS_DIR)
        
        # 2. Load Persisted Event History
        hist_path = os.path.join(cdir, "event_history.json")
        if os.path.exists(hist_path):
            try:
                with open(hist_path, "r", encoding="utf-8") as f:
                    EVENT_HISTORY = json.load(f)
                logging.info(f"CAMPAIGN: Loaded {len(EVENT_HISTORY)} events for '{ACTIVE_CAMPAIGN}'")
            except Exception as e:
                logging.error(f"Failed to load event history: {e}")
                EVENT_HISTORY = []
        else:
            EVENT_HISTORY = []
        # 3. Phase 3: initialize SQLite DB for this campaign
        init_db()
        # 4. Push generic names to DLL
        push_generic_names_to_dll()
    except Exception as e:
        logging.error(f"CAMPAIGN: Critical failure loading config: {e}")

def send_to_pipe(cmd):
    """
    Robust pipe transmission. Prepends CMD: if not already present.
    """
    if not (cmd.startswith("CMD:") or cmd.startswith("NPC_") or cmd.startswith("PLAYER_") or cmd.startswith("SHOW_HISTORY")):
        cmd = "CMD: " + cmd
        
    try:
        with open(r'\\.\pipe\SentientSands', 'wb') as f:
            f.write(cmd.encode('utf-8'))
    except:
        pass

def push_generic_names_to_dll():
    """Syncs generic name lists to the C++ renamer via pipe."""
    try:
        prefixes = GENERIC_CONFIG.get("prefixes", [])
        keywords = GENERIC_CONFIG.get("keywords", [])
        p_str = ",".join(prefixes)
        k_str = ",".join(keywords)
        send_to_pipe(f"POPULATE_GENERIC: {p_str}|{k_str}")
        logging.info("PIPE: Synced generic name lists to DLL")
    except Exception as e:
        logging.error(f"Failed to sync generic names to DLL: {e}")

def save_campaign_history():
    """Phase 3: persist EVENT_HISTORY to DB (sqlite mode) or JSON (legacy)."""
    if load_settings().get("storage_backend", "sqlite") == "sqlite":
        try:
            with _DB_WRITE_LOCK:
                with get_db_connection() as conn:
                    for line in EVENT_HISTORY:
                        conn.execute(
                            "INSERT OR IGNORE INTO event_history (line) VALUES (?)", (line,)
                        )
                    # Keep only the most recent 500 rows
                    conn.execute("""
                        DELETE FROM event_history WHERE id NOT IN (
                            SELECT id FROM event_history ORDER BY id DESC LIMIT 500
                        )
                    """)
                    conn.commit()
        except Exception as e:
            logging.error(f"DB: event_history save failed: {e}")
    else:
        try:
            cdir = get_campaign_dir()
            hist_path = os.path.join(cdir, "event_history.json")
            with open(hist_path, "w", encoding="utf-8") as f:
                json.dump(EVENT_HISTORY, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.error(f"Failed to save event history: {e}")

# =====================================================================
# Phase 3: SQLite Hybrid Storage — DB helpers (must be defined before
# init_server_state() is called so load_campaign_config() can find them)
# =====================================================================

_DB_WRITE_LOCK = threading.Lock()  # serialize concurrent writes

# Phase 4: sqlite-vec extension path (loaded once at startup)
_SQLITE_VEC_PATH = None
try:
    import sqlite_vec as _sv
    _SQLITE_VEC_PATH = _sv.loadable_path()
    logging.info(f"VECTOR_RAG: sqlite-vec found at {_SQLITE_VEC_PATH}")
except Exception as _e:
    logging.warning(f"VECTOR_RAG: sqlite-vec unavailable ({_e}) — numpy fallback will be used")

def get_db_path():
    return os.path.join(get_campaign_dir(), "sentient_sands.db")

def get_db_connection(vec=False):
    """Return a WAL-mode SQLite connection. Pass vec=True to load sqlite-vec extension."""
    conn = sqlite3.connect(get_db_path(), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if vec and _SQLITE_VEC_PATH:
        try:
            conn.enable_load_extension(True)
            conn.load_extension(_SQLITE_VEC_PATH)
            conn.enable_load_extension(False)
        except Exception as e:
            logging.warning(f"VECTOR_RAG: extension load failed: {e}")
    return conn

_DB_SCHEMA = """
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

# Phase 4: additional schema run with vec extension loaded
_VEC_SCHEMA = """
ALTER TABLE durable_memories ADD COLUMN embedding BLOB;
"""
_VEC_INDEX_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS durable_memory_index USING vec0(
    memory_id TEXT,
    embedding FLOAT[256]
);
"""

def init_db():
    """Create base tables. Then add embedding column and vec0 index if sqlite-vec is available."""
    try:
        with get_db_connection() as conn:
            conn.executescript(_DB_SCHEMA)
        logging.info(f"DB: initialized at {get_db_path()}")
    except Exception as e:
        logging.error(f"DB: init failed: {e}")
        return

    # Phase 4: add embedding column (ignore error if column already exists)
    if _SQLITE_VEC_PATH:
        try:
            with get_db_connection() as conn:
                try:
                    conn.execute("ALTER TABLE durable_memories ADD COLUMN embedding BLOB")
                    conn.commit()
                except Exception:
                    pass  # column already exists
            with get_db_connection(vec=True) as conn:
                conn.executescript(_VEC_INDEX_SCHEMA)
            logging.info("VECTOR_RAG: durable_memory_index (vec0) ready.")
        except Exception as e:
            logging.warning(f"VECTOR_RAG: vec0 index init failed ({e}) — numpy fallback.")

# =====================================================================

def is_npc_name_generic(name):
    """Centralized check for generic NPC names to ensure they get unique identities."""
    if not name: return True
    
    # Strip serial IDs (Name|12345)
    clean_name = str(name).split('|')[0].strip()
    
    # Check against hardcoded fallback list (GENERIC_NAMES)
    if clean_name in GENERIC_NAMES:
        return True
        
    # Check against loaded generic_names.json config
    prefixes = GENERIC_CONFIG.get("prefixes", [])
    keywords = GENERIC_CONFIG.get("keywords", [])
    
    # Exact matches for prefixes (case-insensitive)
    lower_clean = clean_name.lower()
    if any(p.lower() == lower_clean for p in prefixes):
        return True
        
    # Keyword substring matches
    if any(k.lower() in lower_clean for k in keywords):
        return True
        
    # Default keywords if config failed to load
    if not keywords:
        default_keywords = [
            "Bandit", "Guard", "Citizen", "Soldier", "Warrior", "Heavy", "Captain", 
            "Sentinel", "Servant", "Wanderer", "Peasant", "Settler", "Thug", "Barman", "Pacifier"
        ]
        if any(k.lower() in lower_clean for k in default_keywords):
            return True
            
    return False

GENERIC_NAMES = [
    "Hungry Bandit", "Dust Bandit", "Starving Vagrant", "Drifter", "Samurai", 
    "Holy Sentinel", "Holy Servant", "Swamper", "Tech Hunter", "Mercenary",
    "Shop Guard", "Caravan Guard", "Slave Hunter", "Slaver", "Manhunter",
    "Escaped Slave", "Rebirth Slave", "United Cities Citizen", "Holy Nation Citizen",
    "Shek Warrior", "Hive Worker", "Hive Soldier", "Hive Prince", "Fogman",
    "Barman", "Pacifier", "Bar Thug",
    "Cannibal", "Outlaw", "Farmer", "Nomad", "Trader", "Gate Guard", 
    "Unknown Entity", "Someone", "Mercenary Heavy", "Mercenary Captain",
    "Holy Nation Outlaw", "Holy Nation Peasant", "United Cities Peasant",
    "Wandering Assassin", "Trader Guard", "Hiver Ronin", "Skeleton Legion",
    "Reaver", "Grass Pirate", "Black Dog", "Crab Raider", "Skeleton Bandit",
    "Bar Thug", "Barman", "Pacifier"
]

KENSHI_NAME_POOL = [
    "Kaelen", "Korg", "Vayn", "Sark", "Mina", "Rook", "Drake", "Silas", "Tane", "Kuna",
    "Zarek", "Jorn", "Lyra", "Kael", "Brena", "Torin", "Sola", "Fen", "Krax", "Vora",
    "Dax", "Nyx", "Garek", "Sora", "Thane", "Kira", "Zane", "Lara", "Marek", "Vina",
    "Rel", "Kaan", "Siv", "Tork", "Meda", "Grox", "Vael", "Syra", "Keld", "Bara",
    "Dorn", "Neld", "Gora", "Sark", "Vane", "Kura", "Zora", "Lena", "Morn", "Vora",
    "Rael", "Kona", "Sima", "Teld", "Mora", "Grak", "Vael", "Sura", "Karn", "Bena",
    "Drak", "Nala", "Gora", "Sina", "Vara", "Kela", "Zana", "Lina", "Mina", "Vorna",
    "Hark", "Skal", "Vorn", "Grek", "Myla", "Rion", "Daka", "Sith", "Tyla", "Korr",
    "Zent", "Lyr", "Brax", "Vort", "Nara", "Grel", "Syk", "Tarn", "Moko", "Vull",
    "Kess", "Tory", "Vann", "Sael", "Miro", "Lorn", "Gryf", "Dael", "Sina", "Kura"
]

def get_used_names():
    if not os.path.exists(CHARACTERS_DIR): return set()
    names = set()
    for f in os.listdir(CHARACTERS_DIR):
        if f.endswith(".json"):
            base = f.replace(".json", "")
            # Handle both formats: Name.json and Name_Faction.json
            if "_" in base:
                name = base.split("_")[0]
                names.add(name.lower())
            else:
                names.add(base.lower())
    return names

# Characters that break downstream parsing if present in a display name:
# '(' '@' ':' break the dedupe/actor regexes, '|' collides with the Name|serial format.
FORBIDDEN_NAME_CHARS = "(@:|)"

def sanitize_lore_name(name):
    """Strip characters that would break event/speaker parsing from a display name."""
    cleaned = "".join(c for c in str(name) if c not in FORBIDDEN_NAME_CHARS).strip()
    return cleaned

def generate_unique_lore_name(gender="Neutral"):
    used = get_used_names()

    gender_key = "Neutral"
    if gender.lower() == "male": gender_key = "Male"
    elif gender.lower() == "female": gender_key = "Female"
    
    # 2. Get pool
    pool = NAMES_CONFIG.get(gender_key, [])
    if not pool and gender_key != "Neutral":
        pool = NAMES_CONFIG.get("Neutral", [])
    
    if not pool:
        pool = KENSHI_NAME_POOL
    
    # 3. Select unique
    available = [n for n in pool if n.lower() not in used]
    if not available:
        base = sanitize_lore_name(random.choice(pool if pool else KENSHI_NAME_POOL))
        for i in range(1, 1000):
            candidate = f"{base} {i}"
            if candidate.lower() not in used:
                return candidate
        return f"{base}_{random.randint(1000, 9999)}"

    # Guard: never hand out a name containing parser-breaking characters,
    # even if the pool data was edited by hand.
    return sanitize_lore_name(random.choice(available))

def get_current_time_prefix():
    if PLAYER_CONTEXT:
        day = PLAYER_CONTEXT.get('day', 0)
        hour = int(PLAYER_CONTEXT.get('hour', 0))
        minute = int(PLAYER_CONTEXT.get('minute', 0))
        return f"[Day {day}, {hour:02d}:{minute:02d}] "
    return ""

def is_future_timestamp(line, cur_d, cur_h, cur_m):
    """Checks if a string containing [Day X, HH:MM] is ahead of the provided current time."""
    match = re.search(r"\[Day (\d+)(?:, (\d+):(\d+))?\]", line)
    if not match: return False
    d = int(match.group(1))
    h = int(match.group(2)) if match.group(2) else 0
    m = int(match.group(3)) if match.group(3) else 0
    if d > cur_d: return True
    if d < cur_d: return False
    if h > cur_h: return True
    if h < cur_h: return False
    return m > cur_m


# Mappings for Kenshi enums

# Mappings for Kenshi enums
SHORT_TERM_MEM = {
    1: "INTRUDER", 2: "AGGRESSOR", 3: "TEMPORARY_ALLY", 4: "TEMPORARY_ENEMY",
    5: "PRISONER", 6: "HAS_BEEN_LOOTED", 7: "CRIMINAL"
}
LONG_TERM_MEM = {
    1: "MY_INTRUDER", 2: "MY_LIFESAVER", 3: "FREED_ME", 4: "STOLE_FROM_ME",
    5: "MY_CAPTOR", 6: "FRIENDLY_AQUAINTANCE", 7: "DEFEATED_MY_SQUAD_ONCE",
    8: "SQUAD_LOST_TO_ME_ONCE", 14: "KILLED_MY_FRIEND", 15: "I_SCREWED_THIS_GUY"
}

def build_detailed_context_string(npc_name, char_data=None):
    # Try to get live context for this specific NPC
    ctx = LIVE_CONTEXTS.get(npc_name)
    
    if not ctx:
        if not char_data:
            return ""
        # If no live context, fallback to persistent char_data
        ctx = char_data
    
    lines = [f"CURRENT CONDITION of {npc_name}:"]

    # --- Character State (imprisoned / enslaved / escaped) ---
    char_state = ctx.get("character_state", "normal")
    is_incapacitated = ctx.get("is_incapacitated", False)
    state_labels = {
        "imprisoned":     f"CRITICAL: {npc_name} is currently IMPRISONED. They are locked up and cannot move freely. They should speak with desperation, resignation, or defiance.",
        "enslaved":       f"CRITICAL: {npc_name} is ENSLAVED and wearing shackles. They are bound to a master. They should speak with fear, exhaustion, or suppressed rage.",
        "escaped-slave":  f"CRITICAL: {npc_name} is an ESCAPED SLAVE — no longer chained but hunted. They should be paranoid, guarded, and desperate.",
        "unconscious":    f"CRITICAL: {npc_name} is UNCONSCIOUS and cannot speak.",
        "dead":           f"CRITICAL: {npc_name} is DEAD.",
    }
    if char_state in state_labels:
        lines.append(state_labels[char_state])

    # Identity
    race = ctx.get("race") or ctx.get("Race", "Unknown")
    gender = ctx.get("gender") or ctx.get("Sex", "Unknown")
    faction = ctx.get("faction") or ctx.get("Faction", "Unknown")
    money = ctx.get("money") or 0
    relation = ctx.get("relation")

    lines.append(f"- RACE: {race}")
    lines.append(f"- SEX: {gender}")
    lines.append(f"- FACTION: {faction}")
    if relation is not None:
        lines.append(f"- FACTION RELATION TO PLAYER: {relation} (Stance: {'ALLIED' if relation >= 50 else 'FRIENDLY' if relation > 0 else 'NEUTRAL' if relation == 0 else 'HOSTILE' if relation <= -30 else 'UNFRIENDLY'})")
    lines.append(f"- MONEY: {money} cats")

    # Group Leader Awareness
    player_faction = PLAYER_CONTEXT.get('faction', 'Nameless')
    if faction == player_faction or ctx.get("factionID") == "Nameless":
        lines.append(f"CRITICAL CONTEXT: {npc_name} is a member of the PLAYER'S FACTION ({player_faction}).")
        lines.append(f"THE PLAYER IS THE LEADER of this group. {npc_name} understand that they and the player are cooperating, this can take many forms such as direct leadership, partnership, or even just individuals traveling together.")
    elif any(f.lower() in faction.lower() for f in MAJOR_FACTIONS):
        lines.append(f"LOYALTY NOTE: {npc_name} belongs to {faction}, a major world power. They are deeply rooted in their society. They will NOT desert their faction to join the player's minor squad without an EXTREMELY compelling narrative reason, high reputation, or having their life saved multiple times. Be highly resistant to recruitment.")
    # Medical
    med = ctx.get("medical", {})
    if med:
        blood = med.get("blood", 100)
        hunger = med.get("hunger", 300)
        limbs = med.get("limbs", {})
        
        status_parts = []
        
        # Hunger Logic
        if hunger < 100: status_parts.append("STARVING")
        elif hunger < 250: status_parts.append("HUNGRY")
        else: status_parts.append("WELL FED") 
        
        # Health Logic
        max_blood = med.get("max_blood", 100)
        blood_pct = blood / max_blood if max_blood > 0 else 1.0
        blood_rate = med.get("blood_rate", 0.0)
        
        if blood_rate > 0.01:
            status_parts.append("BLEEDING")
        elif blood_pct < 0.5:
            status_parts.append("WEAK FROM BLOODLOSS")
        elif blood_pct < 0.85:
            status_parts.append("INJURED")
            
        if med.get("is_unconscious"): status_parts.append("UNCONSCIOUS")
        
        lines.append(f"- CONDITION: {', '.join(status_parts) if status_parts else 'Healthy'}")
        
        # Limb Logic
        injuries = []
        # Filter out _max keys for iteration
        base_limbs = [l for l in limbs.keys() if not l.endswith("_max")]
        for limb in base_limbs:
            hp = limbs.get(limb, 100)
            hp_max = limbs.get(f"{limb}_max", 100)
            hp_pct = hp / hp_max if hp_max > 0 else 1.0
            
            if hp <= -hp_max: 
                injuries.append(f"{limb.upper()} GONE/SEVERED")
            elif hp < 0: 
                injuries.append(f"{limb.upper()} IS CRIPPLED")
            elif hp_pct < 0.5: 
                injuries.append(f"{limb.upper()} IS INJURED")
            
        if injuries: 
            lines.append(f"- INJURIES: {', '.join(injuries)}")
        else:
            lines.append("- INJURIES: None")
    
    # Environment
    env = ctx.get("environment", {})
    if env:
        loc = []
        if env.get("indoors"): loc.append("Indoors")
        if env.get("in_town"): loc.append(f"In town ({env.get('town_name', 'Unknown')})")
        if loc: lines.append(f"- LOCATION: {', '.join(loc)}")

    # Stats & Skills (Visible Power)
    stats = ctx.get("stats", {})
    if stats:
        lines.append(f"VISIBLE POWER of {npc_name}:")
        core = [f"{k[:3].upper()}: {int(float(stats.get(k, 0)))}" for k in ["strength", "dexterity", "toughness", "perception"]]
        lines.append(f"- ATTRIBUTES: {' | '.join(core)}")
        
        notable = []
        combat_skills = ["melee_attack", "melee_defence", "dodge", "katanas", "sabres", "hackers", "heavy_weapons", "blunt", "polearms", "martial_arts", "crossbows", "turrets", "stealth", "athletics"]
        for s in combat_skills:
            val = int(float(stats.get(s, 0)))
            if val > 15: # Only show competent skills
                notable.append(f"{s.replace('_', ' ').capitalize()}: {val}")
        if notable:
            lines.append(f"- NOTABLE SKILLS: {', '.join(notable)}")

    # Memories
    mem = ctx.get("memories", {})
    st = [SHORT_TERM_MEM.get(m, str(m)) for m in mem.get("short_term", [])]
    lt = [LONG_TERM_MEM.get(m, str(m)) for m in mem.get("long_term", [])]
    
    if st or lt:
        lines.append(f"PERCEPTION OF PLAYER:")
        if st: lines.append(f"- SHORT TERM: {', '.join(st)}")
        if lt: lines.append(f"- HISTORY TAGS: {', '.join(lt)}")
        
    # Inventory & Equipment (Categorized)
    inv = ctx.get("inventory", [])
    if inv:
        worn = [i for i in inv if i.get("equipped")]
        held = [i for i in inv if not i.get("equipped")]
        
        if worn:
            lines.append(f"EQUIPMENT WORN by {npc_name}:")
            for item in worn:
                lines.append(f"- {item['name']} (x{item.get('count', 1)}) [{item['slot'].upper()}]")
        
        if held:
            # Phase 1 (filter 4): The player cannot see inside an NPC's pack.
            # Replace the itemized list with a single summary line.
            total_held = sum(int(i.get('count', 1) or 1) for i in held)
            lines.append(f"INVENTORY HELD by {npc_name}: carrying {total_held} assorted item(s) in their pack (contents not visible to others).")
    else:
        lines.append(f"INVENTORY: Empty")

    # Nearby Awareness (Sensory Perception)
    # Phase 1 (filter 2): cap the list to the closest N people and only describe
    # health/gear details within the detail radius (perception approximation).
    nearby = ctx.get("nearby", [])
    if nearby:
        _settings = load_settings()
        max_nearby = int(_settings.get("nearby_max_count", 8))
        detail_radius = float(_settings.get("nearby_detail_radius", 10.0))
        nearby_sorted = sorted(nearby, key=lambda p: float(p.get("dist", 999.0)))
        shown = nearby_sorted[:max_nearby]
        lines.append(f"PEOPLE NEARBY (Visual Awareness):")
        for p in shown:
            dist = float(p.get("dist", 0))
            dist_str = "Immediate proximity" if dist < 2.5 else f"{int(dist)}m away"
            p_name = p.get("name", "Someone")
            p_race = p.get("race", "Unknown")
            p_gender = p.get("gender", "Unknown")
            p_fact = p.get("faction", "Unknown")
            p_fact_display = p_fact
            if p_fact == "Nameless" or p_fact == PLAYER_CONTEXT.get('faction', 'Nameless'):
                p_fact_display = f"Player's Squad: {p_fact}"

            if dist <= detail_radius:
                p_health = p.get("health", "Healthy")
                p_equip = p.get("equipment", "")
                p_desc = f"- {p_name} ({p_gender} {p_race}, {p_fact_display}) | Health: {p_health} | {dist_str}"
                if p_equip:
                    p_desc += f" | Visible Gear: {p_equip}"
            else:
                # Too far to make out condition or gear — identity and distance only
                p_desc = f"- {p_name} ({p_race}, {p_fact_display}) | {dist_str}"
            lines.append(p_desc)
        if len(nearby_sorted) > max_nearby:
            lines.append(f"- ... and {len(nearby_sorted) - max_nearby} more people further away.")

    # Squad Hub (if player talking to self/squad)
    if "your squad" in npc_name.lower():
        squad_list = ctx.get("player", {}).get("squad", [])
        if squad_list:
            lines.append(f"SQUAD MEMBERS PRESENT: {', '.join(squad_list)}")
            lines.append("CONTEXT: You are facilitating a group discussion. The player is talking to the group (or themselves). Respond as a mix of relevant squad members.")
            lines.append("CRITICAL RULES for SQUAD TALK:")
            lines.append("- ONLY use character names from the 'SQUAD MEMBERS PRESENT' list.")
            lines.append("- DO NOT invent new characters or use names not in the list.")
            lines.append("- EACH SPEAKER MUST BE ON A NEW LINE.")
            lines.append("- FORMAT: 'Name: Dialogue'")
            lines.append("- KEEP IT SHORT. One sentence per speaker.")

    return "\n".join(lines)

# Mapping of internal setting keys to INI [Settings] keys
INI_KEY_MAP = {
    "current_model": "CurrentModel",
    "current_campaign": "ActiveCampaign",
    "enable_ambient": "EnableAmbientConversations",
    "radiant_delay": "RadiantDelay",
    "global_events_count": "GlobalEventsCount",
    "synthesis_interval_minutes": "SynthesisIntervalMinutes",
    "favorites": "Favorites",
    "radiant_range": "RadiantRange",
    "talk_radius": "TalkRadius",
    "yell_radius": "YellRadius",
    "min_faction_relation": "MinFactionRelation",
    "max_faction_relation": "MaxFactionRelation",
    "enable_welcome": "EnableWelcomePopup",
    "dialogue_speed_seconds": "DialogueSpeed",
    "bubble_life": "SpeechBubbleLife",
    "language": "Language",
    # Phase 0/1 tunables (new keys are additive — absent INI entries fall back to defaults)
    "short_term_context_count": "ShortTermContextCount",
    "max_prompt_tokens": "MaxPromptTokens",
    "nearby_max_count": "NearbyMaxCount",
    "nearby_detail_radius": "NearbyDetailRadius",
    "yell_compact_profiles": "YellCompactProfiles",
    "event_filter_enabled": "EventFilterEnabled",
    "event_filter_days": "EventFilterDays",
    # 2차 작업 A-1 (chat-event dedupe at prompt-injection time)
    "dedupe_chat_events": "DedupeChatEvents",
    # Phase 2 tunables (mid-term memory digest)
    "digest_enabled": "DigestEnabled",
    "digest_trigger_count": "DigestTriggerCount",
    "digest_keep_recent": "DigestKeepRecent",
    "digest_max_count": "DigestMaxCount",
    "digest_inject_count": "DigestInjectCount",
    "digest_cooldown_seconds": "DigestCooldownSeconds",
    # Phase 1 (archive summary)
    "archive_summary_enabled": "ArchiveSummaryEnabled",
    "archive_digest_threshold": "ArchiveDigestThreshold",
    # Phase 3 tunables (long-term durable memory)
    "durable_memory_enabled": "DurableMemoryEnabled",
    "durable_memory_max_count": "DurableMemoryMaxCount",
    "durable_memory_inject_count": "DurableMemoryInjectCount",
    "durable_memory_inject_tokens": "DurableMemoryInjectTokens",
    "durable_memory_match_threshold": "DurableMemoryMatchThreshold",
    "durable_memory_decay_w3": "DurableMemoryDecayW3",
    "durable_memory_decay_w1": "DurableMemoryDecayW1",
    # Phase 4 tunables (faction RAG)
    "faction_rag_enabled": "FactionRagEnabled",
    "faction_match_threshold": "FactionMatchThreshold",
    "faction_inject_count": "FactionInjectCount",
    "faction_inject_tokens": "FactionInjectTokens",
    "faction_embedding_enabled": "FactionEmbeddingEnabled",
    "faction_semantic_threshold": "FactionSemanticThreshold",
    "faction_embedding_model": "FactionEmbeddingModel",
    # Phase 2 tunables (world_lore chunk RAG)
    "world_lore_rag_enabled": "WorldLoreRagEnabled",
    "world_lore_top_k": "WorldLoreTopK",
    "world_lore_chunk_token_budget": "WorldLoreChunkTokenBudget",
    # Phase 3 (hybrid storage)
    "storage_backend": "StorageBackend",
    "npc_retention_days": "NpcRetentionDays",
    # Phase 4 (vector recall)
    "vector_recall_enabled": "VectorRecallEnabled",
    "vector_recall_threshold": "VectorRecallThreshold",
}

def _save_settings_raw(settings):
    """Save settings to SentientSands_Config.ini."""
    try:
        config = configparser.ConfigParser()
        if os.path.exists(INI_PATH):
            config.read(INI_PATH)
        
        if 'Settings' not in config:
            config['Settings'] = {}
            
        for k, v in settings.items():
            ini_key = INI_KEY_MAP.get(k)
            if ini_key:
                if isinstance(v, list):
                    config['Settings'][ini_key] = ",".join(v)
                elif isinstance(v, bool):
                    config['Settings'][ini_key] = "1" if v else "0"
                else:
                    config['Settings'][ini_key] = str(v)
        
        with open(INI_PATH, "w") as f:
            config.write(f)
        # logging.info(f"Saved settings to INI: {INI_PATH}")
    except Exception as e:
        logging.error(f"Error saving Settings to INI at {INI_PATH}: {e}")

def load_settings():
    defaults = {
        "current_model": "player2-default",
        "current_campaign": "Default",
        "enable_ambient": True,
        "radiant_delay": 240,
        "global_events_count": 5,
        "synthesis_interval_minutes": 15,
        "favorites": [],
        "radiant_range": 100,
        "talk_radius": 100,
        "yell_radius": 200,
        "min_faction_relation": -100,
        "max_faction_relation": 100,
        "enable_welcome": True,
        "dialogue_speed_seconds": 5,
        "bubble_life": 5.0,
        "language": "English",
        # Phase 0/1 tunables
        "short_term_context_count": 20,   # Phase 1: reduced 60→20 to cap token growth over long playthroughs
        "max_prompt_tokens": 6000,        # soft safety net — oldest history lines are trimmed when exceeded.
                                          # Korean mode target: ≤3,000tk (estimate); hard cut stays higher
                                          # so yell mode never starves history to zero (quality guard).
        "nearby_max_count": 8,            # max people listed in PEOPLE NEARBY
        "nearby_detail_radius": 10.0,     # only people within this range get Health/Gear details
        "yell_compact_profiles": True,    # yell mode: 1-line profiles for non-primary listeners
        "event_filter_enabled": True,     # filter raw events by town/relevance/recency
        "event_filter_days": 3,           # drop raw events older than N in-game days
        # 2차 작업 A-1: at prompt-injection time, drop CHAT/BANTER/DIALOGUE events
        # already covered by the short-term dialogue window (same period AND all
        # parties in the current scene). Recording/persistence of EVENT_HISTORY is
        # NOT affected — rumor synthesis and ambient banter still see everything.
        "dedupe_chat_events": True,
        # Phase 2 tunables (mid-term memory digest) — Phase 1: tightened defaults
        "digest_enabled": True,           # master switch for the background digest system
        "digest_trigger_count": 30,       # Phase 1: reduced 60→30 (faster compression over long sessions)
        "digest_keep_recent": 10,         # Phase 1: reduced 20→10 (keep raw tail lean)
        "digest_max_count": 3,            # Phase 1: reduced 5→3 (archive handles older ones)
        "digest_inject_count": 3,         # most recent digests injected into the chat prompt
        "digest_cooldown_seconds": 300,   # per-NPC wall-clock throttle between digest LLM calls
        # Phase 1 (archive summary) — compresses old digests into a single paragraph
        "archive_summary_enabled": True,  # master switch for archive compression
        "archive_digest_threshold": 3,    # digests needed before oldest ones are archived
        # Phase 3 tunables (long-term durable memory — 결정사항 ⑤)
        "durable_memory_enabled": True,        # master switch for RECORD_MEMORY storage + recall injection
        "durable_memory_max_count": 30,        # hard cap per NPC (lowest effective score dropped first; w=5 protected)
        "durable_memory_inject_count": 3,      # max recalled memories injected per prompt
        "durable_memory_inject_tokens": 200,   # soft token budget for the injected memory block
        "durable_memory_match_threshold": 80,  # keyword match score (0-100) required for recall
        "durable_memory_decay_w3": 0.04,       # linear decay per in-game day for w=3 (~75-day lifespan)
        "durable_memory_decay_w1": 0.10,       # linear decay per in-game day for w=1 (~10-day lifespan)
        # Phase 4 tunables (faction RAG — 결정사항 ②③)
        "faction_rag_enabled": True,           # master switch for the [FACTION INTEL] injection
        "faction_match_threshold": 82,         # fuzzy alias score (0-100) needed in the rapidfuzz pass
        "faction_inject_count": 2,             # max MATCHED factions injected (NPC's own faction is extra)
        "faction_inject_tokens": 500,          # soft token budget for the whole FACTION INTEL section
        "faction_embedding_enabled": True,     # 2nd-pass model2vec semantic matching (auto-off if unavailable)
        "faction_semantic_threshold": 0.40,    # cosine similarity needed in the semantic pass
                                               # (calibrated: small-talk noise peaks ~0.34, true
                                               #  paraphrase hits score 0.50+ on potion-multilingual)
        "faction_embedding_model": "potion-multilingual-128M",  # dir under server/models/ (or HF hub id)
        # Phase 2 tunables (world_lore chunk RAG)
        "world_lore_rag_enabled": True,           # master switch; False → legacy full world_lore.txt
        "world_lore_top_k": 2,                    # max non-always_include chunks injected
        "world_lore_chunk_token_budget": 300,     # soft token cap for the whole WORLD LORE section
        # Phase 3 (hybrid storage)
        "storage_backend": "sqlite",   # "sqlite" | "json" — json = legacy full-JSON mode
        "npc_retention_days": 90,      # days before idle NPC conversation history is purged
        # Phase 4 (vector recall)
        "vector_recall_enabled": True,        # use vec0 KNN; False = keyword fallback only
        "vector_recall_threshold": 0.35,      # cosine similarity floor (0-1)
    }
    
    settings = defaults.copy()
    if os.path.exists(INI_PATH):
        try:
            config = configparser.ConfigParser()
            config.read(INI_PATH)
            if 'Settings' in config:
                for k in defaults.keys():
                    ini_key = INI_KEY_MAP.get(k)
                    if ini_key and ini_key in config['Settings']:
                        val = config['Settings'][ini_key]
                        # Type conversion
                        if isinstance(defaults[k], bool):
                            settings[k] = (val == "1" or val.lower() == "true")
                        elif isinstance(defaults[k], int):
                            try: settings[k] = int(val)
                            except: pass
                        elif isinstance(defaults[k], float):
                            try: settings[k] = float(val)
                            except: pass
                        elif isinstance(defaults[k], list):
                            settings[k] = [x.strip() for x in val.split(",") if x.strip()]
                        else:
                            settings[k] = val
        except Exception as e:
            logging.error(f"Error loading settings from INI: {e}")

    return settings

# --- 2차 작업 B-1: output-language enforcement ------------------------------
# The chat template ends with "You MUST write your final response exclusively
# in {language_str}." — with a bare language name this is too weak to reliably
# force non-English output, and it says nothing about the machine-read tag
# syntax. get_language_directive() expands {language_str} into an enforceable
# block for non-English languages while keeping the historical bare "English"
# value (byte-identical prompts) for the default configuration.
# IMPORTANT: action tags / RECORD_MEMORY labels / faction & item names inside
# tags must stay in English — the DLL and the server-side parsers consume them.
def get_language_directive(lang=None):
    """Returns the {language_str} replacement for prompt_chat_template.txt."""
    if lang is None:
        lang = load_settings().get("language", "English")
    lang = str(lang or "English").strip() or "English"
    if lang.lower() == "english":
        return "English"
    return (
        f"{lang}. This is a STRICT, non-negotiable rule:\n"
        f"- Write EVERY line of spoken dialogue in natural, colloquial {lang} that fits Kenshi's harsh world. "
        f"NEVER respond in English and NEVER mix English sentences into the dialogue.\n"
        f"- Proper nouns (people, factions, places, items) may be left in their original form.\n"
        f"- Bracketed system tags are MACHINE-READ: keep every tag keyword and its arguments in their exact English format "
        f"(e.g. [ACTION: ATTACK], [ACTION: GIVE_ITEM:Dried Meat], [ACTION: FACTION_RELATIONS: The Holy Nation: -5]) "
        f"and use official ENGLISH faction and item names inside tags. Only the dialogue OUTSIDE the tags is written in {lang}.\n"
        f"- Inside [RECORD_MEMORY: ...] keep the literal labels 'w=', 'keywords:' and 'text:' in English, "
        f"but write the keyword terms and the memory text itself in {lang} (the language of the conversation) "
        f"so they can be recalled from future {lang} dialogue"  # no trailing period — the template adds one
    )

def aux_language_rule(output_desc, lang=None):
    """One-line LANGUAGE rule appended to auxiliary LLM prompts (memory digest,
    rumor synthesis, profile generation, ambient banter) so background outputs
    follow the configured Language too. Returns '' for English so the default
    configuration keeps the original prompts byte-identical."""
    if lang is None:
        lang = load_settings().get("language", "English")
    lang = str(lang or "English").strip() or "English"
    if lang.lower() == "english":
        return ""
    return (f"\nLANGUAGE REQUIREMENT: Write {output_desc} in natural, fluent {lang} — do NOT use English prose. "
            f"Keep proper nouns and every JSON key / structural label exactly as specified, untranslated.")

def save_settings(new_settings):
    # Flatten multi-level structures if they come in (like radii)
    flat_changes = {}
    for k, v in new_settings.items():
        if k == "radii" and isinstance(v, dict):
            if "radiant" in v: flat_changes["radiant_range"] = v["radiant"]
            if "talk" in v: flat_changes["talk_radius"] = v["talk"]
            if "yell" in v: flat_changes["yell_radius"] = v["yell"]
        else:
            flat_changes[k] = v
            
    settings = load_settings()
    settings.update(flat_changes)
    _save_settings_raw(settings)

# --- INITIALIZATION SEQUENCE ---
load_configs()

def _load_event_history_from_log():
    """Re-populate EVENT_HISTORY from DB (sqlite mode) or log file (legacy)."""
    global EVENT_HISTORY
    # Phase 3: sqlite mode — load from event_history table
    if load_settings().get("storage_backend", "sqlite") == "sqlite":
        try:
            with get_db_connection() as conn:
                rows = conn.execute(
                    "SELECT line FROM event_history ORDER BY id ASC"
                ).fetchall()
                EVENT_HISTORY = [r["line"] for r in rows]
            logging.info(f"DB: Loaded {len(EVENT_HISTORY)} events from event_history table")
            return
        except Exception as e:
            logging.warning(f"DB: event_history load failed ({e}), falling back to log file")

    # Legacy: Re-populate EVENT_HISTORY from the on-disk log
    log_path = os.path.join(get_campaign_dir(), "logs", "global_events.log")
    if not os.path.exists(log_path):
        # Fallback to legacy global log location if campaign one isn't found yet
        log_path = os.path.join(KENSHI_SERVER_DIR, "logs", "global_events.log")
        if not os.path.exists(log_path):
            return
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Lines are prefixed with timestamp, strip it: "[Day] [TYPE] ..."
                bracket = line.find('][') 
                if bracket != -1:
                    line = line[bracket + 1:]  # drop the timestamp prefix
                if line and line not in EVENT_HISTORY:
                    EVENT_HISTORY.append(line)
        logging.info(f"Loaded {len(EVENT_HISTORY)} events from global_events.log")
    except Exception as e:
        logging.error(f"Failed to load event history: {e}")

def init_server_state():
    global ACTIVE_CAMPAIGN, CURRENT_MODEL_KEY
    try:
        settings = load_settings()
        ACTIVE_CAMPAIGN = settings.get("current_campaign", "Default")
        CURRENT_MODEL_KEY = settings.get("current_model", "wizardlm-2")
        logging.info(f"INIT: Active Campaign: {ACTIVE_CAMPAIGN}, Model: {CURRENT_MODEL_KEY}")
        
        # Rewrite the settings to the INI to ensure any missing default keys are populated
        _save_settings_raw(settings)
        
        migrate_to_campaigns()
        load_campaign_config()
        # Load event history AFTER campaign is determined
        _load_event_history_from_log()
    except Exception as e:
        logging.error(f"INIT: Critical state init failure: {e}")

init_server_state()

def load_prompt_component(filename, default_text=""):
    # Try active campaign first
    path = os.path.join(get_campaign_dir(), filename)
    source = f"campaign:{ACTIVE_CAMPAIGN}"
    
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    # Log occasionally or on first load to verify
                    logging.info(f"PROMPT: Loaded {filename} from {source}")
                    return content
        except Exception as e:
            logging.error(f"Error reading {filename} from {source}: {e}")
    
    # Secondary Fallback: Try the templates directory (read-only)
    template_path = os.path.join(TEMPLATES_DIR, filename)
    if os.path.exists(template_path):
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    logging.info(f"PROMPT: Loaded {filename} from templates (read-only)")
                    return content
        except Exception as e:
            logging.error(f"Error reading {filename} from templates: {e}")

    # We no longer fall back to the mod root to ensure campaign isolation.
    return default_text

def format_player_status(player_ctx):
    """Summarizes player vitals and faction into a readable block."""
    if not player_ctx: return "No status data."
    res = "PLAYER STATUS:\n"
    med = player_ctx.get("medical", {})
    if med:
        hunger = med.get("hunger", 300)
        blood = med.get("blood", 100)
        max_blood = med.get("max_blood", 100)
        blood_pct = blood / max_blood if max_blood > 0 else 1.0
        blood_rate = med.get("blood_rate", 0.0)
        status = []
        if hunger < 80: status.append("STARVING")
        elif hunger < 200: status.append("VERY HUNGRY")
        elif hunger < 250: status.append("HUNGRY")
        
        if blood_rate > 0.01: 
            status.append("BLEEDING")
        elif blood_pct < 0.5: 
            status.append("CRITICAL BLOODLOSS")
        elif blood_pct < 0.85: 
            status.append("INJURED")
            
        res += f"- Condition: {', '.join(status) if status else 'Healthy/Fed'}\n"
    res += f"- Money: {player_ctx.get('money', 0)} cats\n"
    res += f"- Faction: {player_ctx.get('faction', 'Nameless')}\n"
    return res

def format_player_inventory(player_ctx, reveal_concealed=False):
    """Categorizes player inventory into Visible vs Concealed for the LLM.

    Phase 1 (filter 6): the itemized CONCEALED list is only injected when the
    player explicitly offers to show their bag (reveal_concealed=True).
    Otherwise a 1-line summary is used — NPCs cannot see inside the pack anyway."""
    if not player_ctx: return "No inventory data."
    inv = player_ctx.get("inventory", [])
    if not inv: return "Inventory: Empty or not visible."

    visible = []
    bag = []
    for item in inv:
        name = item.get("name", "Unknown Item")
        count = item.get("count", 1)
        equipped = item.get("equipped", False)
        slot = item.get("slot", "none")
        display = f"{name} (x{count})"
        if equipped:
            visible.append(f"{display} [{slot.upper()}]")
        else:
            bag.append(display)

    res = "PLAYER EQUIPMENT & INVENTORY:\n"
    res += "VISIBLE (Worn/Held):\n" + ("\n".join([f"- {v}" for v in visible]) if visible else "- Nothing visible.") + "\n"
    res += "CONCEALED (In Bag/Pack):\n"
    if not bag:
        res += "- Bag appears empty."
    elif reveal_concealed:
        res += "\n".join([f"- {b}" for b in bag[:15]])
        if len(bag) > 15:
            res += f"\n- ... and {len(bag)-15} more items."
    else:
        res += f"- The player carries a pack with {len(bag)} item(s). You CANNOT see its contents."
    return res

def filter_relevant_events(events, player_name="", relevant_names=None):
    """Phase 1 (filter 3): approximate relevance filter for raw EVENT_HISTORY lines.

    Events carry no coordinates (only an optional '@ Town' tag), so a true
    line-of-sight check is impossible. Approximation: keep an event only if it
    is recent (<= EventFilterDays in-game days) AND either
      (a) it happened in the player's current town (same heuristic the ambient
          banter system uses), or
      (b) it directly involves the player, their squad, or one of the given
          NPC names (preserves player-relevant context such as
          'player defeated this NPC's guards')."""
    try:
        settings = load_settings()
        max_age_days = int(settings.get("event_filter_days", 3))
        town = ""
        cur_day = None
        if PLAYER_CONTEXT:
            env = PLAYER_CONTEXT.get("environment", {})
            if isinstance(env, dict):
                town = env.get("town_name", "")
            try:
                cur_day = int(PLAYER_CONTEXT.get("day"))
            except (TypeError, ValueError):
                cur_day = None
        names = [n for n in (relevant_names or []) if n]
        if player_name:
            names.append(player_name)

        out = []
        for evt in events:
            # (b) Direct relevance: player / player's squad / current listeners.
            # Checked FIRST and exempt from the recency gate — context directly
            # involving the player (e.g. 'player defeated this NPC's guards')
            # must be preserved per the approved design.
            if "Player's Squad" in evt or any(n in evt for n in names):
                out.append(evt)
                continue
            # (c) Recency gate: drop stale third-party events
            if cur_day is not None:
                m = re.match(r"\[Day (\d+)", evt)
                if m and (cur_day - int(m.group(1))) > max_age_days:
                    continue
            # (a) Location tag: same town as the player right now
            if town and f"@ {town}" in evt:
                out.append(evt)
                continue
            if not town and "@" not in evt:
                # Both event and player are out in the wilds — keep (no better signal)
                out.append(evt)
                continue
            # Otherwise: out-of-sight event (e.g. someone collapsing far away) — drop
        return out
    except Exception as e:
        logging.error(f"Event filter error (falling back to unfiltered): {e}")
        return list(events)

# --- 2차 작업 A-1: EVENT_HISTORY ↔ dialogue-history dedupe (injection-time only) ---
# Event line format (record_event_to_history):
#   "[Day N, HH:MM] [TYPE] Actor (Faction) -> Target (Faction) @ Town: Message"
# (the "[Day ...]" prefix and the "(Faction)" / "@ Town" parts are optional)
_DEDUPE_EVENT_TYPE_RE = re.compile(r"^\s*(?:\[Day \d+(?:, \d+:\d+)?\]\s*)?\[(CHAT|BANTER|DIALOGUE)\]")
_DEDUPE_ACTOR_RE = re.compile(r"\[(?:CHAT|BANTER|DIALOGUE)\]\s*(.*?)\s*(?:\(.*?\))?\s*->")
_DEDUPE_TARGET_RE = re.compile(r"->\s*([^(@:]+)")

def dedupe_window_covered_events(events, player_name="", relevant_names=None, window_start=None):
    """Drops CHAT/BANTER/DIALOGUE events that the short-term dialogue window
    already covers, so the same conversation is not injected twice (once via
    [RECENT DIALOGUE] and once via ## CURRENT SCENE).

    An event is dropped only when ALL of the following hold:
      1. its type is CHAT, DIALOGUE (player<->NPC talk) or BANTER (ambient),
      2. its timestamp falls inside the period covered by the injected
         short-term window (>= window_start; untimestamped events are kept),
      3. its parties belong to the current scene — actor AND target are in
         {player} + listeners (BANTER: actor only, target is always 'Nearby';
         banter lines are appended to every nearby NPC's ConversationHistory).
    Third-party conversations (someone outside the scene) are KEPT: hearing
    about other people's chats is only possible through EVENT_HISTORY.

    Injection-time filter ONLY — EVENT_HISTORY recording/persistence is
    untouched (rumor synthesis and the ambient system consume the full log).
    """
    if window_start is None:
        return list(events)
    try:
        covered = set(n for n in (relevant_names or []) if n)
        if player_name:
            covered.add(player_name)
        out = []
        dropped = 0
        for evt in events:
            m = _DEDUPE_EVENT_TYPE_RE.match(evt)
            if not m:
                out.append(evt)
                continue
            ts = _parse_line_ts(evt)
            if ts is None or ts < window_start:
                out.append(evt)
                continue
            am = _DEDUPE_ACTOR_RE.search(evt)
            actor = am.group(1).strip() if am else ""
            if m.group(1) == "BANTER":
                if actor and actor in covered:
                    dropped += 1
                    continue
                out.append(evt)
                continue
            tm = _DEDUPE_TARGET_RE.search(evt)
            target = tm.group(1).strip() if tm else ""
            if actor in covered and target in covered:
                dropped += 1
                continue
            out.append(evt)
        if dropped:
            logging.info(f"EVENT_DEDUPE: dropped {dropped} chat-type event(s) already covered by the dialogue window")
        return out
    except Exception as e:
        logging.error(f"Event dedupe error (falling back to unfiltered): {e}")
        return list(events)

# =====================================================================
# Phase 4: FACTION RAG (report §2, 결정사항 ②③⑥)
# Hybrid retrieval over config/faction_lore.json (+ faction_lore.d/*.json
# + campaigns/<name>/faction_lore.json overrides):
#   1st pass — rapidfuzz alias/typo matching (pure-python difflib fallback,
#              same dual structure as the durable-memory matcher);
#   2nd pass — model2vec semantic matching (OPTIONAL: numpy + model2vec +
#              local model under server/models/. Loaded on a background
#              thread so chat is never blocked; everything still works
#              with the fuzzy pass alone when embeddings are unavailable).
# Matched lore blocks are injected by build_system_prompt() right after
# ## WORLD LORE as a ## FACTION INTEL section (count/token tunables).
# The primary NPC's own faction is always considered without matching.
# Prompt-assembly only — the DLL protocol is untouched.
# =====================================================================

FACTION_LORE_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "faction_lore.json")
FACTION_LORE_DIR = os.path.join(KENSHI_SERVER_DIR, "config", "faction_lore.d")
FACTION_MODELS_DIR = os.path.join(KENSHI_SERVER_DIR, "models")

# Phase 2: world_lore chunk RAG
WORLD_LORE_CHUNKS_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "world_lore_chunks.json")
WORLD_LORE_DB = []           # normalized chunk list
WORLD_LORE_DB_LOCK = threading.Lock()
WORLD_LORE_EMB = {"matrix": None, "ids": [], "status": "not_started"}

FACTION_DB = []                  # normalized faction entries (list of dicts)
FACTION_DB_LOCK = threading.Lock()
# Embedding state, owned by the background loader thread.
# status: not_started | loading | ready | failed | disabled
FACTION_EMB = {"model": None, "matrix": None, "ids": [], "status": "not_started"}
FACTION_EMB_LOCK = threading.Lock()

def _faction_normalize_entry(raw):
    """Normalizes one faction_lore entry and precomputes its match strings."""
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
        return None
    f = dict(raw)
    f["aliases"] = [str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()]
    f["aliases_ko_unverified"] = [str(a).strip() for a in (raw.get("aliases_ko_unverified") or []) if str(a).strip()]
    f["keywords"] = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()]
    f["summary"] = str(raw.get("summary") or "").strip()
    f["lore"] = str(raw.get("lore") or "").strip()
    f["is_major"] = bool(raw.get("is_major", False))
    # Match strings: name + all aliases (Korean transliterations pending user
    # review are still matched — they only carry a review flag in the JSON).
    seen = set()
    match_strings = []
    for s in [f["name"]] + f["aliases"] + f["aliases_ko_unverified"]:
        key = s.lower()
        if key and key not in seen:
            seen.add(key)
            match_strings.append(s)
    f["_match_strings"] = match_strings
    return f

def _faction_doc_text(f):
    """Text embedded for the semantic pass (summary + keywords + aliases)."""
    parts = [f.get("name", ""), f.get("summary", "")]
    if f.get("keywords"):
        parts.append("Keywords: " + ", ".join(f["keywords"]))
    if f.get("_match_strings"):
        parts.append("Also known as: " + ", ".join(f["_match_strings"]))
    return ". ".join(p for p in parts if p)

def _collect_faction_lore_sources():
    """Yields (source_label, parsed_json) for the base file, drop-in dir and
    the active campaign override, in increasing priority order."""
    sources = [("base", FACTION_LORE_PATH)]
    try:
        if os.path.isdir(FACTION_LORE_DIR):
            for fn in sorted(os.listdir(FACTION_LORE_DIR)):
                if fn.lower().endswith(".json"):
                    sources.append((f"faction_lore.d/{fn}", os.path.join(FACTION_LORE_DIR, fn)))
    except Exception as e:
        logging.error(f"FACTION_RAG: cannot list {FACTION_LORE_DIR}: {e}")
    sources.append((f"campaign:{ACTIVE_CAMPAIGN}", os.path.join(get_campaign_dir(), "faction_lore.json")))
    for label, path in sources:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                yield label, json.load(fh)
        except Exception as e:
            logging.error(f"FACTION_RAG: failed to parse {path}: {e}")

def load_faction_lore():
    """(Re)loads the faction lore DB. Later sources override earlier ones by
    'id', so users can drop custom-mod factions (e.g. UWE) into
    config/faction_lore.d/ or a campaign folder without touching the base file.
    Also feeds is_major factions into the MAJOR_FACTIONS loyalty heuristic."""
    merged = {}
    order = []
    count_per_source = {}
    for label, payload in _collect_faction_lore_sources():
        if isinstance(payload, dict) and "factions" in payload:
            entries = payload.get("factions") or []
        elif isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict) and payload.get("id"):
            entries = [payload]  # single-entry drop-in file
        else:
            entries = []
        n = 0
        for raw in entries:
            f = _faction_normalize_entry(raw)
            if not f:
                continue
            if f["id"] not in merged:
                order.append(f["id"])
            merged[f["id"]] = f
            n += 1
        count_per_source[label] = n
    db = [merged[i] for i in order]
    with FACTION_DB_LOCK:
        FACTION_DB[:] = db
    # Data-driven extension of the hardcoded loyalty heuristic (report 2.5)
    for f in db:
        if f["is_major"] and not any(m.lower() in f["name"].lower() or f["name"].lower() in m.lower()
                                     for m in MAJOR_FACTIONS):
            MAJOR_FACTIONS.append(f["name"])
    logging.info(f"FACTION_RAG: loaded {len(db)} factions from {count_per_source}")
    # Embedding matrix is now stale — rebuild if the model is already loaded
    with FACTION_EMB_LOCK:
        FACTION_EMB["matrix"] = None
        FACTION_EMB["ids"] = []
    if FACTION_EMB["status"] == "ready":
        _rebuild_faction_embeddings()
    return len(db)

def _rebuild_faction_embeddings():
    """Encodes all faction doc texts with the loaded model2vec model.
    Cheap (dozens of docs, <1ms each) — called at load and on /lore/reload."""
    model = FACTION_EMB.get("model")
    if model is None:
        return False
    try:
        import numpy as np
        with FACTION_DB_LOCK:
            db = list(FACTION_DB)
        if not db:
            return False
        docs = [_faction_doc_text(f) for f in db]
        mat = model.encode(docs)
        mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        with FACTION_EMB_LOCK:
            FACTION_EMB["matrix"] = mat
            FACTION_EMB["ids"] = [f["id"] for f in db]
        logging.info(f"FACTION_RAG: embedded {len(db)} faction docs for semantic matching.")
        return True
    except Exception as e:
        logging.error(f"FACTION_RAG: embedding rebuild failed: {e}")
        return False

def load_world_lore_chunks():
    """Phase 2: (Re)loads world_lore_chunks.json into WORLD_LORE_DB.
    If the file is missing, WORLD_LORE_DB stays empty and build_system_prompt()
    falls back to the legacy full world_lore.txt injection."""
    if not os.path.exists(WORLD_LORE_CHUNKS_PATH):
        logging.warning("WORLD_LORE_RAG: world_lore_chunks.json not found — legacy full-text mode active.")
        return 0
    try:
        with open(WORLD_LORE_CHUNKS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        raw_chunks = data.get("chunks") or []
        normalized = []
        for c in raw_chunks:
            if not c.get("id") or not c.get("text"):
                continue
            c = dict(c)
            # embed_text: explicit field > title + first 200 chars of text
            if not c.get("embed_text"):
                c["embed_text"] = (c.get("title", "") + " " + c["text"][:200]).strip()
            normalized.append(c)
        with WORLD_LORE_DB_LOCK:
            WORLD_LORE_DB[:] = normalized
        # Embedding matrix is now stale — reset so _rebuild picks it up
        with threading.Lock():
            WORLD_LORE_EMB["matrix"] = None
            WORLD_LORE_EMB["ids"] = []
            WORLD_LORE_EMB["status"] = "not_started"
        logging.info(f"WORLD_LORE_RAG: loaded {len(normalized)} chunks.")
        return len(normalized)
    except Exception as e:
        logging.error(f"WORLD_LORE_RAG: load failed: {e}")
        return 0

def _rebuild_world_lore_embeddings():
    """Phase 2: Encodes all chunk embed_text strings with the already-loaded
    FACTION_EMB model (shared model2vec instance — zero extra memory).
    Called after _rebuild_faction_embeddings() so the model is guaranteed ready."""
    model = FACTION_EMB.get("model")
    if model is None:
        return False
    try:
        import numpy as np
        with WORLD_LORE_DB_LOCK:
            db = list(WORLD_LORE_DB)
        if not db:
            return False
        docs = [c["embed_text"] for c in db]
        mat = model.encode(docs)
        mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        WORLD_LORE_EMB["matrix"] = mat
        WORLD_LORE_EMB["ids"] = [c["id"] for c in db]
        WORLD_LORE_EMB["status"] = "ready"
        logging.info(f"WORLD_LORE_RAG: embedded {len(db)} chunks.")
        return True
    except Exception as e:
        logging.error(f"WORLD_LORE_RAG: embedding rebuild failed: {e}")
        return False

def retrieve_world_lore_chunks(query_text, settings):
    """Phase 2: Returns the world_lore text to inject for this prompt.
    - always_include chunks are unconditionally added.
    - Remaining slots (up to WorldLoreTopK) are filled by cosine similarity.
    - Returns None when the chunk DB is empty → caller uses legacy full text.
    - Returns '' when no chunks match and no always_include exist (rare)."""
    if not settings.get("world_lore_rag_enabled", True):
        return None  # feature disabled → legacy fallback

    with WORLD_LORE_DB_LOCK:
        db = list(WORLD_LORE_DB)
    if not db:
        return None  # no chunk file → legacy fallback

    top_k = max(1, int(settings.get("world_lore_top_k", 2)))
    token_budget = max(100, int(settings.get("world_lore_chunk_token_budget", 300)))

    always = [c for c in db if c.get("always_include")]
    candidates = [c for c in db if not c.get("always_include")]

    # Semantic ranking (only when embedding matrix is ready)
    ranked = []
    if WORLD_LORE_EMB.get("status") == "ready" and query_text and candidates:
        try:
            import numpy as np
            model = FACTION_EMB.get("model")
            mat = WORLD_LORE_EMB.get("matrix")
            ids = list(WORLD_LORE_EMB.get("ids") or [])
            if model is not None and mat is not None and ids:
                v = model.encode([str(query_text)])
                v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
                sims = (mat @ v.T).ravel()
                id_to_chunk = {c["id"]: c for c in candidates}
                for i, cid in enumerate(ids):
                    if cid in id_to_chunk:
                        ranked.append((float(sims[i]), id_to_chunk[cid]))
                ranked.sort(key=lambda x: x[0], reverse=True)
        except Exception as e:
            logging.error(f"WORLD_LORE_RAG: semantic search failed: {e}")

    # Assemble: always_include first, then top_k ranked within token budget
    selected = list(always)
    used_tokens = sum(estimate_tokens(c["text"]) for c in selected)
    added_ids = {c["id"] for c in selected}
    added_ranked = 0
    for _, chunk in ranked:
        if added_ranked >= top_k:
            break
        if chunk["id"] in added_ids:
            continue
        t = estimate_tokens(chunk["text"])
        if selected and used_tokens + t > token_budget:
            continue
        selected.append(chunk)
        added_ids.add(chunk["id"])
        used_tokens += t
        added_ranked += 1

    if not selected:
        return None  # nothing at all → legacy fallback

    chunk_ids = [c["id"] for c in selected]
    logging.info(f"WORLD_LORE_RAG: injecting {len(selected)} chunks (~{used_tokens}tk): {chunk_ids}")
    return "\n\n".join(c["text"] for c in selected)

def _faction_embedding_worker(model_name):
    """Background loader: imports numpy/model2vec and loads the static
    embedding model (hundreds of MB — 결정사항 ②) without blocking chat.
    Any failure degrades gracefully to fuzzy-only matching."""
    try:
        FACTION_EMB["status"] = "loading"
        import numpy  # noqa: F401 — fail fast if numpy is missing
        from model2vec import StaticModel
        local_dir = os.path.join(FACTION_MODELS_DIR, model_name)
        if os.path.isdir(local_dir):
            os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never hit the network at runtime
            source = local_dir
        else:
            # No local copy — try the hub id as-is (works only with network access)
            source = model_name if "/" in model_name else f"minishlab/{model_name}"
            logging.warning(f"FACTION_RAG: local model dir not found ({local_dir}) — trying hub '{source}'.")
        t0 = time.time()
        model = StaticModel.from_pretrained(source)
        FACTION_EMB["model"] = model
        FACTION_EMB["status"] = "ready"
        logging.info(f"FACTION_RAG: embedding model '{model_name}' loaded in {time.time() - t0:.1f}s.")
        _rebuild_faction_embeddings()
        _rebuild_world_lore_embeddings()  # Phase 2: reuse loaded model
    except Exception as e:
        FACTION_EMB["status"] = "failed"
        FACTION_EMB["model"] = None
        logging.warning(f"FACTION_RAG: embedding model unavailable ({e}) — fuzzy matching only.")

def start_faction_embedding_loader():
    """Spawns the model loader thread once, if enabled. Safe to call anytime."""
    try:
        settings = load_settings()
        if not settings.get("faction_rag_enabled", True) or not settings.get("faction_embedding_enabled", True):
            FACTION_EMB["status"] = "disabled"
            return
        if FACTION_EMB["status"] in ("loading", "ready"):
            return
        model_name = str(settings.get("faction_embedding_model", "potion-multilingual-128M"))
        threading.Thread(target=_faction_embedding_worker, args=(model_name,),
                         daemon=True, name="faction-emb-loader").start()
    except Exception as e:
        logging.error(f"FACTION_RAG: failed to start embedding loader: {e}")

_FACTION_TOKEN_SPLIT_RE = re.compile(r'[\s,.!?;:"\'()\[\]]+')

def _pair_similarity(a, b):
    """Char-level similarity 0-100 (rapidfuzz when available, difflib fallback)."""
    if RAPIDFUZZ_AVAILABLE:
        return float(_rf_fuzz.ratio(a, b))
    return difflib.SequenceMatcher(None, a, b).ratio() * 100.0

def _faction_alias_score(alias_lower, query_lower, query_tokens):
    """Fuzzy score 0-100 for one alias against the query text. Mirrors the
    rapidfuzz <-> pure-python dual structure of _durable_match_score(), plus
    a word-window pass with an exact-anchor bonus so multi-word typos like
    'Holy Nashun' (~73 raw) still clear the threshold while short one-word
    near-misses ('weavers'~'reavers') do not."""
    if alias_lower in query_lower:
        return 100.0
    if RAPIDFUZZ_AVAILABLE:
        best = float(_rf_fuzz.partial_ratio(alias_lower, query_lower))
    else:
        best = 0.0
        for tok in query_tokens:
            if alias_lower in tok or (len(tok) >= 4 and tok in alias_lower):
                return 100.0
            r = _pair_similarity(alias_lower, tok)
            if r > best:
                best = r
    alias_words = alias_lower.split()
    if len(alias_words) >= 2 and query_tokens:
        # Sliding window of the alias's word count over the query words
        n = len(alias_words)
        anchor_words = {w for w in alias_words if len(w) >= 4}
        for i in range(max(1, len(query_tokens) - n + 1)):
            window_tokens = query_tokens[i:i + n]
            s = _pair_similarity(alias_lower, " ".join(window_tokens))
            # Exact anchor word shared with the alias -> strong typo signal
            if anchor_words and any(t in anchor_words for t in window_tokens):
                s += 12.0
            if s > best:
                best = s
    return min(best, 100.0)

def _faction_fuzzy_match(query_lower, threshold):
    """1st pass: best alias score per faction. Returns [(score, faction), ...]
    sorted by score desc, score >= threshold only. Short single-word aliases
    use a stricter floor (max(threshold, 88)) to avoid near-miss collisions."""
    hits = []
    with FACTION_DB_LOCK:
        db = list(FACTION_DB)
    query_tokens = [t for t in _FACTION_TOKEN_SPLIT_RE.split(query_lower) if t]
    for f in db:
        best = 0.0
        for alias in f["_match_strings"]:
            a = alias.lower()
            if len(a) < 3:
                continue
            # Short acronyms (UC, HN): standalone-word presence only
            if len(a) <= 3:
                if a in query_tokens:
                    best = 100.0
                    break
                continue
            s = _faction_alias_score(a, query_lower, query_tokens)
            # Stricter floor for short one-word aliases ('reavers' vs 'weavers')
            if " " not in a and len(a) < 10 and s < max(threshold, 88.0):
                continue
            if s > best:
                best = s
            if best >= 100.0:
                break
        if best >= threshold:
            hits.append((best, f))
    hits.sort(key=lambda h: h[0], reverse=True)
    return hits

def _faction_semantic_match(query_text, threshold, exclude_ids):
    """2nd pass: cosine similarity between the query and faction doc embeddings.
    Only active when the background loader reached 'ready'."""
    if FACTION_EMB["status"] != "ready":
        return []
    with FACTION_EMB_LOCK:
        model = FACTION_EMB.get("model")
        mat = FACTION_EMB.get("matrix")
        ids = list(FACTION_EMB.get("ids") or [])
    if model is None or mat is None or not ids:
        return []
    try:
        import numpy as np
        v = model.encode([str(query_text)])
        v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
        sims = (mat @ v.T).ravel()
        with FACTION_DB_LOCK:
            by_id = {f["id"]: f for f in FACTION_DB}
        hits = []
        for i, fid in enumerate(ids):
            if fid in exclude_ids or fid not in by_id:
                continue
            if float(sims[i]) >= threshold:
                hits.append((float(sims[i]), by_id[fid]))
        hits.sort(key=lambda h: h[0], reverse=True)
        return hits
    except Exception as e:
        logging.error(f"FACTION_RAG: semantic match failed: {e}")
        return []

def resolve_faction_by_name(faction_name):
    """Resolves an NPC's own faction string (from /context / character JSON)
    to a DB entry. Exact name/alias match first, then a strict fuzzy pass."""
    if not faction_name:
        return None
    q = str(faction_name).strip().lower()
    if not q or q in ("unknown", "none", "nameless"):
        return None
    with FACTION_DB_LOCK:
        db = list(FACTION_DB)
    for f in db:
        for alias in f["_match_strings"]:
            if alias.lower() == q:
                return f
    best, best_f = 0.0, None
    q_tokens = [t for t in _FACTION_TOKEN_SPLIT_RE.split(q) if t]
    for f in db:
        for alias in f["_match_strings"]:
            a = alias.lower()
            if len(a) < 3:
                continue
            s = _faction_alias_score(a, q, q_tokens)
            if s > best:
                best, best_f = s, f
    return best_f if best >= 90.0 else None

def _format_faction_intel(f):
    """One injected lore block (~100-150tk)."""
    lines = [f"### {f['name']}" + (f" — {f['summary']}" if f.get("summary") else "")]
    if f.get("lore"):
        lines.append(f["lore"])
    extras = []
    if f.get("leader"):
        extras.append(f"Leader: {f['leader']}")
    if f.get("locations"):
        extras.append("Strongholds: " + ", ".join(str(l) for l in f["locations"][:4]))
    rels = f.get("relations") or {}
    if isinstance(rels, dict) and rels:
        extras.append("Relations: " + "; ".join(f"{k}: {v}" for k, v in list(rels.items())[:4]))
    if extras:
        lines.append(" | ".join(extras))
    return "\n".join(lines)

def build_faction_intel_block(query_text, npc_factions, settings):
    """Selects and formats the faction lore blocks for this prompt.
    Order: the speaking NPC's own faction(s) (no matching needed — report 2.1),
    then fuzzy hits, then semantic hits, bounded by FactionInjectCount and a
    FactionInjectTokens soft budget. Returns '' when nothing applies."""
    if not settings.get("faction_rag_enabled", True):
        return ""
    if not query_text and not npc_factions:
        return ""
    with FACTION_DB_LOCK:
        if not FACTION_DB:
            return ""
    inject_max = max(1, int(settings.get("faction_inject_count", 2)))
    token_budget = max(80, int(settings.get("faction_inject_tokens", 500)))
    fuzzy_threshold = float(settings.get("faction_match_threshold", 82))
    sem_threshold = float(settings.get("faction_semantic_threshold", 0.30))

    selected = []      # (faction, reason)
    selected_ids = set()

    def _add(f, reason):
        if f and f["id"] not in selected_ids:
            selected.append((f, reason))
            selected_ids.add(f["id"])

    # (i) The conversation partner's own faction — always considered (no typo risk)
    for fname in (npc_factions or []):
        _add(resolve_faction_by_name(fname), "own-faction")

    matched_quota = inject_max  # own-faction blocks do not consume the match quota
    if query_text:
        q_lower = str(query_text).lower()
        for score, f in _faction_fuzzy_match(q_lower, fuzzy_threshold):
            if matched_quota <= 0:
                break
            if f["id"] not in selected_ids:
                _add(f, f"fuzzy:{score:.0f}")
                matched_quota -= 1
        if matched_quota > 0:
            for sim, f in _faction_semantic_match(query_text, sem_threshold, selected_ids):
                if matched_quota <= 0:
                    break
                _add(f, f"semantic:{sim:.2f}")
                matched_quota -= 1

    if not selected:
        return ""
    blocks = []
    used_tokens = 0
    reasons = []
    for f, reason in selected:
        block = _format_faction_intel(f)
        t = estimate_tokens(block)
        if blocks and used_tokens + t > token_budget:
            continue
        blocks.append(block)
        used_tokens += t
        reasons.append(f"{f['id']}({reason})")
    if not blocks:
        return ""
    logging.info(f"FACTION_RAG: injecting {len(blocks)} blocks (~{used_tokens}tk): {', '.join(reasons)}")
    return "\n\n".join(blocks)

@app.route('/lore/list', methods=['GET'])
def lore_list():
    """NEW endpoint (additive — not part of the DLL contract). Lists loaded factions."""
    with FACTION_DB_LOCK:
        out = [{"id": f["id"], "name": f["name"], "is_major": f["is_major"],
                "aliases": f["_match_strings"], "source_mod": f.get("source_mod", "")}
               for f in FACTION_DB]
    return jsonify({"count": len(out), "embedding_status": FACTION_EMB["status"], "factions": out})

@app.route('/lore/reload', methods=['GET', 'POST'])
def lore_reload():
    """NEW endpoint (additive). Re-reads faction_lore.json (+ drop-ins) and
    world_lore_chunks.json so users can edit them while the game is running."""
    n_faction = load_faction_lore()
    n_world = load_world_lore_chunks()       # Phase 2
    if FACTION_EMB["status"] == "ready":
        _rebuild_world_lore_embeddings()     # Phase 2: refresh embeddings immediately
    return jsonify({
        "status": "ok",
        "faction_count": n_faction,
        "world_lore_chunks": n_world,
        "embedding_status": FACTION_EMB["status"],
    })

# Initial DB load + background embedding loader (never blocks startup/chat)
try:
    load_faction_lore()
except Exception as e:
    logging.error(f"FACTION_RAG: initial lore load failed: {e}")
try:
    load_world_lore_chunks()  # Phase 2: load chunk definitions at startup
except Exception as e:
    logging.error(f"WORLD_LORE_RAG: initial chunk load failed: {e}")
start_faction_embedding_loader()

def build_system_prompt(player_name="Drifter", relevant_names=None, reveal_concealed=False, section_sink=None,
                        faction_query=None, npc_factions=None, dialogue_window_start=None):
    player_bio = load_prompt_component("character_bio.txt", "A mysterious drifter.")
    player_faction_desc = load_prompt_component("player_faction_description.txt", "")
    npc_base = load_prompt_component("npc_base.txt", "You are an NPC in the world of Kenshi. Stay in character.")
    world_lore_full = load_prompt_component("world_lore.txt", "The world is a brutal, sword-punk wasteland.")
    rules = load_prompt_component("response_rules.txt", "Respond naturally to the player.")
    action_tags = load_prompt_component("prompt_action_tags.txt", "")

    # Combined World Events / Rumors
    settings = load_settings()

    # Phase 2: chunk RAG — try selective injection, fall back to full text
    _chunk_result = retrieve_world_lore_chunks(faction_query, settings)
    world_lore = _chunk_result if _chunk_result is not None else world_lore_full
    ge_count = settings.get("global_events_count", 5)
    events_list = []

    # 1. Load Synthesized Rumors (High-level)
    world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
    if os.path.exists(world_events_path):
        try:
            with open(world_events_path, "r", encoding="utf-8") as f:
                rumors = [l.strip() for l in f.readlines() if l.strip().startswith("- [")]
                # Take most recent rumors
                events_list.extend(rumors[-max(1, ge_count//2):])
        except: pass

    # 2. Load Raw Event History (Recent logs)
    # Phase 1 (filter 3): drop out-of-sight / stale events before injection
    if EVENT_HISTORY:
        if settings.get("event_filter_enabled", True):
            candidate_events = filter_relevant_events(EVENT_HISTORY, player_name, relevant_names)
        else:
            candidate_events = EVENT_HISTORY
        # 2차 작업 A-1: chat-type events already covered by the short-term
        # dialogue window are pure duplicates in ## CURRENT SCENE — drop them
        # at injection time (storage untouched; third-party chat is kept).
        if dialogue_window_start is not None and settings.get("dedupe_chat_events", True):
            candidate_events = dedupe_window_covered_events(
                candidate_events, player_name, relevant_names, dialogue_window_start)
        raw_recent = candidate_events[-max(1, ge_count - len(events_list)):]
        for e in raw_recent:
            events_list.append(f"- {e}")

    events_block = ""
    if events_list:
        events_block = "WORLD STATUS & RUMORS (Hearsay):\n" 
        events_block += "The following are bits of gossip and recent news circulating in the wasteland. Do NOT prioritize these over your core identity or immediate situation. Mention them only if relevant to the conversation.\n"
        events_block += "\n".join(events_list[-ge_count:])

    # Get player faction name (default to Nameless if missing)
    player_faction = PLAYER_CONTEXT.get("faction", "Nameless") if PLAYER_CONTEXT else "Nameless"

    # Only include faction description if it's not empty
    faction_block = ""
    if player_faction_desc.strip():
        faction_block = f"PLAYER FACTION ({player_faction}):\n{player_faction_desc}\n"

    # Location Tag
    location_tag = "The Wasteland"
    if PLAYER_CONTEXT:
        env = PLAYER_CONTEXT.get("environment", {})
        if isinstance(env, dict):
            town = env.get("town_name", "")
            biome = env.get("biome", "")
            if town and biome:
                location_tag = f"{town} (within {biome})"
            elif town:
                location_tag = town
            elif biome:
                location_tag = biome

    # Pre-build dynamic blocks (also used for Phase 0 instrumentation)
    player_status_block = format_player_status(PLAYER_CONTEXT)
    player_inventory_block = format_player_inventory(PLAYER_CONTEXT, reveal_concealed=reveal_concealed)

    player_block = f"""{player_bio}

{faction_block}
### PLAYER AWARENESS & SENSORY RULES
CRITICAL ROLEPLAY RULE: You can SEE the player's VISIBLE equipment, but you CANNOT see what is inside their BAG/PACK.
- Do NOT mention or react to items listed under 'CONCEALED' unless the player explicitly grants you permission in the dialogue (e.g., 'look in my bag', 'take a look at my loot').
- If the player is heavily armed (swords, crossbows WORN), comment on it if appropriate.
- If they are starving or injured, reflect that in your tone.

{player_status_block}
{player_inventory_block}"""

    # Phase 4: FACTION INTEL — dynamic RAG injection right after WORLD LORE
    # (결정사항 ③: majors keep a one-line summary in world_lore.txt; details,
    # minor and custom-mod factions are injected here only when relevant).
    faction_intel_section = ""
    faction_intel_block = ""
    if settings.get("faction_rag_enabled", True):
        try:
            faction_intel_block = build_faction_intel_block(faction_query, npc_factions, settings)
        except Exception as e:
            logging.error(f"FACTION_RAG: intel block build failed: {e}")
        if faction_intel_block:
            faction_intel_section = f"""
## FACTION INTEL
Detailed knowledge relevant to this conversation. Treat it as common knowledge your character grew up hearing:
{faction_intel_block}
"""

    # Phase 1 (3.3): structured Markdown sections — static blocks first (cache-friendly),
    # rules/actions last (instruction recency).
    prompt = f"""## IDENTITY
{npc_base}

## WORLD LORE
{world_lore}
{faction_intel_section}
## CURRENT SCENE
CURRENT LOCATION: {location_tag}

{events_block}

## PLAYER CHARACTER ({player_name})
{player_block}

## RESPONSE FORMAT RULES
{rules}

## ACTION TAGS
{action_tags}
"""

    # Phase 0: per-section token estimates for the caller's instrumentation log
    if section_sink is not None:
        section_sink.update({
            "npc_base": estimate_tokens(npc_base),
            "world_lore": estimate_tokens(world_lore),
            "faction_intel": estimate_tokens(faction_intel_section),
            "events": estimate_tokens(events_block),
            "player": estimate_tokens(player_block),
            "rules": estimate_tokens(rules),
            "action_tags": estimate_tokens(action_tags),
        })

    return prompt.strip()

# Initial build
SYSTEM_PROMPT = build_system_prompt()

# --- WORLD REGISTRY (Save-Based Persistence) ---
WORLD_INDEX = {}
def update_world_index():
    global WORLD_INDEX
    try:
        WORLD_INDEX = build_world_index()
        logging.info(f"World Index Updated: {len(WORLD_INDEX)} names indexed from latest save.")
    except Exception as e:
        logging.error(f"Failed to update world index: {e}")

# Initial scan
update_world_index()

# Requirement: "Character Initialization Attachment"
# Fulfill by ensuring registry files exist for all known characters
def populate_initial_registry():
    registry_dir = os.path.join(get_campaign_dir(), "sentient_sands_registry")
    if not os.path.exists(registry_dir):
        os.makedirs(registry_dir)
    
    for name, platoons in WORLD_INDEX.items():
        clean_name = re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')
        if not clean_name: continue
        reg_file = os.path.join(registry_dir, f"{clean_name}_init.txt")
        if not os.path.exists(reg_file):
             with open(reg_file, "w", encoding="utf-8") as f:
                 f.write(f"Registry: {name} initialized. Location: {platoons[0]}\n")

populate_initial_registry()

# Characters directory is managed by load_campaign_config()
# Do not re-assign here.

def call_llm(messages, max_tokens=2048, temperature=0.8):
    global PLAYER2_SESSION_KEY
    model_entry = MODELS_CONFIG.get(CURRENT_MODEL_KEY)
    if not model_entry:
        logging.error(f"Model Error: {CURRENT_MODEL_KEY} not configured.")
        return None

    provider_name = model_entry.get("provider")
    provider_config = PROVIDERS_CONFIG.get(provider_name)
    if not provider_config:
        logging.error(f"Provider Error: {provider_name} not configured.")
        return None

    api_key = provider_config.get("api_key")
    if provider_name == "player2" and PLAYER2_SESSION_KEY:
        api_key = PLAYER2_SESSION_KEY

    base_url = provider_config.get("base_url").rstrip("/")
    target_url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Sentient Sands Mod",
        "HTTP-Referer": "https://github.com/harvicusdev-glitch/SentientSands"
    }

    # player2 specific header
    if provider_name == "player2":
        headers["player2-game-key"] = "019c93fc-7a93-7ac4-8c6e-df0fd09bec01"

    payload = {
        "model": model_entry["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
    }
    
    last_error = None
    for attempt in range(3):
        try:
            debug_logger.debug(f"LLM REQUEST [{provider_name}] to {target_url} (Payload omitted for security)")
            start_time = time.time()
            response = requests.post(target_url, headers=headers, json=payload, timeout=120)
            elapsed = time.time() - start_time
            
            if response.status_code == 200:
                data = response.json()
                choices = data.get('choices', [])
                if not choices:
                    logging.warning(f"API Success but empty choices: {data}")
                    return None
                    
                msg_obj = choices[0].get('message', {})
                content = msg_obj.get('content')
                
                # Check for alternative fields used by some providers (Thinking/Reasoning/Legacy)
                if content is None:
                    # Try reasoning_content (DeepSeek/Thinking style)
                    content = msg_obj.get('reasoning_content')
                
                if content is None:
                    # Try legacy 'text' field just in case
                    content = choices[0].get('text')

                logging.info(f"API Success in {elapsed:.1f}s (Attempt {attempt+1})")
                
                if content is None:
                    logging.warning(f"API Success but no content found in message. Message body: {msg_obj}")
                    debug_logger.warning(f"EMPTY RESPONSE DETAIL: {data}")
                    # If we got a 200 but no text, return a placeholder instead of None to prevent crashes
                    return "... (Empty Response)"

                debug_logger.debug(f"RAW LLM response received (Length: {len(content) if content else 0})")

                # Robust Reasoning Block Removal
                if "</thought>" in content:
                    content = content.split("</thought>")[-1]
                
                # Strip XML-like thought tags if they remain
                content = re.sub(r'<thought>.*?</thought>', '', content, flags=re.DOTALL | re.IGNORECASE)
                content = re.sub(r'<thought>.*', '', content, flags=re.DOTALL | re.IGNORECASE)

                # Strip internal reasoning prefixes
                if "\n\n" in content and ("thought" in CURRENT_MODEL_KEY.lower() or content.strip().lower().startswith("thought:")):
                    parts = content.split("\n\n")
                    # Only strip if the first part looks like a thought
                    if "thought" in parts[0].lower() or "reasoning" in parts[0].lower():
                        content = "\n\n".join(parts[1:])

                if not content.strip():
                    return "..."
                return sanitize_llm_text(content.strip())
            elif response.status_code == 401 and provider_name == "player2":
                last_error = f"API ERROR 401: Unauthorized - attempting local token refresh"
                logging.warning(f"Player2 token expired/invalid (401). Attempting re-auth...")
                try:
                    auth_url = f"http://localhost:4315/v1/login/web/019c93fc-7a93-7ac4-8c6e-df0fd09bec01"
                    auth_resp = requests.post(auth_url, timeout=5)
                    if auth_resp.status_code == 200:
                        new_key = auth_resp.json().get("p2Key")
                        if new_key:
                            PLAYER2_SESSION_KEY = new_key
                            headers["Authorization"] = f"Bearer {PLAYER2_SESSION_KEY}"
                            logging.info("Successfully refreshed Player2 token locally.")
                except Exception as e:
                    logging.error(f"Failed to refresh Player2 token: {e}")
                
                logging.error(f"Attempt {attempt+1} failed after {elapsed:.1f}s: {last_error}")
                if attempt < 2:
                    time.sleep(1)
            else:
                last_error = f"API ERROR {response.status_code}: {response.text[:200]}"
                logging.error(f"Attempt {attempt+1} failed after {elapsed:.1f}s: {last_error}")
                if attempt < 2:
                    time.sleep(1)

        except Exception as e:
            last_error = str(e)
            logging.error(f"Attempt {attempt+1} Exception: {e}")
            debug_logger.error(f"LLM EXCEPTION STACK (Attempt {attempt+1}):\n{traceback.format_exc()}")
            if attempt < 2:
                time.sleep(1)
    
    return None

# Load Canon Characters
CANON_CHARACTERS_PATH = os.path.join(SCRIPT_DIR, "..", "config", "canon_characters.json")
CANON_CHARACTERS = {}

def load_canon_characters():
    global CANON_CHARACTERS
    if os.path.exists(CANON_CHARACTERS_PATH):
        try:
            # 2차 작업 A-2(1): was platform-default encoding; utf-8-sig also tolerates a BOM
            with open(CANON_CHARACTERS_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                for char in data:
                    CANON_CHARACTERS[char["Name"].lower()] = char
            logging.info(f"Loaded {len(CANON_CHARACTERS)} canon characters.")
        except Exception as e:
            logging.error(f"Failed to load canon_characters.json: {e}")

load_canon_characters()

def generate_character_profile(name, context=""):
    lower_name = name.lower()
    if "your squad" in lower_name or "squad" == lower_name:
        player_faction = PLAYER_CONTEXT.get('faction', 'Nameless')
        return {
            "Personality": "A collective of your loyal companions, each with their own views but united in purpose. They are loyal to you and the squad's goals.",
            "Backstory": f"You have traveled together as members of the {player_faction} through the harsh lands of Kenshi, surviving against all odds.",
            "SpeechQuirks": "Speaks as a representative of the group, sometimes mentioning others in the squad.",
            "Race": "Mixed",
            "Faction": player_faction,
            "Sex": "Mixed"
        }

    if lower_name in CANON_CHARACTERS:
        logging.info(f"Found canon match for {name}")
        return CANON_CHARACTERS[lower_name]

    # Extract race/faction from context or LIVE_CONTEXTS
    live_ctx = LIVE_CONTEXTS.get(name) or {}
    
    race = "Unknown"
    gender = "Unknown"
    faction = "Unknown"
    
    # Try context first
    ctx_data = {}
    if isinstance(context, dict):
        ctx_data = context
    elif isinstance(context, str) and context.strip().startswith('{'):
        try:
            ctx_data = json.loads(context)
        except: pass
        
    if ctx_data:
        race = ctx_data.get('race', race)
        gender = ctx_data.get('gender', gender)
        faction = ctx_data.get('faction', faction)
    
    # Fallback to LIVE_CONTEXTS if still unknown
    if race == "Unknown": race = live_ctx.get('race', 'Unknown')
    if gender == "Unknown": gender = live_ctx.get('gender', 'Unknown')
    if faction == "Unknown": faction = live_ctx.get('faction', 'Unknown')

    # All three fields are required for a meaningful profile.
    # If any are still unknown after checking context + LIVE_CONTEXTS, skip
    # generation entirely — the NPC hasn't been properly registered yet.
    # get_character_data will use a transient placeholder instead.
    missing = [k for k, v in {"race": race, "gender": gender, "faction": faction}.items() if v in ("Unknown", None, "")]
    if missing:
        logging.info(f"Skipping profile for {name}: missing {', '.join(missing)} — will generate on next encounter with full data.")
        return None

    logging.info(f"Generating rich profile for {name} ({gender} {race}, Faction: {faction})...")
    
    template = load_prompt_component("prompt_profile_generation.txt", """You are an expert on Kenshi lore.
Task: Generate a character profile for the NPC named "{name}".
SEX: {gender}
RACE: {race}
FACTION: {faction}
DATA: {context}

CRITICAL RULES:
1. CANON FIRST: If "{name}" is a known Kenshi character (e.g. Beep, Holy Lord Phoenix, Cat-Lon), use exact canon lore.
2. NON-CANON: If generic (e.g. "Dust Bandit", "Shop Guard"), create a grounded profile fitting the setting.
3. PERSONALITY: The character MUST speak and behave according to their sex ({gender}) and race ({race}). 
4. OUTPUT: JSON only with keys: "Personality", "Backstory", "SpeechQuirks".
""")
    prompt = template.format(name=name, gender=gender, race=race, faction=faction, context=context)
    # 2차 작업 B-1: personas drive the dialogue language — generate the free-text
    # fields in the configured language. JSON keys stay English (server-parsed);
    # Race/Faction/Sex are set by the server from context, never by the LLM.
    prompt += aux_language_rule('the "Personality", "Backstory" and "SpeechQuirks" values')

    messages = [{"role": "user", "content": prompt}]
    response_text = call_llm(messages, max_tokens=600, temperature=0.7)
    
    if response_text:
        try:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(0))
                # Add race/faction to result for get_character_data
                result["Race"] = race
                result["Faction"] = faction
                result["Sex"] = gender
                return result
        except Exception as e:
            logging.error(f"Failed to parse generated profile: {e}")
            
    return {
        "Personality": "A weary wanderer.",
        "Backstory": "Trying to survive in the harsh desert.",
        "SpeechQuirks": "None.",
        "Race": race,
        "Faction": faction,
        "Sex": gender
    }

def generate_batch_profiles(npc_list):
    """Lump multiple NPC profile generations into a single LLM call."""
    if not npc_list: return
    
    # Filter out any NPCs that don't have all three required fields.
    # These will be deferred until we have full context from the game.
    complete = []
    for npc in npc_list:
        name = npc.get('name', 'Unknown')
        race = npc.get('race', 'Unknown')
        gender = npc.get('gender', 'Unknown')
        faction = npc.get('faction', 'Unknown')
        missing = [k for k, v in {"race": race, "gender": gender, "faction": faction}.items() if v in ("Unknown", None, "")]
        if missing:
            logging.info(f"BATCH: Skipping {name} \u2014 missing {', '.join(missing)}, will generate on next full context.")
        else:
            complete.append(npc)

    if not complete:
        logging.info("BATCH: No complete NPC data available, deferring all profiles.")
        return
    
    logging.info(f"BATCH: Generating {len(complete)} profiles in one call ({len(npc_list) - len(complete)} deferred)...")
    
    # Prepare descriptions
    descriptions = []
    for npc in complete:
        name = npc.get('name', 'Unknown')
        race = npc.get('race', 'Unknown')
        gender = npc.get('gender', 'Unknown')
        faction = npc.get('faction', 'Unknown')
        descriptions.append(f"- Name: {name}, Sex: {gender}, Race: {race}, Faction: {faction}")
    
    desc_str = "\n".join(descriptions)
    
    template = load_prompt_component("prompt_batch_profile_generation.txt", """You are an expert on Kenshi lore. 
Task: Generate character profiles for several NPCs at once.

NPCS TO GENERATE:
{desc_str}

CRITICAL RULES:
1. CANON FIRST: If a name is a known Kenshi character (e.g. Beep, Holy Lord Phoenix), use exact canon lore.
2. NON-CANON: Generate grounded, cynical, or weary profiles fitting the harsh Kenshi setting.
3. OUTPUT: Return a JSON object where each key is the NPC's Name, and the value is an object with: "Personality", "Backstory", "SpeechQuirks".
""")
    prompt = template.format(desc_str=desc_str)
    # 2차 작업 B-1: same language rule as single profile generation. The top-level
    # JSON keys MUST stay the exact NPC names — they are matched back by name.
    prompt += aux_language_rule('the "Personality", "Backstory" and "SpeechQuirks" values '
                                '(each top-level JSON key MUST remain the exact NPC name as listed above)')

    messages = [{"role": "user", "content": prompt}]
    # We allow more tokens for batch
    response_text = call_llm(messages, max_tokens=1500, temperature=0.7)
    
    if response_text:
        try:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                batch_results = json.loads(json_match.group(0))
                for npc in npc_list:
                    raw_name = npc.get('name', 'Unknown')
                    clean_name = raw_name.split('|')[0] if '|' in raw_name else raw_name
                    gender = npc.get('gender', 'Neutral')
                    
                    # Try to find profile by exact clean name, raw name, or case-insensitive match
                    profile = batch_results.get(clean_name) or batch_results.get(raw_name)
                    
                    if not profile:
                        # Case-insensitive fallback
                        for k, v in batch_results.items():
                            if k.lower() == clean_name.lower() or k.lower() == raw_name.lower():
                                profile = v
                                break
                    
                    if profile:
                        # Determine storage_id: use the name for storage
                        storage_id = clean_name
                        
                        # Clean the ID if it's the Name|ID format
                        if '|' in str(storage_id):
                            storage_id = str(storage_id).split('|')[0]

                        data = {
                            "ID": storage_id,
                            "Name": clean_name,
                            "OriginalName": clean_name,
                            "Race": npc.get('race', 'Unknown'),
                            "Sex": npc.get('gender', 'Unknown'),
                            "Faction": npc.get('faction') or npc.get('Faction') or 'Unknown',
                            "Personality": profile.get("Personality", "A weary traveler."),
                            "Backstory": profile.get("Backstory", "Trying to survive in the harsh desert."),
                            "SpeechQuirks": profile.get("SpeechQuirks", "None."),
                            "ConversationHistory": [],
                            "Relation": 0
                        }
                        save_character_data(storage_id, data)
                        logging.info(f"BATCH: Saved profile for {clean_name} (ID: {storage_id})")
        except Exception as e:
            logging.error(f"BATCH: Failed to parse batch profiles: {e}")

# =====================================================================
# Phase 3: LONG-TERM DURABLE MEMORY (report 1-③, 결정사항 ⑤)
# NPCs record life-defining events via the [RECORD_MEMORY: ...] action
# tag. The tag is consumed SERVER-SIDE ONLY — it must never reach the DLL
# (the unknown-tag fallback in /chat would wrap it as [ACTION: ...]).
# Memories live in the character JSON ("DurableMemories", server-only
# field, DLL-safe), are recalled by keyword match against the player's
# input, and decay lazily by weight: w=5 permanent, w=3/w=1 linear decay
# evaluated at read/write time (no background threads, no locks needed).
# =====================================================================

try:
    from rapidfuzz import fuzz as _rf_fuzz
    RAPIDFUZZ_AVAILABLE = True
    logging.info("DURABLE: rapidfuzz available — fuzzy keyword matching enabled.")
except Exception:
    _rf_fuzz = None
    RAPIDFUZZ_AVAILABLE = False
    logging.info("DURABLE: rapidfuzz not installed — using pure-python fallback matching.")

RECORD_MEMORY_TAG_RE = re.compile(r'^\[\s*RECORD_MEMORY\b', re.IGNORECASE)

def is_record_memory_tag(tag):
    return bool(RECORD_MEMORY_TAG_RE.match(str(tag or "").strip()))

def _durable_decay_rate(w, settings=None):
    """Per-in-game-day linear decay. w=5 memories are permanent (결정사항 ⑤)."""
    if w >= 5:
        return 0.0
    s = settings or {}
    if w >= 3:
        return float(s.get("durable_memory_decay_w3", 0.04))
    return float(s.get("durable_memory_decay_w1", 0.10))

def durable_effective_score(mem, current_day, settings=None):
    """Lazy-evaluated score: score - decay_rate(w) * days_since_last_recall.
    Guards against in-game time reversal (save reload) by skipping decay."""
    try:
        w = int(mem.get("w", 1) or 1)
    except Exception:
        w = 1
    try:
        score = float(mem.get("score", w))
    except Exception:
        score = float(w)
    if w >= 5:
        return score
    try:
        last = int(mem.get("last_recalled_day", mem.get("created_day", 0)) or 0)
    except Exception:
        last = 0
    if current_day < last:
        return score  # time went backwards (save load) — no decay
    return score - _durable_decay_rate(w, settings) * (current_day - last)

def parse_record_memory_tag(raw_tag):
    """Parses '[RECORD_MEMORY: w=5 | keywords: a, b | text: ...]' into a dict.
    Tolerates segment reordering, '=' or ':' separators, and a bare trailing
    segment used as the memory text. Returns {'w','keywords','text'} or None."""
    if not raw_tag:
        return None
    inner = str(raw_tag).strip().strip("[]").strip()
    m = re.match(r'^RECORD_MEMORY\s*:?\s*', inner, re.IGNORECASE)
    if not m:
        return None
    body = inner[m.end():].strip()
    if not body:
        return None
    w = None
    keywords = []
    text = ""
    for part in body.split("|"):
        part = part.strip()
        if not part:
            continue
        kv = re.match(r'^(?:w|weight)\s*[:=]\s*(\d+)\s*$', part, re.IGNORECASE)
        if kv:
            w = int(kv.group(1))
            continue
        kk = re.match(r'^(?:keywords?|kw)\s*[:=]\s*(.+)$', part, re.IGNORECASE)
        if kk:
            keywords = [k.strip() for k in re.split(r'[,;/]', kk.group(1)) if k.strip()]
            continue
        kt = re.match(r'^(?:text|content|memory)\s*[:=]\s*(.+)$', part, re.IGNORECASE)
        if kt:
            text = kt.group(1).strip()
            continue
        if not text:
            text = part  # bare segment — treat as the memory text
    if not text:
        return None
    if w is None:
        w = 1
    # Normalize to the approved weight tiers (결정사항 ⑤: 5 / 3 / 1)
    w = 5 if w >= 5 else (3 if w >= 3 else 1)
    if not keywords:
        # Fallback keywords: longest tokens of the text itself
        words = [t for t in re.split(r'[\s,.!?;:"\'()\[\]]+', text) if len(t) >= 2]
        words.sort(key=len, reverse=True)
        keywords = words[:5]
    return {"w": w, "keywords": keywords[:10], "text": text[:300]}

def sweep_durable_memories(data, settings=None, current_day=None):
    """Write-time sweep (report 1-③ d-3): drops expired memories
    (effective <= 0, w<5 only) and enforces the per-NPC cap by removing the
    lowest effective scores first — w=5 entries are never removed.
    Mutates data in place; returns True if anything changed."""
    mems = data.get("DurableMemories") if data else None
    if not mems or not isinstance(mems, list):
        return False
    if settings is None:
        settings = load_settings()
    if current_day is None:
        try:
            current_day = int(PLAYER_CONTEXT.get("day", 0) or 0)
        except Exception:
            current_day = 0
    changed = False
    kept = []
    for m in mems:
        if not isinstance(m, dict) or not m.get("text"):
            changed = True
            continue
        try:
            w = int(m.get("w", 1) or 1)
        except Exception:
            w = 1
        if w >= 5 or durable_effective_score(m, current_day, settings) > 0:
            kept.append(m)
        else:
            changed = True
            debug_logger.debug(f"DURABLE: expired memory dropped (w={w}, day={current_day}): {m.get('text','')[:80]}")
    cap = max(1, int(settings.get("durable_memory_max_count", 30)))
    if len(kept) > cap:
        mortal = [m for m in kept if int(m.get("w", 1) or 1) < 5]
        overflow = len(kept) - cap
        if overflow > 0 and mortal:
            mortal.sort(key=lambda m: durable_effective_score(m, current_day, settings))
            drop_ids = {id(m) for m in mortal[:overflow]}
            kept = [m for m in kept if id(m) not in drop_ids]
            changed = True
            logging.info(f"DURABLE: cap {cap} exceeded — dropped {len(drop_ids)} lowest-score memories.")
    if changed:
        data["DurableMemories"] = kept
    return changed

def add_durable_memory(data, parsed, settings=None):
    """Appends a parsed RECORD_MEMORY entry to data['DurableMemories'].
    Identical text refreshes the existing entry instead of duplicating."""
    if data is None or not parsed:
        return False
    if settings is None:
        settings = load_settings()
    if not settings.get("durable_memory_enabled", True):
        return False
    if not isinstance(data.get("DurableMemories"), list):
        data["DurableMemories"] = []
    try:
        day = int(PLAYER_CONTEXT.get("day", 0) or 0)
    except Exception:
        day = 0
    new_text_key = parsed["text"].strip().lower()
    for m in data["DurableMemories"]:
        if isinstance(m, dict) and str(m.get("text", "")).strip().lower() == new_text_key:
            # Refresh instead of duplicating (re-consolidation)
            m["w"] = max(int(m.get("w", 1) or 1), parsed["w"])
            m["score"] = float(m["w"])
            m["last_recalled_day"] = day
            merged = list(dict.fromkeys([str(k) for k in (m.get("keywords") or [])] + parsed["keywords"]))
            m["keywords"] = merged[:10]
            sweep_durable_memories(data, settings=settings, current_day=day)
            return True
    entry = {
        "id": f"dm_{int(time.time() * 1000)}_{random.randint(100, 999)}",
        "text": parsed["text"],
        "keywords": parsed["keywords"],
        "w": parsed["w"],
        "score": float(parsed["w"]),
        "created_day": day,
        "last_recalled_day": day,
        "recall_count": 0
    }
    data["DurableMemories"].append(entry)
    sweep_durable_memories(data, settings=settings, current_day=day)

    # Phase 4: store embedding directly in DB (sqlite mode + vec available)
    if (settings.get("storage_backend", "sqlite") == "sqlite"
            and _SQLITE_VEC_PATH and settings.get("vector_recall_enabled", True)):
        try:
            npc_id = data.get("ID") or data.get("Name", "unknown")
            emb = _embed_memory_text(parsed["text"])
            if emb:
                with _DB_WRITE_LOCK:
                    with get_db_connection(vec=True) as conn:
                        conn.execute(
                            "UPDATE durable_memories SET embedding=? WHERE id=?",
                            (emb, entry["id"])
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO durable_memory_index(memory_id,embedding) VALUES(?,?)",
                            (entry["id"], emb)
                        )
                        conn.commit()
        except Exception as e:
            logging.debug(f"VECTOR_RAG: embedding store skipped: {e}")

    return True

def handle_record_memory_tags(memory_tags, npc_name, data, settings=None):
    """Consumes intercepted RECORD_MEMORY tags for one NPC. Returns count stored."""
    stored = 0
    for tag in memory_tags:
        parsed = parse_record_memory_tag(tag)
        if not parsed:
            logging.warning(f"DURABLE: unparseable RECORD_MEMORY tag from {npc_name}: {tag}")
            continue
        if add_durable_memory(data, parsed, settings=settings):
            stored += 1
            logging.info(f"DURABLE: {npc_name} recorded memory (w={parsed['w']}, kw={parsed['keywords'][:4]}): {parsed['text'][:80]}")
    return stored

def _durable_match_score(mem, query_lower):
    """Best keyword-vs-query match score 0-100. Uses rapidfuzz when available
    (typo/word-order tolerant), otherwise a pure-python fallback based on
    lowercase substring containment + difflib token similarity."""
    best = 0.0
    for kw in (mem.get("keywords") or []):
        k = str(kw).strip().lower()
        if len(k) < 2:
            continue
        if k in query_lower:
            return 100.0
        if RAPIDFUZZ_AVAILABLE:
            s = _rf_fuzz.partial_ratio(k, query_lower)
        else:
            s = 0.0
            for tok in re.split(r'[\s,.!?;:"\'()\[\]]+', query_lower):
                if len(tok) < 2:
                    continue
                if k in tok:
                    s = 100.0
                    break
                r = difflib.SequenceMatcher(None, k, tok).ratio() * 100.0
                if r > s:
                    s = r
        if s > best:
            best = s
        if best >= 100.0:
            break
    return best

def recall_durable_memories(data, query_text, settings, current_day):
    """Returns the durable memories relevant to query_text (sorted by match
    score then effective score), capped by count and a soft token budget.
    Selected memories get recall reinforcement (report 1-③ d-2):
    last_recalled_day=today, score=min(w, effective+0.5), recall_count+=1.
    Mutates entries in place — persisted by the caller's normal save path."""
    if not settings.get("durable_memory_enabled", True):
        return []
    mems = data.get("DurableMemories") if data else None
    if not mems or not isinstance(mems, list) or not query_text:
        return []
    threshold = float(settings.get("durable_memory_match_threshold", 80))
    inject_max = max(1, int(settings.get("durable_memory_inject_count", 3)))
    token_budget = max(40, int(settings.get("durable_memory_inject_tokens", 200)))
    query_lower = str(query_text).lower()
    candidates = []
    for m in mems:
        if not isinstance(m, dict) or not m.get("text"):
            continue
        eff = durable_effective_score(m, current_day, settings)
        if eff <= 0 and int(m.get("w", 1) or 1) < 5:
            continue  # expired — left for the write-time sweep
        score = _durable_match_score(m, query_lower)
        if score >= threshold:
            candidates.append((score, eff, m))
    if not candidates:
        return []
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    selected = []
    used_tokens = 0
    for score, eff, m in candidates:
        if len(selected) >= inject_max:
            break
        t = estimate_tokens(m.get("text", ""))
        if selected and used_tokens + t > token_budget:
            break
        selected.append(m)
        used_tokens += t
        # Recall reinforcement — frequently recalled memories effectively persist
        m["last_recalled_day"] = current_day
        m["score"] = min(float(int(m.get("w", 1) or 1)), eff + 0.5)
        m["recall_count"] = int(m.get("recall_count", 0) or 0) + 1
    return selected

def recall_durable_memories_vector(data, query_text, settings, current_day):
    """Phase 4: vector similarity recall for durable memories.
    Uses sqlite-vec KNN when available; falls back to keyword matching."""
    if not settings.get("durable_memory_enabled", True):
        return []
    if not settings.get("vector_recall_enabled", True):
        return recall_durable_memories(data, query_text, settings, current_day)

    npc_id = (data or {}).get("ID") or (data or {}).get("Name", "")
    mems = (data or {}).get("DurableMemories") or []
    if not mems or not query_text:
        return []

    # Try vec0 KNN path
    if (_SQLITE_VEC_PATH and settings.get("storage_backend", "sqlite") == "sqlite"
            and FACTION_EMB.get("status") == "ready"):
        try:
            import numpy as np
            q_emb = _embed_memory_text(query_text)
            if q_emb is None:
                raise ValueError("embed returned None")

            threshold = float(settings.get("vector_recall_threshold", 0.35))
            inject_max = max(1, int(settings.get("durable_memory_inject_count", 3)))
            token_budget = max(40, int(settings.get("durable_memory_inject_tokens", 200)))

            with get_db_connection(vec=True) as conn:
                rows = conn.execute("""
                    SELECT dm.id, dm.text, dm.w, dm.score, dm.created_day,
                           dm.last_recalled_day, dm.recall_count,
                           (1.0 - vec_distance_cosine(dmi.embedding, ?)) AS sim
                    FROM durable_memory_index dmi
                    JOIN durable_memories dm ON dmi.memory_id = dm.id
                    WHERE dm.npc_id = ? AND (1.0 - vec_distance_cosine(dmi.embedding, ?)) >= ?
                    ORDER BY sim DESC
                    LIMIT ?
                """, (q_emb, npc_id, q_emb, threshold, inject_max)).fetchall()

            selected = []
            used_tokens = 0
            mem_by_id = {m.get("id"): m for m in mems if isinstance(m, dict)}
            for row in rows:
                t = estimate_tokens(row["text"])
                if used_tokens + t > token_budget:
                    break
                # Reinforce in-memory entry if present
                m = mem_by_id.get(row["id"])
                if m:
                    m["last_recalled_day"] = current_day
                    m["score"] = min(float(int(m.get("w", 1) or 1)),
                                     durable_effective_score(m, current_day, settings) + 0.5)
                    m["recall_count"] = int(m.get("recall_count", 0) or 0) + 1
                    selected.append(m)
                else:
                    selected.append({"text": row["text"], "w": row["w"]})
                used_tokens += t
            logging.info(f"VECTOR_RAG: recalled {len(selected)} memories for {npc_id} (~{used_tokens}tk)")
            return selected
        except Exception as e:
            logging.warning(f"VECTOR_RAG: vec0 recall failed ({e}) — falling back to keyword matching")

    # Fallback: keyword matching
    return recall_durable_memories(data, query_text, settings, current_day)

def get_character_data(name, context="", char_id=None, skip_generate=False):
    # CRITICAL: If the name contains a pipe (serial ID), split it to get the clean name.
    # This prevents "Name|ID" from creating unique "NameID" junk profiles.
    if '|' in name:
        name_parts = name.split('|')
        name = name_parts[0]
        if not char_id and len(name_parts) > 1:
            char_id = name_parts[1]

    # Fallback to local live context if explicit context is missing
    live_ctx = LIVE_CONTEXTS.get(name) or {}
    
    ctx_data = {}
    if context:
        if isinstance(context, dict):
            ctx_data = context
        elif isinstance(context, str) and context.strip().startswith('{'):
            try:
                ctx_data = json.loads(context)
            except:
                pass
    
    # PERSISTENCE UPGRADE: Force Name-only storage.
    # This ignores any volatile or faction-appended IDs from the context.
    storage_id = name
    
    # Clean the ID if it's the Name|ID format
    if storage_id and '|' in str(storage_id):
        storage_id = str(storage_id).split('|')[0]


    # Sanitize for filesystem
    storage_id_str = str(storage_id)
    safe_filename = "".join([c for c in storage_id_str if c.isalnum() or c in (' ', '_', '-')]).strip()
    path = os.path.join(CHARACTERS_DIR, f"{safe_filename}.json")
    
    # MIGRATION: Logic removed to prevent faction-appended names.
    # We now strictly enforce Name-only filenames.
    
    data = None
    if os.path.exists(path):
        try:
            # 2차 작업 A-2(1): utf-8-sig tolerates a BOM (writes stay BOM-less)
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.loads(f.read())
        except:
            pass
            
    # Schema Migration for legacy files
    if data:
        if "Relation" not in data: data["Relation"] = 0
        if "Race" not in data: data["Race"] = "Unknown"
        if "Sex" not in data: data["Sex"] = "Unknown"
        if "Faction" not in data: data["Faction"] = "Unknown"
        # Phase 3: history fields are always reset here — DB load below overwrites them
        data["ConversationHistory"] = []
        data["Digests"] = []
        data["ArchiveSummary"] = ""
        data["DigestCursorLine"] = ""
        data["DigestCursorTs"] = ""
        data["DurableMemories"] = []

    # Phase 3: load history/memory from DB (sqlite mode only)
    if data and load_settings().get("storage_backend", "sqlite") == "sqlite":
        _sid = data.get("ID", storage_id)
        try:
            with get_db_connection() as conn:
                rows = conn.execute(
                    "SELECT line FROM conversation_history WHERE npc_id=? ORDER BY id ASC",
                    (_sid,)
                ).fetchall()
                data["ConversationHistory"] = [r["line"] for r in rows]

                dg_rows = conn.execute(
                    "SELECT summary,from_ts,to_ts,created_day,line_count FROM digests WHERE npc_id=? ORDER BY id ASC",
                    (_sid,)
                ).fetchall()
                data["Digests"] = [dict(r) for r in dg_rows]

                ar = conn.execute(
                    "SELECT summary FROM archive_summaries WHERE npc_id=?", (_sid,)
                ).fetchone()
                data["ArchiveSummary"] = ar["summary"] if ar else ""

                cur = conn.execute(
                    "SELECT cursor_line,cursor_ts FROM digest_cursors WHERE npc_id=?", (_sid,)
                ).fetchone()
                if cur:
                    data["DigestCursorLine"] = cur["cursor_line"]
                    data["DigestCursorTs"] = cur["cursor_ts"]

                dm_rows = conn.execute(
                    "SELECT * FROM durable_memories WHERE npc_id=?", (_sid,)
                ).fetchall()
                data["DurableMemories"] = [
                    {**dict(r), "keywords": json.loads(r["keywords"] or "[]")}
                    for r in dm_rows
                ]
        except Exception as e:
            logging.error(f"DB: history load failed for {_sid}: {e}")

    # If we have context, try to update race/faction if they are unknown or missing
    ctx_data = {}
    if isinstance(context, dict):
        ctx_data = context
    elif isinstance(context, str) and context.strip().startswith('{'):
        try:
            ctx_data = json.loads(context)
        except:
            pass

    if ctx_data:
        try:
            if data:
                current_race = ctx_data.get("race", "Unknown")
                current_sex = ctx_data.get("gender", "Unknown")
                current_faction = ctx_data.get("faction", "Unknown")
                needs_save = False
                
                if data.get("Race") == "Unknown" and current_race != "Unknown":
                    logging.info(f"Updating Race for {name}: {current_race}")
                    data["Race"] = current_race
                    needs_save = True
                    
                if data.get("Sex") in ("Unknown", None) and current_sex not in ("Unknown", None):
                    logging.info(f"Updating Sex for {name}: {current_sex}")
                    data["Sex"] = current_sex
                    needs_save = True
                    
                if data.get("Faction") == "Unknown" and current_faction != "Unknown":
                    logging.info(f"Updating Faction for {name}: {current_faction}")
                    data["Faction"] = current_faction
                    needs_save = True

                # Force-save immediately when we correct previously-unknown metadata
                # (bypasses should_save_profile which would skip generic-content profiles)
                if needs_save:
                    safe_fn = "".join([c for c in str(storage_id) if c.isalnum() or c in (' ', '_', '-')]).strip()
                    save_character_data(safe_fn, data)
        except Exception as e:
            logging.error(f"Error updating character metadata from context: {e}")

    if not data:
        # If we are only pre-checking for batching or similar, do not generate now
        if skip_generate:
             return {
                "ID": storage_id,
                "Name": name,
                "Race": "Unknown",
                "Sex": "Unknown",
                "Faction": "Unknown",
                "Personality": "A quiet traveler.",
                "Backstory": "Unknown.",
                "SpeechQuirks": "None.",
                "ConversationHistory": [],
                "Relation": 0,
                "_transient": True
            }

        # Generation Lock: Prevent parallel single gens for the same NPC
        with PROGRESS_LOCK:
            if storage_id in PROFILES_IN_PROGRESS:
                return {
                    "ID": storage_id,
                    "Name": name,
                    "Race": "Unknown",
                    "Sex": "Unknown",
                    "Faction": "Unknown",
                    "Personality": "A quiet traveler.",
                    "Backstory": "Unknown.",
                    "SpeechQuirks": "None.",
                    "ConversationHistory": [],
                    "Relation": 0,
                    "_transient": True
                }
            PROFILES_IN_PROGRESS.add(storage_id)

        try:
            # Generate real profile only if we have full context.
            profile = generate_character_profile(name, context)
            if profile is None:
                # Transient placeholder: NOT saved. Next call with full data will generate properly.
                return {
                    "ID": storage_id,
                    "Name": name,
                    "Race": "Unknown",
                    "Sex": "Unknown",
                    "Faction": "Unknown",
                    "Personality": "A quiet traveler who keeps to themselves.",
                    "Backstory": "Their past is unclear.",
                    "SpeechQuirks": "Speaks sparingly.",
                    "ConversationHistory": [],
                    "Relation": 0,
                    "_transient": True
                }
            data = {
                "ID": storage_id,
                "Name": name,
                "Race": profile.get("Race", "Unknown"),
                "Sex": profile.get("Sex", "Unknown"),
                "Faction": profile.get("Faction", "Unknown"),
                "Personality": profile.get("Personality", "Unknown"),
                "Backstory": profile.get("Backstory", "Unknown"),
                "SpeechQuirks": profile.get("SpeechQuirks", ""),
                "ConversationHistory": [],
                "Relation": 0,
                "Digests": [],
                "DurableMemories": []
            }
        finally:
            with PROGRESS_LOCK:
                if storage_id in PROFILES_IN_PROGRESS:
                    PROFILES_IN_PROGRESS.remove(storage_id)
    
    # Enrich with world-index data (Persistence check)
    if name in WORLD_INDEX:
        data["SourcePlatoons"] = WORLD_INDEX[name]

    if should_save_profile(name, storage_id, data):
        save_character_data(storage_id, data)
    return data

def should_save_profile(name, storage_id, data):
    """Checks if we should save this profile, preventing generic clutter."""
    if not name or name in ("Unknown", "Someone"):
        return False
        
    personality = data.get("Personality", "").lower()
    is_generic_content = any(x in personality for x in ("unknown", "generic npc", "weary wanderer", "weary traveler"))
    has_history = len(data.get("ConversationHistory", [])) > 0
    
    # Rule 1: Generic with no history? Don't save.
    if is_generic_content and not has_history:
        return False
        
                
    return True

def save_character_data(storage_id, data):
    safe_filename = "".join([c for c in str(storage_id) if c.isalnum() or c in (' ', '_', '-')]).strip()
    path = os.path.join(CHARACTERS_DIR, f"{safe_filename}.json")

    # Lazy decay sweep before any persistence
    try:
        if data and data.get("DurableMemories"):
            sweep_durable_memories(data)
    except Exception as e:
        logging.error(f"DURABLE: sweep failed for {storage_id}: {e}")

    settings = load_settings()
    use_sqlite = settings.get("storage_backend", "sqlite") == "sqlite"

    if use_sqlite:
        # ── Profile → JSON (history fields excluded) ─────────────────────
        _HISTORY_KEYS = {"ConversationHistory", "Digests", "DigestCursorLine",
                         "DigestCursorTs", "ArchiveSummary", "DurableMemories"}
        profile_only = {k: v for k, v in data.items() if k not in _HISTORY_KEYS}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile_only, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving profile {storage_id}: {e}")

        # ── History → DB ─────────────────────────────────────────────────
        try:
            current_day = int(PLAYER_CONTEXT.get("day", 0) or 0)
            with _DB_WRITE_LOCK:
                with get_db_connection() as conn:
                    # ConversationHistory: insert new lines, trim old ones
                    for line in data.get("ConversationHistory", []):
                        conn.execute(
                            "INSERT OR IGNORE INTO conversation_history (npc_id,line) VALUES (?,?)",
                            (storage_id, line)
                        )
                    conn.execute("""
                        DELETE FROM conversation_history WHERE npc_id=? AND id NOT IN (
                            SELECT id FROM conversation_history WHERE npc_id=?
                            ORDER BY id DESC LIMIT ?
                        )
                    """, (storage_id, storage_id, HISTORY_MAX_LINES))

                    # Digests: full replace
                    conn.execute("DELETE FROM digests WHERE npc_id=?", (storage_id,))
                    for dg in data.get("Digests", []):
                        conn.execute(
                            "INSERT INTO digests (npc_id,summary,from_ts,to_ts,created_day,line_count)"
                            " VALUES (?,?,?,?,?,?)",
                            (storage_id, dg.get("summary",""), dg.get("from_ts",""),
                             dg.get("to_ts",""), dg.get("created_day",0), dg.get("line_count",0))
                        )

                    # ArchiveSummary
                    ar = (data.get("ArchiveSummary") or "").strip()
                    if ar:
                        conn.execute(
                            "INSERT OR REPLACE INTO archive_summaries (npc_id,summary) VALUES (?,?)",
                            (storage_id, ar)
                        )

                    # DigestCursor
                    conn.execute(
                        "INSERT OR REPLACE INTO digest_cursors (npc_id,cursor_line,cursor_ts) VALUES (?,?,?)",
                        (storage_id, data.get("DigestCursorLine",""), data.get("DigestCursorTs",""))
                    )

                    # DurableMemories: upsert
                    for m in data.get("DurableMemories", []):
                        conn.execute("""
                            INSERT OR REPLACE INTO durable_memories
                            (id,npc_id,text,keywords,w,score,created_day,last_recalled_day,recall_count)
                            VALUES (?,?,?,?,?,?,?,?,?)
                        """, (m.get("id", f"dm_{id(m)}"), storage_id,
                              m.get("text",""), json.dumps(m.get("keywords",[])),
                              m.get("w",1), float(m.get("score",1.0)),
                              m.get("created_day",0), m.get("last_recalled_day",0),
                              m.get("recall_count",0)))

                    # npc_last_seen
                    conn.execute(
                        "INSERT OR REPLACE INTO npc_last_seen (npc_id,last_day) VALUES (?,?)",
                        (storage_id, current_day)
                    )
                    conn.commit()
        except Exception as e:
            logging.error(f"DB: history save failed for {storage_id}: {e}")
    else:
        # ── Legacy JSON mode: save everything in one file ─────────────────
        if data and len(data.get("ConversationHistory", [])) > HISTORY_MAX_LINES:
            data["ConversationHistory"] = data["ConversationHistory"][-HISTORY_MAX_LINES:]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving character {storage_id}: {e}")

def purge_old_npc_history(retention_days=None):
    """Phase 3: delete conversation_history rows for NPCs not seen in N days.
    JSON profile files are never touched — only the DB history is pruned."""
    if load_settings().get("storage_backend", "sqlite") != "sqlite":
        return
    if retention_days is None:
        retention_days = int(load_settings().get("npc_retention_days", 90))
    try:
        current_day = int(PLAYER_CONTEXT.get("day", 0) or 0)
        cutoff = current_day - retention_days
        with _DB_WRITE_LOCK:
            with get_db_connection() as conn:
                deleted = conn.execute("""
                    DELETE FROM conversation_history
                    WHERE npc_id IN (
                        SELECT npc_id FROM npc_last_seen WHERE last_day < ?
                    )
                """, (cutoff,)).rowcount
                conn.commit()
        if deleted:
            logging.info(f"DB: purged {deleted} history rows (cutoff day {cutoff})")
    except Exception as e:
        logging.error(f"DB: purge failed: {e}")

# =====================================================================
# Phase 2: MID-TERM MEMORY DIGEST (report section 1-②, 결정사항 ①)
# Older conversation segments are summarized into compact "Digests" by a
# single background worker, then injected into prompts INSTEAD of the raw
# lines they cover (no double-injection). On any failure the cursor does
# not advance, so raw lines keep being injected — safe fallback.
# =====================================================================

def _character_file_path(storage_id):
    safe_filename = "".join([c for c in str(storage_id) if c.isalnum() or c in (' ', '_', '-')]).strip()
    return os.path.join(CHARACTERS_DIR, f"{safe_filename}.json")

def _load_character_file(storage_id):
    """Reads a character JSON straight from disk (no generation). Returns dict or None."""
    path = _character_file_path(storage_id)
    if not os.path.exists(path):
        return None
    try:
        # 2차 작업 A-2(1): utf-8-sig tolerates a BOM (writes stay BOM-less)
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.loads(f.read())
    except Exception as e:
        logging.error(f"DIGEST: failed to read character file {path}: {e}")
        return None

def _parse_line_ts(line):
    """Extracts the '[Day N, HH:MM]' prefix as a sortable (day, hour, minute) tuple, or None."""
    if not line: return None
    m = re.search(r"\[Day (\d+)(?:, (\d+):(\d+))?\]", line)
    if not m: return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))

def get_undigested_lines(data):
    """Returns the ConversationHistory lines NOT yet covered by a digest.

    Primary cursor: exact text of the last digested line (robust against the
    250-line rotation as long as the line is still present). Fallback when the
    cursor line has rotated out: timestamp comparison against DigestCursorTs —
    lines with a timestamp <= cursor are treated as digested (conservative:
    avoids double-injection; untimestamped lines count as undigested).
    """
    history = data.get("ConversationHistory", []) or []
    cursor_line = data.get("DigestCursorLine")
    if not cursor_line:
        return list(history)
    # Search from the end — the cursor is always nearer the tail than the head
    for i in range(len(history) - 1, -1, -1):
        if history[i] == cursor_line:
            return list(history[i + 1:])
    cursor_ts = _parse_line_ts(data.get("DigestCursorTs") or cursor_line)
    if cursor_ts is None:
        return list(history)
    out = []
    for line in history:
        ts = _parse_line_ts(line)
        if ts is None or ts > cursor_ts:
            out.append(line)
    return out

def maybe_queue_digest(storage_id, data, settings=None):
    """Cheap trigger check — called from /log and /chat after history saves.
    Enqueues a digest job when enough un-digested lines have accumulated."""
    try:
        if not storage_id or not data or data.get("_transient"):
            return
        if settings is None:
            settings = load_settings()
        if not settings.get("digest_enabled", True):
            return
        trigger = max(10, int(settings.get("digest_trigger_count", 60)))
        undigested = get_undigested_lines(data)
        if len(undigested) < trigger:
            return
        cooldown = max(0, int(settings.get("digest_cooldown_seconds", 300)))
        now = time.time()
        with DIGEST_LOCK:
            if storage_id in DIGESTS_IN_PROGRESS:
                return
            if now - DIGEST_LAST_RUN.get(storage_id, 0) < cooldown:
                return
            DIGESTS_IN_PROGRESS.add(storage_id)
            DIGEST_LAST_RUN[storage_id] = now
        DIGEST_QUEUE.put(storage_id)
        logging.info(f"DIGEST: queued {storage_id} ({len(undigested)} un-digested lines >= {trigger})")
    except Exception as e:
        logging.error(f"DIGEST: queue check failed for {storage_id}: {e}")

def _embed_memory_text(text):
    """Phase 4: encode text with the loaded FACTION_EMB model2vec model.
    Returns normalised float32 bytes for SQLite BLOB, or None on failure."""
    try:
        model = FACTION_EMB.get("model")
        if model is None:
            return None
        import numpy as np
        v = model.encode([str(text)])
        v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
        return v[0].astype("float32").tobytes()
    except Exception as e:
        logging.debug(f"VECTOR_RAG: embed failed: {e}")
        return None

def maybe_archive_digests(storage_id, data, settings=None):
    """Phase 1: when Digests >= threshold, compress the oldest ones into a single
    ArchiveSummary paragraph via one LLM call and keep only the latest digest.
    Returns True if archiving succeeded, False on LLM failure (digests unchanged)."""
    if settings is None:
        settings = load_settings()
    if not settings.get("archive_summary_enabled", True):
        return False

    digests = data.get("Digests") or []
    threshold = max(2, int(settings.get("archive_digest_threshold", 3)))
    if len(digests) < threshold:
        return False

    # All but the newest digest are candidates for archiving
    to_archive = digests[:-1]
    keep = digests[-1:]

    # Build input text: prepend any existing ArchiveSummary so it is re-compressed
    parts = []
    existing = (data.get("ArchiveSummary") or "").strip()
    if existing:
        parts.append("[이전 아카이브]\n" + existing)
    for dg in to_archive:
        span = ""
        if dg.get("from_ts") or dg.get("to_ts"):
            span = "(%s ~ %s)\n" % (dg.get("from_ts", "?"), dg.get("to_ts", "?"))
        parts.append(span + (dg.get("summary") or "").strip())
    combined = "\n\n".join(p for p in parts if p)

    lang = settings.get("language", "English")
    prompt = (
        "[ARCHIVE COMPRESSION TASK]\n"
        "Compress the following conversation summaries into a single compact paragraph "
        "capturing only the most important relationship facts, promises, and events. "
        "Keep it under 100 words.\n\n"
        + combined
        + aux_language_rule("the compressed archive paragraph", lang)
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Write the compressed archive now."}
    ]
    logging.info("ARCHIVE: compressing %d digests for %s...", len(to_archive), storage_id)
    result = call_llm(messages, max_tokens=200, temperature=0.3)

    if not result or len(result.strip()) < 5:
        logging.warning("ARCHIVE: LLM call failed for %s — keeping original digests.", storage_id)
        return False

    data["ArchiveSummary"] = result.strip()[:600]
    data["Digests"] = keep
    logging.info("ARCHIVE: saved ArchiveSummary for %s (%dtk).", storage_id, estimate_tokens(data["ArchiveSummary"]))
    return True


def process_digest_job(storage_id):
    """Runs inside the single digest worker thread. Summarizes the oldest
    un-digested lines (keeping the newest DigestKeepRecent raw) via one LLM
    call and persists the result. Never raises into the worker loop."""
    settings = load_settings()
    trigger = max(10, int(settings.get("digest_trigger_count", 60)))
    keep_recent = max(0, int(settings.get("digest_keep_recent", 20)))
    if trigger - keep_recent < 10:
        keep_recent = max(0, trigger - 10)  # always digest at least 10 lines

    data = _load_character_file(storage_id)
    if not data:
        logging.warning(f"DIGEST: character file for {storage_id} missing — skip.")
        return
    npc_name = data.get("Name", storage_id)

    undigested = get_undigested_lines(data)
    if len(undigested) < trigger:
        return  # re-check: history may have been rewritten since queueing
    segment = undigested[:len(undigested) - keep_recent]
    if not segment:
        return

    # Clip pathological lines, same policy as prompt injection (200 chars)
    block_lines = [(l[:200] + "...") if len(l) > 200 else l for l in segment]
    history_block = "\n".join(block_lines)

    template = load_prompt_component("prompt_memory_digest.txt", """[MEMORY DIGEST TASK]
The following are {line_count} conversation lines involving {npc_name} in the world of Kenshi.

{history_block}

INSTRUCTIONS:
1. Summarize ONLY what {npc_name} would personally remember, from {npc_name}'s point of view.
2. Output 3 to 5 bullet lines, each starting with "- ". Each bullet is ONE short sentence.
3. Focus on: relationship changes, promises, deals, threats, fights, debts, and notable events with named people.
4. Preserve proper nouns exactly as written. Do NOT invent anything not present in the lines above.
5. Output ONLY the bullet lines. No headers, no commentary.""")
    try:
        prompt = template.format(npc_name=npc_name, line_count=len(segment), history_block=history_block)
    except Exception as e:
        logging.error(f"DIGEST: template format error ({e}) — check prompt_memory_digest.txt placeholders.")
        return
    # 2차 작업 B-1: digests are re-injected into chat prompts — keep them in the
    # configured output language (no-op for English).
    prompt += aux_language_rule("every bullet line", settings.get("language", "English"))

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Write the memory digest for {npc_name} now."}
    ]
    logging.info(f"DIGEST: synthesizing {len(segment)} lines for {npc_name} (~{estimate_tokens(history_block)}tk input)...")
    summary = call_llm(messages, max_tokens=300, temperature=0.3)

    if not summary or len(summary.strip()) < 10:
        # FALLBACK: cursor does not advance — the raw lines stay injected as-is.
        logging.warning(f"DIGEST: LLM call failed/empty for {npc_name} — keeping raw history (no cursor advance).")
        return

    # Light cleanup: drop code fences / blank lines, hard cap length
    summary_lines = [l.strip() for l in summary.strip().splitlines()
                     if l.strip() and not l.strip().startswith("```")]
    summary_text = "\n".join(summary_lines)[:1200]

    last_line = segment[-1]
    ts_match = re.search(r"\[Day \d+(?:, \d+:\d+)?\]", last_line)
    first_ts_match = re.search(r"\[Day \d+(?:, \d+:\d+)?\]", segment[0])
    entry = {
        "summary": summary_text,
        "from_ts": first_ts_match.group(0) if first_ts_match else "",
        "to_ts": ts_match.group(0) if ts_match else "",
        "created_day": PLAYER_CONTEXT.get("day", 0),
        "line_count": len(segment)
    }

    # Re-read the file fresh before writing — /chat or /log may have appended
    # newer history while the LLM call was in flight. We only own the Digest
    # fields, so apply them onto the freshest copy.
    fresh = _load_character_file(storage_id) or data
    if "Digests" not in fresh or not isinstance(fresh.get("Digests"), list):
        fresh["Digests"] = []
    fresh["Digests"].append(entry)
    max_keep = max(1, int(settings.get("digest_max_count", 3)))
    fresh["Digests"] = fresh["Digests"][-max_keep:]
    fresh["DigestCursorLine"] = last_line
    fresh["DigestCursorTs"] = ts_match.group(0) if ts_match else ""
    # Phase 1: archive old digests before saving if threshold exceeded
    maybe_archive_digests(storage_id, fresh, settings)
    save_character_data(storage_id, fresh)
    logging.info(f"DIGEST: saved digest for {npc_name} ({entry['from_ts']} ~ {entry['to_ts']}, "
                 f"{len(segment)} lines -> ~{estimate_tokens(summary_text)}tk).")
    debug_logger.debug(f"DIGEST_RESULT [{npc_name}]:\n{summary_text}")

def digest_worker():
    """Single daemon worker — serializes all digest LLM calls (no stampede)."""
    logging.info("DIGEST: background worker started.")
    while True:
        storage_id = DIGEST_QUEUE.get()
        try:
            process_digest_job(storage_id)
        except Exception as e:
            logging.error(f"DIGEST: worker error for {storage_id}: {e}")
        finally:
            with DIGEST_LOCK:
                DIGESTS_IN_PROGRESS.discard(storage_id)
            DIGEST_QUEUE.task_done()
            time.sleep(1)  # gentle spacing between background calls

threading.Thread(target=digest_worker, daemon=True).start()

def extract_id_from_context(context_json):
    if not context_json: return None
    try:
        # If it's a string, parse it
        if isinstance(context_json, str) and context_json.strip().startswith('{'):
            context_json = json.loads(context_json)
        if isinstance(context_json, dict):
            # PRIORITIZE 'storage_id' (stable) over 'id' (volatile)
            return context_json.get('storage_id') or context_json.get('id')
    except:
        pass
    return None


@app.route('/log', methods=['POST'])
def log_dialogue():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
        
    npc_name = data.get('npc', 'Someone')
    player_name = data.get('player', 'Drifter')
    player_message = data.get('message', '')
    npc_response = data.get('response', '')
    context = data.get('context', '')
    npc_id = extract_id_from_context(context)

    char_data = get_character_data(npc_name, context, char_id=npc_id)
    
    # CRITICAL FIX: Use the stable ID from char_data, NOT the volatile serial ID
    storage_id = char_data.get("ID") or npc_name
    
    time_prefix = get_current_time_prefix()
    
    if player_message:
        char_data["ConversationHistory"].append(f"{time_prefix}{player_name}: {player_message}")
        record_event_to_history("DIALOGUE", player_name, npc_name, player_message)

    if npc_response:
        char_data["ConversationHistory"].append(f"{time_prefix}{npc_name}: {npc_response}")
        record_event_to_history("DIALOGUE", npc_name, player_name, npc_response)
        
    # Limit history to HISTORY_MAX_LINES to prevent massive file sizes and UI lag
    if len(char_data["ConversationHistory"]) > HISTORY_MAX_LINES:
        char_data["ConversationHistory"] = char_data["ConversationHistory"][-HISTORY_MAX_LINES:]
    
    if should_save_profile(npc_name, storage_id, char_data):
        save_character_data(storage_id, char_data)
        # Phase 2: player actually talked to this NPC — check digest trigger
        maybe_queue_digest(storage_id, char_data)
    logging.info(f"LOG [{npc_name} ({storage_id})]: {npc_response}")
    return jsonify({"status": "ok"})

@app.route('/get_unique_identity', methods=['POST'])
def get_unique_identity():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    
    current_name = data.get('name', 'Someone')
    race = data.get('race', 'Human')
    gender = data.get('gender', 'Neutral')
    
    # Check if this name is generic
    is_generic = is_npc_name_generic(current_name)
    
    if is_generic:
        new_name = generate_unique_lore_name(gender=gender)
        logging.info(f"IDENTITY: Assigning unique {gender} name '{new_name}' to generic NPC '{current_name}'")
        return jsonify({
            "status": "rename",
            "new_name": new_name
        })
    
    return jsonify({"status": "ok", "name": current_name})

@app.route('/get_batch_identities', methods=['POST'])
def get_batch_identities():
    batch = request.json # Expect list of {serial, name, gender, race}
    if not batch or not isinstance(batch, list):
        return jsonify({"status": "error", "message": "Invalid batch format"}), 400
    
    results = []
    rename_count = 0
    for item in batch:
        serial = item.get('serial')
        current_name = str(item.get('name', 'Someone')).strip()
        gender = item.get('gender', 'Neutral')
        
        is_generic = is_npc_name_generic(current_name)
        
        if is_generic:
            new_name = generate_unique_lore_name(gender=gender)
            results.append({
                "serial": serial,
                "status": "rename",
                "new_name": new_name
            })
            logging.info(f"IDENTITY-BATCH: Assigning unique name '{new_name}' to generic NPC '{current_name}' (serial {serial})")
            rename_count += 1
        else:
            results.append({
                "serial": serial,
                "status": "ok"
            })
            
    if results:
        logging.info(f"IDENTITY: Batch processed {len(results)} items. Renamed: {rename_count}")
    return jsonify(results)


@app.route('/rename', methods=['POST'])
def rename_character():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    context = data.get('context', '')
    
    if not old_name or not new_name:
        return jsonify({"status": "error", "message": "Missing names"}), 400
        
    logging.info(f"RENAME: Attempting to rename '{old_name}' to '{new_name}'")
    
    # 1. Resolve existing profile (do not generate if missing)
    char_data = get_character_data(old_name, context, skip_generate=True)
    if char_data.get("_transient"):
        logging.info(f"RENAME: No persistent profile for {old_name}, renaming aborted (will create new on next chat)")
        return jsonify({"status": "ok", "message": "No profile to rename"})

    old_id = char_data.get("ID")
    if not old_id:
        return jsonify({"status": "error", "message": "Profile ID resolution failed"}), 500

    # 2. Update internal Name
    char_data["Name"] = new_name
    
    # 3. Handle File Renaming
    # Transition to name-only identities for all renamed characters
    old_safe = "".join([c for c in old_name if c.isalnum() or c in (' ', '_', '-')]).strip()
    if str(old_id).startswith(old_safe) or "_" in str(old_id):
        new_id = new_name
        
        # Sanitize for migration
        new_safe = "".join([c for c in str(new_id) if c.isalnum() or c in (' ', '_', '-')]).strip()
        
        old_path = os.path.join(CHARACTERS_DIR, f"{old_id}.json")
        new_path = os.path.join(CHARACTERS_DIR, f"{new_safe}.json")
        
        if os.path.exists(old_path) and not os.path.exists(new_path):
            try:
                char_data["ID"] = new_id
                with open(new_path, "w", encoding="utf-8") as f:
                    json.dump(char_data, f, indent=2)
                os.remove(old_path)
                logging.info(f"RENAME: Migrated profile file {old_id} -> {new_safe}")
                return jsonify({"status": "ok", "new_id": new_id})
            except Exception as e:
                logging.error(f"RENAME: Failed to migrate profile file: {e}")
                return jsonify({"status": "error", "message": str(e)}), 500

    # Fallback: Just update internal data
    save_character_data(old_id, char_data)
    return jsonify({"status": "ok"})

@app.route('/ambient', methods=['POST'])
def ambient_event():
    debug_logger.debug("ROUTE: /ambient [POST]")
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    
    npcs_data = data.get('npcs', [])
    player_name = data.get('player', 'Drifter')
    
    logging.info(f"RADIANT: Received ambient banter request ({len(npcs_data)} NPCs nearby)")
    
    if not npcs_data:
        return jsonify({"status": "ignore"})

    # Build profiles for nearby characters
    char_profiles = ""
    name_to_id = {}
    
    # 1. Pre-check for missing profiles to batch generate
    missing_npcs = []
    npc_limit = npcs_data[:12] # Increase limit to 12 for better town square coverage
    for npc in npc_limit:
        if isinstance(npc, dict):
            name = npc.get('name', 'Unknown')
            if name.lower() in CANON_CHARACTERS or "your squad" in name.lower():
                continue
            
            # Pre-check for missing profiles to batch generate (skip individual generation)
            info = get_character_data(name, context=json.dumps(npc), skip_generate=True)
            if info.get("_transient"):
                missing_npcs.append(npc)
                
    if missing_npcs:
        generate_batch_profiles(missing_npcs)

    # 2. Extract and format profile summary for banter call
    recent_dialogue = []
    for npc in npc_limit:
        if isinstance(npc, dict):
            name = npc.get('name', 'Unknown')
            nid = npc.get('id', 0)
            name_to_id[name] = nid
            # Use stable name-based retrieval for ambient profiles
            d = get_character_data(name, context=json.dumps(npc))
            
            # Collect recent dialogue to prevent repetition
            if d.get("ConversationHistory"):
                recent_dialogue.extend(d["ConversationHistory"][-15:])

            # Include ID and sensory details for deterministic referencing
            health = npc.get('health', 'Healthy')
            gear = npc.get('equipment', 'nothing notable')
            char_profiles += f"\n- {name}|{nid} ({npc.get('gender')} {npc.get('race')}, {npc.get('faction')}) | Health: {health} | Gear: {gear} | Personality: {d.get('Personality', 'A traveler.')}"
        else:
            name_to_id[npc] = 0
            d = get_character_data(npc, "")
            
            if d.get("ConversationHistory"):
                recent_dialogue.extend(d["ConversationHistory"][-15:])
                
            char_profiles += f"\n- {npc} (A traveler): {d.get('Personality', 'A traveler.')}"

    # Deduplicate and sort history (preserving order)
    # 1. Pull from individual NPC memories
    all_history = list(recent_dialogue)
    
    # 2. Extract global banter/chat history from EVENT_HISTORY for the current location
    location = ""
    if PLAYER_CONTEXT:
        env = PLAYER_CONTEXT.get("environment", {})
        location = env.get("town_name", "") if isinstance(env, dict) else ""

    for evt in reversed(EVENT_HISTORY):
        # Format: "[BANTER] Name (Faction) -> Nearby @ Location: Message"
        if (" [BANTER] " in evt or " [CHAT] " in evt):
            # Only include if it's in the same location (or location is unknown)
            if not location or f"@ {location}" in evt or "@" not in evt:
                if ": " in evt:
                    msg_part = evt.split(": ", 1)[1]
                    # Extract speaker
                    match = re.search(r'\]\s*(.*?)\s*(?:\(.*?\))?\s*->', evt)
                    if match:
                        speaker = match.group(1).strip()
                        all_history.append(f"{speaker}: {msg_part}")
                    else:
                        all_history.append(msg_part)
        if len(all_history) > 100: break

    unique_history = []
    seen_history = set()
    # Work backwards to get the most recent unique lines
    for line in reversed(all_history):
        if line not in seen_history:
            unique_history.append(line)
            seen_history.add(line)
    
    unique_history = list(reversed(unique_history))[-40:] # Take last 40 unique lines
    
    history_block = ""
    if unique_history:
        history_block = "\nRECENT LOCAL DIALOGUE (DO NOT REPEAT TOPICS OR JOKES FROM HERE):\n" + "\n".join(unique_history)

    dynamic_system_prompt = build_system_prompt(player_name)

    # 2차 작업 B-1: banter bubbles are shown in-game — follow the configured
    # output language (empty string for English keeps the prompt unchanged).
    lang_rule = aux_language_rule("every banter dialogue line (the text after the 'Name|ID: ' prefix — "
                                  "keep the Name|ID prefix itself exactly as listed)")

    ambient_system_prompt = f"""{dynamic_system_prompt}

[RADIANT DIALOGUE SYSTEM - BANTER MODE]
You are generating a short, atmospheric back-and-forth conversation (banter) between NPCs in Kenshi.
Kenshi is a post-apocalyptic, harsh world. NPCs should sound cynical, weary, or suspicious.

NEARBY CHARACTERS:
{char_profiles}

{history_block}

INSTRUCTIONS:
1. Select 2 or 3 characters from the list to have a short conversation.
2. Each participant MUST speak AT LEAST TWICE (total 4-6 lines).
3. DO NOT include the Player as a speaker and DO NOT let the Player participate.
4. The topic should be grounded in the harsh reality of Kenshi: local rumors, faction politics, the weather, gear maintenance, hunger, or a passing, often cynical comment about the 'drifter' (player) nearby.
5. Format MUST be 'Name|ID: Message' (e.g., 'Lungrot|1234: Wheeze...').
6. Only use characters from the NEARBY list.
7. Use the EXACT Name and ID strings provided in the list for the 'Name|ID' portion.
8. DO NOT use [ACTION] tags or any bracketed text. Radiant mode is for atmospheric dialogue only.
9. CRITICAL: Do NOT repeat topics, lines, or jokes found in the RECENT LOCAL DIALOGUE section. Talk about something new.
10. WORLD-CENTRIC: Remember that in Kenshi, the player is NOT the center of the universe. NPCs have their own lives, problems, and social circles. They should speak to and about each other about what is going on around them more often than they speak about the player.{lang_rule}
"""
    
    messages = [
        {"role": "system", "content": ambient_system_prompt},
        {"role": "user", "content": "The world is quiet. Generate a radiant interaction."}
    ]
    
    content = call_llm(messages)
    if content:
        # Strip any stray [ACTION] tags that the LLM might hallucinated despite instructions
        content = re.sub(r'\[\s*[A-Z_]+(?::\s*[^\]]+)?\s*\]', '', content).strip()
        
        # Basic cleaning - remove quotes
        content = content.replace('"', '').strip()
        
        # Post-process to ensure IDs are present
        lines = []
        for line in content.split('\n'):
            line = line.strip()
            if not line: continue
            
            if ':' in line:
                header, msg = line.split(':', 1)
                name_part = header.split('|')[0].strip()
                
                # Hallucination check
                if name_part.lower() == player_name.lower():
                    continue

                # Ensure ID is present even if LLM forgot
                if '|' not in header:
                    if name_part in name_to_id:
                        header = f"{name_part}|{name_to_id[name_part]}"
                
                lines.append(f"{header.strip()}: {msg.strip()}")
            elif '|' in line and len(line) < 100: # Maybe just a name header LLM hallucinated
                continue
            else:
                # Append raw text if no colon, though prompt asks for colon
                if len(line) > 5: lines.append(line)
        
        final_text = "\n".join(lines)
        
        # 5. Optimized History Update (One save per NPC)
        # Pre-load character memories for the nearby group (only those in npc_limit)
        memories = {}
        for npc_obj in npc_limit:
            name = npc_obj.get('name') if isinstance(npc_obj, dict) else npc_obj
            # Use skip_generate=True here just in case, though they should be generated by now
            memories[name] = get_character_data(name, context=json.dumps(npc_obj) if isinstance(npc_obj, dict) else "", skip_generate=True)

        # Append all new lines to the relevant memories
        for line in lines:
            if ':' in line:
                header, msg = line.split(':', 1)
                speaker_name = header.split('|')[0].strip()
                time_prefix = get_current_time_prefix()
                processed_msg = f"{time_prefix}{speaker_name}: {msg.strip()}"
                
                for name, d in memories.items():
                    d["ConversationHistory"].append(processed_msg)
                    # Trimming removed (was 50 line cap)
                
                # Also log to global history for narrative synthesis
                speaker_faction = memories.get(speaker_name, {}).get("Faction", "None")
                record_event_to_history("BANTER", speaker_name, "Nearby", msg.strip(), actor_faction=speaker_faction)

        # Batch save everything
        for name, d in memories.items():
            storage_id = d.get("ID", name)
            save_character_data(storage_id, d)

        logging.info(f"AMBIENT BARK:\n{final_text}")
        return jsonify({"status": "ok", "text": final_text})
    
    return jsonify({"status": "none"})

@app.route('/ping', methods=['GET', 'POST'])
def ping():
    return jsonify({"status": "ok"})

@app.route('/test_llm', methods=['GET', 'POST'])
def test_llm():
    """Verify both server and LLM connectivity."""
    try:
        messages = [{"role": "user", "content": "Keep your response extremely short. Reply with the word: Success"}]
        response = call_llm(messages, max_tokens=10, temperature=0.7)
        if response:
            logging.info(f"TEST_LLM: Success! Response: {response}")
            # Ensure fixed key order and no extra spaces for C++ parsing
            return '{"status":"ok","llm":"ok","response":"' + response.replace('"', "'") + '"}'
        else:
            logging.error("TEST_LLM: call_llm returned None.")
            return '{"status":"ok","llm":"error","message":"Global LLM call failed."}'
    except Exception as e:
        logging.error(f"TEST_LLM: Exception during test: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route('/chat', methods=['POST'])
def chat():
    global CURRENT_MODEL_KEY
    data = request.json
    debug_logger.debug(f"ROUTE: /chat [POST] (Request details omitted for security)")
    if not data: return jsonify({"text": "Error: No JSON data provided"}), 400
    
    # Parse comma-separated NPC names and stabilize IDs
    raw_npc = data.get('npc', 'Someone')
    raw_npcs = data.get('npcs', [])
    
    # Stabilize name-to-id mapping for resolution accuracy
    name_to_id = {}
    
    def register(raw):
        if not raw: return ""
        clean = raw.split('|')[0] if '|' in raw else raw
        name_to_id[clean] = raw
        return clean

    primary_npc = register(raw_npc)
    npcs = [register(n) for n in raw_npcs]
    
    # Ensure primary_npc is logic-ready
    player_name = data.get('player', 'Drifter')
    mode = data.get('mode', 'talk')
    
    # 3. Update LIVE_CONTEXTS from provided nearby data (ensures reactions work immediately)
    nearby = data.get('nearby', [])
    if nearby:
        for n in nearby:
            name = n.get('name')
            sid = n.get('storage_id') or n.get('id')
            if name:
                # 2차 작업 A-2(2): MERGE into the existing LIVE_CONTEXTS entry
                # instead of replacing it. The old wholesale replace downgraded
                # the rich /context payload (medical, stats, inventory,
                # equipment, money, relation, character_state, ...) to this
                # 6-field stub, so build_detailed_context_string() lost the
                # NPC's live condition right when a conversation started.
                entry = LIVE_CONTEXTS.get(name)
                if not isinstance(entry, dict):
                    entry = {}
                    LIVE_CONTEXTS[name] = entry
                if sid:
                    entry["id"] = f"{name}|{sid}"
                elif "id" not in entry:
                    entry["id"] = name
                # Identity fields: never overwrite real data with 'Unknown'
                for key in ("race", "faction", "gender"):
                    val = n.get(key)
                    if val and val != 'Unknown':
                        entry[key] = val
                    elif key not in entry:
                        entry[key] = 'Unknown'
                # Positional data is always fresh in this request — update it
                entry["nearby"] = [x for x in nearby if x.get('name') != name]
                entry["player_dist"] = n.get('dist', 999.0)
    
    # Filter player out of available NPCs to avoid hallucinated PC responses
    npcs = [n for n in npcs if n != player_name]
    if primary_npc == player_name and len(npcs) > 0:
        primary_npc = npcs[0]
        
    player_message = data.get('message', '')
    
    # --- TEST COMMAND INTERCEPT ---
    if player_message.startswith('/'):
        cmd_parts = player_message[1:].split(' ', 1)
        cmd = cmd_parts[0].lower()
        args = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
        
        test_action = None
        if cmd == "help" or cmd == "commands":
            help_text = "[DEBUG] Available test commands:\n" + \
                        "/take (takes 1st inv item), /attack, /follow, /idle, /patrol, /join, /leave, /release,\n" + \
                        "/notify [msg], /give_cats [n], /take_cats [n], /drop [item],\n" + \
                        "/take_item [item], /spawn [T|N|D], /relations [Faction] [n], /task [TASKNAME]"
            return jsonify({"text": help_text, "actions": []}), 200
            
        if cmd == "attack": test_action = "[ATTACK]"
        elif cmd == "follow": test_action = "[ACTION: FOLLOW_PLAYER]"
        elif cmd == "idle": test_action = "[ACTION: IDLE]"
        elif cmd == "patrol": test_action = "[ACTION: PATROL_TOWN]"
        elif cmd == "join": test_action = "[ACTION: JOIN_PARTY]"
        elif cmd == "leave": test_action = "[ACTION: LEAVE]"
        elif cmd == "release": test_action = "[ACTION: RELEASE_PLAYER]"
        elif cmd == "notify": test_action = f"[ACTION: NOTIFY: {args}]"
        elif cmd == "give_cats": test_action = f"[ACTION: GIVE_CATS: {args}]"
        elif cmd == "take_cats": test_action = f"[ACTION: TAKE_CATS: {args}]"
        elif cmd == "take_item": test_action = f"[ACTION: TAKE_ITEM: {args}]"
        elif cmd == "take":
            inv = PLAYER_CONTEXT.get("inventory", [])
            if inv:
                item_name = inv[0].get("name", "Unknown Item")
                test_action = f"[ACTION: TAKE_ITEM: {item_name}]"
            else:
                return jsonify({"text": "[DEBUG] Error: Player inventory is empty or unknown. Call /context to refresh.", "actions": []}), 200
        elif cmd == "drop": test_action = f"[ACTION: DROP_ITEM: {args}]"
        elif cmd == "spawn": test_action = f"[ACTION: SPAWN_ITEM: {args}]"
        elif cmd == "relations":
            rparts = args.rsplit(' ', 1)
            if len(rparts) == 2:
                test_action = f"[ACTION: FACTION_RELATIONS: {rparts[0].strip()}: {rparts[1].strip()}]"
        elif cmd == "task": test_action = f"[TASK: {args.upper()}]"
        
        if test_action:
            logging.info(f"TEST COMMAND: {cmd} -> {test_action}")
            return jsonify({
                "text": f"[DEBUG] Executing test command: {test_action}",
                "actions": [test_action]
            }), 200
            
    event = data.get('event')
    
    # Ignore internal events that aren't chat prompts
    if event == "selection_clear":
        return jsonify({"status": "ignored"}), 200
        
    # Prevent unprompted generation if no message is provided (unless it's an ambient event)
    if not player_message and event != "ambient_flavor":
        return jsonify({"text": "...", "actions": []}), 200
    
    # Handle Ambient Flavor (NPC to NPC chat)
    is_ambient = event == "ambient_flavor"
    if is_ambient:
        player_message = "[AMBIENT CONVERSATION TRIGGERED]"
        
    context = data.get('context', '')
    primary_id = extract_id_from_context(context)

    # 3.1 Register Primary NPC with LIVE_CONTEXTS (critical for batch generation)
    if primary_npc and context:
        try:
            ctx_dict = json.loads(context) if isinstance(context, str) else context
            if ctx_dict:
                # Merge with existing context to preserve "nearby" list and other tracking
                if primary_npc not in LIVE_CONTEXTS:
                    LIVE_CONTEXTS[primary_npc] = {}
                
                target = LIVE_CONTEXTS[primary_npc]
                target["id"] = primary_id if primary_id else (ctx_dict.get('id') or target.get('id', primary_npc))
                if ctx_dict.get('storage_id'): target["storage_id"] = ctx_dict.get('storage_id')
                if ctx_dict.get('race'): target["race"] = ctx_dict.get('race')
                if ctx_dict.get('faction'): target["faction"] = ctx_dict.get('faction')
                if ctx_dict.get('origin_faction'): target["origin_faction"] = ctx_dict.get('origin_faction')
                
                # DLL context often includes its own nearby list — PRESERVE IT
                if "nearby" in ctx_dict:
                    target["nearby"] = ctx_dict["nearby"]
                
                if "dist" in ctx_dict:
                    target["player_dist"] = ctx_dict["dist"]
        except Exception as e:
            logging.error(f"Error registering primary context: {e}")
    
    # radii
    whisper_radius, talk_radius, yell_radius = get_config_radii()
    
    npcs_in_radius = []
    # USE THE ROOT NEARBY LIST FOR ACCURATE PROXIMITY DETECTION
    nearby_data = data.get('nearby', [])
    for n in nearby_data:
        name = n.get("name")
        if not name or name == player_name or name == primary_npc:
            continue
            
        dist = n.get("dist", 999.0)
        # Check if they are in radius based on communication mode
        if mode == "whisper":
            # Whisper is one-on-one, no one eavesdrops in this mode now
            continue 
        elif mode == "talk":
            if dist <= talk_radius: npcs_in_radius.append(name)
        elif mode == "yell":
            if dist <= yell_radius: npcs_in_radius.append(name)

    # 4. History Update (Overhearing)
    
    def get_local_context_and_id(target_name):
        # Clean target_name for comparison
        clean_target = target_name.split('|')[0] if '|' in target_name else target_name
        
        if clean_target == primary_npc:
            return context, primary_id
            
        # Check current request's nearby data first (highest accuracy)
        nearby_data = data.get('nearby', [])
        for n in nearby_data:
            n_name = n.get("name", "")
            clean_n = n_name.split('|')[0] if '|' in n_name else n_name
            if clean_n == clean_target:
                return json.dumps(n), (n.get("storage_id") or n.get("id"))
                
        # Fallback to LIVE_CONTEXTS cache
        if clean_target in LIVE_CONTEXTS:
            c = LIVE_CONTEXTS[clean_target]
            return json.dumps(c), (c.get("storage_id") or c.get("id"))
            
        return "", None

    # Determine listeners (everyone in radius)
    # Ensure listeners are clean names for logic processing
    raw_listeners = list(set([primary_npc] + npcs_in_radius))
    listeners = []
    for l in raw_listeners:
        clean_l = l.split('|')[0] if '|' in l else l
        if clean_l not in listeners: listeners.append(clean_l)

    # 5. Determine who the LLM actually responds as
    if mode == 'yell':
        npcs = listeners
    else:
        npcs = [primary_npc]

    if not primary_id:
        # (bugfix) 'live_ctx' was referenced here without being defined — resolve from LIVE_CONTEXTS
        primary_id = LIVE_CONTEXTS.get(primary_npc, {}).get("id")

    # BATCH GENERATION: Pre-emptively generate profiles for anyone (participants or overhearers) missing one
    missing_for_batch = []
    checked_ids = set()
    for name in listeners:
        cid = primary_id if name == primary_npc else None
        npc_ctx, local_cid = get_local_context_and_id(name)
        sid = cid if cid else local_cid
        
        # Determine the storage ID to check disk (STRICT NAME-ONLY)
        storage_id = name
        if '|' in str(storage_id): storage_id = str(storage_id).split('|')[0]
        
        if storage_id in checked_ids: continue
        checked_ids.add(storage_id)
        
        safe_fn = "".join([c for c in str(storage_id) if c.isalnum() or c in (' ', '_', '-')]).strip()
        path = os.path.join(CHARACTERS_DIR, f"{safe_fn}.json")
        
        if not os.path.exists(path):
            # Atomic check to avoid redundant generation for the same NPC
            with PROGRESS_LOCK:
                if storage_id in PROFILES_IN_PROGRESS:
                    continue
                PROFILES_IN_PROGRESS.add(storage_id)

            # Get data for batch
            ctx_dict = {}
            if npc_ctx:
                try: ctx_dict = json.loads(npc_ctx) if isinstance(npc_ctx, str) else npc_ctx
                except: pass
            
            if not ctx_dict:
                live = LIVE_CONTEXTS.get(name, {})
                ctx_dict = {
                    "name": name,
                    "race": live.get("race", "Unknown"),
                    "gender": live.get("gender", "Unknown"),
                    "faction": live.get("faction", "Unknown"),
                    "storage_id": storage_id
                }
            else:
                ctx_dict["storage_id"] = storage_id
                
            missing_for_batch.append(ctx_dict)

    if missing_for_batch:
        try:
            generate_batch_profiles(missing_for_batch)
        finally:
            with PROGRESS_LOCK:
                for ctx in missing_for_batch:
                    sid = ctx.get("storage_id")
                    if sid in PROFILES_IN_PROGRESS:
                        PROFILES_IN_PROGRESS.remove(sid)

    char_datas = {}
    threads = []
    def fetch_npc_thread(name, cid, delay):
        if delay > 0:
            time.sleep(delay)
        try:
            npc_context, local_cid = get_local_context_and_id(name)
            thread_cid = cid if cid else local_cid
            char_datas[name] = get_character_data(name, npc_context, char_id=thread_cid)
        except Exception as e:
            logging.error(f"Thread Error fetching {name}: {e}")

    delay_counter = 0
    for name in listeners:
        cid = primary_id if name == primary_npc else None
        
        # Check if background already exists to avoid unnecessary delays (STRICT NAME-ONLY)
        storage_id = name
        if '|' in str(storage_id): storage_id = str(storage_id).split('|')[0]
            
        safe_filename = "".join([c for c in str(storage_id) if c.isalnum() or c in (' ', '_', '-')]).strip()
        path = os.path.join(CHARACTERS_DIR, f"{safe_filename}.json")
        
        delay = 0
        if not os.path.exists(path):
            delay = delay_counter
            delay_counter += 1
            
        t = threading.Thread(target=fetch_npc_thread, args=(name, cid, delay), daemon=True)
        t.start()
        threads.append(t)
        
    for t in threads:
        t.join()

    # Safety Fallback
    for name in npcs:
        if name not in char_datas or not char_datas[name]:
            logging.error(f"Failed to retrieve data for {name}, using fallback.")
            char_datas[name] = {"Name": name, "Personality": "A generic NPC.", "Backstory": "Unknown", "ConversationHistory": []}
    
    # TALK mode now allows fall-through to prompt only the primary NPC
    # while others overheard via history updates above.

    logging.info(f"Prompting LLM for {mode} communication with {primary_npc} (Total participants: {len(npcs)})...")
    # Context building similar to Fallout 2 mod...
    primary_data = char_datas[primary_npc]

    settings = load_settings()

    # Short-term history window (INI: ShortTermContextCount, default 60 — 결정사항 ①)
    short_term_count = int(settings.get("short_term_context_count", 60))
    digest_on = bool(settings.get("digest_enabled", True))
    digests = primary_data.get("Digests", []) if digest_on else []

    # Phase 2 no-double-injection rule: when digests exist, the raw window only
    # covers lines AFTER the digest cursor; the digested segment enters the
    # prompt exclusively as its summary below.
    if digests:
        raw_tail = get_undigested_lines(primary_data)
    else:
        raw_tail = list(primary_data.get("ConversationHistory", []))
    recent_history = raw_tail[-short_term_count:]
    # Clip overly long lines at injection time only (stored history stays intact)
    recent_history = [(l[:200] + "...") if len(l) > 200 else l for l in recent_history]

    # --- Phase 3: durable (long-term) memory recall --------------------------
    # Query = player's message + last 3 history lines (covers pronouns/ellipsis,
    # report 1-③ c). Recall reinforcement mutates primary_data in place and is
    # persisted by the normal listener save loop at the end of this request.
    durable_block = ""
    if not is_ambient and settings.get("durable_memory_enabled", True) and primary_data.get("DurableMemories"):
        try:
            current_day = int(PLAYER_CONTEXT.get("day", 0) or 0)
            recall_query = "\n".join([player_message or ""] + raw_tail[-3:])
            recalled = recall_durable_memories_vector(primary_data, recall_query, settings, current_day)
            if recalled:
                mem_lines = "\n".join(f"- {m.get('text', '')}" for m in recalled)
                durable_block = (f"[DURABLE MEMORIES — {primary_npc}'s long-term memories relevant to this moment]\n"
                                 f"{mem_lines}\n\n")
                logging.info(f"DURABLE: injected {len(recalled)} memories for {primary_npc} (~{estimate_tokens(mem_lines)}tk)")
        except Exception as e:
            logging.error(f"DURABLE: recall failed for {primary_npc}: {e}")

    digest_block = ""
    if digests:
        inject_n = max(1, int(settings.get("digest_inject_count", 3)))
        dg_lines = []
        for dg in digests[-inject_n:]:
            span = ""
            if dg.get("from_ts") or dg.get("to_ts"):
                span = f"({dg.get('from_ts', '?')} ~ {dg.get('to_ts', '?')})\n"
            dg_lines.append(f"{span}{(dg.get('summary') or '').strip()}")
        digest_block = ("[EARLIER EVENTS — MEMORY DIGEST (older conversations, summarized)]\n"
                        + "\n".join(dg_lines)
                        + "\n\n[RECENT DIALOGUE]\n")

    # Keep the [RECENT DIALOGUE] separator when only the durable block exists
    if durable_block and not digest_block:
        durable_block += "[RECENT DIALOGUE]\n"

    # Phase 1: ArchiveSummary block — injected before digest/durable blocks
    archive_block = ""
    _archive_text = (primary_data.get("ArchiveSummary") or "").strip()
    if _archive_text and settings.get("archive_summary_enabled", True):
        archive_block = ("[ARCHIVE — 핵심 관계 요약 (오래된 대화 압축)]\n"
                         + _archive_text + "\n\n")

    def _compose_history_str():
        joined = "\n".join(recent_history)
        prefix = archive_block + durable_block + digest_block
        return (prefix + joined) if prefix else joined

    history_str = _compose_history_str()

    # Phase 1 (filter 6): only reveal concealed bag contents when the player offers to show them
    _msg_lower = (player_message or "").lower()
    reveal_concealed = any(kw in _msg_lower for kw in (
        "bag", "pack", "inventory", "loot", "wares", "가방", "배낭", "소지품", "인벤"))

    # Phase 1 (filter 1, decision ④): in yell mode only the primary NPC gets a full
    # profile + live context; other listeners get a 1-line summary. This removes the
    # O(N^2) PEOPLE NEARBY duplication across listener profiles.
    yell_compact = bool(settings.get("yell_compact_profiles", True))
    npc_profiles = ""
    compact_lines = []
    for name in npcs:
        d = char_datas[name]
        if mode == 'yell' and yell_compact and name != primary_npc:
            live = LIVE_CONTEXTS.get(name, {})
            gender = d.get('Sex') or live.get('gender', 'Unknown')
            race = d.get('Race') or live.get('race', 'Unknown')
            faction = d.get('Faction') or live.get('faction', 'Unknown')
            personality = (d.get('Personality') or 'A wasteland dweller.').strip().replace("\n", " ")
            first_sentence = re.split(r'(?<=[.!?])\s+', personality)[0][:160]
            compact_lines.append(f"- {name} ({gender} {race}, {faction}): {first_sentence}")
            continue
        npc_profiles += f"\nCHARACTER: {name}\n"
        npc_profiles += f"RACE: {d.get('Race')}\n"
        npc_profiles += f"FACTION: {d.get('Faction')}\n"
        npc_profiles += f"PERSONALITY: {d.get('Personality')}\n"
        npc_profiles += f"BACKSTORY: {d.get('Backstory')}\n"

        # Add live context (stats, health, etc.)
        live_context = build_detailed_context_string(name, char_data=d)
        if live_context:
            npc_profiles += f"{live_context}\n"

    if compact_lines:
        npc_profiles += "\nOTHER PEOPLE LISTENING (brief profiles — they may answer with short lines):\n"
        npc_profiles += "\n".join(compact_lines) + "\n"

    primary_race = primary_data.get('Race', 'Unknown')
    is_animal = any(kw.lower() in primary_race.lower() for kw in ANIMAL_RACES)

    sys_sections = {}  # Phase 0: per-section token estimates from build_system_prompt
    if is_animal:
        dynamic_system_prompt = f"CRITICAL: {primary_npc} is an ANIMAL ({primary_race}). Animals in Kenshi CANNOT speak human languages. They do not use words, symbols, or telegram-style speech. They ONLY react with brief physical actions, sounds, or gestures described within asterisks."
        final_instruction = f"Respond as {primary_npc} (the animal). Provide a single, BRIEF action description or sound in asterisks (e.g. *Growls*, *Tilts head*, *Nuzzles hand*). DO NOT USE WORDS OR SPEECH. Keep it under 6 words."
    else:
        # Phase 4: faction RAG query = player utterance + recent context tail
        # (pronoun/ellipsis robustness — report 2.3); the primary NPC's own
        # faction is always considered without matching (report 2.1).
        faction_query = "\n".join(s for s in ([player_message or ""] + raw_tail[-3:]) if s)
        npc_factions = []
        for src in (primary_data.get("Faction"), LIVE_CONTEXTS.get(primary_npc, {}).get("faction")):
            if src and src not in npc_factions:
                npc_factions.append(src)

        # 2차 작업 A-1: period actually covered by the injected short-term
        # window = timestamp of its oldest parseable line. Chat-type events
        # inside that period (same parties) are deduped from ## CURRENT SCENE.
        dialogue_window_start = None
        if settings.get("dedupe_chat_events", True):
            for _line in recent_history:
                _ts = _parse_line_ts(_line)
                if _ts is not None:
                    dialogue_window_start = _ts
                    break

        dynamic_system_prompt = build_system_prompt(player_name, relevant_names=listeners,
                                                    reveal_concealed=reveal_concealed,
                                                    section_sink=sys_sections,
                                                    faction_query=faction_query,
                                                    npc_factions=npc_factions,
                                                    dialogue_window_start=dialogue_window_start)

        if mode == 'yell':
            volume_status = "The player is addressing everyone nearby at a clear, projected volume."
            yell_instruction = f"\nCRITICAL: {volume_status} This can be heard by everyone nearby ({', '.join(npcs)}). This is a public address or talking to a crowd; it is NOT yelling or shouting aggressively. DO NOT tell the player to quiet down or react with annoyance to the volume. You SHOULD respond as multiple characters from the list to create a realistic crowd reaction. Every speaker MUST be on a new line started with 'Name: ' (e.g., 'Beep: Hey!')."
            dynamic_system_prompt += yell_instruction
        elif mode == 'whisper':
            volume_status = "The player is WHISPERING to you privately. This is a quiet, intimate, or secretive moment."
            whisper_instruction = f"\nCRITICAL: {volume_status} ONLY {primary_npc} should respond. Keep the tone hushed and private."
            dynamic_system_prompt += whisper_instruction
        else:
            volume_status = "The player is speaking at a normal, conversational volume."

            # Transition reinforcement: inform LLM they stopped the public address
            if "[ACTION: ADDRESSES GROUP]" in history_str:
                 volume_status += " They have STOPPED addressing the group and are now speaking at a calm, normal volume."
                 
            talk_instruction = f"\nINFO: {volume_status} Respond naturally. This is a standard, polite conversation. You are calm and composed. DO NOT tell the player to quiet down, do NOT react with annoyance to their volume, and do NOT mention noise or shouting unless they are actually being aggressive."
            dynamic_system_prompt += talk_instruction
        
        # (Phase 1, 3.3-3) The former extra group_instruction duplicated the yell
        # instruction above ("respond as multiple characters", "Name: Dialogue") —
        # removed to avoid injecting the same directive twice.

        final_instruction = f"Respond as {primary_npc} to the player's last message."
        if mode != 'yell':
            final_instruction = f"Respond ONLY as {primary_npc}. Do not speak as anyone else. Keep the response to 1-2 short sentences in a single paragraph."
        else:
            final_instruction = f"Respond as several characters from this list: ({', '.join(npcs)}) to the player's group address. Ensure at least 2-3 unique characters speak on separate lines if they are nearby."

    template = load_prompt_component("prompt_chat_template.txt", """[SYSTEM CORE]
{system_prompt}

[CURRENT CHARACTER: {primary_npc}]
{npc_profiles}

[CONVERSATION HISTORY]
{history_str}

[FINAL INSTRUCTION]
{final_instruction}
Keep it immersive, short, and grounded in the world of Kenshi.
You MUST write your final response exclusively in {language_str}.
""")
    
    # 2차 작업 B-1: expand the bare language name into an enforceable directive
    # (English stays the bare name — prompt unchanged in the default config).
    user_lang = get_language_directive(settings.get("language", "English"))

    rich_prompt = template.format(
        system_prompt=dynamic_system_prompt,
        primary_npc=primary_npc,
        npc_profiles=npc_profiles,
        history_str=history_str,
        final_instruction=final_instruction,
        language_str=user_lang
    )
    # Tag the player message with mode for history clarity
    mode_action = ""
    if mode == 'whisper':
        mode_action = f" [ACTION: WHISPERS TO {primary_npc}]"
    elif mode == 'yell':
        mode_action = " [ACTION: ADDRESSES GROUP]"
    else:
        # If they were addressing the group before, explicitly state they are talking normally now
        if "[ACTION: ADDRESSES GROUP]" in history_str:
            mode_action = " [ACTION: TALKS NORMALLY]"
    time_prefix = get_current_time_prefix()
    full_player_entry = f"{time_prefix}{player_name}{mode_action}: {player_message}"

    # --- Phase 1 (3.4-3): soft token budget — trim oldest history lines if exceeded ---
    # (Digest block is preserved: it is the cheapest representation of old context.)
    max_prompt_tokens = int(settings.get("max_prompt_tokens", 8000))
    total_est = estimate_tokens(rich_prompt) + estimate_tokens(full_player_entry)
    if total_est > max_prompt_tokens and recent_history:
        while recent_history and total_est > max_prompt_tokens:
            dropped = recent_history.pop(0)
            total_est -= estimate_tokens(dropped)
        history_str = _compose_history_str()
        rich_prompt = template.format(
            system_prompt=dynamic_system_prompt,
            primary_npc=primary_npc,
            npc_profiles=npc_profiles,
            history_str=history_str,
            final_instruction=final_instruction,
            language_str=user_lang
        )
        total_est = estimate_tokens(rich_prompt) + estimate_tokens(full_player_entry)
        logging.info(f"PROMPT_BUDGET: over {max_prompt_tokens}tk limit — history trimmed to {len(recent_history)} lines (now ~{total_est}tk)")

    # --- Phase 0: per-section prompt token instrumentation (length-based, no perf cost) ---
    try:
        sys_detail = f" [{format_token_breakdown(sys_sections)}]" if sys_sections else ""
        logging.info(
            f"PROMPT_TOKENS [chat/{mode}] listeners={len(listeners)} responders={len(npcs)} "
            f"total~{total_est}tk | "
            + format_token_breakdown({
                "system": dynamic_system_prompt,
                "profiles": npc_profiles,
                "archive": archive_block,
                "durable": durable_block,
                "digest": digest_block,
                "history_raw": "\n".join(recent_history),
                "instruction": final_instruction,
                "user_msg": full_player_entry,
            })
            + sys_detail
        )
        # Full assembled prompt goes to the rotating debug.log only (not server.log)
        debug_logger.debug(f"PROMPT_ASSEMBLED [{mode}] for {primary_npc} (~{total_est}tk):\n{rich_prompt}\nUSER: {full_player_entry}")
    except Exception as _e:
        debug_logger.debug(f"PROMPT_TOKENS logging failed: {_e}")

    messages = [
        {"role": "system", "content": rich_prompt},
        {"role": "user", "content": full_player_entry}
    ]

    # Debug Logging: Log the full request (DISABLED)
    # DEBUG_LOG = os.path.join(SCRIPT_DIR, "..", "logs", "llm_debug.log")
    # with open(DEBUG_LOG, "a", encoding="utf-8") as f:
    #     f.write(f"\n{'='*50}\n")
    #     f.write(f"TIMESTAMP: {time.ctime()}\n")
    #     f.write(f"REQUEST FOR: {primary_npc}\n")
    #     f.write(f"PROMPT:\n{rich_prompt}\n")
    #     f.write(f"USER MESSAGE: {player_message}\n")
    #     f.write(f"{'-'*30}\n")

    logging.info(f"Calling main chat LLM...")
    content = call_llm(messages)
    
    # Debug Logging: Log the response (DISABLED)
    # if content:
    #     with open(DEBUG_LOG, "a", encoding="utf-8") as f:
    #         f.write(f"RAW LLM RESPONSE:\n{content}\n")
    #         f.write(f"{'='*50}\n")
    # else:
    #     logging.error("LLM returned None for chat response.")
    #     with open(DEBUG_LOG, "a", encoding="utf-8") as f:
    #         f.write(f"LLM RESPONSE FAILED (None)\n")
    #         f.write(f"{'='*50}\n")
    
    if content:
        # Extract Action Tags safely matching bracketed keywords (tolerates spaces).
        action_pattern = r'(\[\s*[A-Z_]+(?::\s*[^\]]+)?\s*\])'
        actions = re.findall(action_pattern, content)

        # Remove tags from the dialogue text
        content = re.sub(action_pattern, '', content).strip()

        # --- Phase 3: intercept RECORD_MEMORY tags FIRST ---------------------
        # These are consumed server-side and must NEVER reach the DLL: the
        # unknown-tag fallback in the normalization loop below would otherwise
        # wrap them as [ACTION: ...] and forward them to the game.
        memory_tags = [t for t in actions if is_record_memory_tag(t)]
        if memory_tags:
            actions = [t for t in actions if not is_record_memory_tag(t)]
            try:
                handle_record_memory_tags(memory_tags, primary_npc, primary_data, settings=settings)
            except Exception as e:
                logging.error(f"DURABLE: failed to store RECORD_MEMORY for {primary_npc}: {e}")

        # Normalize Action Tags for C++ engine
        updated_actions = []
        for raw_tag in actions:
            inner = raw_tag.strip("[] \t")
            upper_inner = inner.upper()
            
            # Map common sloppy inputs to formal tags
            final_tag = ""
            
            # Known keywords that should trigger specific tags
            known_actions = ["ATTACK", "JOIN_PARTY", "LEAVE", "FOLLOW_PLAYER", "IDLE", "PATROL_TOWN", "RELEASE_PLAYER", "NOTIFY", "GIVE_CATS", "TAKE_CATS", "GIVE_ITEM", "TAKE_ITEM", "DROP_ITEM", "SPAWN_ITEM", "FACTION_RELATIONS"]
            
            # Peel off prefixes for normalization check
            core = upper_inner
            if core.startswith("ACTION:"): core = core[7:].strip()
            elif core.startswith("TASK:"): core = core[5:].strip()

            # 2차 작업 A-2(3): argument extraction must use the PREFIX-STRIPPED
            # form (case preserved). Splitting the raw inner at its first ':'
            # treated the tag name of '[ACTION: X]' as an argument, producing
            # '[ACTION: X: X]' (and '[ACTION: X: X: args]' with real args).
            inner_core = inner
            _pm = re.match(r"(?i)^(?:ACTION|TASK)\s*:\s*", inner_core)
            if _pm:
                inner_core = inner_core[_pm.end():]

            # Check for matches
            matched = False
            for ka in known_actions:
                if ka in core:
                    # Preserve arguments if any (e.g. JOIN_PARTY: Faction)
                    args = ""
                    if ":" in inner_core:
                        args = inner_core.split(":", 1)[1].strip()
                    
                    if ka == "LEAVE":
                        origin_faction = primary_data.get("Faction", "Unknown")
                        final_tag = f"[ACTION: LEAVE: {origin_faction}]" if origin_faction != "Unknown" else "[ACTION: LEAVE]"
                    elif ka in ["WANDERER", "CHASE", "IDLE", "MELEE_ATTACK"] or upper_inner.startswith("TASK:"):
                        # Tasks use the TASK prefix
                        final_tag = f"[TASK: {ka if not args else f'{ka}: {args}'}]"
                    else:
                        # Standard actions
                        final_tag = f"[ACTION: {ka}{f': {args}' if args else ''}]"
                    
                    matched = True
                    break
            
            if not matched:
                # Fallback: if it's uppercase and bracketed, try to wrap it as an action anyway
                if core.strip():
                    final_tag = f"[ACTION: {core}]"

            if final_tag:
                # REDUNDANCY FILTER
                # Avoid repetitive wanderer/idle tasks if no change
                if "TASK:WANDERER" in final_tag or "TASK:IDLE" in final_tag:
                     last_history = primary_data["ConversationHistory"][-1] if primary_data["ConversationHistory"] else ""
                     if final_tag in last_history:
                         continue
                updated_actions.append(final_tag)
        actions = updated_actions

        # Advanced Cleaning
        content = content.replace('"', '').strip()
        
        # Split into lines and filter out thoughts/meta-text
        lines = content.split('\n')
        filtered_lines = []
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # If in YELL mode, look for "Name: Response" format to split bubbles
            is_group_response = (mode == 'yell')
            if is_group_response:
                # Try to extract "Beep: Hello!" or "Hobbs: Let's go."
                match = re.match(r'^([^:]+):\s*(.*)$', line)
                if match:
                    actor_name = match.group(1).strip()
                    actor_clean = actor_name.lower()
                    actor_speech = match.group(2).strip()
                    # Only accept if actor is NOT the player (hallucination)
                    if actor_clean != player_name.lower():
                        # Use full ID if mapping exists to aid C++ resolution
                        full_actor = name_to_id.get(actor_name, actor_name)
                        filtered_lines.append(f"{full_actor}: {actor_speech}")
                        continue
                    else:
                        logging.info(f"Hallucination Filter: Discarded LLM attempt to speak as {player_name}")
                        continue
            
            # Skip common non-dialogue prefixes/meta-talk and hallucinated log lines
            lower_line = line.lower()
            if any(lower_line.startswith(prefix) for prefix in [
                "thought:", "thinking:", "observation:", "note:", "(thinking", 
                "*", "as an ai", "i cannot", "here is", "raw llm response:", 
                "timestamp:", "request for:", "prompt:", "user message:",
                "history:", "character:", "personality:", "backstory:", "current condition"
            ]):
                continue
            
            # Skip separator lines
            if line.startswith('=') or line.startswith('-') or len(set(line)) <= 2:
                continue
                
            # Remove "CHARACTER_NAME: " prefixes ONLY if NOT in squad mode
            # Remove "CHARACTER_NAME: " prefixes ONLY if NOT in multi/squad mode
            if len(npcs) <= 1:
                # Hallucination Filter: If talking to ONE person, ensure they don't speak as the player or someone else
                prefix_match = re.match(r'^([A-Za-z0-9가-힣 _\-\.]+):\s*', line)
                if prefix_match:
                    p = prefix_match.group(1).strip().lower()
                    if p == player_name.lower():
                        logging.info(f"Hallucination Filter: Discarded player entry {line}")
                        continue
                    if p != primary_npc.lower():
                        # Discard line for a different persona
                        logging.info(f"Hallucination Filter: Discarded line from {p} (expected {primary_npc})")
                        continue
                # Strip the prefix if it existed
                line = re.sub(r'^[A-Za-z0-9가-힣 _\-\.]+:\s*', '', line)
            
            # intra-line splitting for multi/squad talk (catch "Name1: text Name2: text")
            if len(npcs) > 1:
                # Find all "Name: Dialogue" blocks
                # We look for a name followed by a colon, then text until the next name: or string end
                # The name must avoid common dialogue words
                # Korean names have no upper/lowercase, so allow a Hangul-led name
                # alongside the original Capitalized-English pattern.
                pattern = r'([A-Z가-힣][a-z0-9가-힣 \-\.]*):\s*([^:]+?)(?=\s+[A-Z가-힣][a-z0-9가-힣 \-\.]*:\s*|$)'
                sub_matches = re.findall(pattern, line)
                if sub_matches:
                    for actor, speech in sub_matches:
                        actor_clean = actor.strip()
                        if actor_clean.lower() != player_name.lower():
                            full_actor = name_to_id.get(actor_clean, actor_clean)
                            filtered_lines.append(f"{full_actor}: {speech.strip()}")
                    continue

            if line:
                filtered_lines.append(line)
        
        # Join lines - newlines represent separate bubbles in multi-NPC mode
        if filtered_lines:
            if mode != 'yell':
                # For single responder modes, merge into one bubble to prevent rapid-fire flashing
                content = " ".join(filtered_lines)
            else:
                content = "\n".join(filtered_lines)
        else:
            content = "..."

        # Final safety truncation (Kenshi bubbles are intended for short lines)
        if len(content) > 500:
            content = content[:497] + "..."
        
        # Log initial player prompt to global history once
        player_faction = PLAYER_CONTEXT.get("faction", "None")
        primary_faction = char_datas.get(primary_npc, {}).get("Faction", "None")
        record_event_to_history("CHAT", player_name, primary_npc, player_message, actor_faction=player_faction, target_faction=primary_faction)

        # Save history for ALL listeners (Participants + Overhearers)
        for name in listeners:
            is_overhearing = name not in npcs
            overheard_tag = "(Overheard) " if is_overhearing else ""
            
            if name not in char_datas:
                # Need to fetch for overhearers who weren't participants
                ctx, sid = get_local_context_and_id(name)
                char_datas[name] = get_character_data(name, ctx, char_id=sid)
                
            char_datas[name]["ConversationHistory"].append(f"{time_prefix}{overheard_tag}{player_name}{mode_action}: {player_message}")
            
            # If multiple lines/speakers, append them all to history
            if "\n" in content:
                for line in content.split('\n'):
                    if not line.strip(): continue
                    
                    # Ensure the line has a speaker attribution in the history
                    history_line = line.strip()
                    if ':' not in history_line:
                         # Append primary name if LLM forgot the prefix in single-responder modes
                         history_line = f"{primary_npc}: {history_line}"
                    
                    # If this is the LAST line and there are actions, append them for history context
                    if line == filtered_lines[-1] and actions:
                        history_line += f" {' '.join(actions)}"

                    char_datas[name]["ConversationHistory"].append(f"{time_prefix}{overheard_tag}{history_line}")
                    
                    # Log NPC speech to global history
                    if ':' in history_line:
                        h, m = history_line.split(':', 1)
                        speaker_name = h.strip()
                        speaker_faction = char_datas.get(speaker_name, {}).get("Faction", "None")
                        player_faction = PLAYER_CONTEXT.get("faction", "None")
                        record_event_to_history("CHAT", speaker_name, player_name, m.strip(), actor_faction=speaker_faction, target_faction=player_faction)
                    else:
                        primary_faction = char_datas.get(primary_npc, {}).get("Faction", "None")
                        player_faction = PLAYER_CONTEXT.get("faction", "None")
                        record_event_to_history("CHAT", primary_npc, player_name, history_line, actor_faction=primary_faction, target_faction=player_faction)
            else:
                # Fallback for single-line responses
                history_line = content
                if ':' not in history_line:
                     history_line = f"{primary_npc}: {history_line}"
                
                history_entry = f"{time_prefix}{overheard_tag}{history_line}"
                if actions:
                    history_entry += f" {' '.join(actions)}"
                char_datas[name]["ConversationHistory"].append(history_entry)
                
                primary_faction = char_datas.get(primary_npc, {}).get("Faction", "None")
                player_faction = PLAYER_CONTEXT.get("faction", "None")
                record_event_to_history("CHAT", primary_npc, player_name, content, actor_faction=primary_faction, target_faction=player_faction)

            # Limit history to HISTORY_MAX_LINES
            if len(char_datas[name]["ConversationHistory"]) > HISTORY_MAX_LINES:
                char_datas[name]["ConversationHistory"] = char_datas[name]["ConversationHistory"][-HISTORY_MAX_LINES:]
                
            storage_id = char_datas[name].get("ID", name)
            # CRITICAL: Prevent transient fallback profiles from overwriting real ones
            if char_datas[name].get("_transient"):
                logging.warning(f"SKIP SAVE: {name} is using a transient fallback profile. Blocking disk override.")
            elif should_save_profile(name, storage_id, char_datas[name]):
                save_character_data(storage_id, char_datas[name])
                # Phase 2: digest trigger only for NPCs the player actually conversed
                # with (responders), never for pure overhearers or ambient banter —
                # this caps background LLM call volume (report 1-② item 3).
                if name in npcs and not is_ambient:
                    maybe_queue_digest(storage_id, char_datas[name], settings=settings)

        logging.info(f"AI RESPONSE: {content} | ACTIONS: {actions}")
        return jsonify({"text": content, "actions": actions})
    return jsonify({"text": "...", "actions": []})


def record_event_to_history(etype, actor, target, msg, actor_faction="None", target_faction="None"):
    """Centralized helper to record events for both the log and narrative synthesis."""
    global EVENT_HISTORY, GLOBAL_EVENT_COUNTER, EVENT_THROTTLE, LAST_STATE_LOG
    if not msg: return
    
    # Format: [TYPE] Actor (Faction) -> Target (Faction) @ Location: Message
    p_fact = PLAYER_CONTEXT.get('faction', 'Nameless')
    a_fact_display = actor_faction
    if actor_faction == "Nameless" or actor_faction == p_fact:
        a_fact_display = f"Player's Squad: {p_fact}"
        
    t_fact_display = target_faction
    if target_faction == "Nameless" or target_faction == p_fact:
        t_fact_display = f"Player's Squad: {p_fact}"

    actor_part = f"{actor} ({a_fact_display})" if a_fact_display and a_fact_display != "None" else actor
    target_part = f"{target} ({t_fact_display})" if t_fact_display and t_fact_display != "None" else target
    
    # Include location from player context if available
    location = ""
    if PLAYER_CONTEXT:
        env = PLAYER_CONTEXT.get("environment", {})
        town = env.get("town_name", "") if isinstance(env, dict) else ""
        if town:
            location = f" @ {town}"
    
    time_str = get_current_time_prefix().strip()
    prefix = f"{time_str} " if time_str else ""
    evt_str = f"{prefix}[{etype}] {actor_part} -> {target_part}{location}: {msg}"
    
    # --- STATE SUPPRESSION ---
    # For repetitive state hooks (knockout, recovery, etc), only log if the status actually CHANGES.
    state_key = f"{target_part}|{etype}"
    with STATE_LOCK:
        if LAST_STATE_LOG.get(state_key) == msg:
            return  # Message is identical to last recorded state, skip
        LAST_STATE_LOG[state_key] = msg
        # Cleanup if it gets massive
        if len(LAST_STATE_LOG) > 2000: LAST_STATE_LOG.clear()

    # --- THROTTLE CHECK ---
    # Cooldown for non-stateful rapid repeats
    throttle_key = f"{etype}|{actor_part}|{target_part}|{msg}"
    now = time.time()
    with THROTTLE_LOCK:
        last_time = EVENT_THROTTLE.get(throttle_key, 0)
        # Increased cooldown to 30s for exact same event to prevent spam
        if now - last_time < 30.0:
            return
        EVENT_THROTTLE[throttle_key] = now
        # Periodic cleanup: Instead of clearing everything, just trim if it gets too large
        if len(EVENT_THROTTLE) > 1000:
            # Simple way to trim: keep most recent half
            sorted_items = sorted(EVENT_THROTTLE.items(), key=lambda x: x[1])
            EVENT_THROTTLE = dict(sorted_items[500:])

    # Log to file (Active Campaign Log) - Always log for live debugger feed
    try:
        log_dir = os.path.join(get_campaign_dir(), "logs")
        if not os.path.exists(log_dir): os.makedirs(log_dir)
        with open(os.path.join(log_dir, "global_events.log"), "a", encoding="utf-8") as f:
            f.write(f"{evt_str}\n")
    except:
        pass

    # Simple deduplication based on exact string for memory/synthesis
    if evt_str not in EVENT_HISTORY:
        EVENT_HISTORY.append(evt_str)
        GLOBAL_EVENT_COUNTER += 1
        save_campaign_history()
            
    # Narrative check (OLD: based on counter)
    # Removed in favor of timed synthesis as per user request.
    # if GLOBAL_EVENT_COUNTER >= 100:
    #     logging.info(f"NARRATIVE: Threshold reached ({GLOBAL_EVENT_COUNTER}). Triggering synthesis.")
    #     threading.Thread(target=generate_global_narrative_thread, daemon=True).start()
    #     GLOBAL_EVENT_COUNTER = 0

    if len(EVENT_HISTORY) > 500:
        EVENT_HISTORY = EVENT_HISTORY[-500:]

def generate_global_narrative_thread():
    """Synthesizes the last 100 events into a global rumor for NPCs to overhear."""
    global EVENT_HISTORY
    # Lower threshold for manual trigger so small sessions can still synthesize
    min_needed = 5
    if len(EVENT_HISTORY) < min_needed:
        logging.warning(f"NARRATIVE: Not enough events to synthesize (have {len(EVENT_HISTORY)}, need {min_needed}).")
        return None
    
    settings = load_settings()
    ge_count = settings.get("global_events_count", 10)
    
    # Use ge_count * 5 as the synthesis sample to ensure variety and context,
    # but the prompt instructions will emphasize the 'global_events_count' recent actions.
    sample_size = min(len(EVENT_HISTORY), max(ge_count, 100))
    last_chunk = EVENT_HISTORY[-sample_size:]
    events_text = "\n".join(last_chunk)
    
    template = load_prompt_component("prompt_world_synthesis.txt", """[KENSHI WORLD SYNERGY]
The following is a log of recent interactions in the world of Kenshi.
Your task is to synthesize these events into a single, high-impact 'Global Rumor'.

RECENT LOGS:
{events_text}

INSTRUCTIONS:
1. Write one flavorful, immersive rumor — 1 to 3 sentences, grounded in Kenshi's brutal world.
2. Keep it short, cynical, and specific. Reference real names and factions from the logs.
3. Output ONLY the rumor text itself, with no prefix tags or formatting.
""")
    prompt = template.format(events_text=events_text)
    # 2차 작업 B-1: rumors are shown in-game (NOTIFY) and injected into prompts —
    # follow the configured output language (no-op for English).
    prompt += aux_language_rule("the rumor text", settings.get("language", "English"))
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Synthesize the rumors of the borderlands."}
    ]
    
    logging.info("NARRATIVE: Calling LLM to synthesize world events...")
    rumor_text = call_llm(messages)
    
    if rumor_text:
        # Strip any accidental tags the LLM might still output
        rumor_text = rumor_text.strip()
        # If LLM still used the old format, extract just the inner text
        tag_match = re.search(r'\[RUMOR:\s*(.*?)\]', rumor_text, re.DOTALL)
        if tag_match:
            rumor_text = tag_match.group(1).strip()
        # Remove any leading dashes or bullets
        rumor_text = re.sub(r'^[-•*]\s*', '', rumor_text).strip()
        
        if len(rumor_text) > 10:
            time_prefix = get_current_time_prefix().strip()
            rumor_tagged = f"- {time_prefix} [RUMOR: {rumor_text}]"
            # Try campaign dir first
            world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
            if not os.path.exists(world_events_path):
                 # Create empty if missing
                 with open(world_events_path, "w", encoding="utf-8") as f:
                     f.write("# Dynamic rumors generated for this campaign\n")

            try:
                if os.path.exists(world_events_path):
                    with open(world_events_path, "a", encoding="utf-8") as f:
                        f.write(f"\n{rumor_tagged}\n")
                    logging.info(f"NARRATIVE: Generated and saved new global event: {rumor_tagged}")
                    # Notify player of the new rumor in-game
                    send_to_pipe(f"NOTIFY: [WORLD EVENT] {rumor_text}")
                    return rumor_tagged
                else:
                    logging.warning(f"Could not find world_events.txt at {world_events_path}")
            except Exception as e:
                logging.error(f"Error saving global event rumor: {e}")
    return None

@app.route('/synthesize', methods=['POST'])
def manual_synthesize():
    """Manual trigger for global narrative synthesis."""
    # Run synchronously for the manual trigger so we can return the result
    rumor = generate_global_narrative_thread()
    if rumor:
        return jsonify({"status": "ok", "rumor": rumor})
    else:
        return jsonify({"status": "error", "message": "Failed to generate rumor or not enough events (need 5)."}), 400


@app.route('/events', methods=['GET', 'POST'])
def list_events():
    logging.info(f"ROUTE: /events [{request.method}]")

    """Return only synthesized [RUMOR:] entries from world_events.txt.
    Left list: '1. First few words...' — no # symbol (avoids MyGUI color-tag parsing).
    Right panel: full formatted card for the selected rumor.
    """
    world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
    rumors = []

    if os.path.exists(world_events_path):
        try:
            with open(world_events_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            rumor_count = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                # Find [RUMOR: ...] anywhere in the line to skip over new date tags
                match = re.search(r'\[RUMOR:\s*(.*?)\]', stripped)
                if not match:
                    continue

                rumor_count += 1
                inner = match.group(1).strip()

                # Build a safe label: "N. first 7 words..." with no special chars
                words = inner.split()
                short = " ".join(words[:7]) + ("..." if len(words) > 7 else "")
                label = f"{rumor_count}. {short}"
                rumors.append({"id": str(i + 1), "title": label[:80], "content": stripped, "inner": inner})

        except Exception as e:
            logging.error(f"Error reading world_events.txt: {e}")

    formatted = "--- DYNAMIC WORLD RUMORS ---\n" + "\n".join(r["content"] for r in rumors) if rumors else "(No rumors yet. Use 'Synthesize Rumors' to generate some.)"
    return jsonify({"status": "ok", "text": formatted, "events": rumors})


@app.route('/events/content', methods=['POST'])
def events_content():
    """Return formatted multi-line detail text for a selected world event entry.
    The right panel (SetEventsText) splits on newlines, so each line becomes a row.
    """
    data = request.json or {}
    line_id = data.get("day", "")
    
    # Only use campaign-specific events
    world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
        
    try:
        line_num = int(line_id) - 1  # id is 1-indexed line number
        with open(world_events_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if 0 <= line_num < len(lines):
            raw = lines[line_num].strip()
            match = re.search(r'\[RUMOR:\s*(.*?)\]', raw)
            if match:
                # Extract plain text from the capture group
                inner = match.group(1).strip()
                import textwrap
                wrapped = textwrap.wrap(inner, width=76)
                card_lines = [
                    "=" * 38,
                    "  WORLD RUMOR",
                    "=" * 38,
                    "",
                ] + wrapped + [
                    "",
                    "(Synthesized from recent world events)"
                ]
                return jsonify({"status": "ok", "text": "\n".join(card_lines)})
    except Exception as e:
        logging.error(f"events/content error: {e}")
    return jsonify({"status": "error", "text": "Entry not found."}), 404

@app.route('/context', methods=['POST'])
def update_context():
    global PLAYER_CONTEXT, LAST_NPC_NAME
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    
    # Process and deduplicate world events
    # SKIP processing if the game is paused or at speed 0 to prevent loops
    is_paused = data.get("is_paused", False)
    game_speed = data.get("gamespeed", 1.0)
    
    if not is_paused and game_speed > 0.05:
        new_events = data.get("events", [])
        for e in new_events:
            record_event_to_history(
                e.get("type", "EVENT"),
                e.get("actor", "Unknown"),
                e.get("target", "None"),
                e.get("msg", ""),
                actor_faction=e.get("actor_faction", "None"),
                target_faction=e.get("target_faction", "None")
            )
    elif is_paused:
        # Check if we should log at least once that the world is paused?
        # No, better to keep it clean.
        pass

    if data.get("type") == "player":
        prev_paused = PLAYER_CONTEXT.get("is_paused")
        PLAYER_CONTEXT = data
        if prev_paused != data.get("is_paused"):
             logging.info(f"CONTEXT: Player pause state changed to {data.get('is_paused')} (Speed: {data.get('gamespeed')})")
    else:
        name = data.get("name")
        if name:
            LIVE_CONTEXTS[name] = data
            LAST_NPC_NAME = name
    return jsonify({"status": "ok"})


@app.route('/context', methods=['GET'])
def get_context():
    npc_ctx = LIVE_CONTEXTS.get(LAST_NPC_NAME) if LAST_NPC_NAME else {}
    return jsonify({
        "status": "ok",
        "campaign": ACTIVE_CAMPAIGN,
        "npc": npc_ctx,
        "player": PLAYER_CONTEXT,
        "synthesis": SYNTHESIS_STATUS
    })

@app.route('/settings', methods=['GET', 'POST'])
def settings_endpoint():
    global CURRENT_MODEL_KEY
    logging.info(f"ROUTE: /settings [{request.method}]")
    load_configs()

    # ---------- READ (GET or POST with no body) ----------
    data = None
    if request.method == 'POST':
        try:
            data = request.get_json(silent=True)
        except:
            data = None

    if not data:
        # The C++ WelcomeWindow calls POST /settings with empty body to fetch config.
        # The visual_debugger calls GET /models. Both need the same response.
        settings = load_settings()
        r, t, y = get_config_radii()
        campaigns = [d for d in os.listdir(CAMPAIGNS_DIR) if os.path.isdir(os.path.join(CAMPAIGNS_DIR, d))] if os.path.exists(CAMPAIGNS_DIR) else []
        
        # Grouped map for dropdowns: Provider -> [Models]
        mbp = {}
        for k, v in MODELS_CONFIG.items():
            p = v.get("provider", "unknown")
            if p not in mbp: mbp[p] = []
            mbp[p].append(k)
        
        # Determine current provider
        curr_prov = MODELS_CONFIG.get(CURRENT_MODEL_KEY, {}).get("provider", "unknown")

        return jsonify({
            "status": "ok",
            "models": mbp,        # C++ dropdowns loop uses this
            "all_models": MODELS_CONFIG, # C++ initialization lookup
            "providers": list(PROVIDERS_CONFIG.keys()),
            "current": CURRENT_MODEL_KEY,
            "current_provider": curr_prov,
            "campaigns": campaigns,
            "current_campaign": ACTIVE_CAMPAIGN,
            "enable_ambient": settings.get("enable_ambient", True),
            "ambient_timer": settings.get("radiant_delay", 240),
            "synthesis_timer": settings.get("synthesis_interval_minutes", 15),
            "global_events_count": settings.get("global_events_count", 5),
            "dialogue_speed": settings.get("dialogue_speed_seconds", 5),
            "bubble_life": settings.get("bubble_life", 5),
            "radii": {
                "radiant": settings.get("radiant_range", r),
                "talk": settings.get("talk_radius", t),
                "yell": settings.get("yell_radius", y)
            },
            "language": settings.get("language", "English"),
            "supported_languages": list(LOCALIZATION_CONFIG.keys()),
            "ui_translation": LOCALIZATION_CONFIG.get(settings.get("language", "English"), {})
        })

    # ---------- WRITE (POST with JSON body) ----------
    logging.info(f"Received settings update request: {json.dumps(data)}")
    changes = {}

    new_model = data.get("current_model")
    if new_model and new_model in MODELS_CONFIG:
        CURRENT_MODEL_KEY = new_model
        changes["current_model"] = CURRENT_MODEL_KEY
        logging.info(f"Model switched to: {CURRENT_MODEL_KEY}")

    enable_ambient = data.get("enable_ambient")
    if enable_ambient is not None:
        changes["enable_ambient"] = enable_ambient
        send_to_pipe(f"SET_CONFIG: g_enableAmbient: {'1' if enable_ambient else '0'}")
        logging.info(f"Ambient enabled set to: {enable_ambient}")

    ambient_timer = data.get("ambient_timer")
    if ambient_timer is not None:
        val = int(ambient_timer)
        changes["radiant_delay"] = val
        send_to_pipe(f"SET_CONFIG: g_ambientIntervalSeconds: {val}")
        logging.info(f"Radiant delay set to: {val}")

    radii = data.get("radii")
    if radii:
        r = radii.get("radiant")
        t = radii.get("talk")
        y = radii.get("yell")
        if r is not None:
            send_to_pipe(f"SET_CONFIG: g_radiantRange: {r}")
        if t is not None:
            send_to_pipe(f"SET_CONFIG: g_proximityRadius: {t}")
        if y is not None:
            send_to_pipe(f"SET_CONFIG: g_yellRadius: {y}")
        changes["radii"] = radii

    min_rel = data.get("min_faction_relation")
    if min_rel is not None:
        send_to_pipe(f"SET_CONFIG: g_minFactionRelation: {min_rel}")
        changes["min_faction_relation"] = min_rel

    lang = data.get("language")
    if lang is not None:
        changes["language"] = lang
        logging.info(f"Language set to: {lang}")

    max_rel = data.get("max_faction_relation")
    if max_rel is not None:
        send_to_pipe(f"SET_CONFIG: g_maxFactionRelation: {max_rel}")
        changes["max_faction_relation"] = max_rel

    ge_count = data.get("global_events_count")
    if ge_count is not None:
        try:
            val = int(ge_count)
            changes["global_events_count"] = val
            logging.info(f"Global events count set to: {val}")
        except: pass

    syn_timer = data.get("synthesis_timer")
    if syn_timer is not None:
        try:
            val = int(syn_timer)
            changes["synthesis_interval_minutes"] = val
            logging.info(f"Synthesis timer set to: {val} minutes")
        except: pass

    diag_speed = data.get("dialogue_speed")
    if diag_speed is not None:
        try:
            val = int(diag_speed)
            changes["dialogue_speed_seconds"] = val
            send_to_pipe(f"SET_CONFIG: g_dialogueSpeedSeconds: {val}")
            logging.info(f"Dialogue speed set to: {val} seconds")
        except: pass

    bubble_life = data.get("bubble_life")
    if bubble_life is not None:
        try:
            val = float(bubble_life)
            changes["bubble_life"] = val
            send_to_pipe(f"SET_CONFIG: g_speechBubbleLife: {val}")
            logging.info(f"Bubble life set to: {val} seconds")
        except: pass

    if changes:
        save_settings(changes)

    campaign = data.get("current_campaign")
    if campaign:
        if switch_campaign(campaign):
            changes["current_campaign"] = ACTIVE_CAMPAIGN
            logging.info(f"Campaign switched to: {ACTIVE_CAMPAIGN}")

    if changes:
        save_settings(changes)
        logging.info(f"Successfully saved {len(changes)} setting changes.")
        return jsonify({"status": "ok", **changes})

    return jsonify({"status": "error", "message": "No valid settings provided"}), 400

@app.route('/campaigns/list', methods=['GET'])
def list_campaigns_route():
    logging.info("ROUTE: /campaigns/list [GET]")
    if not os.path.exists(CAMPAIGNS_DIR):
        os.makedirs(CAMPAIGNS_DIR)
    
    # Ensure Default exists
    d_dir = os.path.join(CAMPAIGNS_DIR, "Default")
    if not os.path.exists(d_dir): os.makedirs(d_dir)
        
    camps = [d for d in os.listdir(CAMPAIGNS_DIR) if os.path.isdir(os.path.join(CAMPAIGNS_DIR, d))]
    return jsonify({"status": "ok", "campaigns": camps, "current": ACTIVE_CAMPAIGN})

@app.route('/campaigns/create', methods=['POST'])
def create_campaign_route():
    logging.info("ROUTE: /campaigns/create [POST]")
    data = request.json
    name = data.get("name")
    if not name: return jsonify({"status": "error", "message": "Missing name"}), 400
    
    # Sanitize
    safe_name = "".join([c for c in name if c.isalnum() or c in (' ', '_', '-')]).strip()
    if not safe_name: return jsonify({"status": "error", "message": "Invalid name"}), 400
    
    cdir = os.path.join(CAMPAIGNS_DIR, safe_name)
    if os.path.exists(cdir):
        return jsonify({"status": "error", "message": "Campaign already exists"}), 400
        
    os.makedirs(cdir)
    ensure_campaign_seeded(cdir)
    
    # Automatically switch to the new campaign
    switch_campaign(safe_name)
            
    logging.info(f"CAMPAIGN: Created and switched to new campaign '{safe_name}'")
    return jsonify({"status": "ok", "name": safe_name, "current": ACTIVE_CAMPAIGN})

@app.route('/campaigns/switch', methods=['POST'])
def switch_campaign_route():
    logging.info("ROUTE: /campaigns/switch [POST]")
    data = request.json
    name = data.get("name")
    if not name: return jsonify({"status": "error", "message": "Missing name"}), 400
    if switch_campaign(name):
        return jsonify({"status": "ok", "current": ACTIVE_CAMPAIGN})
    return jsonify({"status": "error", "message": "Campaign not found"}), 404

@app.route('/campaigns/cull', methods=['POST'])
def cull_campaign_route():
    logging.info("ROUTE: /campaigns/cull [POST]")
    
    current_day = int(PLAYER_CONTEXT.get("day", 0))
    current_hour = int(PLAYER_CONTEXT.get("hour", 0))
    current_min = int(PLAYER_CONTEXT.get("minute", 0))

    cdir = get_campaign_dir()
    logging.info(f"CULL: Starting cull for [Day {current_day}, {current_hour:02d}:{current_min:02d}] in {cdir}")

    # 1. Cull NPC JSONs
    char_dir = os.path.join(cdir, "characters")
    if os.path.exists(char_dir):
        for f in os.listdir(char_dir):
            if f.endswith(".json"):
                fpath = os.path.join(char_dir, f)
                try:
                    with open(fpath, "r", encoding="utf-8-sig") as fh:
                        cdata = json.load(fh)
                    
                    history = cdata.get("ConversationHistory", [])
                    new_history = [l for l in history if not is_future_timestamp(l, current_day, current_hour, current_min)]
                    
                    if len(new_history) != len(history):
                        cdata["ConversationHistory"] = new_history
                        with open(fpath, "w", encoding="utf-8") as fw:
                            json.dump(cdata, fw, indent=2)
                        logging.info(f"CULL: Culled {len(history) - len(new_history)} lines from {f}")
                except: pass

    # 2. Cull event_history.json
    ev_history_path = os.path.join(cdir, "event_history.json")
    if os.path.exists(ev_history_path):
        try:
            with open(ev_history_path, "r", encoding="utf-8") as fh:
                ev_data = json.load(fh)
            new_ev_data = [l for l in ev_data if not is_future_timestamp(l, current_day, current_hour, current_min)]
            if len(new_ev_data) != len(ev_data):
                with open(ev_history_path, "w", encoding="utf-8") as fw:
                    json.dump(new_ev_data, fw, indent=2)
                global EVENT_HISTORY
                EVENT_HISTORY = new_ev_data
                logging.info(f"CULL: Culled {len(ev_data) - len(new_ev_data)} events from event_history.json")
        except: pass

    # 3. Cull world_events.txt (rumors)
    world_events_path = os.path.join(cdir, "world_events.txt")
    if os.path.exists(world_events_path):
        try:
            with open(world_events_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            new_lines = [l for l in lines if not is_future_timestamp(l, current_day, current_hour, current_min)]
            if len(new_lines) != len(lines):
                with open(world_events_path, "w", encoding="utf-8") as fw:
                    fw.writelines(new_lines)
                logging.info(f"CULL: Culled {len(lines) - len(new_lines)} lines from world_events.txt")
        except: pass

    return jsonify({"status": "ok"})

def switch_campaign(name):
    global ACTIVE_CAMPAIGN, LIVE_CONTEXTS, EVENT_HISTORY
    cdir = os.path.join(CAMPAIGNS_DIR, name)
    if os.path.exists(cdir):
        ACTIVE_CAMPAIGN = name
        save_settings({"current_campaign": name})  # Persist across restarts
        # Clear volatile state
        LIVE_CONTEXTS.clear()
        EVENT_HISTORY = []
        load_campaign_config()
        update_world_index() # Re-scan save for new campaign context
        return True
    return False

@app.route('/models', methods=['GET'])
def get_models():
    """Alias for GET /settings — used by the visual debugger."""
    load_configs()
    settings = load_settings()
    return jsonify({
        "status": "ok",
        "models": MODELS_CONFIG,
        "providers": list(PROVIDERS_CONFIG.keys()),
        "current": CURRENT_MODEL_KEY,
        "enable_ambient": settings.get("enable_ambient", True),
    })


@app.route('/history', methods=['POST'])
def get_history():
    logging.info("ROUTE: /history [POST]")
    data = request.json or {}
    
    # Accept both 'npc' (from Library) and 'name' (from older calls)
    npc_name = data.get('npc', data.get('name', 'Someone'))
    
    logging.info(f"HISTORY: Request for {npc_name}")
    
    # CRITICAL: Clean the name from pipes (serial IDs) before any lookup.
    clean_npc_name = npc_name.split('|')[0] if '|' in npc_name else npc_name
    context = data.get('context', '')
    
    # DIRECT FILE LOAD
    char_data = None
    safe_fn = "".join([c for c in str(clean_npc_name) if c.isalnum() or c in (' ', '_', '-')]).strip()
    direct_path = os.path.join(CHARACTERS_DIR, f"{safe_fn}.json")
    logging.info(f"HISTORY: Trying direct load for {clean_npc_name} from: {direct_path}")
    
    if os.path.exists(direct_path):
        try:
            with open(direct_path, "r", encoding="utf-8-sig") as f:
                char_data = json.load(f)
            logging.info(f"HISTORY: Direct load SUCCESS for {clean_npc_name}")
        except Exception as e:
            logging.error(f"HISTORY: Direct load failed for {clean_npc_name}: {e}")
            char_data = None
    
    # Fallback to standard resolution if direct load didn't work
    if not char_data:
        logging.info(f"HISTORY: Falling back to get_character_data for {clean_npc_name}")
        char_data = get_character_data(clean_npc_name, context)
    
    # Schema migration for legacy files
    if "ConversationHistory" not in char_data: char_data["ConversationHistory"] = []
    if "Race" not in char_data: char_data["Race"] = "Unknown"
    if "Faction" not in char_data: char_data["Faction"] = "Unknown"
    
    # Return full history as requested
    history = char_data.get('ConversationHistory', [])
    
    import textwrap
    def _wrap(text):
        if not text: return ""
        paragraphs = text.split('\n')
        wrapped = []
        for p in paragraphs:
            if not p.strip():
                wrapped.append("")
                continue
            wrapped.extend(textwrap.wrap(p, width=110))
        return "\n".join(wrapped)
        
    lines = []
    lines.append(f"--- PROFILE: {char_data.get('Name', clean_npc_name)} ---")
    lines.append(f"Faction: {char_data.get('Faction', 'Unknown')} | Race: {char_data.get('Race', 'Unknown')}")
    lines.append("-" * 30)
    lines.append("PERSONALITY:")
    lines.append(_wrap(char_data.get('Personality', 'Unknown')))
    lines.append("")
    lines.append("BACKSTORY:")
    lines.append(_wrap(char_data.get('Backstory', 'Unknown')))
    lines.append("-" * 30)
    lines.append(f"CONVERSATION LOG (Showing last 250 of {len(history)} lines):")
    if history:
        # Limit display to HISTORY_MAX_LINES to prevent UI freeze
        trimmed_history = history[-HISTORY_MAX_LINES:]
        for log_line in trimmed_history:
            lines.append(_wrap(log_line))
    else:
        lines.append("(No history recorded)")
        
    formatted_output = "\n".join(lines)
    
    logging.info(f"HISTORY: Returning formatted report for {clean_npc_name} ({len(history)} lines)")
    return jsonify({
        "status": "ok",
        "text": formatted_output
    })

@app.route('/characters', methods=['GET', 'POST'])
def list_characters():
    data = request.json or {}
    sort_mode = data.get("sort", "alphabetical") # alphabetical or latest
    
    settings = load_settings()
    favorites = settings.get("favorites", [])

    logging.info(f"Scanning for characters in: {CHARACTERS_DIR} (Sort: {sort_mode})")
    if not os.path.exists(CHARACTERS_DIR):
        return jsonify({"status": "ok", "characters": ""})
    
    npc_list = []
    for f in os.listdir(CHARACTERS_DIR):
        if not f.endswith('.json'):
            continue
        storage_id = f.replace('.json', '')
        try:
            fpath = os.path.join(CHARACTERS_DIR, f)
            mtime = os.path.getmtime(fpath)
            with open(fpath, "r", encoding="utf-8-sig") as fh:
                cdata = json.load(fh)
            display = cdata.get('Name', storage_id)
            
            npc_list.append({
                "display": display,
                "sid": storage_id,
                "mtime": mtime,
                "is_fav": storage_id in favorites
            })
        except:
            npc_list.append({
                "display": storage_id,
                "sid": storage_id,
                "mtime": 0,
                "is_fav": storage_id in favorites
            })

    # Deduplicate by display name (keeping original logic preference for underscores)
    unique_npcs = {}
    for n in npc_list:
        name = n["display"]
        if name not in unique_npcs:
            unique_npcs[name] = n
        else:
            # If current has underscore, prefer it
            if '_' in n["sid"]:
                unique_npcs[name] = n

    final_list = list(unique_npcs.values())

    # Sorting logic
    if sort_mode == "latest":
        final_list.sort(key=lambda x: x["mtime"], reverse=True)
    else:
        final_list.sort(key=lambda x: x["display"].lower())

    # Favorites always on top
    favs = [n for n in final_list if n["is_fav"]]
    others = [n for n in final_list if not n["is_fav"]]
    
    sorted_npcs = favs + others
    
    names = [f"{n['display']}|{n['sid']}" for n in sorted_npcs]
    
    return jsonify({
        "status": "ok",
        "characters": ",".join(names),
        "names": ",".join(names),
        "favorites": favorites
    })

@app.route('/favorite', methods=['POST'])
def toggle_favorite():
    data = request.json or {}
    sid = data.get("sid")
    if not sid:
        return jsonify({"status": "error"}), 400
    
    settings = load_settings()
    favorites = settings.get("favorites", [])
    
    if sid in favorites:
        favorites.remove(sid)
        status = "removed"
    else:
        favorites.append(sid)
        status = "added"
    
    settings["favorites"] = favorites
    save_settings(settings)
    
    return jsonify({"status": "ok", "state": status})
@app.route('/player_profile', methods=['GET', 'POST'])
def player_profile_route():
    # Robust handling for C++ client sending empty JSON body
    data = None
    if request.is_json:
        try:
            data = request.get_json(silent=True)
        except:
            pass
    
    # If GET, or POST with no usable JSON (loading call)
    if request.method == 'GET' or not data:
        logging.info("PROMPT: Loading player profile (GUI request).")
        bio = load_prompt_component("character_bio.txt", "A mysterious drifter.")
        faction = load_prompt_component("player_faction_description.txt", "")
        return jsonify({
            "status": "ok",
            "character_bio": bio,
            "player_faction": faction
        })
    else:
        # Save
        bio = data.get("character_bio")
        faction = data.get("player_faction")
        
        cdir = get_campaign_dir()
        if bio is not None:
            with open(os.path.join(cdir, "character_bio.txt"), "w", encoding="utf-8") as f:
                f.write(bio)
        if faction is not None:
            with open(os.path.join(cdir, "player_faction_description.txt"), "w", encoding="utf-8") as f:
                f.write(faction)
        
        logging.info("PROMPT: Player profile updated via UI.")
        return jsonify({"status": "ok"})

@app.route('/test_connection', methods=['POST'])
def test_connection():
    logging.info("Testing LLM connection...")
    test_prompt = [{"role": "user", "content": "You are a Kenshi NPC. Say 'Connection Successful!' in a very short way."}]
    try:
        response = call_llm(test_prompt, max_tokens=20)
        if response:
            logging.info(f"Test Successful: {response}")
            return f"NOTIFY: Connection Successful! AI says: {response}", 200
        else:
            return "NOTIFY: ERROR: No response from AI. Check your API key and Provider settings.", 200
    except Exception as e:
        logging.error(f"Test Failed: {e}")
        return f"NOTIFY: ERROR: {str(e)}", 200

@app.route('/reset', methods=['POST'])
def reset_server():
    logging.info("Resetting server state...")
    try:
        LIVE_CONTEXTS.clear()
        load_configs()
        build_world_index() 
        logging.info("Server reset complete (Cache cleared, configs reloaded).")
        return "NOTIFY: Server Reset Complete (Identity cache cleared and configs reloaded).", 200
    except Exception as e:
        return f"NOTIFY: Reset failed: {str(e)}", 200

def synthesis_loop():
    """Background loop to periodically synthesize world rumors."""
    logging.info("NARRATIVE: Synthesis background loop started.")
    elapsed_minutes = 0
    while True:
        try:
            settings = load_settings()
            interval = settings.get("synthesis_interval_minutes", 60)
            if interval < 1: interval = 1 # Safety
            
            SYNTHESIS_STATUS["interval"] = interval
            
            # If interval was shortened below current elapsed, trigger now
            if elapsed_minutes >= interval:
                logging.info(f"NARRATIVE: Interval shortened ({interval}m). Triggering synthesis.")
                generate_global_narrative_thread()
                elapsed_minutes = 0
                SYNTHESIS_STATUS["elapsed"] = 0
                continue

            # Sleep in smaller chunks to be responsive to game state changes
            for _ in range(6): # Check pulse 6 times per minute (every 10s)
                time.sleep(10)
                speed = PLAYER_CONTEXT.get("gamespeed", 1.0)
                is_paused = PLAYER_CONTEXT.get("is_paused", False)
            
            # After ~60s of total time, check if we progressed
            speed = PLAYER_CONTEXT.get("gamespeed", 1.0)
            
            if speed > 0.1:
                elapsed_minutes += 1
                SYNTHESIS_STATUS["elapsed"] = elapsed_minutes
                if elapsed_minutes % 10 == 0:
                    logging.info(f"NARRATIVE: Timer progress: {elapsed_minutes}/{interval} minutes.")
            
            if elapsed_minutes >= interval:
                logging.info(f"NARRATIVE: Timer reached ({interval}m). Triggering periodic synthesis.")
                generate_global_narrative_thread()
                elapsed_minutes = 0
                SYNTHESIS_STATUS["elapsed"] = 0
                    
        except Exception as e:
            logging.error(f"Error in synthesis loop: {e}")
            time.sleep(60)

# Start synthesis thread
threading.Thread(target=synthesis_loop, daemon=True).start()

def player2_ping_loop():
    """Periodically pings player2 server and refreshes p2Key if it is the active provider."""
    global PLAYER2_SESSION_KEY
    # Use debug for the thread start to stay out of the way for non-p2 users
    logging.debug("HEALTH: Player2 background thread initialized.")
    game_id = "019c93fc-7a93-7ac4-8c6e-df0fd09bec01"
    
    while True:
        try:
            model_entry = MODELS_CONFIG.get(CURRENT_MODEL_KEY)
            if model_entry and model_entry.get("provider") == "player2":
                # 1. Quick Start: Attempt to fetch fresh p2Key from local Player2 App
                # ONLY if we don't already have one (Beginning of session/usage)
                if not PLAYER2_SESSION_KEY:
                    try:
                        auth_url = f"http://localhost:4315/v1/login/web/{game_id}"
                        auth_resp = requests.post(auth_url, timeout=5)
                        if auth_resp.status_code == 200:
                            new_key = auth_resp.json().get("p2Key")
                            if new_key:
                                PLAYER2_SESSION_KEY = new_key
                                logging.info("HEALTH: Player2 session authorized at startup.")
                    except Exception as e:
                        # App might not be running or not logged in; silently fall back
                        pass


                # 2. Ping /health as a health check
                provider_config = PROVIDERS_CONFIG.get("player2")
                if provider_config:
                    base_url = provider_config.get("base_url").rstrip("/")
                    try:
                        # Use player2-game-key header and Authorization for health check
                        h = {
                            "player2-game-key": game_id,
                            "Authorization": f"Bearer {PLAYER2_SESSION_KEY}" if PLAYER2_SESSION_KEY else ""
                        }
                        resp = requests.get(f"{base_url}/health", headers=h, timeout=5)
                        if resp.status_code == 200:
                            logging.debug("HEALTH: Player2 server is UP")
                        else:
                            logging.warning(f"HEALTH: Player2 server returned status {resp.status_code}")
                    except Exception as e:
                        logging.error(f"HEALTH: Player2 server is DOWN or unreachable: {e}")
            
        except Exception as e:
            logging.error(f"Error in player2 background thread: {e}")
        
        time.sleep(60)

# Start player2 ping thread
threading.Thread(target=player2_ping_loop, daemon=True).start()

def monitor_kenshi_process():
    """Background thread that monitors the parent process (Kenshi) and exits if it's gone."""
    try:
        ppid = os.getppid()
        if ppid <= 1:
            logging.info("SYSTEM: Parent PID is 0 or 1, skipping auto-shutdown monitor.")
            return
            
        logging.info(f"SYSTEM: Monitoring parent process (PID {ppid}) for auto-shutdown.")
        
        # Windows constants
        PROCESS_QUERY_INFORMATION = 0x0400
        STILL_ACTIVE = 259
        
        # Use ctypes for more reliable process checking on Windows
        kernel32 = ctypes.windll.kernel32
        
        while True:
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, ppid)
            if not handle:
                # If we can't open it, the process is likely gone
                logging.info(f"SYSTEM: Parent Kenshi process (PID {ppid}) no longer found. Shutting down server.")
                os._exit(0)
                
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                if exit_code.value != STILL_ACTIVE:
                    kernel32.CloseHandle(handle)
                    logging.info(f"SYSTEM: Parent Kenshi process (PID {ppid}) has exited. Shutting down server.")
                    os._exit(0)
            else:
                # GetExitCodeProcess failed, might be gone
                kernel32.CloseHandle(handle)
                logging.info(f"SYSTEM: Failed to query parent process state. Assuming it closed. Shutting down server.")
                os._exit(0)
            
            kernel32.CloseHandle(handle)
            time.sleep(5) 
            
    except Exception as e:
        logging.error(f"SYSTEM: Error in kenshi process monitor: {e}")

# Start Kenshi monitor thread
threading.Thread(target=monitor_kenshi_process, daemon=True).start()

if __name__ == '__main__':
    logging.info("Kenshi LLM Server Starting on port 5000...")
    # Enable threaded=True to handle multiple simultaneous requests (polling + settings)
    app.run(host='127.0.0.1', port=5000, threaded=True)
