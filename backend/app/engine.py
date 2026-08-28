"""The FastAPI ``app``: relationship upload/status/result endpoints, the RabbitMQ
connection every pipeline publishes on, and the worker-consumer lifecycle.

This is the surviving core of a ~8.3k-line monolith that once served five pipelines.
Only two remain, and they split the work cleanly:

  relationship  — one RabbitMQ message per RUN. This module creates the run in S3 and
                  publishes it; ``services/relationship/`` drives it. No state.json, no
                  local disk: S3 object presence IS the state.
  ai_bulk/ai_deep — ``routers/ai_mode.py`` + ``services/ai_mode/``. Rides this module's
                  RabbitMQ connection on its own channel/queue, and its consumers are
                  started/stopped by ``start_worker_consumers`` below.

Everything provider-facing lives elsewhere: scrape.do clients in ``services/relationship/``
and ``services/ai_mode/``, shared HTTP/env/text/batch helpers in ``services/common/``.
"""
import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import quote

import aio_pika
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.core.config import PROJECT_ROOT
from app.services.common import llm_batch
from app.services.common.env import get_bool_env, get_float_env, get_int_env
from app.services.relationship.constants import PIPELINE_RELATIONSHIP


def load_local_env(env_path: str = ".env") -> None:
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as env_file:
            for line in env_file:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                # Only fill keys that are truly missing. Tests may intentionally blank
                # credential vars (via tests/__init__.py) to prevent accidental hits to
                # real cloud endpoints; respect that by not re-populating blanked vars.
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


def patch_aio_pika_connection_del() -> None:
    # aio_pika Connection.__del__ schedules self.close() via ensure_future().
    # During interpreter/thread shutdown (e.g., GC in asyncio_0), there may be
    # no current event loop, which raises RuntimeError and leaks a coroutine warning.
    # Guarding here keeps shutdown noise-free without changing normal close flow.
    try:
        import aio_pika.connection as aio_pika_connection_module
    except Exception:
        return

    connection_cls = getattr(aio_pika_connection_module, "Connection", None)
    if connection_cls is None:
        return
    if getattr(connection_cls, "_safe_del_patched", False):
        return

    def _safe_del(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            is_closed = bool(getattr(self, "is_closed", True))
        except Exception:
            is_closed = True
        if is_closed or loop.is_closed():
            return
        try:
            loop.create_task(self.close())
        except Exception:
            pass

    connection_cls.__del__ = _safe_del
    setattr(connection_cls, "_safe_del_patched", True)


patch_aio_pika_connection_del()


load_local_env(str(PROJECT_ROOT / ".env"))
app = FastAPI(title="Single RA ISI API", version="2.0.0")

rabbitmq_connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
rabbitmq_channel: Optional[aio_pika.abc.AbstractChannel] = None
rabbitmq_exchange: Optional[aio_pika.abc.AbstractExchange] = None
rabbitmq_consumer_tasks: list[asyncio.Task] = []
rabbitmq_last_error: Optional[str] = None
ai_mode_reconciler_task: Optional[asyncio.Task] = None
ai_mode_reconciler_stop: Optional[asyncio.Event] = None

# Env readers live in common/env.py; kept under their private names here so the
# module-qualified callers (main.py, tests) resolve.
_get_float_env = get_float_env
_get_int_env = get_int_env
_get_bool_env = get_bool_env

_RELATIONSHIP_FILES = ("confirmed_relation.csv", "notconfirmed_relation.csv",
                       "retry.csv", "report.json", "run.log")

# Keyed by BOTH status.json phases and report.json summary statuses (the two vocabularies
# do not collide). "stopped" maps to "completed" deliberately: run_detail.js's terminal
# set has no rule for it, so passing it through would poll forever and never render the
# Files card — and a stopped run IS terminal, with its outputs written.
_RELATIONSHIP_RUN_STATUS = {
    "queued": "queued", "scraping": "processing", "cleaning": "processing",
    "reporting": "processing", "completed": "completed",
    "completed_with_errors": "completed_with_errors",
    "stopped": "completed", "failed": "failed",
}
# Mirrors run_detail.js's ROW_TERMINAL_STATUSES.
_RELATIONSHIP_TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed"}


def _build_rabbitmq_url(host: str, port: int, user: str, password: str, vhost: str) -> str:
    encoded_vhost = quote(vhost, safe="")
    return f"amqp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{encoded_vhost}"


def get_rabbitmq_url() -> str:
    host = os.getenv("RABBITMQ_HOST", "127.0.0.1")
    port = _get_int_env("RABBITMQ_PORT", 5672)
    user = os.getenv("RABBITMQ_USER", "guest")
    password = os.getenv("RABBITMQ_PASS", "guest")
    vhost = os.getenv("RABBITMQ_VHOST", "/")
    return _build_rabbitmq_url(host, port, user, password, vhost)


async def init_rabbitmq() -> None:
    global rabbitmq_connection, rabbitmq_channel, rabbitmq_exchange, rabbitmq_last_error

    host = os.getenv("RABBITMQ_HOST", "127.0.0.1")
    port = _get_int_env("RABBITMQ_PORT", 5672)
    user = os.getenv("RABBITMQ_USER", "guest")
    password = os.getenv("RABBITMQ_PASS", "guest")
    vhost = os.getenv("RABBITMQ_VHOST", "/")

    primary_url = _build_rabbitmq_url(host, port, user, password, vhost)
    try:
        rabbitmq_connection = await aio_pika.connect_robust(primary_url)
    except Exception as primary_exc:
        # Local dev fallback: try guest/guest on localhost if custom creds are rejected.
        if host in {"127.0.0.1", "localhost"} and not (user == "guest" and password == "guest"):
            fallback_url = _build_rabbitmq_url(host, port, "guest", "guest", vhost)
            try:
                rabbitmq_connection = await aio_pika.connect_robust(fallback_url)
                rabbitmq_last_error = (
                    f"Primary credentials failed ({str(primary_exc)}); "
                    "connected using guest/guest fallback."
                )
            except Exception:
                raise primary_exc
        else:
            raise
    rabbitmq_channel = await rabbitmq_connection.channel()
    await rabbitmq_channel.set_qos(prefetch_count=1)

    rabbitmq_exchange = await rabbitmq_channel.declare_exchange(
        os.getenv("RABBITMQ_EXCHANGE", "singleRA_search"),
        aio_pika.ExchangeType.DIRECT,
        durable=True,
    )
    rabbitmq_last_error = None

    # The relationship run queue, declared by the PUBLISHER as well as the worker. A
    # direct exchange drops an unroutable message on the floor, so publishing a run before
    # the worker had ever bound its queue lost the message entirely and the run stalled at
    # phase="queued" until the stale-run scan found it. See declare_run_queues.
    try:
        from app.services.relationship import s3_run_driver

        await s3_run_driver.declare_run_queues(rabbitmq_channel, rabbitmq_exchange)
    except Exception as exc:
        print(f"[s3-run-queues] declare failed (runs will wait for the re-drive scan): {exc}")

    # AI Mode rides the same connection with its OWN channel/queue (independent QoS —
    # scrape.do concurrency must not share the relationship prefetch). Best-effort: a
    # failure here 503s AI-Mode uploads (broker.is_ready() False) but must not take the
    # relationship path down with it.
    try:
        from app.services.ai_mode import broker as ai_mode_broker

        await ai_mode_broker.init_ai_mode_broker(rabbitmq_connection)
    except Exception as exc:
        print(f"[ai-mode-broker] init failed (relationship unaffected): {exc}")


async def close_rabbitmq() -> None:
    global rabbitmq_connection, rabbitmq_channel, rabbitmq_exchange, rabbitmq_consumer_tasks

    await stop_worker_consumers()

    try:
        from app.services.ai_mode import broker as ai_mode_broker

        await ai_mode_broker.close_ai_mode_broker()
    except Exception:
        pass

    rabbitmq_exchange = None
    rabbitmq_channel = None

    if rabbitmq_connection is not None:
        await rabbitmq_connection.close()
        rabbitmq_connection = None


async def publish_relationship_run(run_id: str) -> None:
    """Publish the ONE message that drives a whole relationship run.

    Goes to the shared named exchange under RELATIONSHIP_ROUTING_KEY — the same
    exchange consume_relationship_runs binds its queue to, and the one AI Mode already
    uses, so every binding is visible in one place in the management UI.

    Failure is tolerated — and that tolerance lives HERE, not at the call sites, so every
    caller (upload, retry-failed-rows, anything added later) gets it. Both callers reach
    this only AFTER the run is fully created in S3, so letting a wait_for timeout or any
    AMQP error propagate 500s the request with no upload_id while the run exists and the
    re-drive scan starts spending money on it 300s later. Never raises: the worker's
    stale-run scan picks the run up within RELATIONSHIP_REDRIVE_SCAN_SEC, so a broker
    hiccup delays a run, never loses it.
    """
    from app.services.relationship.relationship_runner import RELATIONSHIP_ROUTING_KEY

    if rabbitmq_exchange is None:
        print(f"[relationship] run {run_id} queued without a broker; "
              f"the re-drive scan will start it")
        return
    try:
        await asyncio.wait_for(
            rabbitmq_exchange.publish(
                aio_pika.Message(
                    body=json.dumps({"run_id": run_id}).encode("utf-8"),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                ),
                routing_key=RELATIONSHIP_ROUTING_KEY,
            ),
            timeout=5.0,
        )
    except Exception as exc:
        print(f"[relationship] publish failed for run {run_id} "
              f"({type(exc).__name__}: {exc}); the re-drive scan will start it")


async def periodic_ai_mode_reconciler() -> None:
    """Re-run AI Mode's reconcile sweep every GEMINI_BATCH_RECONCILE_INTERVAL_SEC until
    the stop event is set — it catches a lost in-process finish task without a restart.

    Relationship needs no equivalent here: its own re-drive scan lives in the worker
    (``relationship_runner.redrive_stale_runs``, started by ``worker._start_run_workers``).
    """
    interval = max(30.0, _get_float_env("GEMINI_BATCH_RECONCILE_INTERVAL_SEC", 300.0))
    stop = ai_mode_reconciler_stop
    if stop is None:
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            from app.services.ai_mode import worker as ai_mode_worker

            await ai_mode_worker.reconcile_ai_mode_runs()
        except Exception as exc:
            print(f"[ai-mode-reconcile] sweep failed: {exc}")


async def start_worker_consumers() -> None:
    global ai_mode_reconciler_task, ai_mode_reconciler_stop

    # relationship_runner._poll_to_terminal does a BLOCKING sleep inside
    # asyncio.to_thread, so each in-flight Gemini shard holds one default-executor thread for
    # the job's whole (multi-hour) life. The default pool is min(32, cpu+4) -- SIX on a 2-vCPU
    # box -- so GEMINI_BATCH_MAX_INFLIGHT above that created nothing. Its own docstring
    # prescribes exactly this fix.
    #
    # Sized off max_inflight PLUS headroom, never max_inflight alone: this executor is shared
    # with every S3 write, CSV parse and _write_json in the worker, and starving those to make
    # room for pollers would trade one bottleneck for a worse one. Consumers-only path, since
    # the API process makes no provider calls.
    #
    # AI Mode does NOT need this (it polls all shards from a single thread) -- it is
    # relationship that pays.
    executor_size = max(32, llm_batch.max_inflight() + 16)
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=executor_size, thread_name_prefix="worker"))
    print(f"[worker] default executor sized to {executor_size} threads "
          f"(GEMINI_BATCH_MAX_INFLIGHT={llm_batch.max_inflight()})")

    if ai_mode_reconciler_task is None:
        ai_mode_reconciler_stop = asyncio.Event()
        ai_mode_reconciler_task = asyncio.create_task(periodic_ai_mode_reconciler())

    # AI Mode consumers ride the same worker process (own channel/queue/QoS).
    # Best-effort: an AI-Mode failure must not take the relationship consumers down.
    try:
        from app.services.ai_mode import worker as ai_mode_worker

        await ai_mode_worker.start_ai_mode_consumers()
        # Startup sweep: re-dispatch dead finish tasks, republish lost batches,
        # and flip phantom-'running' runs left behind by a hard kill.
        await ai_mode_worker.reconcile_ai_mode_runs()
    except Exception as exc:
        print(f"[ai-mode-worker] consumers failed to start: {exc}")


async def stop_worker_consumers() -> None:
    global rabbitmq_consumer_tasks

    try:
        from app.services.ai_mode import worker as ai_mode_worker

        await ai_mode_worker.stop_ai_mode_consumers()
    except Exception:
        pass

    if not rabbitmq_consumer_tasks:
        return

    # Give loops time to exit naturally (avoids cancelling in-flight RPC).
    done, pending = await asyncio.wait(rabbitmq_consumer_tasks, timeout=2.0)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            await task
        except asyncio.CancelledError:
            pass
    for task in rabbitmq_consumer_tasks:
        if task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
    rabbitmq_consumer_tasks = []


@app.on_event("startup")
async def startup_event() -> None:
    global rabbitmq_consumer_tasks, rabbitmq_last_error

    try:
        # The API is producer-only. Consumers live in worker.py — and only there, since
        # relationship's consumer is started from that module and AI Mode's finish-task
        # registry is in-process, so a second consumer host would half-work.
        await init_rabbitmq()
    except Exception as exc:
        rabbitmq_last_error = str(exc)
        rabbitmq_consumer_tasks = []


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global ai_mode_reconciler_task, ai_mode_reconciler_stop
    if ai_mode_reconciler_stop is not None:
        ai_mode_reconciler_stop.set()
    if ai_mode_reconciler_task is not None:
        ai_mode_reconciler_task.cancel()
        try:
            await ai_mode_reconciler_task
        except (asyncio.CancelledError, Exception):
            pass
        ai_mode_reconciler_task = None
        ai_mode_reconciler_stop = None
    # Release the pooled scrape.do connections shared by the relationship AI-Mode client.
    try:
        from app.services.common import scrapedo_http

        await scrapedo_http.close_shared_client()
    except Exception:
        pass
    await close_rabbitmq()


@app.get("/", response_class=JSONResponse)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "single-ra-isi"}


@app.get("/ui")
async def ui_page() -> RedirectResponse:
    """Legacy UI retired — redirect to the new console at /app."""
    return RedirectResponse(url="/app", status_code=307)


@app.post("/uploads/relationship/preview")
async def preview_relationship_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Dry-run parse for the New Run preview: header mapping, row count, and a
    sample of the rows that would be searched (one row in → one row out, no dedup).
    Costs nothing — no state, no queue, no Supabase."""
    from app.services.relationship.relationship_csv import (
        InvalidRelationshipCSV,
        parse_relationship_csv,
    )

    raw = await file.read()
    try:
        parsed = parse_relationship_csv(raw)
    except InvalidRelationshipCSV as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = parsed["rows"]          # first SAMPLE_LIMIT only, never the whole file
    total = parsed["total_rows"]
    warnings: list[str] = []
    if not rows:
        warnings.append("No searchable rows — the CSV contains no data rows.")

    return {
        "total_rows": total,
        "relationship": True,
        "warnings": warnings,
        "columns_detected": parsed["columns_detected"],
        "sample_columns": ["company_name_x", "company_name_y", "input_url"],
        "sample_rows": [
            {
                "company_name_x": r["x_name"],
                "company_name_y": r["y_name"],
                "input_url": r["input_url"],
            }
            for r in rows
        ],
    }


@app.post("/uploads/relationship")
async def create_relationship_upload(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    company_name: str = Form(""),
) -> dict[str, Any]:
    import uuid

    from app.core import s3 as core_s3
    from app.services.relationship import s3_run_store as rel_store
    from app.services.relationship.relationship_csv import (
        InvalidRelationshipCSV,
        parse_relationship_csv,
    )

    raw = await file.read()
    # CSV first: a bad CSV must report the CSV problem, not a config problem.
    try:
        # sample_limit=0: the upload path needs the row COUNT and the raw bytes, which
        # go straight to S3 for the worker to stream. It never looks at parsed rows.
        parsed = parse_relationship_csv(raw, sample_limit=0)
    except InvalidRelationshipCSV as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # No empty-rows guard: parse_relationship_csv raises on a blank required value and
    # on a header-only CSV, so total_rows is never 0 when it returns.
    for env_key in ("GEMINI_API_KEY", "SCRAPEDO_TOKEN"):
        if not os.getenv(env_key, "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"{env_key} is not configured — required for the relationship pipeline.")
    # S3 is not optional here the way it is for the state-driven pipelines: this run has
    # no local disk and no state.json, so an unset bucket means the very next put_bytes
    # raises RuntimeError and the user gets an opaque 500 instead of this 400.
    if not core_s3.is_configured():
        raise HTTPException(
            status_code=400,
            detail="S3_BUCKET is not configured — required for the relationship pipeline.")

    run_id = uuid.uuid4().hex
    prefix = rel_store.run_prefix(company_name or company_id, run_id)
    total = parsed["total_rows"]

    # Best-effort Supabase run row; create_run never raises (returns None untracked).
    # get_company_service() itself is None when Supabase is unconfigured.
    from app.services.companies import get_company_service

    svc = get_company_service()
    run_db_id = await asyncio.to_thread(
        svc.create_run, company_id=company_id, pipeline=PIPELINE_RELATIONSHIP,
        run_ref=run_id, total_rows=total) if svc is not None else None

    await asyncio.to_thread(rel_store.put_bytes, rel_store.input_key(prefix), raw)
    # run_db_id rides in the pointer: phase 3 has no state dict to read it from.
    await asyncio.to_thread(rel_store.write_run_pointer, run_id, prefix,
                            company_name or company_id, run_db_id)
    # The API writes status.json exactly once, before publishing. From the first scrape
    # on, the worker is the only writer.
    # created_at is stamped ONCE here and carried by every later drive, so total wall
    # clock survives a worker restart mid-run.
    counters = rel_store.Counters(prefix, rows_total=total, phase="queued")
    await asyncio.to_thread(counters.flush, True)

    await publish_relationship_run(run_id)
    return {"upload_id": run_id, "pipeline": PIPELINE_RELATIONSHIP,
            "total_rows": total, "company_id": company_id}


def _find_s3_run(run_id: str) -> tuple[Optional[dict[str, Any]], str]:
    """(pointer, segment) for a relationship run, or (None, "") if this id is not one.

    A relationship run lives entirely in S3 with no state.json. Every endpoint that takes
    an upload_id — status, stop, retry, failure-analysis, result files — resolves it here,
    and a miss is what turns into the 404.
    """
    from app.services.relationship import s3_run_store as run_store

    pointer = run_store.read_run_pointer(run_id, run_store.PIPELINE_SEGMENT)
    return (pointer, run_store.PIPELINE_SEGMENT) if pointer else (None, "")


@app.post("/uploads/{upload_id}/retry-failed-rows")
async def retry_failed_rows(
    upload_id: str,
    limit: int = Query(0, ge=0, le=5000),
) -> dict[str, Any]:
    """Rerun failed: drop the error markers so a re-drive rescrapes exactly those rows.

    Deliberately not guarded on the broker — the re-drive scan starts the run even if the
    publish below is skipped.
    """
    from app.services.relationship import s3_run_store as rel_store

    pointer, _segment = await asyncio.to_thread(_find_s3_run, upload_id)
    if not pointer:
        raise HTTPException(status_code=404, detail="Upload ID not found")
    prefix = pointer["prefix"]

    def _dead_markers() -> list[str]:
        keys = list(rel_store.iter_keys(f"{prefix}/errors/"))
        # `limit` used to be accepted and then ignored, so a cautious "retry 100 of
        # them" silently tried all 50k.
        return keys[:limit] if limit else keys

    # Bulk delete: 1000 keys per call instead of one call per row, which is what made
    # this time out on any run with a lot of dead rows.
    removed = await asyncio.to_thread(
        lambda: rel_store.delete_objects(_dead_markers()))

    # Rows that scraped fine but never got a verdict, because their Gemini shard died.
    # They have NO error marker, so `removed` cannot see them — and a run whose only
    # problem was a dead shard therefore reported "Enqueued 0 failed rows" while
    # correctly republishing and redoing exactly this work. Counted, not deleted: their
    # scrape objects are what make the rerun free on the provider side. Every scraped
    # relationship row owes an LLM result — its Gemini verdict IS the answer, and there
    # is no inline path — so the owed set is simply done-minus-cleaned.
    def _unjudged() -> int:
        return len(rel_store.list_done_rows(prefix) - rel_store.list_cleaned_rows(prefix))

    pending_llm = await asyncio.to_thread(_unjudged)
    await asyncio.to_thread(rel_store.clear_stop, prefix)
    await publish_relationship_run(upload_id)
    return {"upload_id": upload_id, "retried_rows": removed,
            "pending_llm_rows": pending_llm,
            # Both kinds of work count: a dead row to re-scrape and an unjudged row to
            # re-send to Gemini are both things this request just put back in flight.
            "enqueued_rows": removed + pending_llm,
            "status_url": f"/uploads/{upload_id}/status"}


@app.post("/uploads/{upload_id}/stop")
async def stop_upload(upload_id: str) -> dict[str, Any]:
    """Stop a running relationship run.

    There are no per-row records and no engine-side Gemini batch to cancel: the stop is
    an S3 marker the runner polls between rows.
    """
    from app.services.relationship import s3_run_store as rel_store

    pointer, _segment = await asyncio.to_thread(_find_s3_run, upload_id)
    if not pointer:
        raise HTTPException(status_code=404, detail="Upload ID not found")
    await asyncio.to_thread(rel_store.request_stop, pointer["prefix"])
    # run_detail.js reports stopped_rows/batch_cancelled in its confirmation message;
    # neither is knowable here (rows are not tracked individually), so send honest zeros
    # rather than leave the UI rendering "undefined".
    return {"upload_id": upload_id, "stop_requested": True,
            "stopped_rows": 0, "batch_cancelled": False,
            "status_url": f"/uploads/{upload_id}/status"}


def _s3_run_available_files(prefix: str, names: tuple[str, ...]) -> list[str]:
    """Which of the run's output files actually exist.

    One scoped LIST per name (each returns 0 or 1 keys) rather than a single LIST of the
    run prefix: at 500k rows that prefix holds a million per-row objects.
    """
    from app.services.relationship import s3_run_store as run_store

    return [name for name in names
            if next(run_store.iter_keys(f"{prefix}/{name}"), None) is not None]


def _s3_run_elapsed(counters: dict[str, Any]) -> Optional[int]:
    """Wall clock from an S3-only run's created_at to now, for mid-run display.

    report.json only exists once the run is terminal, so without this the Processing-time
    tile stayed blank for the whole run.
    """
    import calendar
    import time as _time

    try:
        started = calendar.timegm(
            _time.strptime(str(counters.get("created_at")), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return max(0, int(_time.time() - started))


async def _s3_run_status(run_id: str, *, segment: str, pipeline: str,
                         files: tuple[str, ...],
                         fallback: Any) -> Optional[dict[str, Any]]:
    """Build the /status response for an S3-only run from its status.json counters.

    Returns None when this id is not a run of this pipeline. `fallback` builds the mid-run
    summary for the window before report.json exists.
    """
    from app.services.relationship import s3_run_store as rel_store

    pointer = await asyncio.to_thread(rel_store.read_run_pointer, run_id, segment)
    if not pointer:
        return None
    prefix = str(pointer.get("prefix") or "")
    counters = await asyncio.to_thread(rel_store.read_status, prefix) or {}
    phase = str(counters.get("phase") or "queued")
    total = int(counters.get("rows_total") or 0)
    scraped = int(counters.get("rows_scraped") or 0)
    failed = int(counters.get("rows_failed") or 0)

    report = await asyncio.to_thread(
        rel_store.get_object, f"{prefix}/report.json") or {}
    summary = report.get("summary") or fallback(counters, total, scraped, failed)
    # Prefer the status write_outputs computed — it is what _notify_terminal sent to
    # Supabase, so taking it here is what stops the detail page and the Runs list
    # disagreeing (phase only ever reaches "completed", never "completed_with_errors").
    #
    # But ONLY when phase == "completed", because report.json OUTLIVES its run:
    # retry_failed_rows deletes the error markers, not the outputs, so a re-driven run
    # sits at phase "scraping" with the PREVIOUS run's terminal report.json still in
    # place. write_outputs rewrites report.json immediately before setting the phase to
    # "completed", so that phase — and only that one — proves the report is this drive's.
    # ("stopped" needs no special case: write_outputs makes summary["status"] "stopped"
    # exactly when it sets that phase, and both map to "completed" below.)
    reported = str(summary.get("status") or "") if phase == "completed" else ""
    status = _RELATIONSHIP_RUN_STATUS.get(reported or phase, "processing")

    # Only advertise files that exist. A run that failed mid-scrape is terminal — so the
    # UI renders the Files card — but wrote none of the four, and without this guard every
    # link is an enabled 404. Gated on the derived status, so a re-driven run
    # (non-terminal again) advertises nothing either.
    available: list[str] = []
    if status in _RELATIONSHIP_TERMINAL_STATUSES:
        available = await asyncio.to_thread(_s3_run_available_files, prefix, files)

    return {
        "upload_id": run_id,
        "pipeline": pipeline,
        # Mid-run the report doesn't exist yet, so serve timings from the counters. The UI
        # reads processing_seconds_total/avg at the top level, not inside the summary.
        "created_at": counters.get("created_at"),
        "processing_seconds_total": summary.get("processing_seconds_total"),
        "processing_seconds_avg": summary.get("processing_seconds_avg"),
        "company_name": pointer.get("company_name"),
        "status": status,
        "total_rows": total,
        "processed_rows": scraped + failed,
        "failed_rows": failed,
        "phase": phase,
        "updated_at": counters.get("updated_at"),
        "run_summary": {**summary, "available_files": available},
        "files": list(files),
    }


def _relationship_fallback_summary(counters: dict[str, Any], total: int, scraped: int,
                                   failed: int) -> dict[str, Any]:
    """The summary shown while a relationship run is still in flight."""
    return {
        "total_rows": total, "websites_found": 0,
        "websites_not_found": max(0, total - scraped),
        "confidence_mode": "llm",
        # Always true for this pipeline — phase 2 is always the Gemini Batch verdict pass,
        # there is no per-row LLM path. Must match relationship_outputs' summary so the
        # Batch chip doesn't flip from "On" to "Off" while a run is still in progress.
        "is_batch": True,
        # Mid-run the LLM hasn't reported usage yet (it arrives with the batch results), so
        # these are honest zeros rather than absent keys — the tiles render 0, not blank,
        # and fill in for real once report.json exists.
        "model": llm_batch.batch_model(),
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "processing_seconds_total": _s3_run_elapsed(counters),
        "phase_seconds": {"scraping": int(counters.get("scrape_seconds") or 0),
                          "cleaning": int(counters.get("llm_seconds") or 0)},
        "outcome_breakdown": {"found": 0, "not_found": 0, "errored": failed},
        # Same key relationship_outputs writes into report.json, so the mid-run card and
        # the terminal one render the same chips.
        "empty_response_breakdown": {
            "no_ai_text": int(counters.get("rows_billed_empty") or 0),
            "llm_incomplete": 0},
        "cost": {"scrapedo_requests": int(counters.get("requests") or 0),
                 "scrapedo_credits": int(counters.get("credits") or 0),
                 "scrapedo_error_requests": 0, "scrapedo_billed_empty":
                     int(counters.get("rows_billed_empty") or 0),
                 "llm_usd": 0.0, "total_usd": 0.0},
    }


async def _relationship_status(run_id: str) -> Optional[dict[str, Any]]:
    """Build the /status response for a relationship run. None if it is not one."""
    from app.services.relationship import s3_run_store as rel_store

    return await _s3_run_status(
        run_id, segment=rel_store.PIPELINE_SEGMENT, pipeline=PIPELINE_RELATIONSHIP,
        files=_RELATIONSHIP_FILES, fallback=_relationship_fallback_summary)


@app.get("/uploads/{upload_id}/status")
async def upload_status(upload_id: str) -> dict[str, Any]:
    """A relationship run is counter-driven — there is no state.json to summarise."""
    status = await _relationship_status(upload_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Upload ID not found")
    return status


async def _relationship_failure_analysis(
    run_id: str, sample_limit: int,
) -> Optional[dict[str, Any]]:
    """build_failure_analysis's shape, sourced from errors/ objects instead of rows[].

    The "View failed rows" control run_detail.js offers whenever outcome_breakdown.errored
    is non-zero calls this; without a branch here a relationship run answered 404 and the
    page showed a red error under its own button.

    ``failed_rows`` is exact (one LIST of errors/, the same scan list_done_rows does). The
    aggregate buckets are computed over the SAMPLE only — the alternative is GETting up to
    500k error objects to answer a debug panel that reads neither.
    """
    from app.services.relationship import s3_run_store as rel_store

    pointer, _segment = await asyncio.to_thread(_find_s3_run, run_id)
    if not pointer:
        return None
    prefix = str(pointer.get("prefix") or "")
    limit = max(1, int(sample_limit))

    def _by_row_index(key: str) -> tuple[int, int, str]:
        # NOT plain sorted(): shard directories are numeric but unpadded, so errors/10/
        # sorts before errors/2/ and the "first N" sample becomes a lexicographic slice
        # once a run exceeds ten shards.
        idx = rel_store._idx_from_key(key)
        return (1, 0, key) if idx is None else (0, idx, "")

    def _collect() -> tuple[int, list[dict[str, Any]]]:
        keys = list(rel_store.iter_keys(f"{prefix}/errors/"))
        rows: list[dict[str, Any]] = []
        for key in sorted(keys, key=_by_row_index)[:limit]:
            envelope = rel_store.get_object(key) or {}
            fields = envelope.get("fields") or {}
            rows.append({
                "row_index": envelope.get("row_index"),
                "company_name": fields.get("y_name"),
                # How many scrape.do calls this row actually cost before it died. Without
                # it a row that burned all SCRAPEDO_MAX_RETRIES reads exactly like a row
                # that was tried once, and the retries look like they were never there.
                "attempts": envelope.get("request_count"),
                # Only scrape.do can produce an error object — the verdict and reporting
                # phases never write one (see relationship_runner._scrape_one).
                "error_source": "scrapedo",
                "error_category": envelope.get("error_category"),
                "error": envelope.get("error"),
                "official_website": None,
                "status_updated_at": None,
            })
        rows.sort(key=lambda row: (row["row_index"] is None, row["row_index"]))
        return len(keys), rows

    counters = await asyncio.to_thread(rel_store.read_status, prefix) or {}
    failed_count, sample = await asyncio.to_thread(_collect)
    total_rows = int(counters.get("rows_total") or 0)

    def _buckets(field: str, name: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in sample:
            value = str(row.get(field) or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
        return [{name: k, "count": v} for k, v in
                sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]]

    return {
        "upload_id": run_id,
        "status": str(counters.get("phase") or ""),
        "gemini_batch": None,
        "total_rows": total_rows,
        "failed_rows": failed_count,
        "failed_rate_pct": (round((failed_count / total_rows) * 100, 2)
                            if total_rows else 0.0),
        "failed_missing_official_website": failed_count,
        "error_buckets": _buckets("error", "reason"),
        "search_attempt_error_buckets": [],
        "by_source": _buckets("error_source", "source"),
        "by_category": _buckets("error_category", "category"),
        "sample_failed_rows": sample,
    }


@app.get("/uploads/{upload_id}/failure-analysis")
async def upload_failure_analysis(
    upload_id: str,
    sample_limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    analysis = await _relationship_failure_analysis(upload_id, sample_limit)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Upload ID not found")
    return analysis


@app.get("/uploads/{upload_id}/result")
async def upload_result_file(
    upload_id: str,
    file: str = Query(...),
    download: bool = Query(False),
) -> Response:
    # ponytail: reads the whole object into memory before responding. Fine for a report,
    # but confirmed_relation.csv on a 500k-row run is large — stream it if that bites.
    from app.services.relationship import s3_run_store as rel_store

    pointer, _segment = await asyncio.to_thread(_find_s3_run, upload_id)
    if not pointer:
        raise HTTPException(status_code=404, detail="Upload ID not found")
    allowed = set(_RELATIONSHIP_FILES) | {"input.csv", "status.json"}
    if file not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown file {file!r}")
    data = await asyncio.to_thread(rel_store.get_bytes, f"{pointer['prefix']}/{file}")
    if data is None:
        raise HTTPException(status_code=404, detail=f"{file} not available yet")
    if file.endswith(".csv"):
        media = "text/csv"
    elif file.endswith(".json"):
        media = "application/json"
    else:
        media = "text/plain"
    headers = {"Content-Disposition": f'attachment; filename="{file}"'} if download else {}
    return Response(content=data, media_type=media, headers=headers)
