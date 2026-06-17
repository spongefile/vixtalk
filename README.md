# VIX / VVIX reading site

Static page that shows the latest VIX and VVIX levels with a plain-English
interpretation. A scheduled GitHub Action runs the Python script daily and
commits the result; the page just reads that file. No server to maintain,
no hosting cost.

## How it fits together

- `vix_vvix_monitor.py` — fetches VIX/VVIX from Yahoo Finance, interprets them, writes `data/latest.json`.
- `.github/workflows/update.yml` — runs the script on a schedule (weekdays, ~1.5 hours after US market close) and commits the updated JSON.
- `index.html` — fetches `data/latest.json` and renders it. This is the only thing GitHub Pages actually serves to visitors.
- `data/latest.json` — committed sample data so the page works immediately; gets overwritten by the workflow on its first run.

## Setup (one-time)

1. **Create a new GitHub repo** (public — GitHub Pages on the free tier requires a public repo unless you're on GitHub Pro/Team/Enterprise).
2. **Push this folder's contents** to that repo (root of the repo, not a subfolder, unless you adjust paths in `index.html` and the workflow accordingly).
3. **Enable GitHub Pages**: repo Settings → Pages → Source → "Deploy from a branch" → branch `main`, folder `/ (root)`. Save.
4. **Enable Actions write permission**: repo Settings → Actions → General → Workflow permissions → "Read and write permissions". Without this, the workflow can run the script but can't commit the result back.
5. Wait a minute, then visit `https://<your-username>.github.io/<repo-name>/`.

## Testing it without waiting for the schedule

In the repo's Actions tab, select "Update VIX/VVIX reading" → "Run workflow" to trigger it manually (this works because of the `workflow_dispatch` line in the workflow file). Check that `data/latest.json` gets updated with a new commit.

## Adjusting the schedule

Edit the `cron` line in `.github/workflows/update.yml`. It's in UTC. Current setting is `30 21 * * 1-5` — 21:30 UTC, Monday–Friday. Note GitHub's cron scheduler can run a few minutes late under load; don't rely on it for anything time-critical.

## If you later want intraday/live data instead of daily close

Yahoo's free index data is daily-close-quality; this setup is built around that. Genuine intraday VIX/VVIX would need a different source (CBOE direct feed, or a broker API like Interactive Brokers) and a different architecture — a small persistent backend rather than a scheduled static rebuild, since per-visitor live execution would actually do something at that point. Worth revisiting only if the daily-close cadence stops being good enough.

## Known limitations (carried over from the script itself)

The VIX/VVIX severity bands and the VVIX/VIX ratio read are conventional heuristics, not derived from rigorous statistical backtesting — reasonable defaults, not predictions. The ratio is flagged as "not very informative" when VIX is very low, since dividing by a small number mechanically inflates it regardless of actual convexity demand.
