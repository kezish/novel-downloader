# Bounded live probes from dawn16, Nov 24 UTC
Engine commit 70a622f. No full books downloaded.
- 52: search HTTP 200, 2 parsed results; detail HTTP 200, 151 ordered reading pages; first reading page HTTP 200 with nonempty content. Saved prior sample: 191 pages and first-page content. Whole-book live download not tested.
- Biquge5: search HTTP 200, at least 5 parsed results (limited to 5); detail HTTP 200, 27 first-page catalog entries; first chapter request timed out at 12s. Complete catalog and real chapter pagination unverified.
- CZBooks: search HTTP 403. Offline fixture parsing only. No access-control bypass attempted.
- Jiujiu: search form POST HTTP 302 to /result/, same-origin GET HTTP 404. Download-page HTTP 200, original TXT link parsed and validated (file not downloaded). Direct delivery remains separate from chapter queue.
- API application imports successfully and declares /api/v1/sites; live service / task end-to-end not verified.

## Follow-up: CZBooks
Ordinary urllib requests to homepage, search and detail returned HTTP 403 with
server cloudflare and cf-mitigated: challenge. The adapter now uses the original
upstream CZBooks request contract (URL path plus q parameter) and explicitly
reports challenge responses. With the native async search pipeline, 三體 and
斗破蒼穹 each returned 15 usable results in a bounded live test. This does not
prove continued availability or download access. No challenge bypass was used.

## Download-chain validation
Live public-domain title search: CZBooks 三國演義 returned 15 results, the
sample detail parsed 119 directory links and first chapter parsed successfully.
Biquge5 三国演义 returned 6 results, the sample first directory page parsed
100 links and first chapter parsed successfully. Only bounded page probes;
no complete live book was downloaded.
Full synthetic books for Biquge5 (including directory and chapter pagination),
CZBooks and 52 passed through the real client, SQLite and TXT exporter.
Strict pagination rejects failed follow-up requests and repeated bodies.

## Search pagination follow-up
Observed 52 and Biquge5 search pages with 20 rows and p=N links; CZBooks
search pages with 42 rows and /s/<keyword>/<page> links in .paginate.
Search now uses native registered parsers without a per-page result cap, a
bounded three-page batch per source, per-site continuation and explicit errors.
Observed keyword 重生: 52 60 results per batch, continuation 4 then 7;
CZBooks 126 results from three pages, continuation 4. Biquge5 returned 40
results before HTTP 500 on page 3; the continuation retains page 3 for retry.
Challenge responses are reported, not bypassed.
