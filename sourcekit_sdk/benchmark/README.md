# Search coverage benchmark

Metadata only. No chapter retrieval, login, challenge solving or access-control bypass.
The curated cases.tsv contains 46 book-title queries and 6 author queries, with
explicit edition/title and simplified/traditional aliases. The set is biased
toward female-oriented fiction intentionally, not representative of all web fiction.

Run from sourcekit_sdk in its development environment:

```bash
nix-shell --run 'python -m sourcekit compile && python benchmark/run.py --output build/benchmark-pilot.json'
nix-shell --run 'python benchmark/run.py --sites sk_shuku52 sk_czbooks qimao --ids 03,06,09,10,11,12,16,20,25,28,37,48 --output build/benchmark-expanded.json'
nix-shell --run 'python benchmark/report.py build/benchmark-*.json'
```

Use small batches. Each site has one session and sequential requests with at least
3 seconds between searches (16 seconds for bixiange). Stop at 403/429 or a challenge,
or two consecutive transport failures. No retries that circumvent site restrictions.
The runner exercises native registered search adapters and the first search response
only, without a 15-result application cap; it is not a test of exhaustive pagination.
`zero_unverified` is not a proven catalog miss. Matching requires both expected
title (or a reviewed alias) and author; a title-only match is not credited.
The report merges repeated queries by site and case, and can re-evaluate title aliases
against exact-author title evidence. Potential increments mean absent from the
observed baseline union, not proven absent from a site's full catalog.
Author counts are unique returned title strings by exact author, not complete bibliographies.
Raw probe reports stay in ignored build/. No credentials or chapter content are recorded.

## Initial screening — 2026-11-25 Asia/Singapore

100 query/site attempts, 19 distinct queries (17 titles, 2 authors), 17 sites.
Hand-reviewed edition aliases added after the initial probe; merged report
recomputed matches using recorded exact-author title evidence.

| Site | Matched titles | Attempted title queries | Parsed title queries |
|---|---:|---:|---:|
| sk_czbooks | 15 | 17 | 17 |
| sk_shuku52 | 13 | 17 | 17 |
| sk_biquge5 | 3 | 9 | 4 |
| qimao | 3 | 17 | 17 |
| n8novel | 0 | 4 | 1 |
| kadokado | 0 | 4 | 4 |
| n37yue | 0 | 4 | 1 |
| ttkan / ixdzs8 / n23qb / n101kanshu / hetushu | 0 confirmed | 1 each | 0 |
| ciluke / yodu | 0 confirmed | 4 each | 0 |
| i25zw / laoyaoxs / bixiange | 0 confirmed | 1 each | 0 |

Baseline union: 15/17. Qimao matched Dong Gong (case 25), absent from the observed
baseline union, but Biquge5's query failed. Qimao is a legitimate catalog candidate,
not an approved TXT source. n8novel returned a matching title for Chang Feng Du
but its search parser provides no author; no confirmed title+author hit.
The blocked/unverified candidates cannot be scored as absent titles.
Remaining 33 distinct queries and production-host validation are outstanding.
No production source changes are made by this benchmark.
