# BYU Speeches catalog

A nightly job that reads the public listing of talks on speeches.byu.edu and publishes one JSON
file of metadata (titles, speakers, dates, topics, links to BYU's own audio, view counts) for a
personal listening app. It does not copy audio or transcripts; the app streams from BYU's servers.
Not affiliated with Brigham Young University.

- `builder/build_catalog.py` — the builder (Python standard library only).
- `tests/` — `python3 -m unittest discover -s tests`
- `data/details.json` — what was read from each talk page (duration, YouTube ids), so a page is
  fetched once and again only when the site changes it.
- `data/snapshots/` — each night's view counts, for 7/30/90-day gains (kept 120 days).
- `docs/catalog.json` — the published catalog (GitHub Pages serves `docs/`).

The builder makes about one request a second with an identifying user agent: roughly 450 requests
a night, plus one per new or changed talk.

YouTube views and likes are added when the repository has a `YOUTUBE_API_KEY` secret
(YouTube Data API v3; about 100 of the 10,000 free daily units).

Site view counts are rounded by the site ("953k views"), so gains for the biggest talks move in
steps of a thousand; YouTube counts are exact.
