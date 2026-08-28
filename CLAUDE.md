# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

One FastAPI app + one vanilla-JS UI hosting **two pipelines** that share an upload/runs/Supabase tracking layer. Both scrape Google through **scrape.do's AI Mode endpoint** (`/plugin/google/search/ai-mode`) and reason over the answer with **Gemini**. There is no other search provider and no other LLM provider.

1. **Relationship** (`relationship`) — verifies an X↔Y financial relationship from an OCR'd portfolio-page CSV and emits Y's website ONLY when the relationship is confirmed. RabbitMQ + worker + S3, one message per RUN. Lives in `backend/app/services/relationship/`.
2. **AI Mode** (`ai_bulk`, `ai_deep`) — bulk website discovery: scrape.do AI Mode → LLM cleanup. RabbitMQ + worker + local dir mirrored to S3, one message per BATCH. Lives in `backend/app/services/ai_mode/`.

Cross-provider helpers both use are in `backend/app/services/common/` (`text`, `env`, `llm_batch`, `provider_limits`, `scrapedo_http`).

Python 3.12. The package root is `backend/app/`; dependencies are in `backend/requirements.txt`.

> This repo was carved out of a five-pipeline monolith on 2026-08-28. `gsearch` (SerpWow), `gmaps` and `firmographics` were deleted along with the SerpWow vendor client, the `state.json` row engine, the Operations and Tools views, and ~5,300 lines of `engine.py`. If a comment references any of those, it is a leftover — fix it.

## Run & test

The **FastAPI `app` object is defined in `backend/app/engine.py`**, not `main.py` — `main.py` imports it, then adds the AI Mode + companies routers, mounts `/static`, and serves the UI at `/app`. `app.core.config` is imported first so `.env` (at repo root) loads before anything reads env.

```bash
# Web server (two equivalent ways; run.py just sets sys.path/cwd for you)
cd backend && python -m app.main      # or: python run.py  from the repo root
# Host/port from env: API_HOST (0.0.0.0), API_PORT (code default 11500; .env overrides),
# API_RELOAD, UVICORN_LOG_LEVEL. UI: http://localhost:<port>/app  (/ui 307-redirects to /app)

# Worker — required for BOTH pipelines. Uploads 503 when RabbitMQ is down.
docker compose up -d rabbitmq        # broker (mgmt UI on 15672)
python worker.py                     # from repo root; consumes relationship_runs + ai_mode_jobs
# The API is producer-only. Run exactly ONE worker process — AI Mode's finish/reconciler
# task registry is in-process.

# Tests — all offline (no network); unittest, NOT pytest (pytest isn't installed).
cd backend && python -m unittest discover -s tests -t .   # full suite; -t . makes tests import
                                                          # as the 'tests' package so
                                                          # tests/__init__.py's hermeticity
                                                          # guard runs
cd backend && python -m unittest tests.test_relationship_runner -v      # one module
cd backend && python -m unittest tests.test_results.TestEntityResultSerialization.test_flags_csv

# UI DOM contracts — plain node, no dependencies.
cd backend && for f in tests/*.mjs; do node "$f"; done
```

`aio-pika` / `boto3` / `supabase` are imported at module top in `engine.py`, so they must be installed even for AI-Mode-only use.

## Architecture (the parts that span files)

### Relationship pipeline — S3-only, built for 500k rows
**One** AI Mode call per row (`scrapedo_ai_client.search_ai_mode`) driven by a single editable prompt at `prompts/relationship_search.txt`, whose only placeholders are `{x_name}`, `{y_name}`, `{x_domain}` and `{input_url}` (a test enforces that set — the file goes through `.format()`, so an unsupported one KeyErrors on every row). The verdict is a Gemini Batch job: `build_relationship_prompt` / `apply_relationship_gate` / `update_relationship_block` in `gemini_llm.py`. **Every `https://` URL anywhere in the answer** — prose, `snippet_links[].link`, `references[].link` — is the candidate set the gate validates against, minus disallowed hosts and Company X's own domain.

**No `state.json` and no local disk.** S3 object presence IS the state (`raw/<shard>/row_NNNNNN.json` = done, `errors/<shard>/row_NNNNNN.json` = dead, `cleaned/…` = has a verdict; `NNNNNN` counts from **1**, so input.csv's first data row is `row_000001` while the index stays 0-based in code — `s3_run_store._idx_from_key` undoes it). **A scraped row is ONE object holding scrape.do's response body and nothing else** — the exact bytes off the wire, not a wrapper and not re-serialised JSON, because it is read by hand to judge the search prompt. There is deliberately no sidecar: the query and locale come back inside the response's own `search_parameters`, the row index IS the filename, the CSV fields are in input.csv (which the verdict phase streams via `_iter_scraped_rows`), credits are `CREDITS_PER_CALL` per billed 200, and the run's attempt total is a counter in `status.json` — the one figure not recoverable from the objects, which is why `write_outputs` takes `scrapedo_requests` from the counter.

`modes/relationship.py::ai_mode_arrays` is the single reader of `text_blocks`/`references`, and **`evidence_text` is what the verdict LLM reads: EVERY string in the stored response, in order, one per line**. Plain text, not JSON — the braces were ~25% of the evidence tokens. Nothing is selected by key; it walks whatever is there, because every rendering of "just the parts we need" lost something real — first the EVIDENCE bullets (they live under `list`, which carries no `snippet`), then `snippet_links[].link`, the resolved target of an inline link and, on a real 100-row run where `references[]` came back empty for all 100 rows, the ONLY link source scrape.do gave us. `_NON_EVIDENCE_KEYS` is an **exclude** list, the inverse of the include-list that kept losing content, so a key the provider adds tomorrow survives by default: it drops only `search_parameters` (our own prompt echoed back, and where the `https://example.com` format example and X's portfolio URL live — both would otherwise be pickable as Y's website) and `type`/`level`/`index`/`reference_indexes`. The candidate set comes from that same text, so there is one source of truth for both. AI Mode also structures the same question differently from call to call — sometimes headings, sometimes one `ordered_list`, sometimes a single paragraph — which is why nothing may key off block types.

This layout makes the EC2 instance disposable and a re-drive resumes for free. `status.json` holds O(1) counters only (~2KB at any run size) and is a cache — the phase barrier re-LISTs. Resume is ONE paginated LIST building an in-memory index set, not 500k HEADs. Writes go through `s3_run_store.put_object`, which **raises** — unlike `s3_sync.mirror_file_to_s3`, because with S3 as the only copy a swallowed PUT loses work.

**One RabbitMQ message per RUN** (`relationship_runs`, own queue and channel), body `{"run_id": …}`; concurrency comes from a bounded task window inside the consumer, sized from **`SCRAPEDO_CONCURRENCY`** (the per-account vendor cap it shares with AI Mode) — this pipeline has no concurrency knob of its own, because a second setting could only disagree with the semaphore every call already passes through. `WORKER_CONCURRENCY` is irrelevant here: it sets RabbitMQ prefetch, and there is one message per run. The consumer **acks on receipt** — the repo's only inversion of ack-after-persist — because RabbitMQ's 30-minute `consumer_timeout` would tear down a multi-hour run. Durability comes from `redrive_stale_runs` instead, which also covers a worker that was down at publish time or an instance that was replaced. One message means no barrier machinery: phase 2 is simply the next statement after phase 1.

**In-flight Gemini shards are remembered.** A Batch job runs on Google's side and is billed whether or not we are still polling, so each shard's job name is written to `<run>/batches/<first>-<last>.json` before the first poll and cleared only once its verdicts are durable in `cleaned/`. `_reattach_batches` runs at the top of the verdict phase and re-polls any surviving record, so a timeout, worker restart or crash re-attaches to the job already paid for instead of submitting an identical one.

Billing is credits only, `10 × HTTP-200`. `report.json` is **summary only** — no per-row array, which at 500k would be gigabytes.

### AI Mode engine — broker-driven (`ai_mode/{broker,worker,ai_mode_service}.py`)
Built for 500k–1M-row runs. **One RabbitMQ message = one scrape.do batch** (entities inline; 1M rows @ ai_bulk-10 = 100k messages), on a **separate durable queue** `ai_mode_jobs` (`AI_MODE_QUEUE`) bound to the shared exchange under `ai_mode.scrape`, consumed on AI Mode's **own channel** (aio_pika QoS is per-channel; prefetch + consumer count = `AI_MODE_WORKER_CONCURRENCY`, default 20) inside the same `worker.py` process.

**Durable per-batch state is FILE PRESENCE, not a state dict**: scraped ⇔ `raw_responses/request_NNNNNN.json` parses; terminal scrape failure ⇔ `request_NNNNNN.error.json` marker; cleaned ⇔ `cleaned/batch-NNNNNN.json` parses — all write-through mirrored to S3. `status.json` holds only O(1) counters, flushed at most every `AI_MODE_STATUS_FLUSH_SEC` (2s). **Single-writer rule**: the API writes status.json only before publishing; from the first publish on, only the worker writes it. Counters are a cache — the barrier always recounts the directory.

Run lifecycle:
1. **Upload** (`routers/ai_mode.py`): 503 before prepare when `broker.is_ready()` is false; otherwise prepare + Supabase row, then a background `worker.publish_run_batches(run_id)` publishes all scrape messages + one trailing `check` kick (publish failures tolerated — the reconciler republishes).
2. **Scrape** (`worker.process_scrape_job`): idempotent — parseable raw file or error marker ⇒ skip; a scrape.do API failure is a **result** (error marker + ack, `error`/`scrapedo` taxonomy); infra crashes get one redelivery then are terminalized (poison guard). **Ack only after the raw/error file is durably on disk.** The last ack's recount flips `phase` scraping→cleaning and dispatches the finish task exactly once (per-run lock + `_finish_tasks` registry).
3. **Finish** (`ai_mode_service.run_ai_mode_finish` — Phases 2+3, never raises): LLM cleanup, sync (`LLM_BATCH=false`) or the **Gemini Batch API** (`=true`; File-API JSONL, results map back **by key**, each persisted to `cleaned/<key>.json` as a durable checkpoint) — then **streaming assembly** via `run_reporting.StreamingRunReport`: reads one batch's raw+cleaned files at a time (memory O(one batch)), writes found/notFound CSVs incrementally, caps `final_report.json`'s `entities` array at `AI_MODE_REPORT_ENTITIES_MAX` (50k; above → `entities_omitted:true`), computes cost, mirrors to S3 (also on the failure path), updates Supabase, Slack ping.

**Reconciler** (`worker.reconcile_ai_mode_runs`, worker startup + `engine.periodic_ai_mode_reconciler`): (a) publishing/scraping runs with missing batches and no activity for `AI_MODE_BATCH_STALE_TIMEOUT_SEC` (900s) get missing batches republished (safe — workers skip existing files) up to `AI_MODE_BATCH_MAX_REQUEUE` (1) times, then terminalized via error markers so the barrier ALWAYS resolves (gated on a drained queue); (b) `cleaning` runs with no live finish task are re-dispatched (resumable via `cleaned/`); (c) legacy (`engine != "broker"`) runs stuck `queued/running` past `AI_MODE_LEGACY_STALE_SEC` are flipped to `failed` so the UI's "Rerun failed" button appears.

Modes in `mode_config.py` (`ai_bulk`=batch 10 / `ai_bulk_search.txt`; `ai_deep`=batch 3 / `ai_deep_search.txt`; both share `ai_cleanup.txt`). Keys validated at **upload time** (`GEMINI_API_KEY`, 400 on missing) and again in the worker (`SCRAPEDO_TOKEN`) — so the worker's env needs the scrape.do/LLM/S3/Supabase/Slack vars too.

**One retry action** (`POST …/{run_id}/resume`, "Rerun failed"): allowed only for `failed`/`completed_with_errors` (409 otherwise), 503 when the broker is down. It deletes `*.error.json` markers, clears `gemini_batch_jobs`/`requeue_attempts`, then republishes **only** batches without a parseable raw file — no scrape.do re-spend on successes; the finish task reuses `cleaned/` so only failed LLM work is redone. If the local run dir is gone, it first rehydrates from S3. **Offline tests** drive this whole engine without RabbitMQ via `tests/ai_mode_drive.py`.

### Concurrency model — worker slots vs provider caps
Two **kinds** of dial, deliberately not one:
- **`WORKER_CONCURRENCY`** = worker **slots** (RabbitMQ prefetch + consumer count). It is AI Mode's dial — `AI_MODE_WORKER_CONCURRENCY` falls back to it when unset, so one number configures the fleet. Relationship ignores it entirely. Needs a worker restart — consumers are built at startup.
- **`SCRAPEDO_CONCURRENCY`** = concurrent calls to scrape.do, enforced by ONE semaphore (`common/provider_limits.scrapedo_slot()`) so raising the slot count can never breach the vendor limit. The cap is per *account* and both pipelines run in the same worker process, so two separate caps could sum past it. It stays even when you intend to run one pipeline at a time, because that isn't fully manual: AI Mode's reconciler re-dispatches work on its own. If scrape.do also enforces requests/second, a token bucket goes **inside** that helper and no call site changes.

**Per-row latency:** one shared pooled `httpx.AsyncClient` in `common/scrapedo_http.py` — a fresh client per row cost ~400ms of DNS/TCP/TLS setup per call (636ms → 233ms measured) and forfeited keep-alive, i.e. ~55 hours across 500k rows; keepalive is sized to `SCRAPEDO_CONCURRENCY` so every slot holds a warm connection, and it is closed in `shutdown_event`. boto3 retries are `adaptive` (AWS's client-side rate limiter, their documented answer to `SlowDown`).

**What live runs proved about the provider (2026-08-25):** three completed 100-row runs at concurrency 100 (`8ffe96d9` 44s, `1a46cba0` 36s, `64b81c68` **34s / 2.94 rows/s**). 500k ≈ 47 hours at that rate. Every run shows **1 attempt per row, zero 429s**. scrape.do *queues* rather than 429ing, so client concurrency above its real capacity becomes latency — an earlier A/B (before the pooled client) had 100 running 1.9× SLOWER than 25. That A/B has not been repeated, so "100 beats 25 today" is unproven; only "100 got much faster" is. `SCRAPEDO_TIMEOUT_SECONDS=90` is the ceiling that turns a slow row into a failed one.

### Gemini batch config — ONE resolver (`common/llm_batch.py`)
Both pipelines already shared ONE driver — `ai_mode/gemini_batch.py`. `common/llm_batch.py` owns every batch setting and each call site reads it there.

- **`LLM_BATCH` is the ONLY toggle.** `_TOGGLEABLE` is `{ai_bulk, ai_deep}`; a leftover per-pipeline override in someone's `.env` is inert. `_flag`'s blank-counts-as-unset tri-state stays — `.env.example` ships `NAME=` and `env.get_bool_env` falls back only on an ABSENT variable.
- **Relationship is in `_ALWAYS_BATCH`** — its Gemini call IS the verdict, there is no inline path, so a toggle would be a setting that cannot be honoured.
- **One key per mechanical knob**: `GEMINI_BATCH_SHARD_SIZE` (5000), `GEMINI_BATCH_MAX_INFLIGHT` (5), `GEMINI_BATCH_TIMEOUT_SEC` (**172800 = 48h, Gemini's job expiry** — a Batch job runs and bills on Google's side whether or not we poll, so a shorter deadline abandons work already paid for), `GEMINI_BATCH_POLL_SEC`.
- **`GEMINI_BATCH_TIMEOUT_SEC` bounds ONE SHARD's poll, not the run and not the LLM phase.** The deadline is built inside `_poll_to_terminal`, which runs in phase 2 — so a scrape phase of any length cannot consume it, and 20 sequential waves of shards can far exceed 48h in total because each wave starts its own clock. Pinned by `test_batch_timeout_scope.py`. Never hoist that deadline to run start.
- **A shard Google never answered leaves its rows RETRYABLE.** `is_terminal()` is true for SUCCEEDED/FAILED/CANCELLED/EXPIRED alike — it means "stop polling", not "there are results". `is_success()` short-circuits on `gemini_batch.FAILED_STATES`. `_abort_if_shard_died` then drops the `batches/` record (else the next drive re-polls a corpse) and **raises before writing anything**, so the rows keep their markers and only they are resubmitted. Recovery is "Rerun failed" → the scrape phase skips every row with an object (**0 scrape.do credits**) → the LLM phase resubmits.
- **Retry is MANUAL, button-only.** `redrivable()` keeps `completed` terminal to the re-drive scan, so no run re-spends Gemini tokens unattended. In exchange, a row whose scrape succeeded but whose Gemini shard died is named `llm_incomplete` everywhere — its own `empty_response_breakdown` bucket and billing chip, its own `reporting.retry_row` reason so it reaches `retry.csv`, and a **Rerun button on the run detail page**. The discriminator is object presence: `cleaned/` **missing** = the shard died, retryable; `cleaned/` present with a null result = Gemini answered and had nothing, final. A callout renders `task_errors`, so `completed_with_errors` always has a visible cause.
- **Model: `GEMINI_BATCH_MODEL` → `GEMINI_MODEL` → `gemini-2.5-flash-lite`.** The second step is load-bearing. `test_llm_batch_config` guards it by **scanning every pipeline module for a direct env read** of any batch key.
- **Worker executor is sized** (`start_worker_consumers`): `max(32, max_inflight + 16)` via `loop.set_default_executor`. This is **relationship-only** — AI Mode polls all shards from one thread, while `relationship_runner._poll_to_terminal` blocks a pool thread per in-flight shard for hours. Headroom matters because that executor is shared with every S3 write and CSV parse in the worker.
- **Gemini is the only LLM provider, and there is no `provider` field anywhere.** `LLMConfig` is `{api_key, model, base_url=GEMINI_BASE_URL, max_retries, timeout_seconds}` and the ONLY thing env chooses is the model. `make_llm_client` stays despite having no branch left: it adapts `LLMConfig` to the client kwargs and is the seam every offline AI-Mode test patches.

## HTTP surface

| Route | Serves |
|---|---|
| `POST /uploads/relationship`, `/uploads/relationship/preview` | relationship |
| `POST /uploads/ai-mode`, `/uploads/preview`, `/uploads/ai-mode/{id}/resume` | AI Mode |
| `GET /uploads/ai-mode`, `/uploads/ai-mode/{id}/status`, `/{id}/result` | AI Mode |
| `GET /uploads/{id}/status`, `/failure-analysis`, `/result` | relationship |
| `POST /uploads/{id}/retry-failed-rows`, `/{id}/stop` | relationship |
| `GET/POST /companies`, `/companies/stats`, `/companies/runs` | shared (Supabase) |
| `GET /`, `/ui`, `/app`, `/static/*` | shared |

The Runs and Dashboard views read `/companies/runs`, i.e. Supabase — not an upload listing.

## Shared layers
- **Relationship input CSV** (`relationship_csv.parse_relationship_csv`): requires `Input_URL`, `Company_Name_X`, `Company_Name_Y` (aliases `company_x`/`company_y`). One row in is one row out — no (X,Y) dedup, no location, because the OCR'd page supplies none. Company Y is passed VERBATIM including OCR noise: that noise is meaningful input the model is asked to resolve, not something to clean up. Sample: `samples/sample_relationship.csv`.
- **AI Mode input CSV** (`models/entities.py::parse_entities_csv`): requires a company-name column (aliases `company_name|company|name|entity_name|entity|organization|organisation|legal_name`) **and** a country column (`country|country_name|nation`); optional `company_local_name|address|firm_id|industry`. Headerless 2+-col files parse positionally. `InvalidCSVError` → 400. Sample: `samples/sample_ai_mode.csv`.
- **Output CSV columns — the input file plus what we worked out.** `confirmed_relation.csv` / `notconfirmed_relation.csv` / `retry.csv` (and AI Mode's `found.csv`/`notFound.csv`) carry **every column of the uploaded CSV, verbatim and in original order**, then the computed columns. The rule is one pair of helpers in `common/text.py` — `passthrough_fieldnames` (collision-safe: an input column named `website_url` becomes `website_url__orig`; `error` is reserved for BOTH files so their headers stay parallel; `source_overrides` undoes `s3_run_store.iter_input_rows`' injected `row_index`) and `passthrough_row`. It lives there, NOT in `reporting`, because `ai_mode/run_reporting.py` is standalone-by-contract. **AI Mode's writer owns a forward-only cursor** over its `input.csv`, pulling one input row per `EntityResult` — alignment is an invariant of the writer, not of the caller — and it **replays `parse_entities_csv`'s skip-a-blank-company-name rule**, which spends no `sno`, or every row after the first blank one gets the previous row's cells. Never joins on `EntityResult.sno` (it takes the LLM's echoed value when present).
- **Per-entity result schema** (`models/results.py`): `EntityResult{website_url, confidence(0–100), flags[{flag,why}], attempt_log[{query,result,url}], error}`. `flags_csv()`/`attempt_log_csv()` join with `"\n"`, so each flag/attempt is its own line **inside one quoted CSV cell** (a real embedded newline — Excel renders multi-line; it does not break the row).
- **Outcome taxonomy** (`relationship/outcomes.py`): the found/not_found/error buckets plus the `SRC_*` / `CAT_*` vocabulary, shared with AI Mode. Pure, no I/O.
- **S3 layout** — bucket `S3_BUCKET`, region `S3_REGION`, keyed **`<company>/<pipeline>/<run_id>/…`** via the shared `core/s3.py`. Relationship is S3-ONLY (no local copy — an upload without `S3_BUCKET` is rejected with 400). AI Mode keeps a local dir and **write-throughs each file as it's produced** (`s3_sync.mirror_file_to_s3`), with an end-of-run `mirror_run_to_s3` as a backstop; its S3 writes are best-effort and never fail a run. The company-folder segment is `slugify_company` (lowercase, non-alphanumeric→`-`) for both.
- **Supabase tracking** (`core/supabase_client.py`, `services/companies.py`, `supabase/migrations/`): `companies` + `runs` tables. `runs.pipeline` is free text — `relationship | ai_bulk | ai_deep`; no enum, so removing a pipeline needs no migration. `SUPABASE_URL` must be the **bare** `https://<ref>.supabase.co` (not the `:5432` postgres string); `SUPABASE_SERVICE_ROLE_KEY` is the long JWT. Unconfigured → company/AI-upload endpoints return **503**; relationship run writes are best-effort and never fail a run. `get_supabase()` is a **thread-safe lazy singleton** (lock + double-checked locking) — the sync dashboard endpoints run on parallel threadpool threads, and an earlier race returned a half-built `None` and 503'd intermittently until reload.
- **Slack notifications** (`core/notify.py`): best-effort run completion/failure pings to `SLACK_WEBHOOK_URL` (Block Kit; no-op when unset, swallows all errors, never raises). AI Mode calls `notify_run_complete`/`notify_run_failed` in `run_ai_mode_finish`/`_fail_run`; relationship calls them from `s3_run_driver._notify_terminal`. Fields are dropped when not passed, so each run type shows only its own detail.
- **UI** (`static/js/`, `templates/index.html`): vanilla ES-module SPA with a hash router (`main.js`). Views: Dashboard, Companies, New Run, Runs, Run detail. Each view module exports `render(root, params)` returning an optional cleanup fn. `api.js` has `api()` (fetch + one retry), `pollStatus()` (2s until terminal), and `el(tag, attrs, …children)`. `ui.js` holds the ONE pipeline registry (`PIPELINES`, `PIPELINE_LABELS`, `AI_MODE_PIPELINES`, `engineOf`) — add a pipeline there, not per view. Run detail probes `/uploads/ai-mode/{id}/status` then `/uploads/{id}/status`; `?engine=ai|relationship` is an ordering hint only, never a requirement, so a bookmarked URL with no hint still resolves.

## Disk layout of run outputs
- **Relationship**: S3 only — `<company>/relationship/<run_id>/` with `input.csv`, `status.json`, `raw/`, `errors/`, `cleaned/`, `batches/`, and at terminal `confirmed_relation.csv`, `notconfirmed_relation.csv`, `retry.csv`, `report.json`, `run.log`.
- **AI Mode**: `ai_mode_results/<company_slug>/<run_id>/` — `input.csv`, `status.json`, `final_report.json`, `found.csv`, `notFound.csv`, `run.log`, `raw_responses/request_NNNNNN.json`, `cleaned/batch-NNNNNN.json`. Mirrored to S3. Legacy read-only dir: `ai_mode_result/` (singular).

## Conventions & gotchas
- **Always use ponytail for coding.** The `ponytail` plugin/skill is the required mode for all coding in this repo (writing, refactoring, reviewing, choosing dependencies): laziest solution that actually works — YAGNI, reuse existing code, stdlib/native before new deps, shortest correct diff — without ever shortcutting understanding the problem first.
- **Never add a `Co-Authored-By: Claude` (or any AI-attribution) trailer to commits.** Tell any committing subagent the same.
- **Never `git push` and never query the production Supabase DB without explicit user approval.**
- `el()` sets every attr via `setAttribute`, so `el("a", {href: undefined})` literally sets `href="undefined"`. Use the spread pattern: `...(cond ? {href: x} : {})`.
- **Billing.** scrape.do's AI Mode endpoint bills **10 credits per successful (HTTP-200) call** — never USD. Relationship reports `scrapedo_requests`/`scrapedo_credits`; AI Mode treats the same endpoint as a flat fee and reports a `scrapedo_searches` count. Only the LLM half has a USD figure (`llm_usd`, `total_usd`), from `GEMINI_*_USD_PER_1M_TOKENS`.
- **Known ceiling — `upload_result_file` reads the whole S3 object into memory** before responding. Fine for `report.json`; `confirmed_relation.csv` on a 500k-row run is large. Stream it if that bites (marked with a `ponytail:` comment at the call site).
- The full env reference lives in `.env.example`. Variables removed with the deleted pipelines are **commented out there rather than dropped**, so an old `.env` can be diffed against it.
