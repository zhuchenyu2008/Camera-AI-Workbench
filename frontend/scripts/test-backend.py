"""独立的本机浏览器验收环境。只使用临时数据、合成视频与本地模型响应。

运行：根目录 .venv/Scripts/python frontend/scripts/test-backend.py
仅监听 127.0.0.1:18180；正式 app.main 中没有这些测试控制路由。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import io
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
import uvicorn
from cryptography.fernet import Fernet
from fastapi import Request
from PIL import Image, ImageDraw
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.analysis.client import AIClient
from app.capture.frames import FrameWriter
from app.capture.supervisor import Supervisor
from app.config import Config
from app.main import create_app
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import SummaryOutput
from app.services.workbench import Workbench
from app.summaries.service import Summaries
from app.timeutils import iso, timestamp
from app.worker.executor import Executor
from tests.conftest import make_camera, make_minute, make_provider
from tests.test_analysis import make_person, fake_visual
from tests.rtsp_source import SyntheticRTSP


class Clock:
    def __init__(self):
        self.value = timestamp("2026-10-18T08:00:00+08:00")
        self.started = None

    def __call__(self):
        return self.value + (int((time.monotonic() - self.started) * 1000) if self.started else 0)


output = ROOT / "frontend/.test-output"
output.mkdir(exist_ok=True)
data = Path(tempfile.mkdtemp(prefix="browser-", dir=output))
master = data / "master.key"
master.write_bytes(Fernet.generate_key())
clock = Clock()
config = Config(data, f"sqlite:///{(data / 'db/workbench.sqlite3').as_posix()}", master,
                allowed_origin="http://127.0.0.1:5173", secure_cookies=False,
                provider_local_endpoints=("localhost:8888",), clock=clock)
w = Workbench(config, migrate=True)
w.auth.create_admin("frontend-test", "frontend-browser-test-2026")

# A clearly synthetic still is used only in isolated test storage.
picture = Image.new("RGB", (960, 540), "#dce4d7")
draw = ImageDraw.Draw(picture)
draw.rectangle((0, 350, 960, 540), fill="#7d8f77")
draw.rounded_rectangle((180, 150, 690, 395), radius=12, fill="#b7c5a6")
draw.rectangle((270, 250, 610, 400), fill="#f7f3e5")
draw.line((440, 250, 440, 400), fill="#acb6a0", width=4)
draw.text((24, 20), "SYNTHETIC TEST SOURCE - NOT A PERSONAL IMAGE", fill="#304538")
buffer = io.BytesIO()
picture.save(buffer, "JPEG")
jpeg = buffer.getvalue()
provider = make_provider(w)
person, asset = make_person(w, jpeg)
camera = make_camera(w)
with w.db.write() as conn:
    w.assets.update_entity(conn, person, {"is_primary_subject": True}, '"rev-3"')
    row = one(conn, s.cameras, camera)
    cfg = row["config_json"] | {"schedule": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "start": "08:00", "end": "12:00", "end_day_offset": 0}]}
    conn.execute(update(s.cameras).where(s.cameras.c.id == camera).values(name="书桌 · 合成测试源", config_json=cfg))
    w.save_schedule(conn, one(conn, s.cameras, camera))

start = clock.value
minutes = []
for index in range(200):
    activity = "reading" if index < 76 else "writing" if index < 118 else "computer" if index < 136 else "unknown" if index < 149 else "resting" if index >= 191 else None
    minute = make_minute(w, camera, start + index * 60000, captured=0 if 171 <= index < 191 else (1 << 60) - 1, activity=activity, subject=person)
    minutes.append(minute)

# A real 20-second analysed interruption, excluded from the event duration.
with w.db.write() as conn:
    original = dict(conn.execute(select(s.activity_spans).where(s.activity_spans.c.minute_id == minutes[55])).mappings().one())
    conn.execute(update(s.activity_spans).where(s.activity_spans.c.id == original["id"]).values(end_at_ms=original["start_at_ms"] + 20000))
    conn.execute(s.activity_spans.insert().values(**(original | {"id": new_id("span"), "start_at_ms": original["start_at_ms"] + 40000})))
    conn.execute(s.activity_spans.insert().values(**(original | {"id": new_id("span"), "start_at_ms": original["start_at_ms"] + 20000, "end_at_ms": original["start_at_ms"] + 40000, "activity": "unknown"})))

# Store genuine JPEG evidence through the production writer for one analysed minute.
sid = new_id("stream")
with w.db.write() as conn:
    conn.execute(s.stream_sessions.insert().values(id=sid, camera_id=camera, started_at_ms=start, source_revision=1))
    conn.execute(update(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera).values(stream_session_id=sid))
writer = FrameWriter(w)
frames = []
for second in range(60):
    at = start + 199 * 60000 + second * 1000
    clock.value = at
    frames.append(writer.save(camera, sid, jpeg, at, str(second), second))
with w.db.write() as conn:
    ids = [f["id"] for f in frames]
    conn.execute(update(s.activity_spans).where(s.activity_spans.c.minute_id == minutes[199]).values(evidence_ids_json=ids, details_json={"observation": "合成测试画面：书页保持展开。此图不代表个人真实活动。", "labels": [], "object_ids": []}))
    conn.execute(update(s.minute_revisions).where(s.minute_revisions.c.minute_id == minutes[199]).values(result_json={"analysis_state": "complete", "analysis_mode": "dense", "description": "合成测试分钟，具备 60 个实际保存秒槽。", "evidence_ids": ids, "sampling": {"target_frame_count": 60, "captured_frame_count": 60, "submitted_frame_count": 4, "successful_frame_count": 4}}))
    w.assets.keep_evidence(conn, ids, "minute", minutes[199], 1)
    w.ledger.project(conn)

older = FrameWriter(w).save(camera, sid, jpeg, start + 160 * 60000, "1", 1)

clock.value = timestamp("2026-10-18T11:20:00+08:00")
clock.started = time.monotonic()
with w.db.write() as conn:
    service = Summaries(w)
    summary = service.ensure(conn, "day", start, clock.value, {"camera_ids": [camera], "subject_id": person})
    context = service.context(conn, summary)
    service.commit(conn, summary, context, SummaryOutput(narrative="合成验收数据：阅读与书写均有对应记录。部分范围尚在分析，另有采集缺口，请结合时间覆盖和来源分钟查看。", highlights=[], source_refs=[]))
    events = [w.ledger.event(conn, r.id) for r in conn.execute(select(s.events.c.id))]

state = {"camera": camera, "person": person, "provider": provider, "minute": minutes[199], "event": next(e["id"] for e in events if e["activity"] == "reading"), "evidence_event": next(e["id"] for e in events if e["activity"] == "resting"), "evidence": frames[0]["id"], "older_evidence":older["id"], "summary": summary["id"], "data_dir": str(data)}
(output / "browser-state.json").write_text(json.dumps(state), encoding="utf-8")
(output / "upload.jpg").write_bytes(jpeg)


def model_response(request):
    payload = json.loads(request.content)
    content = payload["messages"][1]["content"]
    if any(c.get("text", "").startswith("现场证据：") for c in content):
        return fake_visual(request, person)
    if any("image_labels" in c.get("text", "") for c in content):
        labels = []
        for c in content:
            if c.get("type") != "image_url":
                continue
            raw = base64.b64decode(c["image_url"]["url"].split(",", 1)[1])
            r, g, b = Image.open(io.BytesIO(raw)).resize((1, 1)).getpixel((0, 0))
            labels.append("red" if r > b else "blue")
        result = {"image_labels": labels}
    else:
        result = {"narrative": "本地合成模型响应。依据已保存记录生成，可通过来源检查；不代表真实 AI 推理质量。", "highlights": [], "source_refs": []}
    return httpx.Response(200, json={"id": "synthetic-test-response", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})


app = create_app(config, w)
original_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application), SyntheticRTSP(config.ffmpeg, seconds=300) as source:
        source.frames = source.frames * 120
        w.config = dataclasses.replace(config, rtsp_allowed_ports=(source.port, 15554, 15555))
        with w.db.write() as conn:
            row = one(conn, s.cameras, camera)
            conn.execute(update(s.cameras).where(s.cameras.c.id == camera).values(config_json=row["config_json"] | {"source": {"host": "127.0.0.1", "port": source.port, "path": "/live", "username": ""}}, credentials_cipher=None))
        state["rtsp_port"] = source.port
        (output / "browser-state.json").write_text(json.dumps(state), encoding="utf-8")
        supervisor = Supervisor(w)
        client = AIClient(w, httpx.MockTransport(model_response))
        executor = Executor(w, supervisor, client)

        async def pump():
            while True:
                await supervisor.tick()
                now = w.now()
                with w.db.write() as conn:
                    conn.execute(sqlite_insert(s.system_state).values(id="worker", value_json={"state": "online"}, updated_at_ms=now).on_conflict_do_update(index_elements=["id"], set_={"updated_at_ms": now}))
                job = await asyncio.to_thread(w.queue.claim, "browser-test")
                if job:
                    await executor.execute(job)
                await asyncio.sleep(0.5)

        task = asyncio.create_task(pump())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await supervisor.close()
            await client.close()


app.router.lifespan_context = lifespan


@app.get("/api/_test/state", include_in_schema=False)
def test_state():
    return state | {"now": iso(w.now())}


@app.post("/api/_test/control", include_in_schema=False)
async def test_control(request: Request):
    body = await request.json()
    if body.get("expire_evidence"):
        with w.db.write() as conn:
            conn.execute(update(s.frames).where(s.frames.c.id == body["expire_evidence"]).values(expires_at_ms=w.now() - 1, state="expired", reason="ttl"))
    if body.get("expire_preview"):
        clock.value += 301000
    return {"ok": True}


if __name__ == "__main__":
    print("Isolated browser backend: http://127.0.0.1:18180", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=18180, log_level="warning")
