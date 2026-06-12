CUSTOM FACTION DROP-IN DIRECTORY (Faction RAG, Phase 4)
========================================================

Any *.json file placed in this directory is merged into the faction lore DB
at server start (or via GET http://127.0.0.1:5000/lore/reload while running).

- Accepted file shapes: a single entry object, a list of entries,
  or {"factions": [ ... ]} — same schema as ../faction_lore.json.
- Entries with the same "id" OVERRIDE the base faction_lore.json entry.
- A campaign-specific override is also possible:
  server/campaigns/<CampaignName>/faction_lore.json (highest priority).
- Files that do not end in .json (like this README, or *.json.example) are ignored.

Use example_uwe_faction.json.example as a template for custom mod factions
(e.g. UWE): copy it, rename to something like uwe_factions.json, edit, reload.

Korean aliases: put verified transliterations in "aliases"; put guesses in
"aliases_ko_unverified" — they are still used for matching but are flagged
for human review.
