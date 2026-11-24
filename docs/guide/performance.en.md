# Performance and Capacity

This page is for large libraries, books with many chapters, and long batch downloads. It explains which settings actually affect throughput, memory, and disk, and how to locate the bottleneck. Every default, bound, and behaviour below matches the current implementation; **the recommendations are starting points, not promises** — site tolerance, network quality, and disk type change the result.

Related pages: [Settings Reference](./settings-reference.en.md) · [Storage and Backup](./storage.en.md) · [Troubleshooting](./troubleshooting.en.md) · [CLI Usage](./cli-examples.en.md) · [Local API Service](./api.en.md) · [Music Downloads](./music.en.md)

---

## Measure first {#measure-first}

Establish a baseline before changing anything:

```bash
# 1. Environment, configuration, disk, chapter storage, search cache, site plugins, API port
novel-cli doctor
novel-cli doctor --json

# 2. Usage and free space of the four data directories
novel-cli storage --json

# 3. Same book, same chapter range, fetch and store only (skip export)
time novel-cli download <book_id> --site <site> \
  --start <first_chapter_id> --end <last_chapter_id> --no-export
```

Guidelines:

* **Change one setting at a time** and re-run the same book, time window, and range.
* `--no-export` separates "fetch + parse + write" from "export TXT/EPUB/HTML"; the two stages usually have different bottlenecks.
* Resuming is the default: with `cache_chapter = true`, completed chapters are skipped, so a first run and a later run are not comparable. Make sure the test range is not already cached, or pick an undownloaded range.
* Book metadata and the catalog need only a few requests; the more chapters, the more steady-state fetching dominates.

---

## Settings at a glance {#parameters}

### Download and concurrency {#download-concurrency}

All keys below can live in `[general]` and be overridden per site in `[sites.<site>]` (site values win).

| Key | Default | Valid range | Effect | Increase | Decrease |
| --- | --- | --- | --- | --- | --- |
| `workers` | `4` | 1–4096 | Chapter-fetching coroutines **per book** | More throughput for one book | Less site pressure and memory |
| `request_interval` | `0.5` | ≥ 0 | Seconds waited **per book** after each completed chapter (jittered) | More conservative | Faster, but likelier to trip rate limits |
| `max_connections` | `10` | 1–4096 | Connection-pool ceiling for one site's fetcher | Helps on high-latency links | Use when the site or proxy is connection-sensitive |
| `max_rps` | `1000.0` | ≥ 0, `0` disables | Per-site token-bucket rate limit; shared by page, image, and font requests | Relax | Throttle deliberately |
| `timeout` | `10.0` | > 0 | Single-request timeout (seconds) | Slow sites / large images | Fail fast |
| `retry_times` | `3` | ≥ 0 | Request-layer retries (timeouts/connection errors and 408/425/429/5xx only) | Unstable links | Fewer wasted waits |
| `backoff_factor` | `2.0` | ≥ 0 | Retry backoff factor | Avoid hammering a limiter | Retry sooner |
| `download_retry_times` | `2` | ≥ 0 | Extra retries for a whole-book download task | Occasional drops | Fail fast |
| `storage_batch_size` | `1` | 1–65536 | Chapters per SQLite transaction | Fewer commits | Less crash loss and lower memory peak |
| `cache_book_info` | `true` | bool | Reuse book metadata | — | Force a catalog refresh |
| `cache_chapter` | `true` | bool | Skip chapters already written | — | Force re-download |

**Concurrency does not simply add up.** Each book gets its own `workers` coroutines. The desktop app and the API run one book per site at a time (FIFO within a site) and different sites in parallel. With several sites downloading, total coroutines are roughly the sum of each site's `workers`, and memory grows with it.

Steady-state throughput for one book is approximately:

```text
time per chapter ≈ request time + request_interval (waited after each chapter)
per-book RPS ≈ min(max_rps, workers ÷ time per chapter)
```

This is why raising `workers` from 4 to 16 while keeping `request_interval = 0.5` gets you at most roughly 4×, not 16×: every coroutine sleeps 0.5 s after each chapter. If `max_rps` is set low, it becomes the hard ceiling, and extra `workers` only add queued coroutines and memory.

### Search {#search-limits}

Search uses its own connection pool and timeouts; `[sites.*]` `workers` does not apply.

| Entry point | Parameter | Default | Maximum |
| --- | --- | --- | --- |
| CLI | `--site-limit` | `10` | none (handled per site) |
| CLI | `--limit` | unlimited | none |
| CLI | `--timeout` | `5.0` s per site | none |
| REST | `per_site_limit` | `5` | 100 |
| REST | `limit` | unlimited | 500 |
| REST | `sites` | all sites | 100 |
| REST | `timeout` | `5.0` s per site | 120 |

The aggregate search pool is fixed at **64 total connections and 8 per host**, with DNS cached for 300 seconds. Raising `--site-limit` does not make one site faster; it only asks each site for more rows.

The search result cache is **TTL 300 s, at most 64 entries (LRU)** in memory. The disk layer is enabled only when `NOVELDL_SEARCH_CACHE_DIR` is set (at most 256 files, 2 MiB per snapshot). Cache keys include the keyword, site set, per-site limit, timeout, and filters; sorting and grouping are applied after the cache, so changing sort or grouping does not go back to the sites.

### Music {#music-limits}

| Item | Default | Notes |
| --- | --- | --- |
| Download `workers` | `3` | From music settings (`settings.json`, editable in the desktop settings page); there is no CLI/API `--workers` flag |
| Per-track limit | 1 GiB | Exceeding it aborts and deletes the partial file |
| Per-batch limit | 8 GiB | Shared atomic budget across concurrent workers; failed/cancelled attempts refund unreserved bytes |
| Cover limit | 8 MiB | Per cover image |
| Chunk size | 64 KiB | Write granularity |
| Aggregate search threads | 32 | One thread per platform, bounded by the platform count |
| Active API music jobs | 4 | Beyond that returns `429`; at most 100 job records are retained |

1 GiB / 8 GiB are **size ceilings, not memory use**: downloads are written in 64 KiB chunks and a whole track is not buffered in memory. When the batch budget is exhausted, remaining tracks fail; split the work into smaller batches.

### Chapter storage (SQLite) {#storage-limits}

* Each `chapter.<stage>.sqlite` uses WAL mode, `synchronous = NORMAL`, and `busy_timeout = 5000` ms.
* The download side has a single write connection; `upsert_chapters` commits the whole batch in one transaction. `storage_batch_size = 1` commits per chapter; larger values mean fewer commits and longer transactions.
* WAL lets an exporter read while a processor writes the same book, but **one book should still have only one writer**. Two processes writing the same database only produce `database is locked` after `busy_timeout`.
* `storage backup` uses SQLite's online backup API and captures committed WAL frames; do not copy `.sqlite` files under a running process as a substitute.

### Local API server {#api-limits}

| Item | Default | Notes |
| --- | --- | --- |
| Per-IP rate limit | 120 requests / 60 s | Health endpoints such as `/api/v1/health` are exempt; `--no-rate-limit` disables it |
| Request body limit | 1 MiB | Over the limit returns `413`; both declared `Content-Length` and streamed accumulation are checked |
| Concurrent connections | 100 | uvicorn `limit_concurrency`; an SSE stream also holds one |
| Blocking endpoint timeouts | search 120 s / browse 60 s / music search 120 s / music download 1800 s | Over budget returns `504` (the music job is marked failed) |
| Download task concurrency | 1 per site | FIFO within a site, parallel across sites; desktop and API use the same task-manager implementation, and each process maintains and persists its own queue |

!!! note "Rate-limit counters are per process"
    The `novel-api` entry point refuses multiple workers. Only a hand-rolled `uvicorn --workers N` deployment gives each worker its own counters, so the effective limit becomes `N × 120`. Do not rely on that deployment to raise capacity.

---

## Recommendations by library size {#library-size}

Start from the chapter count. The values below are **starting points**; if a site is sensitive, its actual responses win. Re-measure with "Measure first" after each change.

| Setting | Small (< 1,000 chapters) | Medium (1,000–10,000) | Large (> 10,000) |
| --- | --- | --- | --- |
| `workers` | `4` (default) | `6–8` | `8–12` (if the site keeps up) |
| `request_interval` | `0.5` | `0.5` | `0.5–1.0` |
| `max_connections` | `10` | `16` | `24` |
| `max_rps` | leave the default | set `2–10` per site | set `2–10` per site; do not leave 1000 |
| `storage_batch_size` | `20–50` | `50–100` | `100–200` |
| `cache_chapter` / `cache_book_info` | `true` / `true` | `true` / `true` | `true` / `true` |
| Run style | one book at a time | one book, or one per site | batch by site, one book per site |

### Small library (under 1,000 chapters) {#small-library}

The defaults are enough. The one worthwhile change is `storage_batch_size`: default commits per chapter, which is not noticeable at this size, while `20–50` cuts disk syncs and loses at most one uncommitted batch on a crash. If the site itself is fast, `workers = 4` is usually not the bottleneck.

### Medium library (1,000–10,000 chapters) {#medium-library}

* Raise `workers` to `6–8` and set an explicit `max_rps` for common sites (for example `2–10`). The default `1000` is effectively no limit; keep it only for sites that clearly tolerate it.
* `max_connections = 16` helps on high-latency links; on a low-latency local link with a fast site, the gain is limited.
* Use `storage_batch_size = 50–100` to amortise per-chapter fsync cost.
* Keep `cache_chapter = true`; resuming after an interruption skips completed chapters.
* Use `--site-limit` to bound search fan-out. A common split is a small search page and a separate download run.

### Large library (over 10,000 chapters) {#large-library}

* **The bottleneck is almost always the site, not the machine.** Set `max_rps` and `request_interval` per site and keep `workers` within what the site tolerates. Pushing to 32/64 usually just stacks rate-limit errors in the queue.
* Divide and conquer: run site by site, one book per site at a time. The desktop app and API already work this way; do not start several CLI processes against the same site.
* `storage_batch_size = 100–200` is reasonable, with two costs: longer transactions (WAL growth, longer lock hold) and more uncommitted chapters lost on a crash. Do not go large on a slow HDD or a network drive.
* `max_connections` does not need to grow linearly with chapter count; around `24` already exceeds most sites' tolerance.
* Turn off `general.debug.save_html`. Leaving it on for a large library writes every chapter's raw HTML to disk and raises both space use and parse time.
* Schedule exports separately. Building an EPUB of 10,000+ chapters holds chapter XHTML and images at once, so the memory peak is higher than during download. Export by volume (`general.output.split_mode = "volume"`) or download first without exporting (`--no-export`).

---

## Slow network or site rate limiting {#slow-network}

First decide whether the network is slow or the site is refusing:

| Symptom | More likely | First step |
| --- | --- | --- |
| Requests generally take close to or more than `timeout`, few errors | Slow network/proxy | Raise `timeout`, check the proxy, run `novel-cli doctor --online` |
| `429` / `503` / `Retry-After` | Site rate limiting | Lower `max_rps`, raise `request_interval`; do not add `workers` |
| Connection resets, TLS errors, intermittent timeouts | Link or site risk control | Raise `retry_times` / `backoff_factor`; consider a site-specific `backend` / `impersonate` |
| Only search is slow, download is fine | Search fan-out too large | Fewer sites, lower `--site-limit`, higher `--timeout` |

Per-site override example (affects only site `example`):

```toml
[general]
request_interval = 0.5
max_rps = 5.0
timeout = 15.0

[sites.example]
workers = 2
request_interval = 2.0
max_rps = 1.0
timeout = 30.0
retry_times = 5
backoff_factor = 3.0
```

Key points:

* `max_rps` is a **per-site** token bucket shared by page, image, and font requests. When a site returns 429, dropping to 1–2 is more effective than retrying.
* `request_interval` is the jittered wait after each chapter. On a slow site, 1–2 seconds is usually more direct than lowering `workers`.
* `timeout` applies to one request. A slow but working site can use 20–30 seconds so every chapter is not retried three times.
* Retries honour the server's `Retry-After`; `retry_times` and `backoff_factor` apply only to retryable errors. Deterministic states such as 403/404 are not retried.
* Bound search fan-out with CLI `--site <site>` / `--site-limit 5` / `--timeout 10`, or REST `sites` / `per_site_limit` / `timeout`. More sites do not mean better results; a slow site drags down the whole aggregate budget.
* `batch --interval` is the wait **between book searches**, separate from the per-chapter `request_interval`.
* For sites that need a browser fingerprint, see [Book Sources](./book-sources.en.md) for `backend` / `impersonate`; proxy and certificate issues are covered in [Troubleshooting](./troubleshooting.en.md#proxy-vpn).

---

## Disk and backup {#disk-backup}

### Check usage first {#disk-usage}

```bash
novel-cli storage            # usage of raw_data / cache / downloads / logs, plus free space
novel-cli storage --json     # machine-readable, suitable for monitoring
```

`raw_data` holds the chapter databases (SQLite), `downloads` holds exported files, `cache` holds cookies, fonts, images, and the search cache, and `logs` holds runtime logs. They grow for different reasons; do not look only at the total.

### Backup frequency and retention {#backup-cadence}

```bash
# After a large batch, before cleaning, before an upgrade
novel-cli storage backup --out /path/to/backups --keep 7

# Only report what --keep would delete
novel-cli storage backup --out /path/to/backups --keep 7 --dry-run

# Verification runs by default after writing; skip it only when you are sure
novel-cli storage backup --out /path/to/backups --no-verify
```

Guidelines:

* **Active library**: daily, or after each large batch. Chapter databases only append/update, and backup cost scales with library size.
* **Low-frequency library**: weekly or monthly is fine; `storage backup` produces full snapshots, not increments.
* **`--keep N`** retains the newest N per "book × stage"; `0` disables pruning. Plan space as `N × database size`, not as an incremental size.
* Backups cover only `chapter.<stage>.sqlite` under `raw_data`; they **exclude** `downloads/`, `cache/`, `settings.toml`, and login state. Back up exported files separately, and sign in again after moving machines.
* Verify with `novel-cli storage verify` before restoring; `storage restore` needs `-y` to replace an existing database. Full fields and failure semantics are in [Storage and Backup](./storage.en.md#storage-backup).

!!! warning "Do not let a sync client write into a live library"
    SQLite WAL depends on the `-wal`/`-shm` sidecars and file locks in the same directory. Letting a real-time sync client (OneDrive, Dropbox, etc.) write directly into a running `raw_data` can cause lock conflicts or unsynchronised sidecars. Sync the **backup directory**, not the live `raw_data`. Likewise, do not place `raw_data` on SMB/NFS or another network filesystem.

---

## Observing memory and CPU {#memory-cpu}

### doctor {#doctor}

```bash
novel-cli doctor
novel-cli doctor --json

# Disk headroom only
novel-cli doctor --json | jq '.checks[] | select(.key == "disk_space")'

# Chapter-storage sample only
novel-cli doctor --json | jq '.checks[] | select(.key == "storage")'

# Search-cache counters only
novel-cli doctor --json | jq '.checks[] | select(.key == "search_cache")'
```

`doctor` checks Python/package versions, `settings.toml`, configuration validity, optional dependencies, output/cache/log directories, disk headroom (warns below 512 MiB), chapter storage (samples up to 3 databases; if all are ≤ 1 MiB it inspects them all), search cache, sites and plugins, music dependencies, and the API port. `--online` adds an egress probe. It does not measure throughput or report process memory.

### Logs {#logs}

* Location comes from `general.debug.log_dir` (default `./logs`); the CLI log file rotates daily and keeps 7 files.
* `general.debug.log_level` controls the **console** level (default `INFO`); the CLI writes its log file at `DEBUG`, so the file is more detailed than the terminal and grows faster on long large-library runs.
* Logs redact credentials. Do not leave `save_html` on for long: raw HTML lands under `debug/`.

### Task queue {#task-queue}

* Desktop and API share one task manager: **one queue and one worker per site**, strict FIFO within a site, parallel across sites; a separate single-threaded process/export worker handles the later stages.
* Inspect the queue in the desktop Tasks page, or with `GET /api/v1/tasks` (snapshot `{pending, running, completed}`) and `GET /api/v1/tasks/stream` (SSE, once per second).
* Completed history keeps the newest 100 tasks by default; these are small in-memory objects, not a major memory source.
* If `pending` piles up for one site, that site's single worker is advancing chapter by chapter under `request_interval`; adding tasks for other sites will not speed it up.

### Main memory consumers {#memory-sources}

From largest to smallest:

1. **Search result merging**: aggregate search collects every site's rows before cleaning, ranking, and grouping, and applies the total `limit` last. Memory scales roughly with "number of sites × `per_site_limit`"; many sites with large pages produce a clear peak. The search cache separately keeps at most 64 merged snapshots (each a copy), and the disk layer caps one file at 2 MiB.
2. **`storage_batch_size`, `workers`, and the write queue**: each worker holds one chapter's text; after it is handed to the storage queue, the queue buffers up to 10 more chapters, and batches accumulate up to `storage_batch_size` chapters before writing. Together they set the download-stage memory peak. `storage_batch_size = 1` is the lightest; `100–200` raises the peak noticeably.
3. **Export**: EPUB construction holds chapter XHTML and images at once, with a 16 MiB cap per image; with many chapters the peak exceeds the download stage. TXT/HTML are lighter.
4. **OCR and font deobfuscation**: `general.parser.enable_ocr = true` loads PaddleOCR models, using far more CPU and memory than plain text parsing; the model name, `device`, and `cpu_threads` all matter.
5. **Translation cache**: the translation processor caches up to 4096 entries by default, a steady block of memory during long runs.
6. **Music**: 3 workers and 64 KiB chunks keep memory small; 1 GiB/8 GiB are disk-byte budgets.

At the OS level, watch process RSS with Task Manager (Windows) or `ps`/`top`, then eliminate items above one at a time. There is no built-in memory metric, and `doctor` does not report RSS.

### CPU {#cpu}

* Fetching is asynchronous I/O; parsing runs in the `asyncio.to_thread` pool. Plain text parsing is usually not the bottleneck; CPU goes mainly to font deobfuscation, image chapters, OCR, EPUB construction, and translation.
* `workers` above the CPU core count does not necessarily help. If CPU is near 100%, reduce OCR/translation/export or batch the work instead of adding coroutines.
* When measuring fetch-and-store only, add `--no-export` to remove export CPU from the baseline.

---

## Common bottlenecks and how to identify them {#bottlenecks}

### Site throughput (most common) {#site-throughput}

**Signs**: consistently high per-chapter time; 429/503/`Retry-After` in logs; raising `workers` barely changes wall time but increases errors or waiting.

**Diagnosis**: run a fixed range twice — defaults, then with a lower `max_rps` (for example 2). If the lower limit is more stable and completes more chapters, the site's tolerance is the bottleneck, not the machine. You can also test one site with `--site <site>`.

**Fix**: lower `max_rps`, raise `request_interval`, and moderately raise `timeout` per site; do not force it with retries. See the per-site example above.

### SQLite locking {#sqlite-lock}

**Signs**: `database is locked` or `storage_error` in logs, progress stuck at the storage stage, or `-wal`/`-shm` files that persist for a long time.

**Diagnosis**:

```bash
# Read-only check; modifies nothing
novel-cli storage verify

# Look for multiple processes writing the same book
# Windows: Task Manager / PowerShell Get-Process
# Linux/macOS: ps aux | grep novel
```

Also confirm `raw_data` is not on SMB/NFS and is not being reopened by real-time antivirus, indexing, or sync clients. SQLite's write lock is database-wide; `busy_timeout` is only 5 seconds before it errors.

**Fix**: keep one writer per book; put `raw_data` on a local disk; exclude the data directory from real-time scanning; back up with `storage backup` instead of copying files. Larger `storage_batch_size` holds the lock longer per transaction, so on lock contention lower it first.

### Disk I/O {#disk-io}

**Signs**: fetching is fast but progress stalls; `--no-export` is clearly faster; especially on HDDs or network drives; `storage` shows tight free space.

**Diagnosis**: compare `--no-export` with a normal export run; watch `free_bytes` from `storage --json`; on Windows, watch disk activity in Task Manager. `doctor` warns below 512 MiB free.

**Fix**: put `raw_data` and `downloads` on a local SSD; export fewer formats at once; export large books by volume; clean old `logs`, `debug/`, and unneeded `raw_data` (back up first). `cache_chapter` only avoids re-fetching; it does not reduce first-write I/O.

### Memory {#memory-bottleneck}

**Signs**: process RSS rises with search or batch size; the system starts swapping and everything slows down.

**Diagnosis**: observe RSS during a large search, during download only (`--no-export`), and during export. Re-test after lowering `storage_batch_size` or the site set.

**Fix**: lower `storage_batch_size`, reduce concurrent sites/`workers`, limit sites and `--site-limit` during search, export in batches, disable OCR/translation. The search cache needs no manual clearing; entries expire after the 300 s TTL.

### API server {#api-bottleneck}

**Signs**: clients receive `429`, `413`, or `504`, or connections are refused.

**Diagnosis**:

* `429`: the per-IP limit of 120 requests/60 s, or 4 active music jobs.
* `413`: request body over 1 MiB.
* `504`: a blocking endpoint exceeded its budget (search 120 s, browse 60 s, music search 120 s, music download 1800 s).
* Connection buildup: the concurrent-connection cap is 100, and SSE streams count.

**Fix**: lower client polling frequency, use `/tasks/stream`, and split large requests. Only a trusted local environment should consider `--no-rate-limit`. A hand-rolled multi-worker deployment does not share counters and does not change the single-process task model.

### Music downloads {#music-bottleneck}

**Signs**: a batch fails midway with a size-limit message, or several concurrent submissions return `429`.

**Diagnosis**: a track over 1 GiB or a batch over 8 GiB aborts; the API allows only 4 active music jobs. External dependencies (CDM, `N_m3u8DL-RE`, platform reachability) can also leave a platform unresponsive for a long time.

**Fix**: split into smaller batches, lower the music `workers` (default 3), and check platforms with `novel-cli music health` first. The size-limit semantics are in [Troubleshooting](./troubleshooting.en.md#music-size-limit).

---

## Capacity estimation {#capacity-estimate}

Do not estimate from a fixed "KB per chapter"; measure your own books:

1. Pick a fully downloaded book and record its chapter count and the total bytes of `chapter.*.sqlite` under `raw_data/<site>/<book>/`.
2. Average bytes per chapter ≈ total database bytes ÷ chapter count. Text sites, image chapters, and different stages vary widely.
3. Target library size ≈ average bytes per chapter × expected chapters × (1 + headroom). Headroom covers WAL, temporary files, and later updates.
4. Estimate exported files separately: measure `downloads/` for one exported book, then scale by book count; EPUB with images is much larger than plain text.
5. Backup space ≈ `--keep N` × total chapter-database size (full snapshots, not increments).

`novel-cli storage --json` reports actual bytes and free space for each directory, which turns these estimates into a monitorable baseline.

---

## See also {#see-also}

* [Settings Reference](./settings-reference.en.md) — every `[general]` / `[sites.*]` key and default
* [Storage and Backup](./storage.en.md) — full semantics of `storage backup` / `verify` / `restore`
* [Troubleshooting](./troubleshooting.en.md) — rate limits, 403, login, API 413/415, SQLite damage, and other concrete symptoms
* [CLI Usage](./cli-examples.en.md#doctor-subcommand) — `doctor` checks and `--json` fields
* [Local API Service](./api.en.md) — rate limiting, body limits, tasks, and SSE
* [Music Downloads](./music.en.md) — platforms, budgets, and dependencies
