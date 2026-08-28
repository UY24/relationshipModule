# relationshipModule

Two pipelines behind one FastAPI app, both scraping Google through **scrape.do's AI Mode**
endpoint and reasoning over the answer with **Gemini**:

- **Financial Relationship** (`relationship`) — takes an OCR'd portfolio-page CSV of
  (investor X, company Y, page URL), verifies the X↔Y financial relationship, and returns
  Y's website **only when the relationship is confirmed**.
- **AI Mode** (`ai_bulk`, `ai_deep`) — bulk company-website discovery. `ai_bulk` for broad
  batches, `ai_deep` for thorough per-entity investigation.

Company/run tracking lives in Supabase; the UI is served at `/app`.

## Prerequisites

- Python 3.12 (Homebrew python is fine: `brew install python@3.12`)
- Docker — for RabbitMQ. **Both** pipelines are broker-driven; neither runs without it.
- A Supabase project (free tier works) for company/run tracking
- An S3 bucket — required for the relationship pipeline, which keeps no local copy
- A scrape.do token and a Gemini API key

## Setup

1. Clone and enter the repo:

   ```sh
   git clone <repo-url> && cd relationshipModule
   ```

2. Create the venv and install dependencies:

   ```sh
   python3.12 -m venv .venv
   .venv/bin/pip install -r backend/requirements.txt
   ```

3. Create your `.env`:

   ```sh
   cp .env.example .env
   ```

   Fill the **"1. REQUIRED — core"** section (`SCRAPEDO_TOKEN`, `GEMINI_API_KEY`, plus
   `API_PORT` if you want a non-default port) and the **"2. Supabase"** section (next
   step). Set `S3_BUCKET`/`S3_REGION` in section 4. Everything else has working defaults.

4. Supabase: create a project, open the dashboard **SQL Editor**, run the contents of
   `supabase/migrations/001_init.sql` then `002_add_model_is_batch.sql`, and paste into
   `.env`:

   - `SUPABASE_URL` — the **bare** project URL (`https://<ref>.supabase.co`), **not** the
     postgres `:5432/postgres` connection string
   - `SUPABASE_SERVICE_ROLE_KEY` — the full **service_role** secret (~200+ chars) from
     Project Settings → API

   Upload endpoints return 503 until both are set.

   Quick sanity check without printing secrets:

   ```sh
   awk -F= '/^SUPABASE_URL=/{print $2}' .env
   ```

   The value must look like `https://abcxyz.supabase.co` and must not contain `:5432` or
   `/postgres`.

## Run locally

Three things, in this order.

**RabbitMQ** (the bundled compose file provides `rabbitmq:3.13-management`; user/pass
default to `guest`/`guest` unless set in `.env`):

```sh
docker compose up -d rabbitmq
```

Management UI at `http://localhost:15672`.

**Terminal 1 — the app (dev, auto-reload):**

```sh
.venv/bin/uvicorn run:app --reload --host 127.0.0.1 --port 8080
```

Then open `http://localhost:8080/app` (port = `API_PORT` in `.env`). `run:app` is
intentional — uvicorn needs the `module:asgi_app` import string (`run.py` exposes `app`);
plain `uvicorn run --reload` won't work.

No-reload alternatives (same app):

```sh
.venv/bin/python run.py                          # from repo root, reads API_* from .env
cd backend && ../.venv/bin/python -m app.main    # or run the module directly
```

**Terminal 2 — the worker.** Required for both pipelines; uploads sit `queued` without it.
Run exactly **one** worker process.

```sh
.venv/bin/python worker.py        # from repo root (resolves into backend/app)
```

## Tests

```sh
cd backend && ../.venv/bin/python -m unittest discover -s tests -t .
cd backend && for f in tests/*.mjs; do node "$f"; done      # UI DOM contracts
```

All offline — no live API calls. The `-t .` matters: it makes the tests import as the
`tests` package so `tests/__init__.py`'s hermeticity guard (which blanks every credential
env var) actually runs.

## Typical workflow

Upload a CSV on the **New Run** page, pick a company and a pipeline, and check the
**preview** (column mapping + row count) before starting. Sample inputs are in `samples/`.

- Relationship needs `Input_URL`, `Company_Name_X`, `Company_Name_Y`.
- AI Mode needs a company-name column and a country column.

Finished runs have a **"Rerun failed"** button on the run detail page. For relationship it
deletes the error markers and re-drives — every row that already has a result is skipped,
so rows that only need their Gemini verdict redone cost **no scrape.do credits**.

## More docs

- `CLAUDE.md` — architecture (durable): both engines, the S3 layout, the batch config
  resolver, and the conventions to follow when changing things.
- `.env.example` — every environment variable, with what reads it and why.
