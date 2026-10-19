from __future__ import annotations

import asyncio
import concurrent.futures
import os
import signal
import time

from sqlalchemy import func, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.analysis.client import AIClient
from app.capture.recovery import CaptureRecovery
from app.capture.supervisor import Supervisor
from app.config import Config
from app.repositories import schema as s
from app.repositories.db import new_id
from app.services.analysis import Analysis
from app.services.workbench import Workbench
from app.summaries.service import Summaries
from app.worker.cleanup import Cleaner
from app.worker.executor import Executor
from app.worker.queue import AI_KINDS, enqueue


class ProcessLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("已有 worker 正在运行") from None
        return self

    def __exit__(self, *_):
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


class Worker:
    def __init__(self, workbench, client=None):
        self.w = workbench
        self.owner = new_id("worker")
        self.supervisor = Supervisor(workbench)
        self.client = client or AIClient(workbench)
        self.executor = Executor(workbench, self.supervisor, self.client)
        self.running = {}
        self.stop = asyncio.Event()

    def heartbeat(self):
        now = self.w.now()
        with self.w.db.write() as conn:
            value = {"owner": self.owner, "state": "online"}
            conn.execute(
                sqlite_insert(s.system_state)
                .values(id="worker", value_json=value, updated_at_ms=now)
                .on_conflict_do_update(
                    index_elements=["id"], set_={"value_json": value, "updated_at_ms": now}
                )
            )

    def schedule(self):
        CaptureRecovery(self.w).finalize_before()
        with self.w.db.write() as conn:
            Analysis(self.w).schedule(conn)
            Summaries(self.w).schedule(conn)
            self.schedule_exchange_rate(conn)
            from app.services.discovery import Discovery

            cfg = self.w.settings_config(conn)
            if cfg.auto_discovery_enabled and cfg.vision_provider_id and Discovery(self.w).pending(conn, 1):
                enqueue(
                    conn,
                    self.w.now(),
                    "entity_discovery",
                    {"provider_id": cfg.vision_provider_id},
                    f"entity-discovery:{self.w.now() // 300000}",
                )

    def schedule_exchange_rate(self, conn):
        if not self.w.has_usd_pricing(conn):
            return
        from datetime import datetime

        from app.timeutils import BEIJING
        from app.worker.queue import enqueue

        now_dt = datetime.fromtimestamp(self.w.now() / 1000, tz=BEIJING)
        if now_dt.hour == 23 and now_dt.minute == 15:
            local_date = now_dt.strftime("%Y-%m-%d")
            dedup = f"exchange_rate_daily:{local_date}"
            enqueue(conn, self.w.now(), "exchange_rate_refresh", {"trigger": "daily"}, dedup)

    async def heartbeat_loop(self):
        while not self.stop.is_set():
            await asyncio.to_thread(self.heartbeat)
            await asyncio.to_thread(self.w.queue.recover)
            for task, job in list(self.running.items()):
                if not task.done() and not await asyncio.to_thread(self.w.queue.renew, job):
                    task.cancel()
            await self.wait(15)

    async def capture_loop(self):
        previous_utc, previous_mono = self.w.now(), time.monotonic()
        while not self.stop.is_set():
            now, mono = self.w.now(), time.monotonic()
            if abs((now - previous_utc) / 1000 - (mono - previous_mono)) > 5:
                for camera in list(self.supervisor.streams):
                    await self.supervisor.stop(camera, "clock_discontinuity")
            previous_utc, previous_mono = now, mono
            await self.supervisor.tick()
            await self.wait(1)

    async def cleanup_loop(self):
        while not self.stop.is_set():
            await asyncio.to_thread(Cleaner(self.w).run)
            with self.w.db.read() as conn:
                interval = self.w.settings_config(conn).cleanup_interval_seconds
            await self.wait(interval)

    async def scheduler_loop(self):
        while not self.stop.is_set():
            await asyncio.to_thread(self.schedule)
            await self.wait(10)

    async def wait(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except TimeoutError:
            pass

    async def run(self):
        await asyncio.to_thread(Cleaner(self.w).run)
        await asyncio.to_thread(self.w.queue.recover)
        await asyncio.to_thread(CaptureRecovery(self.w).finalize_before, recovered=True)
        # 进程中断留下的流会话只关闭元数据，不复用旧 HLS。
        with self.w.db.write() as conn:
            conn.execute(
                update(s.stream_sessions)
                .where(s.stream_sessions.c.ended_at_ms.is_(None))
                .values(
                    ended_at_ms=func.max(self.w.now(), s.stream_sessions.c.started_at_ms),
                    exit_reason="worker_restarted",
                )
            )
            conn.execute(
                update(s.camera_runtime).values(
                    stream_session_id=None, capture_state="stopped", density_state=None
                )
            )
            if self.w.has_usd_pricing(conn) and not self.w.has_valid_rate(conn):
                from app.worker.queue import enqueue

                enqueue(
                    conn,
                    self.w.now(),
                    "exchange_rate_refresh",
                    {"trigger": "startup"},
                    f"exchange_rate_startup:{self.w.now()}",
                )
        background = [
            asyncio.create_task(fn())
            for fn in (self.heartbeat_loop, self.capture_loop, self.cleanup_loop, self.scheduler_loop)
        ]
        try:
            while not self.stop.is_set():
                for task in list(self.running):
                    if task.done():
                        await asyncio.gather(task, return_exceptions=True)
                        self.running.pop(task)
                if any(t.done() for t in background):
                    for task in background:
                        if task.done():
                            await task
                    raise RuntimeError("worker 监管循环意外退出")
                with self.w.db.read() as conn:
                    cfg = self.w.settings_config(conn)
                ai = sum(j["kind"] in AI_KINDS for j in self.running.values())
                excluded = {j["payload_json"].get("camera_id") for j in self.running.values()}
                if len(self.running) < cfg.ai_concurrency + 2:
                    job = await asyncio.to_thread(
                        self.w.queue.claim,
                        self.owner,
                        excluded_cameras=excluded,
                        allow_ai=ai < cfg.ai_concurrency,
                    )
                    if job:
                        # 同 provider 的具体并发上限首版取全局值，由有界客户端约束连接数。
                        task = asyncio.create_task(self.executor.execute(job))
                        self.running[task] = job
                        continue
                await self.wait(0.25)
        finally:
            self.stop.set()
            for task in background + list(self.running):
                task.cancel()
            await asyncio.gather(*background, *self.running, return_exceptions=True)
            await self.supervisor.close()
            await self.client.close()


def main(*, stop_file=None):
    config = Config.from_env()
    config.prepare()
    with ProcessLock(config.data_dir / "worker.lock"):
        from app.observability.logging import configure_logging

        configure_logging(config, "worker")
        w = Workbench(config)

        async def run():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=6))
            worker = Worker(w)

            async def watch_local_stop():
                while not worker.stop.is_set():
                    if stop_file.exists():
                        worker.stop.set()
                        return
                    await worker.wait(0.5)

            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, worker.stop.set)
                except (NotImplementedError, RuntimeError):
                    signal.signal(signum, lambda *_: loop.call_soon_threadsafe(worker.stop.set))
            watcher = asyncio.create_task(watch_local_stop()) if stop_file is not None else None
            try:
                await worker.run()
            finally:
                if watcher:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)

        try:
            asyncio.run(run())
        finally:
            w.db.close()


if __name__ == "__main__":
    main()
