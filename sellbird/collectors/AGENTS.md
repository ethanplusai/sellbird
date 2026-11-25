# DETERMINISTIC COLLECTORS

## OVERVIEW
Cost-controlled discovery and public-site profiling; score 8, distinct fact-collection boundary.

## WHERE TO LOOK
| Task | Location | Entry or responsibility |
|------|----------|-------------------------|
| Discovery runs | `discover.py` | `run_discovery`, provider adapters and spend ledger |
| Discovery queries | `discover.py` | `build_queries`, `estimate_cost`, `preset_templates` |
| Profile companies | `profile.py` | `ProfileCollector`, `profile_companies` |
| Name and role evidence | `people.py` | `find_people`, `menu_links`, `emails_on_page` |
| Hosted job boards | `ats.py` | `detect_board`, `read_board`, `matching_roles` |
| Resume durable stages | `../pipeline.py` | Database-selected profile backlog |

## CONVENTIONS
- Deterministically available facts do not consume model calls.
- Each collector checks confirmed signal codes even when the HTML already contains a fact.
- Discovery estimates before paid fetching and checks pause/actual spend between queries.
- Account provider `last_cost` even when a fetch or response parse fails.
- Flush discovery per batch and profiling per company so interrupted work survives.
- Domain identity is normalized; websiteless companies use provider external IDs.
- Google Business Profile aliases reconcile pack/Maps observations for the same business.
- Rank observations retain search phrase and search location; duplicate repeats retain improvements.
- Configured coordinates win; geocoding caches successes and misses.
- Dry-run can still resolve coordinates; it is not a blanket no-network guarantee.
- People extraction preserves accents but matches accent-folded text; cards, schema and prose retain evidence.
- Company-owned links/embeds establish ATS board identity before a bounded public-feed read.
- ATS `None` means unreadable; an empty parsed list means no roles, not a request failure.
- Hiring keywords prefer `icp.hiring_signals`, then `icp.titles`; negative observations retire stale roles.
- Focused checks: `python -m pytest -q tests/test_discover.py tests/test_collectors_profile.py tests/test_people_finder.py tests/test_ats_hiring.py`.

## ANTI-PATTERNS
- Do not guess ATS tokens or attribute a hosted-board vendor as the prospect company.
- Do not convert exhausted/throttled/failed providers into successful empty-market results.
- Do not replace junk-domain patterns with a literal domain blocklist.
- Do not log Scavio response bodies; upstream failures can echo credentials.
- Do not treat TinyFish ordering as Google rank or Maps position as organic rank.
- Do not infer no website from a missing website button.
- Do not identify testimonial owners, article authors or departed founders as current staff.
- Do not use unrelated homepage dates to date a funding claim.
