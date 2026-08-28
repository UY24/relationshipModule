"""Run the RabbitMQ worker from the repo root.

Consumes both queues: `relationship_runs` (one message per run) and `ai_mode_jobs`
(one message per scrape batch). Run exactly ONE of these processes.

Command:
  python worker.py
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.services.relationship.worker import main  # noqa: E402


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
