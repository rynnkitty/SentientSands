# -*- coding: utf-8 -*-
"""
build_faction_lore.py — OFFLINE faction-lore builder (Phase 4, 결정사항 ⑥)
===========================================================================

Standalone utility that crawls faction articles from the official Kenshi Wiki
(kenshi.fandom.com, MediaWiki API) and uses an LLM to distill each article
into a faction_lore.json entry (the schema used by the Faction RAG system).

THIS SCRIPT IS *NOT* PART OF THE GAME RUNTIME.
  - The game server (kenshi_llm_server.py) never imports or runs it.
  - Running it is OPTIONAL: the shipped config/faction_lore.json already
    contains hand-written vanilla faction data. Use this tool only to
    regenerate/extend entries.
  - The LLM refinement step REQUIRES an API key. Without one you can still
    run with --fetch-only to dump raw wiki text for manual editing.
  - Korean transliterated aliases produced by the LLM are written into
    "aliases_ko_unverified" — a human MUST review them before promoting
    them to "aliases" (결정사항 ⑥: 한국어 음차는 사용자 검수).

USAGE (run with the embedded python):
    cd server
    python\\python.exe scripts\\build_faction_lore.py --list
        ... list faction page titles found on the wiki
    python\\python.exe scripts\\build_faction_lore.py --fetch-only -o out\\raw.json
        ... crawl article text only (no LLM, no API key needed)
    python\\python.exe scripts\\build_faction_lore.py --factions "Crab Raiders,Reavers" -o out\\review.json
        ... crawl + LLM-refine the named factions into schema entries

    API key resolution order:
      1) --api-key / --base-url / --model CLI flags
      2) OPENAI_API_KEY / OPENAI_BASE_URL / LORE_MODEL environment variables
      3) server/config/providers.json + models.json (same files the server uses)

REVIEW WORKFLOW:
    The output file is a REVIEW artifact, not live data. Inspect/fix it, then
    either merge entries into config/faction_lore.json or drop the file into
    config/faction_lore.d/<name>.json and call GET /lore/reload.

NETWORK NOTE: behind a TLS-intercepting proxy, install 'truststore'
(python\\python.exe -m pip install truststore) — this script auto-uses it.
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import truststore  # optional: corporate proxy / TLS interception support
    truststore.inject_into_ssl()
except Exception:
    pass

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(SCRIPT_DIR)
WIKI_API = "https://kenshi.fandom.com/api.php"
REQUEST_DELAY_S = 1.0  # be polite to the wiki
USER_AGENT = "SentientSands-FactionLoreBuilder/1.0 (offline modding tool)"

SCHEMA_EXAMPLE = {
    "id": "snake_case_unique_id",
    "name": "Faction Name",
    "aliases": ["English alias 1", "abbreviation"],
    "aliases_ko_unverified": ["한국어 음차 후보 (사람 검수 필요)"],
    "keywords": ["leader name", "home region", "ideology", "notable terms"],
    "source_mod": "vanilla",
    "is_major": False,
    "leader": "Leader Name or null",
    "summary": "One-line summary used for semantic embedding.",
    "lore": "300-600 character lore block injected verbatim into prompts: leader, home, ideology, attitude toward outsiders/the player, relations.",
    "relations": {"Other Faction": "hostile/allied/neutral"},
    "locations": ["Town 1", "Region 1"],
    "recruit_resistance": "low/medium/high",
}

REFINE_PROMPT = """You are building a lore database for a Kenshi game mod.
From the wiki article below, produce EXACTLY ONE JSON object following this schema
(no markdown fences, no commentary, JSON only):

{schema}

Rules:
- 'lore' must be 300-600 characters of in-world knowledge an NPC could plausibly know
  (leader, home territory, ideology, how they treat strangers, relations). No wiki meta-talk.
- 'aliases' = common English alternative names/abbreviations actually used for the faction.
- 'aliases_ko_unverified' = your best-guess Korean transliterations of the faction name
  (these will be human-reviewed; guess freely but keep them plausible).
- 'leader' = null if the article names none.
- 'is_major' = true only for the great powers (Holy Nation, United Cities, Shek Kingdom, hives, skeletons).
- Do not invent facts that contradict the article.

ARTICLE TITLE: {title}

ARTICLE TEXT:
{text}
"""


def wiki_get(params):
    params = dict(params)
    params.setdefault("format", "json")
    r = requests.get(WIKI_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.json()


def list_faction_pages():
    """Page titles in Category:Factions (paged)."""
    titles, cont = [], {}
    while True:
        data = wiki_get({
            "action": "query", "list": "categorymembers",
            "cmtitle": "Category:Factions", "cmlimit": "200", **cont,
        })
        for m in data.get("query", {}).get("categorymembers", []):
            if m.get("ns") == 0:  # articles only, skip sub-categories/files
                titles.append(m["title"])
        cont = data.get("continue") or {}
        if not cont:
            break
        time.sleep(REQUEST_DELAY_S)
    return titles


def fetch_article_text(title):
    """Plain-text extract of one article."""
    data = wiki_get({
        "action": "query", "prop": "extracts", "explaintext": "1",
        "redirects": "1", "titles": title,
    })
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        text = page.get("extract") or ""
        if text:
            return re.sub(r"\n{3,}", "\n\n", text).strip()
    return ""


def resolve_llm_config(args):
    """CLI flags > env vars > server config files. Returns (base_url, api_key, model) or None."""
    base_url = args.base_url or os.environ.get("OPENAI_BASE_URL")
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    model = args.model or os.environ.get("LORE_MODEL")
    if base_url and api_key and model:
        return base_url, api_key, model
    # Fall back to the server's own provider config
    try:
        with open(os.path.join(SERVER_DIR, "config", "providers.json"), "r", encoding="utf-8-sig") as f:
            providers = json.load(f)
        with open(os.path.join(SERVER_DIR, "config", "models.json"), "r", encoding="utf-8-sig") as f:
            models = json.load(f)
        model = model or args.model
        if not model:
            # pick the first model that has a provider with an api key
            for mname, minfo in models.items():
                pname = minfo.get("provider") if isinstance(minfo, dict) else minfo
                p = providers.get(pname) or {}
                if p.get("api_key") and p.get("base_url"):
                    return base_url or p["base_url"], api_key or p["api_key"], mname
        else:
            minfo = models.get(model) or {}
            pname = minfo.get("provider") if isinstance(minfo, dict) else minfo
            p = providers.get(pname) or {}
            if p.get("api_key") and p.get("base_url"):
                return base_url or p["base_url"], api_key or p["api_key"], model
    except Exception as e:
        print(f"  (provider config fallback failed: {e})")
    return None


def call_llm(base_url, api_key, model, prompt):
    url = base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, timeout=120, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    }, json={
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1200,
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def extract_json_object(text):
    """Tolerates code fences / leading prose around the JSON object."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in LLM output")
    return json.loads(m.group(0))


def refine_article(llm_cfg, title, text):
    base_url, api_key, model = llm_cfg
    prompt = REFINE_PROMPT.format(
        schema=json.dumps(SCHEMA_EXAMPLE, ensure_ascii=False, indent=2),
        title=title,
        text=text[:12000],  # keep the request bounded
    )
    raw = call_llm(base_url, api_key, model, prompt)
    entry = extract_json_object(raw)
    entry.setdefault("source_mod", "vanilla")
    entry.setdefault("_review_note", "generated by build_faction_lore.py — verify facts and Korean aliases before use")
    return entry


def main():
    ap = argparse.ArgumentParser(description="Offline Kenshi-wiki -> faction_lore.json builder (optional tool)")
    ap.add_argument("--list", action="store_true", help="list faction pages on the wiki and exit")
    ap.add_argument("--factions", default="", help="comma-separated page titles (default: whole Factions category)")
    ap.add_argument("--fetch-only", action="store_true", help="dump raw article text without LLM refinement (no API key needed)")
    ap.add_argument("--api-key", default="", help="OpenAI-compatible API key (or OPENAI_API_KEY env)")
    ap.add_argument("--base-url", default="", help="OpenAI-compatible base URL (or OPENAI_BASE_URL env)")
    ap.add_argument("--model", default="", help="model name (or LORE_MODEL env / server models.json)")
    ap.add_argument("-o", "--output", default="faction_lore_review.json",
                    help="output file for human review (default: faction_lore_review.json)")
    args = ap.parse_args()

    if args.list:
        for t in list_faction_pages():
            print(t)
        return 0

    titles = [t.strip() for t in args.factions.split(",") if t.strip()] or list_faction_pages()
    print(f"Fetching {len(titles)} faction article(s) from {WIKI_API} ...")

    llm_cfg = None
    if not args.fetch_only:
        llm_cfg = resolve_llm_config(args)
        if not llm_cfg:
            print("ERROR: no LLM credentials found (flags/env/providers.json).")
            print("       Re-run with --fetch-only to crawl without refinement.")
            return 2
        print(f"LLM: {llm_cfg[2]} @ {llm_cfg[0]}")

    results, raw_dump = [], {}
    for i, title in enumerate(titles, 1):
        print(f"[{i}/{len(titles)}] {title}")
        try:
            text = fetch_article_text(title)
        except Exception as e:
            print(f"  fetch failed: {e}")
            continue
        if not text:
            print("  (empty article, skipped)")
            continue
        if args.fetch_only:
            raw_dump[title] = text
        else:
            try:
                results.append(refine_article(llm_cfg, title, text))
            except Exception as e:
                print(f"  LLM refinement failed: {e}")
        time.sleep(REQUEST_DELAY_S)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    payload = {"_NOTE": "REVIEW ARTIFACT — verify facts and Korean aliases, then merge into "
                        "config/faction_lore.json or drop into config/faction_lore.d/",
               "factions": results} if not args.fetch_only else raw_dump
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {args.output} ({len(results) if not args.fetch_only else len(raw_dump)} item(s)). "
          f"Review before merging into the live DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
