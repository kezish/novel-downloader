# Bounded live probes from dawn16, Nov 24 UTC
Engine commit 70a622f. No full books downloaded.
- 52: search HTTP 200, 2 parsed results; detail HTTP 200, 151 ordered reading pages; first reading page HTTP 200 with nonempty content. Saved prior sample: 191 pages and first-page content. Whole-book live download not tested.
- Biquge5: search HTTP 200, at least 5 parsed results (limited to 5); detail HTTP 200, 27 first-page catalog entries; first chapter request timed out at 12s. Complete catalog and real chapter pagination unverified.
- CZBooks: search HTTP 403. Offline fixture parsing only. No access-control bypass attempted.
- Jiujiu: search form POST HTTP 302 to /result/, same-origin GET HTTP 404. Download-page HTTP 200, original TXT link parsed and validated (file not downloaded). Direct delivery remains separate from chapter queue.
- API application imports successfully and declares /api/v1/sites; live service / task end-to-end not verified.
