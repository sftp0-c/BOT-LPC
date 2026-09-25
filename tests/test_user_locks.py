import asyncio

from utils import UserLocks


def test_lock_is_kept_until_all_interested_calls_release_it():
    locks = UserLocks()

    first = locks.get("user")
    second = locks.get("user")

    assert first is second
    assert len(locks) == 1

    locks.release("user")
    assert len(locks) == 1

    locks.release("user")
    assert len(locks) == 0


async def test_waiting_handler_and_third_handler_share_the_same_lock():
    locks = UserLocks()
    key = "user"
    b_waiting = asyncio.Event()
    b_entered = asyncio.Event()
    b_may_finish = asyncio.Event()
    c_started = asyncio.Event()
    c_entered = asyncio.Event()
    c_may_finish = asyncio.Event()

    async def b_handler():
        lock = locks.get(key)
        b_waiting.set()
        async with lock:
            b_entered.set()
            await b_may_finish.wait()
        locks.release(key)

    a_lock = locks.get(key)
    await a_lock.acquire()
    b_task = asyncio.create_task(b_handler())
    await b_waiting.wait()

    a_lock.release()
    locks.release(key)
    c_lock = locks.get(key)
    same_lock = c_lock is a_lock

    await b_entered.wait()

    async def c_handler():
        c_started.set()
        async with c_lock:
            c_entered.set()
            await c_may_finish.wait()
        locks.release(key)

    c_task = asyncio.create_task(c_handler())
    await c_started.wait()
    await asyncio.sleep(0)
    c_entered_while_b_held = c_entered.is_set()

    b_may_finish.set()
    await b_task
    await c_entered.wait()
    c_may_finish.set()
    await c_task

    assert same_lock
    assert not c_entered_while_b_held
    assert len(locks) == 0
