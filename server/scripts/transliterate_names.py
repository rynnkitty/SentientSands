"""
OFFLINE TOOL — Korean transliteration of the NPC name pool (config/names.json).

* This script is NOT part of the runtime server. kenshi_llm_server.py never
  imports it. It is run manually, once, to convert the English name pool to
  Korean transliterations via an OpenAI-compatible LLM endpoint
  (providers.json / models.json, model key "gemini-3-flash").

* Safe to re-run: entries that already contain Hangul are skipped, and a
  checkpoint file lets an interrupted run resume without re-paying finished
  batches.

* The original English pool is preserved as config/names.json.en.bak
  (created once, never overwritten). To revert the localization:
      copy names.json.en.bak -> names.json

Usage:
    python.exe scripts/transliterate_names.py            # full run
    python.exe scripts/transliterate_names.py --dry-run  # plan only, no API calls

Rules enforced on output names (mirrors server-side FORBIDDEN_NAME_CHARS):
  - Hangul syllables only, plus internal spaces/hyphens
  - never empty, no '(' ')' '@' ':' '|' (these break event/speaker parsing)
  - globally injective mapping (no two source names collapse to one Korean name)
"""
import json
import os
import re
import sys
import time

# Corporate-proxy TLS: optional, offline tooling only (runtime server is untouched).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.path.join(SERVER_DIR, "config")
NAMES_PATH = os.path.join(CONFIG_DIR, "names.json")
BACKUP_PATH = os.path.join(CONFIG_DIR, "names.json.en.bak")
CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "translit_checkpoint.json")

BATCH_SIZE = 100
MAX_BATCH_RETRIES = 3
FORBIDDEN_CHARS = "(@:|)"
# Hangul words separated by single spaces or hyphens, nothing else.
VALID_KO_RE = re.compile(r"^[가-힣]+(?:[ \-][가-힣]+)*$")

SYSTEM_PROMPT = (
    "You are localizing character names for the Korean edition of the video game "
    "Kenshi (post-apocalyptic desert world; gritty, Japanese/wasteland-flavoured lore).\n"
    "Task: transliterate each given English/romanized name into Korean Hangul.\n"
    "Rules:\n"
    "1. Prioritize how the name SOUNDS and the game's mood over strict Korean "
    "transcription standards. Examples: Takao -> 타카오, Kaelen -> 카엘렌, "
    "Jarak -> 자라크, O'Dreg -> 오드레그.\n"
    "2. Names beginning with 'The' keep a transliterated article: The Wall -> 더 월.\n"
    "3. Hyphenated names keep the hyphen: Luk-Luk -> 루크-루크.\n"
    "4. Output characters allowed: Hangul syllables, spaces, hyphens ONLY. "
    "No Latin letters, digits, apostrophes, parentheses, '@', ':', '|'.\n"
    "5. Keep results short and name-like (1-3 words).\n"
    "6. Respond with ONLY a JSON object mapping every input name to its "
    "transliteration, e.g. {\"Takao\": \"타카오\"}. Every input must appear "
    "exactly once. No commentary, no markdown fences."
)

VARIANT_PROMPT_SUFFIX = (
    "\n7. IMPORTANT: for each name, the transliterations given in parentheses are "
    "already taken — produce a DIFFERENT natural variant (e.g. alternate vowel "
    "rendering: Kaelen -> 케일런 instead of 카엘렌)."
)


def log(msg):
    print(msg, flush=True)


def load_llm_config():
    with open(os.path.join(CONFIG_DIR, "providers.json"), encoding="utf-8-sig") as f:
        providers = json.load(f)
    with open(os.path.join(CONFIG_DIR, "models.json"), encoding="utf-8-sig") as f:
        models = json.load(f)
    entry = models["gemini-3-flash"]
    prov = providers[entry["provider"]]
    return prov["base_url"].rstrip("/"), prov["api_key"], entry["model"]


def call_llm(base_url, api_key, model, system_prompt, user_prompt):
    last_err = None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        # Generous budget: Gemini 3 spends part of max_tokens on internal
        # "thinking"; too small a cap yields empty/truncated content.
        "max_tokens": 32000,
        "reasoning_effort": "low",
    }
    for attempt in range(3):
        try:
            r = requests.post(
                base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=300,
            )
            if r.status_code == 400 and "reasoning_effort" in payload:
                # endpoint may not know the knob -- drop it and retry
                payload.pop("reasoning_effort")
                continue
            if r.status_code != 200:
                # Never echo request headers/keys; status + trimmed body is enough.
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            choice = r.json()["choices"][0]
            content = choice["message"].get("content") or ""
            if not content.strip():
                raise RuntimeError(
                    f"empty content (finish_reason={choice.get('finish_reason')})")
            return content
        except Exception as e:  # noqa: BLE001 - retry then surface
            last_err = e
            log(f"    ! LLM call failed (attempt {attempt + 1}/3): {e}")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after retries: {last_err}")


def parse_json_object(text):
    text = text.strip()
    # tolerate ```json fences despite instructions
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def has_hangul(s):
    return any("가" <= c <= "힣" for c in s)


def valid_korean_name(s):
    return bool(s) and VALID_KO_RE.match(s) is not None and \
        not any(c in s for c in FORBIDDEN_CHARS)


def transliterate_batch(base_url, api_key, model, names, taken_values, variant_hints=None):
    """Returns (accepted {en: ko}, collisions {en: ko_that_was_taken})."""
    if variant_hints:
        lines = [f"{n} (taken: {', '.join(sorted(variant_hints.get(n) or []))})"
                 if variant_hints.get(n) else n for n in names]
        sys_prompt = SYSTEM_PROMPT + VARIANT_PROMPT_SUFFIX
    else:
        lines = names
        sys_prompt = SYSTEM_PROMPT
    user_prompt = "Transliterate these names:\n" + "\n".join(lines)

    raw = call_llm(base_url, api_key, model, sys_prompt, user_prompt)
    try:
        mapping = parse_json_object(raw)
    except (ValueError, json.JSONDecodeError) as e:
        log(f"    ! unparseable response ({e}); whole batch will be retried")
        log(f"      head of response: {raw[:200]!r}")
        return {}, {}

    accepted = {}
    collisions = {}
    for en in names:
        ko = mapping.get(en)
        if ko is None:
            continue
        ko = str(ko).strip()
        if not valid_korean_name(ko):
            log(f"    ! rejected {en!r} -> {ko!r} (invalid characters/empty)")
            continue
        if ko in taken_values:
            # collision with an already-assigned Korean name -> retried with hints
            log(f"    ~ collision {en!r} -> {ko!r} (already taken)")
            collisions[en] = ko
            continue
        accepted[en] = ko
        taken_values.add(ko)
    return accepted, collisions


def main():
    dry_run = "--dry-run" in sys.argv

    with open(NAMES_PATH, encoding="utf-8-sig") as f:
        pools = json.load(f)

    # 1. one-time backup of the English pool
    if not os.path.exists(BACKUP_PATH):
        if dry_run:
            log(f"[dry-run] would create backup {BACKUP_PATH}")
        else:
            with open(NAMES_PATH, "rb") as src, open(BACKUP_PATH, "wb") as dst:
                dst.write(src.read())
            log(f"Backup created: {BACKUP_PATH}")
    else:
        log(f"Backup already exists (kept as-is): {BACKUP_PATH}")

    # 2. collect unique names still needing transliteration (re-run safe)
    unique_names = []
    seen = set()
    for pool in pools.values():
        for n in pool:
            if n not in seen:
                seen.add(n)
                unique_names.append(n)
    todo = [n for n in unique_names if not has_hangul(n)]
    done_already = len(unique_names) - len(todo)

    # resume from checkpoint if present
    mapping = {}
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH, encoding="utf-8") as f:
            mapping = {k: v for k, v in json.load(f).items() if k in seen}
        log(f"Checkpoint loaded: {len(mapping)} mappings reused")
    todo = [n for n in todo if n not in mapping]

    log(f"Pools: {{{', '.join(f'{k}: {len(v)}' for k, v in pools.items())}}}")
    log(f"Unique names: {len(unique_names)} | already Hangul (skipped): {done_already} "
        f"| from checkpoint: {len(mapping)} | to convert: {len(todo)}")
    if dry_run:
        log("[dry-run] stopping before API calls")
        return 0
    if not todo and not mapping:
        log("Nothing to do.")
        return 0

    base_url, api_key, model = load_llm_config()
    log(f"LLM endpoint OK: model={model} (key not shown)")

    # Seed taken-values with checkpoint results AND any Korean names already in
    # the pools (re-run case) so we never introduce duplicates.
    taken_values = set(mapping.values())
    taken_values.update(n for n in unique_names if has_hangul(n))

    retry_counts = {}
    collision_hints = {}   # en -> set of Korean values it collided with
    failed = []
    stats = {"calls": 0, "retried_names": 0}

    queue = list(todo)
    variant_queue = []
    while queue or variant_queue:
        if queue:
            batch, queue = queue[:BATCH_SIZE], queue[BATCH_SIZE:]
            hints = None
        else:
            # smaller batches for the variant pass: precision over throughput
            batch, variant_queue = variant_queue[:30], variant_queue[30:]
            hints = collision_hints
        stats["calls"] += 1
        log(f"Batch of {len(batch)} (remaining {len(queue) + len(variant_queue)}"
            f"{', variant pass' if hints else ''}) ...")
        accepted, collisions = transliterate_batch(
            base_url, api_key, model, batch, taken_values, variant_hints=hints)
        mapping.update(accepted)

        # checkpoint after every batch
        with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=1)

        for n in [x for x in batch if x not in accepted]:
            retry_counts[n] = retry_counts.get(n, 0) + 1
            if n in collisions:
                collision_hints.setdefault(n, set()).add(collisions[n])
            if retry_counts[n] > MAX_BATCH_RETRIES:
                failed.append(n)
            elif n in collision_hints:
                stats["retried_names"] += 1
                variant_queue.append(n)   # retry with explicit "taken" hints
            else:
                stats["retried_names"] += 1
                queue.append(n)           # plain retry (parse failure etc.)

    # 3. apply mapping (names without a mapping stay English -- still functional)
    new_pools = {}
    for key, pool in pools.items():
        new_pools[key] = [mapping.get(n, n) for n in pool]

    # 4. final validation before write
    problems = []
    for key, pool in new_pools.items():
        if len(pool) != len(pools[key]):
            problems.append(f"{key}: count changed {len(pools[key])} -> {len(pool)}")
        if len(set(pool)) != len(pool):
            problems.append(f"{key}: duplicates introduced")
        for n in pool:
            if not n or not n.strip():
                problems.append(f"{key}: empty name")
            if any(c in n for c in FORBIDDEN_CHARS):
                problems.append(f"{key}: forbidden char in {n!r}")
    if problems:
        log("VALIDATION FAILED -- names.json NOT written:")
        for p in problems:
            log("  - " + p)
        log(f"(checkpoint kept at {CHECKPOINT_PATH} for inspection)")
        return 1

    with open(NAMES_PATH, "w", encoding="utf-8") as f:  # no BOM
        json.dump(new_pools, f, ensure_ascii=False, indent=4)
        f.write("\n")

    if os.path.exists(CHECKPOINT_PATH):
        os.remove(CHECKPOINT_PATH)

    converted = sum(1 for n in unique_names if n in mapping)
    log("--- DONE ---")
    log(f"Unique names: {len(unique_names)} | converted: {converted} | "
        f"already-Hangul skipped: {done_already} | kept English (failed): {len(failed)}")
    log(f"LLM batch calls: {stats['calls']} | re-queued name retries: {stats['retried_names']}")
    if failed:
        log("Names kept in English: " + ", ".join(failed[:50]))
    log(f"Backup: {BACKUP_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
