# Search: keywords, intent, ranking and caching

`novel-cli search`, `POST /api/v1/search`, the desktop search box and Android search all call the same aggregation code (`plugins.search` / `plugins.ranking`). This page explains how a keyword is interpreted, why results come back in a given order, how filtering and caching behave, and what exit codes and JSON a failed search produces.

Streaming search (`GET /api/v1/search/stream` and the progressive results in the desktop/Android clients) reuses only "fetch per site + clean + local filters" from that pipeline: it does not rank, does not detect intent and does not attach derived fields. See the streaming section of [Local API Service](./api.md) for the differences.

## What one search does {#pipeline}

1. Parse arguments and the site selection. An explicit `--site` that names no registered searcher exits as a usage error (`2`) before any request is sent, instead of degrading into "0 results".
2. Normalize the keyword, build the cache key and look the query up (see [Caching](#cache)).
3. On a cache miss, query the selected sites concurrently: each site gets its own `per_site_limit` and `timeout` wall-clock budget, so the whole search takes roughly `timeout` rather than being held hostage by the slowest site.
4. Clean every row: drop rows whose site key is unusable or that have neither a usable `book_id` nor a usable `book_url`; clear non-`http(s)` links; strip control characters and ANSI sequences from display fields such as title and author.
5. Fold exact duplicates within one site (same-book rows from different sites are only merged with `--group`).
6. Detect the query intent (only for `sort=quality`, see [Query intent](#query-intent)).
7. Sort (see [Ranking](#ranking)).
8. Apply the local filters: word-count bounds and update-date lower bound (see [Filtering](#filters)).
9. For `sort=quality` only, apply the relevance floor (see [Ranking](#ranking)); it keeps the more relevant rows but **never empties the result set**.
10. Optionally merge across sites (`--group`): one row per book, with `sources` ordered best first.
11. Apply `limit` (counting books with `--group`, rows otherwise).
12. Attach the read-only derived fields `rank_score`, `match_tier` and `query_intent` to every final row.

The cache stores the **merged, unranked snapshot**, so every call sorts, groups and slices its own copy; those steps never contaminate each other and are never written back.

## Keyword normalization {#normalization}

Before a keyword participates in matching, de-duplication, grouping or cache identity, `ranking.normalize_text` applies:

| Step | Effect | Example |
| --- | --- | --- |
| NFKC | Folds full-width onto half-width, compatibility characters and composed accents | `ＡＢＣ` → `ABC`; `１２３４` → `1234` |
| casefold | Case-insensitive matching | `ABC` → `abc`; `Straße` → `strasse` |
| Remove `[\W_]+` | Drops punctuation, whitespace, symbols and underscores; keeps letters and digits of any script | `Re:Zero` → `rezero`; `斗破 苍穹` → `斗破苍穹` |

Consequences:

- `"斗破 苍穹"` and `"斗破苍穹"` are the same query and share one cache entry;
- `"ＡＢＣ"` and `"abc"` are the same title/author;
- Leading/trailing whitespace, punctuation and CJK title marks (`《》`), English colons and similar characters never affect a match;
- A query made only of punctuation (e.g. `"!!!"`) normalizes to empty: **matching** treats it as an empty keyword, but the **cache key** falls back to the NFKC-folded raw text, so `"!!!"` and `"???"` do not wrongly share a snapshot.

The same normalization is used for de-duplication and grouping: two rows whose titles differ only in full-width/half-width forms or punctuation count as the same title.

## Multi-word queries (title + author) {#multi-word}

An aggregated search accepts a title and an author in one keyword. The keyword is NFKC-folded and then split on:

- any whitespace (including the full-width space `U+3000`)
- the middle dot `·`
- half-width `,` and full-width `，`
- the hyphen `-` (full-width `－` folds to `-` under NFKC and splits the same way)

After splitting, only **2–4 distinct, non-empty tokens** enable multi-token scoring. Every other count keeps the previous behavior:

| Tokens | Behavior |
| --- | --- |
| 1 | A normal query: the whole keyword is compared with title and author |
| 2–4 | Each token is compared with both fields; the best title token and the best author token feed the relevance score |
| 5 or more | Treated as a pasted sentence; the whole string is used as one keyword |

The whole keyword always stays in the comparison, so multi-token scoring can only **raise** relevance; it never lowers a title that already matched. Multi-token handling also applies to `match_tier` and to intent detection.

```bash
# These two are equivalent (quoting is recommended so the shell does not split the query)
novel-cli search "斗破苍穹 天蚕土豆"
novel-cli search 斗破苍穹 天蚕土豆

# The middle dot and commas are separators too
novel-cli search "斗破苍穹·天蚕土豆"
novel-cli search "斗破苍穹，天蚕土豆"
```

!!! note "Token splitting does not change the cache key"
    Cache identity is the **normalized full keyword**, not the token list. So `"斗破苍穹 天蚕土豆"` and `"斗破苍穹"` are different queries with separate entries, while `"斗破 苍穹"` and `"斗破苍穹"` are the same query. Token splitting affects ranking and intent detection only.

## Query intent `query_intent` {#query-intent}

A short keyword can be either a title or an author name. `quality` ordering first judges the **result set as a whole**: does this batch look more like a title search or an author search? It then feeds that verdict back into the ordering.

How the verdict is produced (`ranking.detect_query_intent`):

1. For every row, compute the keyword's similarity to `title` and to `author`, then sum each side (`math.fsum`, so the verdict does not depend on the order rows arrived in).
2. Weight the two sums by the length of the normalized keyword:

   | Keyword length | Title weight | Author weight | Rationale |
   | --- | --- | --- | --- |
   | ≤ 4 chars | 0.35 | 0.65 | Chinese pen names are usually 2–4 characters |
   | 5–9 chars | 0.50 | 0.50 | Both readings are plausible; the actual matches decide |
   | ≥ 10 chars | 0.65 | 0.35 | At this length it is nearly always a full title |

3. If the two weighted scores differ by no more than **15%** of their total (or there is no overlap at all), the verdict is "no clear intent"; otherwise the higher side wins.

For a multi-word keyword each token is judged separately. The verdict is used only when every token with evidence points the same way; one token pointing at the title and another at the author (the typical "title + author" query), or too little evidence, yields "no clear intent".

Where the verdict goes:

- Only `sort=quality` computes and uses it. An explicit `--sort priority/site/title/author/words/update` does not compute it, and `query_intent` is `null`.
- It is computed from the **cleaned, de-duplicated** result set *before* filters/grouping/limit, so filters cannot change it.
- It does not decide which rows are kept (except indirectly through ordering and the relevance floor), and it is never written back to the cache.

### How it affects ordering

Once the verdict is `title` or `author`, `quality` ordering uses a two-level key:

```text
(match_tier of the primary field, rank_score)
```

In other words, **rows that match the primary field more closely always come first**, even if the other row has more words, a fresher update or a cover. This is what keeps an author's own sparse-metadata book above a metadata-rich book whose *title* happens to contain the author's name. With "no clear intent" the ordering falls back to the single `rank_score` key (the historical behavior).

### Where it is shown

| Client | Location |
| --- | --- |
| CLI | Result page title, e.g. `搜索结果 · 第 1/2 页 · 书名匹配` or `· 作者匹配` |
| Desktop | A **list-level** tag next to the result count (`书名匹配` / `作者匹配`) |
| Android | The same list-level tag next to the result count, only when results are non-empty and the verdict is `title` / `author` |
| JSON | Every row of `POST /search` carries `query_intent`; the response envelope has the same key (`"title"` / `"author"` / `null`) |

`query_intent` is a **query-level** verdict; the row-level verdict is `match_tier`. They are different things: a row can match very well while the query as a whole still has no clear intent. Streaming endpoints, browse results and old cache snapshots have neither field.

## Ranking {#ranking}

### `rank_score`: a 0..1 quality score

`ranking.rank_score` is a weighted sum of five signals (weights total 1.0):

| Signal | Weight | Computation |
| --- | --- | --- |
| Keyword relevance | 0.55 | See the match table below; the primary field is boosted once an intent is decided |
| Word count | 0.15 | `log10(words) / 7`, capped at 1.0; 0.5 when unparseable |
| Update recency | 0.12 | ≤1 day 1.0; ≤7 days 0.98; ≤30 days 0.95; ≤90 days 0.9; ≤365 days 0.8; ≤730 days 0.6; older 0.5; unparseable 0.5 |
| Site priority | 0.08 | `priority` ≤10 → 1.0; ≤50 → 0.9; ≤100 → 0.75; ≤200 → 0.6; ≤500 → 0.45; ≤1000 → 0.3; larger → 0.2; missing/`NaN` → 0.6 |
| Completeness | 0.10 | Cover present +0.5; latest chapter present +0.5 |

Relevance itself is computed per field (the whole keyword and each token take part, keeping the higher value):

| Match relation | Title score | Author score |
| --- | --- | --- |
| Exact equality | 1.00 | 0.90 |
| Prefix | 0.75 | 0.70 |
| Contains | 0.70 | 0.70 |
| Only some characters overlap | 0.55 × coverage | 0.50 × coverage |
| No overlap | 0 | 0 |

The two sides are then combined by intent: `either` uses `max(title, author × 0.9)`; `title` uses `max(title, author × 0.6)`; `author` uses `max(author, title × 0.6)`.

`rank_score` is rounded to 6 decimal places in the output. It includes the recency signal, so the same query can drift very slightly between calls; `match_tier` and `query_intent` are deterministic for a given keyword and result set.

### `match_tier`: the 4..0 match level

| Value | Meaning |
| --- | --- |
| `4` | Exact equality with the normalized field |
| `3` | The field starts with the keyword (prefix) |
| `2` | The keyword occurs inside the field (contains) |
| `1` | Only some characters of the keyword occur in the field |
| `0` | No character of the keyword occurs in the field |

With a decided intent it reports the tier of the intent's primary field; otherwise it reports the better of title and author. A multi-word query reports the best tier any token reaches. Under `quality` ordering this tier is the outer ordering key.

### Sort keys and their natural direction

| `--sort` | Compares | Natural direction |
| --- | --- | --- |
| `quality` (default) | `(match_tier, rank_score)` when an intent is decided, otherwise `rank_score` | `desc` |
| `priority` | Numeric site priority; **a smaller number means higher priority**; missing or `NaN` counts as 0 | `asc` |
| `site` | Normalized site key | `asc` |
| `title` | Normalized title | `asc` |
| `author` | Normalized author | `asc` |
| `words` | Parsed word count; unparseable counts as -1 | `desc` |
| `update` | Parsed update timestamp; unparseable counts as -1 | `desc` |

`--order asc|desc` overrides the natural direction. Common combinations:

```bash
# Most recently updated first (desc is already natural for update)
novel-cli search 三体 --sort update

# Fewest words first (words is naturally desc, so ask for asc)
novel-cli search 三体 --sort words --order asc

# Highest-priority sites first (priority is naturally asc, because a smaller number is better)
novel-cli search 斗破苍穹 --sort priority

# Title ascending, emitted as JSON
novel-cli search 三体 --sort title --json
```

An unparseable `words` / `update` value compares as -1: last under the default descending order, first under `--order asc`. Empty `title` / `author` / `site` values compare as the empty string in ascending order, so they sort first.

### How ties are broken {#tie-break}

Sorting runs as two stable passes:

1. A total pre-sort over the row content: `(site, normalized title, book_id, normalized author, book_url, word_count, update_date, latest_chapter, cover_url, priority)`;
2. A stable sort by the primary key (and direction).

Rows with equal primary keys therefore never fall back to "whichever site answered first": concurrent runs produce the same final order. The derived `rank_score` / `match_tier` fields **never** take part in the comparison, so stale telemetry on a cached row cannot influence ordering.

### The relevance floor (quality only)

`quality` ordering also applies a default relevance floor of **0.45** (not exposed as a CLI/API parameter):

- Without grouping it judges rows; with grouping it judges each book's **primary row** (a mirror site's alternate entry may look irrelevant on its own while the book is clearly relevant);
- The threshold is compared against `relevance_of` (pure keyword relevance, without words/time/priority), not `rank_score`;
- It **never empties the result set**: if nothing clears the floor, every row with relevance greater than 0 is kept; if even those do not exist, the input is returned unchanged.
- It is **not** applied for explicit `--sort words` / `update` and other keys, so switching sort keys never loses rows to relevance.

## Filtering {#filters}

Filtering is a **local** step: sites still return their results and only matching rows are kept. It runs after ranking and before `--group` merging and `limit` truncation, so a filtered-out row consumes neither a book slot nor a `limit` slot.

### Word count: `--min-words` / `--max-words`

- Both bounds are **inclusive**, in the range `0`–`1000000000` (the same ceiling in the CLI and REST).
- Parsing matches the ranking layer: `"30万字"` → 300000, `"1,234"` → 1234, `"1.2M"` → 1200000, `"3千字"` → 3000; full-width digits and separators are NFKC-folded first.
- As soon as either word-count filter is set, rows with **no word count or an unparseable one** are dropped — including with `--min-words 0`, because the filter is active.

### Update date: `--updated-after`

- Accepts `YYYY-MM-DD` (the CLI enforces exactly that format; REST parses it through the request schema) as an **inclusive** lower bound: a row updated at any time on that day passes.
- Relative dates in a row's `update_date` (e.g. `"3天前"`) are resolved against **one clock per call**, so every row in one search uses the same reference instant.
- Once set, rows with **no update date or an unparseable one** are dropped.

### Boundary cases

| Case | Result |
| --- | --- |
| `--updated-after` is a future date | No row can satisfy the lower bound, so the result is empty; this is not an error (the exit code stays `0` when sites answered) |
| `--min-words` greater than `--max-words` | The CLI and desktop reject the pair as a usage error before sending a request (CLI exit code `2`); the REST schema validates each field's `0..1000000000` range but does not compare them, so it returns `200` with `total: 0` |
| Date is not `YYYY-MM-DD` | CLI exit code `2` (it does not silently drop the filter); REST returns `422` |
| Word count out of range or not an integer | CLI exit code `2`; REST returns `422` |
| Every row is filtered out | Empty result, exit code `0` (this is "nothing matched the filters", not a failed request) |

Filters only narrow the current result set; they do not change the ranking scores, the response fields or the raw cached snapshot. Different filter combinations are different requests and are cached separately, never reusing an unfiltered snapshot.

```bash
# At least 300k words, at most 1M words, updated on or after 2024-01-01
novel-cli search 斗破苍穹 --min-words 300000 --max-words 1000000 --updated-after 2024-01-01

# Filter + cross-site merge + one-line JSON
novel-cli search 斗破苍穹 --min-words 300000 --updated-after 2024-01-01 --group --json
```

The equivalent REST request:

```bash
curl -s -X POST "$BASE/api/v1/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keyword":"斗破苍穹","sites":["qidian","biquge5"],"per_site_limit":10,"min_words":300000,"max_words":1000000,"updated_after":"2024-01-01"}'
```

Fewer results after filtering usually means a site did not return `word_count` / `update_date`, not that the search failed; see "No results after search filters" in [Troubleshooting](./troubleshooting.md).

## Caching {#cache}

The search cache holds the **merged, unranked raw snapshot**: a hit performs no site requests, but every call still re-sorts, re-groups and re-slices its own copy according to its `sort` / `order` / `limit` / `group`.

### What the cache key contains

| Component | In the key? | Notes |
| --- | --- | --- |
| `keyword` | Yes | The normalized full keyword; punctuation-only queries fall back to the NFKC raw text |
| `sites` | Yes | Each key is stripped, lowercased and prefixed with `n` when it starts with a digit, then de-duplicated and sorted; `None` (all sites) differs from an empty selection |
| `per_site_limit` | Yes | A different per-site fetch size yields a different snapshot |
| `nsfw` | Yes | Whether NSFW searchers are included |
| `timeout` | Yes | Different budgets can drop different slow sites |
| Config fingerprint | Aggregate only | Added when `sites` is `None`: a 16-hex-character SHA-256 prefix over the participating site set plus the browser-assisted set |
| `min_words` / `max_words` / `updated_after` | Yes | Filters change which rows are accepted; the date is normalized to ISO form |
| `sort` / `order` / `limit` / `group` / relevance floor | **No** | Presentation/selection parameters, re-applied on top of the cached snapshot per call |

Direct consequences:

- Changing `--sort`, `--limit` or `--group` still hits the same snapshot;
- Changing `--site`, `--site-limit`, `--timeout`, a filter, or the keyword is a different entry;
- For an aggregate search (no `--site`), any change to the site set or browser-assisted configuration changes the fingerprint, invalidating every old aggregate entry; the first search after the change misses entirely;
- A search that produced no results at all is **not** cached (it is usually a transient failure), so one bad run is not pinned for a whole TTL.

### Memory layer

- In-process and enabled by default; TTL **300 seconds**, capacity **64 entries**, LRU eviction beyond that.
- It lives in the current process only: every `novel-cli` run is a new process, so ordinary CLI invocations usually do not share hits. Long-lived desktop or API processes do reuse it.

### Disk layer (optional)

The disk layer exists for cross-process reuse and must be configured **before the search module is first imported** (for the CLI, before the process starts):

```bash
# Git Bash / Linux / macOS
NOVELDL_SEARCH_CACHE_DIR=~/.cache/novel-downloader/search novel-cli search 三体
```

```powershell
# PowerShell
$env:NOVELDL_SEARCH_CACHE_DIR = "$env:LOCALAPPDATA\novel_downloader\search-cache"
novel-cli search 三体
```

The disk layer uses the same TTL, the same key and the same field whitelist as memory:

- The directory path supports `~` expansion; with the variable unset, no file is written at all;
- File names are `v1-<sha256>.json`; the payload stores the format version and the full key, so a version mismatch or a key that does not match the file name is a miss;
- Each file is capped at 2 MiB, and at most 256 files of this format are kept in the directory; the oldest are pruned first;
- Writes are atomic (a same-directory temp file plus `os.replace`), with mode `0600` on POSIX;
- A missing, unreadable or corrupt file is simply a miss; the next write rebuilds it;
- A disk hit is promoted into memory and inherits the *remaining* TTL, so it cannot extend an entry's lifetime.

### Clearing it

- **Desktop**: "Settings → Search cache" offers "Clear search cache", which drops memory and disk entries while keeping the cumulative hit/miss counters.
- **Memory-only mode**: restart the process.
- **Manual disk cleanup**: delete only `v1-*.json` files in the configured directory; other files there are neither read nor removed.
- `novel-cli clean cache` clears the configured cache directories (site cookies, chapter caches, ...) and does **not** clear the search cache.

### Diagnosing with `doctor`

The `search_cache` check in `novel-cli doctor` is a read-only snapshot: it counts but never purges, never opens a payload, and never reports keywords, site selections or filter values.

```bash
# Only the search-cache check; without jq, find "key": "search_cache" in the full JSON's checks[]
novel-cli doctor --json | jq '.checks[] | select(.key == "search_cache")'
```

The fields (`memory_entries`, `expired_entries`, `disk_enabled`, `disk_entries`, `hits`, `misses`, `evictions`, `fingerprint`) and how to read them are documented under "Search cache diagnostics" in [CLI usage § doctor](./cli-examples.md#doctor-subcommand). The essentials:

- `hits` / `misses` / `evictions` are cumulative **for the current process**. `doctor` is a fresh process that has not searched, so they are usually all `0`; that is not a cache failure.
- `disk_enabled=false` with `disk_entries=0` is the default memory-only mode, not a fault.
- A changing `fingerprint` means the searcher set or the browser-assisted site set changed, so every old aggregate entry has expired.

## Failures, exit codes and JSON {#failures}

### One failing site does not sink the search

- Each site has its own wall-clock budget (`--timeout`, default 5 seconds). A site that misses it is cancelled and counted as `failed`; the other sites' results are still returned.
- A site that raises, or returns something other than a list of rows, is likewise reported as that site's failure and does not crash the aggregate.
- An aggregate search (no `--site`) skips sites configured for browser-assisted search, reporting `browser_skipped`. That is **not** a failure; explicitly selecting the site with `--site <site>` enables its browser fallback.
- Sites whose `nsfw` flag does not match are skipped silently in an aggregate search.
- Novel search has **no cross-call circuit breaker**: unlike the music aggregate search, which freezes a failing platform for 10 minutes, every search retries every selected site. "Breaking the circuit" here is per-call only: at the deadline the site is cancelled and no longer awaited.

### CLI exit codes

In human-readable mode the CLI prints one event per site on stderr, then a tally:

```text
✓ qidian · 8 result(s)
✗ biquge5 · Search failed: ...
↷ fanqienovel · skipped (browser-assisted)
3 site(s) completed / 1 failed / 0 skipped
```

| Situation | Exit code | Notes |
| --- | --- | --- |
| Search completed (≥1 site answered, including 0 results) | `0` | Normal |
| Some sites failed/timed out, but at least one answered | `0` | Only the successful sites' rows are returned; the tally shows the failures |
| **Every** answering site failed (0 `completed`, ≥1 `failed`) | `1` | Treated as an outage, not as "no such book" |
| Explicit `--site` typo, or a plugin that is not installed | `2` | stderr lists the available sites; no empty array masquerading as success |
| Blank keyword; `--min-words > --max-words`; invalid date; `--timeout` is `nan`/`inf`; `--json` combined with `--first` | `2` | Usage error |
| The user cancels the selection in interactive mode | `0` | Nothing is downloaded |
| Login, download or export fails after a row was selected | `1` | Unrelated to the search itself |

If there is neither a `completed` nor a `failed` event (for example every selected site was skipped), the run counts as a normal finish and exits with code `0`.

`--limit` or `--site-limit` below 1 is silently raised to 1; a finite non-positive `--timeout` is raised to 0.1 seconds; `nan` / `inf` are usage errors.

### How `--json` behaves

- stdout carries **one line** of JSON (an array, `ensure_ascii=False`, compact separators); per-site events and the tally go to stderr, keeping stdout clean.
- Each element is the site's raw fields plus the three derived fields:

  ```json
  {
    "site": "qidian",
    "book_id": "123456",
    "book_url": "https://...",
    "cover_url": "https://...",
    "title": "斗破苍穹",
    "author": "天蚕土豆",
    "latest_chapter": "第 1 章",
    "update_date": "2024-01-01",
    "word_count": "100万字",
    "priority": 1,
    "rank_score": 0.912345,
    "match_tier": 4,
    "query_intent": "title"
  }
  ```

- With `--group`, each row also carries `sources` (every matching site, best first), and `site` / `book_id` / `book_url` point at the best source.
- **Even when every site fails, the JSON is emitted first** (usually `[]`) and the process then exits with code `1`. Scripts must check the exit code, not just whether the array is empty.
- Zero results with at least one site answering still exits `0`; stderr says `No results found for '<keyword>'.`, which distinguishes "no match" from an outage.

The REST endpoints have a different shape: `POST /search` returns `{total, results, query_intent}` (derived fields on each row); all sites failing still yields `200` with `total: 0`; an unknown site is `422`; exceeding the server's overall budget returns `504`.

## FAQ {#faq}

### Few or no results {#few-or-no-results}

1. Read the tally: `N completed / M failed / K skipped`. Many `failed` entries usually mean sites did not answer within the 5-second budget; raise `--timeout` or verify one site with `--site` first.
2. If `completed` is high but results are empty, the keyword may be too specific; try the "title + author" multi-word form.
3. An aggregate search skips browser-assisted sites (such as fanqienovel); select one explicitly with `--site fanqienovel` when you need it.
4. With `--min-words` / `--updated-after` set, rows lacking the corresponding field are dropped; remove the filters to check whether the book itself is searchable.
5. A wrong `--site` exits with code `2` and lists the available sites instead of returning an empty list.

### The order looks wrong {#order-looks-wrong}

- `quality` is a **blended score**, not pure relevance: relevance is 0.55, while words, recency, site priority and completeness together are 0.45. Read `rank_score` and `match_tier` in `--json` to explain each row's position.
- With a decided intent, the outer key is the primary field's `match_tier`. A sparse-metadata book whose title matches exactly is deliberately placed above a metadata-rich book whose title merely contains an author name.
- `rank_score` includes a recency signal and can drift very slightly across calls; `match_tier` does not.
- To order by one field, just switch keys: `--sort title` / `author` / `words` / `update`. Missing `words` / `update` values compare as -1, so they come last under the default descending order.
- Rows with equal primary keys are ordered by the content tuple (site, title, `book_id`, ...), independent of arrival order; "nothing changed" usually means those fields are equal too.

### The cache does not seem to work

- A normal `novel-cli` invocation is its own process, so the memory cache dies with it; cross-process reuse needs the disk layer, and its environment variable must be set before the process starts.
- The TTL is 300 seconds by default; after that, repeating the search hits the sites again.
- Check that the key really matches: the `--site` set (order does not matter, but adding or removing one does), `--site-limit`, `--timeout`, `nsfw`, filters and the aggregate configuration fingerprint all participate.
- `--sort`, `--order`, `--limit` and `--group` do **not** participate; changing only those should still hit.
- Empty results are not cached; more than 64 entries triggers LRU eviction.
- Use `doctor --json` to inspect `hits` / `misses` / `evictions` / `fingerprint`, remembering that the counters belong to the `doctor` process.

### Does a multi-word query return fewer results

Not because of the splitting itself. Multi-token scoring only raises relevance and does not change the row set a site returns; what can change is the ordering (the intent may become "no clear intent", so the primary-field tier no longer leads) and which rows the relevance floor keeps.

## Related documentation {#see-also}

- [CLI usage](./cli-examples.md): every option and example for the `search` subcommand
- [Local API Service](./api.md): `POST /search`, SSE streaming and error codes
- [Troubleshooting](./troubleshooting.md): empty results after filters, repeated site requests
- [Settings reference](./settings-reference.md): proxy, timeouts and site configuration
