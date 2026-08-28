# Code map

Where things live and how a run moves through them. Read this first; read `CLAUDE.md`
when you need to know *why* something is the way it is.

Two pipelines, both scraping Google via **scrape.do's AI Mode endpoint** and reasoning
with **Gemini**:

| pipeline | key | one message per | state lives in |
|---|---|---|---|
| Financial Relationship | `relationship` | **run** | S3 only |
| AI Mode Bulk / Deep | `ai_bulk`, `ai_deep` | **batch of rows** | local dir, mirrored to S3 |

---

## Entry points

| File | What it does |
|---|---|
| `run.py` | Repo-root launcher. Sets `sys.path`/cwd, then runs uvicorn. |
| `worker.py` | Repo-root launcher for the worker. **Run exactly one.** |
| `docker-compose.yml` | RabbitMQ only. Start it before the API. |
| `backend/app/main.py` | Mounts routers + `/static`, serves the UI at `/app`. Imports `app` from `engine.py`. |
| `backend/app/engine.py` | The FastAPI `app`, the RabbitMQ connection, and every relationship HTTP endpoint. ~850 lines. |

## Shared plumbing — `backend/app/core/`

| File | What it does |
|---|---|
| `config.py` | Paths (`PROMPTS_DIR`, `STATIC_DIR`, …) and the single `load_dotenv()`. Imported first, everywhere. |
| `s3.py` | Thin boto3 helper: client, upload dir, iterate keys, download. |
| `supabase_client.py` | Lazy thread-safe Supabase singleton. Returns `None` when unconfigured → callers 503. |
| `notify.py` | Slack run-complete / run-failed pings. Best-effort, never raises. |

## Shared models + routers

| File | What it does |
|---|---|
| `models/entities.py` | Parses **AI Mode's** input CSV (company name + country). Not used by relationship. |
| `models/results.py` | `EntityResult` — the one output-row schema both pipelines write. |
| `routers/ai_mode.py` | All `/uploads/ai-mode*` endpoints. |
| `routers/companies.py` | `/companies*` — Supabase-backed. Feeds the Dashboard and Runs views. |
| `services/companies.py` | Company + run row lifecycle. Bookkeeping only: **never raises**. |

## Cross-pipeline helpers — `backend/app/services/common/`

| File | What it does |
|---|---|
| `env.py` | `get_int_env` / `get_float_env` / `get_bool_env`. Blank counts as unset. |
| `text.py` | `slugify_company`, and the passthrough helpers that keep the user's original CSV columns in every output file. |
| `provider_limits.py` | **One** semaphore for scrape.do. The cap is per *account*, and both pipelines share the process — so they share the gate. `SCRAPEDO_CONCURRENCY`. |
| `scrapedo_http.py` | One pooled `httpx.AsyncClient` + backoff + token redaction. |
| `llm_batch.py` | Every Gemini Batch setting, resolved in one place. Also decides *which* pipeline batches. |

## Relationship pipeline — `backend/app/services/relationship/`

| File | What it does |
|---|---|
| `relationship_csv.py` | Parses the input CSV: `Input_URL`, `Company_Name_X`, `Company_Name_Y`. |
| `query_builders.py` | Loads `prompts/relationship_search.txt` and fills it for one row. |
| `scrapedo_ai_client.py` | The scrape.do AI Mode call. One per row. |
| `modes/relationship.py` | Pure row logic — evidence, candidate URLs, verdict gate, output fields. No I/O. |
| `gemini_llm.py` | Builds the Gemini **verdict** prompt and applies the gate that decides whether Y's website is allowed out. |
| `s3_run_store.py` | The S3 run store. **Object presence IS the state.** |
| `s3_run_driver.py` | Generic run driving: task window, stop flag, redrive, terminal Supabase + Slack write. |
| `relationship_runner.py` | Orchestrates the phases: scrape → Gemini Batch verdict → outputs. |
| `relationship_outputs.py` | Writes `confirmed_relation.csv`, `notconfirmed_relation.csv`, `retry.csv`, `report.json`, `run.log`. |
| `reporting.py` | `retry.csv` row building + the input-column passthrough. |
| `outcomes.py` | found / not_found / error buckets, and the error source + category vocabulary. Shared with AI Mode. |
| `url_utils.py` | URL normalising, canonicalising, the disallow-list. |
| `cost.py` | Gemini USD per 1M tokens. scrape.do bills credits, so it isn't here. |
| `constants.py` | The pipeline id and the user-visible row-error strings. |
| `row_logging.py` | `[row-trace]` log lines. |
| `worker.py` | Starts the relationship consumer + its redrive loop. |

> `s3_run_store.py` and `s3_run_driver.py` are the **shared** S3 run machinery — in the
> full repo, gmaps and firmographics ride on these same two modules. Worth knowing well.

## AI Mode pipeline — `backend/app/services/ai_mode/`

| File | What it does |
|---|---|
| `settings.py` | `Settings` / `LLMConfig` dataclasses from env. |
| `mode_config.py` | The `ai_bulk` / `ai_deep` registry: batch size + prompt file. |
| `broker.py` | AI Mode's own RabbitMQ channel + queue on the shared connection. |
| `scrapedo_client.py` | Its own scrape.do AI Mode client. |
| `worker.py` | Consumes one message = one scrape batch. Acks only after the file is on disk. |
| `ai_mode_service.py` | The orchestrator: prepare run, LLM cleanup, report, Supabase, Slack. Biggest file here. |
| `cleanup.py` | The cleanup prompt + parsing the LLM's JSON into `EntityResult`s. |
| `llm_client.py` | Sync Gemini client + token accounting. |
| `gemini_batch.py` | The Gemini **Batch API** driver. **Used by both pipelines.** |
| `run_reporting.py` | Streaming writer for `found.csv` / `notFound.csv` / `final_report.json`. |
| `run_store.py` | Local run directories under `ai_mode_results/`. |
| `s3_sync.py` | Mirrors a run dir to S3 and back. Best-effort. |
| `cost.py`, `models.py` | Per-run LLM cost; small dataclasses + `utc_now_iso`. |

## UI — `backend/app/static/js/`

| File | What it does |
|---|---|
| `main.js` | Hash router for the app shell. |
| `api.js` | `api()` (fetch + one retry), `pollStatus()`, `el()` DOM builder. |
| `ui.js` | Shared widgets **and the one pipeline registry**. Add a pipeline here, not per view. |
| `dashboard.js` / `companies.js` / `runs.js` | The three list views. All read `/companies*`. |
| `new_run.js` | The upload workflow: company → pipeline → CSV → preview → start. |
| `run_detail.js` | Live run detail. Polls status, renders outcomes/cost/files, offers Rerun and Stop. |

---

## Flow 1 — a relationship run

```
UI  new_run.js
      │  POST /uploads/relationship/preview   (dry parse, costs nothing)
      │  POST /uploads/relationship           (file + company)
      ▼
API engine.create_relationship_upload
      │  relationship_csv.parse_relationship_csv   validate
      │  s3_run_store.put_bytes                    input.csv -> S3
      │  s3_run_store.write_run_pointer            so any process can find the run
      │  companies.create_run                      Supabase row
      │  engine.publish_relationship_run           ONE message: {"run_id": ...}
      ▼
                          RabbitMQ  relationship_runs
                                     │
WORKER worker.py -> relationship/worker.py -> s3_run_driver.consume_runs
      │  acks ON RECEIPT (a run outlives RabbitMQ's 30-min consumer timeout)
      ▼
relationship_runner._phases
   │
   ├── PHASE 1  scrape          run_scrape_phase
   │      for each row, bounded by SCRAPEDO_CONCURRENCY:
   │        query_builders.build_relationship_search_query
   │        scrapedo_ai_client.search_ai_mode        1 call/row, 10 credits per HTTP 200
   │        s3_run_store  ->  raw/<shard>/row_NNNNNN.json     success
   │                          errors/<shard>/row_NNNNNN.json  dead after retries
   │      a row with either object is DONE — a re-drive skips it for free
   │
   ├── PHASE 2  verdict         run_llm_phase
   │      modes/relationship.ai_mode_arrays     read the stored response
   │      gemini_llm.build_relationship_prompt  build the verdict prompt
   │      gemini_batch                          submit shards, job name -> batches/
   │      gemini_llm.apply_relationship_gate    website released ONLY if confirmed
   │      s3_run_store  ->  cleaned/<shard>/row_NNNNNN.json
   │
   └── PHASE 3  outputs         relationship_outputs.write_outputs
          streams raw/ + cleaned/ + input.csv, writes:
          confirmed_relation.csv  notconfirmed_relation.csv
          retry.csv  report.json  run.log
      ▼
s3_run_driver._notify_terminal   ->  Supabase run row + Slack ping

UI  run_detail.js polls GET /uploads/{id}/status every 2s until terminal,
    then shows the Files card, Rerun failed, and the cost breakdown.
```

**The idea to hold on to:** there is no `state.json` and no local disk. Which S3 objects
exist *is* the state. That makes the machine disposable and every retry free for work
already done.

## Flow 2 — an AI Mode run

```
UI  new_run.js  POST /uploads/ai-mode (mode=ai_bulk|ai_deep)
      ▼
routers/ai_mode.py -> ai_mode_service.prepare_ai_mode_run
      │  writes ai_mode_results/<company>/<run_id>/ + Supabase row
      │  worker.publish_run_batches  ->  ONE message per BATCH of rows
      ▼
                          RabbitMQ  ai_mode_jobs
                                     │
WORKER ai_mode/worker.process_scrape_job     (idempotent: file exists -> skip)
      │  scrapedo_client -> raw_responses/request_NNNNNN.json
      │  ack only AFTER the file is durable
      │  last ack flips phase scraping -> cleaning, dispatches the finish task once
      ▼
ai_mode_service.run_ai_mode_finish
      │  cleanup.py + llm_client (or gemini_batch when LLM_BATCH=true)
      │      -> cleaned/batch-NNNNNN.json
      │  run_reporting.StreamingRunReport -> found.csv / notFound.csv / final_report.json
      │  s3_sync.mirror_run_to_s3, Supabase, Slack
```

## Where the two pipelines actually touch

Only four places, and they are worth knowing:

| Shared thing | Module |
|---|---|
| The RabbitMQ connection | `engine.init_rabbitmq` — AI Mode opens its own channel on it |
| The Gemini Batch driver | `ai_mode/gemini_batch.py` |
| The scrape.do concurrency gate | `common/provider_limits.py` |
| The outcome vocabulary | `relationship/outcomes.py` |

Everything else is separate. They do **not** share a queue, a state model, or a scrape client.

---

## Reading order for your first day

1. `readme.md` — get it running.
2. This file — find your way around.
3. `backend/app/services/relationship/relationship_runner.py` — the three phases, top to bottom.
4. `backend/app/services/relationship/s3_run_store.py` — the object-presence state model.
5. `CLAUDE.md` — the reasoning behind the awkward bits (ack-on-receipt, why evidence is
   every string, why one Gemini shard's timeout is not the run's).

Break something and run `cd backend && python -m unittest discover -s tests -t .` —
470 tests, all offline, no network.
