# Running & Deploying Timex Scout

Plain-English guide to getting the tool live in a browser. (The build plan is in
[README.md](README.md); the build story is in [BUILD_JOURNAL.md](BUILD_JOURNAL.md).)

## The two pieces

- **GitHub** holds the *code*. It does not run anything; it's storage + version history.
- A **host** (a small cloud service) *runs* the code and gives you a **public link** you can
  open from any browser, on any laptop.

So: push the code to GitHub once, then connect a host to it for the link.

## What's in the repo (and what's deliberately not)

**In:** all the source code (`src/`), the tests, the build plan (`README.md`), the build journal
(`BUILD_JOURNAL.md`), the bundled sample of real captured listings (`src/timex_scout/fixtures/`),
and the deploy config (`render.yaml`).

**Never in (kept secret / local):** your API keys (`.env`), the Python virtual environment
(`.venv/`), and the runtime cache (`state/`). These are excluded by `.gitignore`. Your keys live
only on your machine and in the host's settings, never in GitHub.

## Option A: a public link (recommended for demoing)

Deploy to **[Render](https://render.com)** (free tier):

1. Push this repo to GitHub.
2. On Render: **New → Blueprint → connect this repo.** Render reads `render.yaml` and configures
   the build/start commands automatically.
3. In the Render dashboard, add your secret environment variable:
   - `GEMINI_API_KEY`: your Google Gemini key (this powers the AI scoring).
   - *(Optional)* for **live eBay data** instead of the bundled sample, also add
     `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, and `EBAY_ENV=production`.
4. Deploy. You'll get a link like `https://timex-scout-xxxx.onrender.com`. Open it anywhere.

By default (Gemini key only, no eBay keys) the app runs on the **bundled sample of ~500 real
captured listings** with full AI scoring, so it always works and never touches your eBay quota.
Add the eBay keys when you want it pulling live inventory.

> Free-tier note: the app sleeps after ~15 min idle; the first visit then takes ~30–40s to wake.
> Open the link a minute before a demo to warm it up, or use Render's paid instance ($7/mo) for an
> always-on link.

## Option B: run it locally on your laptop

If you'd rather run it on your own machine:

```bash
git clone <your-repo-url>
cd "Timex Scout"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install .
cp .env.example .env          # then put your GEMINI_API_KEY (and optional eBay keys) in .env
uvicorn timex_scout.web.app:app --reload --port 8082
```

Open <http://127.0.0.1:8082>. (Works only while that command is running, and only on that laptop.)

## Which keys do what

| Key | Needed for | If missing |
|-----|------------|------------|
| `GEMINI_API_KEY` | AI scoring of every listing | Falls back to a basic keyword score (still renders) |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` / `EBAY_ENV=production` | Live eBay listings + full descriptions/specifics | Runs on the bundled sample of real captured listings |
| `ETSY_KEYSTRING` | Etsy as a second source (pending approval) | Etsy shows as "wired, pending" and is skipped |
