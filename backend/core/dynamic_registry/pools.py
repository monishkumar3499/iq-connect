"""Per-platform thread pools, the per-person cap and timeouts."""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")


class Busy(Exception):
    """No capacity right now; the message says what to do."""


class CallTimedOut(Exception):
    pass


class PlatformPools:
    def __init__(self, *, threads_per_platform: int, calls_per_person: int,
                 wait_seconds: float) -> None:
        self._threads = threads_per_platform
        self._per_person = calls_per_person
        self._wait = wait_seconds
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._platform_slots: dict[str, asyncio.Semaphore] = {}
        # Only people with calls in flight have an entry; it is dropped when
        # their last call ends, so a thousand past users cost nothing.
        self._person_slots: dict[str, asyncio.Semaphore] = {}
        self._person_users: defaultdict[str, int] = defaultdict(int)

    def _for(self, platform: str) -> tuple[ThreadPoolExecutor, asyncio.Semaphore]:
        if platform not in self._executors:
            self._executors[platform] = ThreadPoolExecutor(
                max_workers=self._threads, thread_name_prefix=f"iq-{platform}"
            )
            self._platform_slots[platform] = asyncio.Semaphore(self._threads)
        return self._executors[platform], self._platform_slots[platform]

    async def run(self, platform: str, person: str, fn: Callable[[], T], *,
                  timeout: float) -> T:
        executor, platform_slots = self._for(platform)
        person_slots = self._person_slots.setdefault(person, asyncio.Semaphore(self._per_person))
        self._person_users[person] += 1

        def leave() -> None:
            self._person_users[person] -= 1
            if self._person_users[person] == 0:
                del self._person_users[person]
                del self._person_slots[person]

        if not await _acquire(person_slots, self._wait):
            leave()
            raise Busy("You already have queries running. Wait for them to finish, then retry.")
        if not await _acquire(platform_slots, self._wait):
            person_slots.release()
            leave()
            raise Busy(f"{platform} is busy right now. Retry in a few seconds.")

        def release(_: object) -> None:
            # Runs on the event loop when the thread really finishes.
            platform_slots.release()
            person_slots.release()
            leave()

        loop = asyncio.get_running_loop()
        # Copy the caller's context so logging (request id) follows the call.
        call = functools.partial(contextvars.copy_context().run, fn)
        future = loop.run_in_executor(executor, call)
        future.add_done_callback(release)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            raise CallTimedOut(f"The query did not finish within {timeout:.0f} seconds.") from None

    def shutdown(self) -> None:
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)


async def _acquire(semaphore: asyncio.Semaphore, wait: float) -> bool:
    try:
        await asyncio.wait_for(semaphore.acquire(), wait)
        return True
    except TimeoutError:
        return False
