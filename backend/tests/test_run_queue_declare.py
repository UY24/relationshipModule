"""The PUBLISHER must declare the run queue, not just the worker.

Live failure this pins (run 7a03eaa0, 2026-08-25): the exchange is DIRECT, and
``relationship_runs`` was declared+bound only inside ``consume_runs`` — the worker. A run
published while the worker had never bound its queue against that broker was silently
DISCARDED by RabbitMQ: no error, no log, no message in any queue, and the run sat at
phase="queued" until the stale-run scan found it up to RELATIONSHIP_STALE_SEC (900s) later.
"""
import asyncio
import unittest

from app.services.relationship import s3_run_driver as driver


class FakeQueue:
    def __init__(self, name: str) -> None:
        self.name = name
        self.bindings: list[tuple[object, str]] = []

    async def bind(self, exchange, routing_key: str) -> None:
        self.bindings.append((exchange, routing_key))


class FakeChannel:
    def __init__(self, fail_on: str | None = None) -> None:
        self.queues: dict[str, FakeQueue] = {}
        self.fail_on = fail_on

    async def declare_queue(self, name: str, durable: bool = False) -> FakeQueue:
        if name == self.fail_on:
            raise RuntimeError("broker said no")
        assert durable, f"{name} must be durable — a run message outlives a broker restart"
        queue = self.queues.setdefault(name, FakeQueue(name))
        return queue


class DeclareRunQueuesTests(unittest.TestCase):
    def test_the_queue_is_declared_and_bound(self) -> None:
        channel, exchange = FakeChannel(), object()
        asyncio.run(driver.declare_run_queues(channel, exchange))
        expected = dict(driver.run_queues())
        self.assertEqual(set(channel.queues), set(expected))
        self.assertEqual({"relationship_runs"}, set(channel.queues))
        for name, queue in channel.queues.items():
            self.assertEqual(queue.bindings, [(exchange, expected[name])],
                             f"{name} not bound to the exchange on its routing key")

    def test_a_failing_declare_never_raises(self) -> None:
        """Best-effort, and it must NEVER raise: this runs inside the API's startup, so a
        broker hiccup here would take the whole API down."""
        channel = FakeChannel(fail_on="relationship_runs")
        asyncio.run(driver.declare_run_queues(channel, object()))
        self.assertEqual({}, channel.queues)

    def test_routing_key_matches_what_the_publisher_sends(self) -> None:
        """The binding is useless if it does not match the key
        engine.publish_relationship_run uses — that mismatch is the same silent drop,
        just with the queue existing."""
        from app.services.relationship import relationship_runner

        self.assertEqual(dict(driver.run_queues()), {
            relationship_runner.RELATIONSHIP_QUEUE:
                relationship_runner.RELATIONSHIP_ROUTING_KEY,
        })


if __name__ == "__main__":
    unittest.main()
