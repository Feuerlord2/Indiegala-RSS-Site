# Indiegala-RSS-Site

RSS feeds for IndieGala bundles and store sales, served via GitHub Pages from
the `docs/` folder.

**Site:** https://feuerlord2.github.io/Indiegala-RSS-Site/

## Feeds

| Feed | Content | Source |
| --- | --- | --- |
| `games.rss` | New IndieGala game bundles | [barter.vg public JSON API](https://bartervg.com/bundles/json) (IndieGala = store ID 7) |
| `sales.rss` | Discounted games in the IndieGala store | [Official IndieGala store RSS feed](https://www.indiegala.com/store_games_rss?sale) |

## How it works

IndieGala has **no public API for bundles**. Its only official machine-readable
interface is the store RSS feed documented at
[docs.indiegala.com/support/rss_feed.html](https://docs.indiegala.com/support/rss_feed.html)
(games only — IndieGala does not sell book or software bundles). Bundle
launches are therefore taken from barter.vg, a community site that tracks
bundles across stores and offers a key-less JSON API.

The GitHub Action in
[`.github/workflows/generate-feeds.yml`](.github/workflows/generate-feeds.yml)
runs [`scripts/generate_feeds.py`](scripts/generate_feeds.py) every 6 hours
(2 HTTP requests per run — far below IndieGala's documented feed limit of
240 requests/hour) and commits the regenerated feeds to `docs/` only when
their content actually changed. If a data source is temporarily unreachable,
the previously generated feed is kept as-is, and pubDates of existing items
are preserved so feed readers never see duplicates.

## Running locally

```bash
python3 scripts/generate_feeds.py
```

No dependencies beyond the Python 3 standard library.
