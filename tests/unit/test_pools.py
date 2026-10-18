from __future__ import annotations

import asyncio
import threading
import time

import pytest

from iq_connect.execution.pools import Busy, CallTimedOut, PlatformPools


def pools(**overrides) -> PlatformPools:
    values = {"threads_per_platform": 4, "calls_per_person": 2, "wait_seconds": 0.1, **overrides}
    return PlatformPools(**values)


def test_runs_off_the_event_loop():
    async def main():
        loop_thread = threading.get_ident()
        worker = await pools().run("demo", "alice", threading.get_ident, timeout=5)
        return loop_thread, worker

    loop_thread, worker = asyncio.run(main())
    assert loop_thread != worker


def test_one_person_cannot_take_more_than_their_share():
    gate = threading.Event()

    async def main():
        p = pools()
        slow = [asyncio.create_task(p.run("demo", "alice", gate.wait, timeout=5))
                for _ in range(2)]
        await asyncio.sleep(0.05)
        with pytest.raises(Busy, match="already have queries running"):
            await p.run("demo", "alice", lambda: 1, timeout=5)
        assert await p.run("demo", "bob", lambda: "bob ran", timeout=5) == "bob ran"
        gate.set()
        await asyncio.gather(*slow)

    asyncio.run(main())


def test_a_full_platform_answers_busy():
    gate = threading.Event()

    async def main():
        p = pools(threads_per_platform=1, calls_per_person=5)
        hold = asyncio.create_task(p.run("demo", "alice", gate.wait, timeout=5))
        await asyncio.sleep(0.05)
        with pytest.raises(Busy, match="busy right now"):
            await p.run("demo", "bob", lambda: 1, timeout=5)
        gate.set()
        await hold

    asyncio.run(main())


def test_timeout_answers_at_once_but_keeps_the_thread_counted():
    async def main():
        p = pools(threads_per_platform=1, calls_per_person=5, wait_seconds=0.05)
        started = time.perf_counter()
        with pytest.raises(CallTimedOut):
            await p.run("demo", "alice", lambda: time.sleep(0.5), timeout=0.1)
        assert time.perf_counter() - started < 0.4
        # The driver is still running: the platform's only thread is taken.
        with pytest.raises(Busy):
            await p.run("demo", "bob", lambda: 1, timeout=1)
        await asyncio.sleep(0.6)
        assert await p.run("demo", "bob", lambda: "free again", timeout=1) == "free again"

    asyncio.run(main())


def test_finished_people_leave_no_trace():
    async def main():
        p = pools()
        for person in ("alice", "bob", "carol"):
            await p.run("demo", person, lambda: None, timeout=1)
        await asyncio.sleep(0)
        return p

    p = asyncio.run(main())
    assert p._person_slots == {}
    assert dict(p._person_users) == {}


def test_errors_pass_through_and_free_the_slot():
    async def main():
        p = pools(calls_per_person=1)

        def boom():
            raise ValueError("bad sql")

        with pytest.raises(ValueError, match="bad sql"):
            await p.run("demo", "alice", boom, timeout=1)
        assert await p.run("demo", "alice", lambda: "ok", timeout=1) == "ok"

    asyncio.run(main())
