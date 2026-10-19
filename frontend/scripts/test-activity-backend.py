"""活动优先的独立界面验收源；没有用户摄像头或外部模型调用。

根目录运行 .venv/Scripts/python frontend/scripts/test-activity-backend.py。
前端使用 CAW_DEV_API=http://127.0.0.1:18181，Vite端口5174。
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn
from cryptography.fernet import Fernet
from PIL import Image, ImageDraw
from sqlalchemy import select, update

from app.config import Config
from app.main import create_app
from app.repositories import schema as s
from app.repositories.db import new_id
from app.security.secrets import digest
from app.services.discovery import Discovery
from app.services.workbench import Workbench
from app.timeutils import timestamp
from tests.conftest import make_camera, make_minute, make_provider

output = ROOT / "frontend/.test-output"
output.mkdir(exist_ok=True)
data = Path(tempfile.mkdtemp(prefix="activity-browser-", dir=output))
key = data / "master.key"
key.write_bytes(Fernet.generate_key())
clock = [timestamp("2026-10-19T00:00:00+08:00")]
config = Config(data, f"sqlite:///{(data / 'db/workbench.sqlite3').as_posix()}", key,
    allowed_origin="http://127.0.0.1:5174", secure_cookies=False,
    provider_local_endpoints=("localhost:8888",), clock=lambda: clock[0])
w = Workbench(config, migrate=True)
w.auth.create_admin("activity-test", "activity-browser-test-2026")
make_provider(w)
camera = make_camera(w)
with w.db.write() as conn:
    row = conn.execute(select(s.cameras)).mappings().one()
    conn.execute(update(s.cameras).values(name="卧室 · 合成验收", config_json=row["config_json"] | {"last_test": {"source_revision": 1}}))
    people = [Discovery(w).new_entity(conn, dict(kind="person", name="人物", description="合成人物，非个人照片")) for _ in range(2)]
for n, person in enumerate(people):
    picture = Image.new("RGB", (320, 240), ["#cbdcd1", "#d9cdb9"][n])
    ImageDraw.Draw(picture).text((12, 100), "SYNTHETIC TEST PROFILE", fill="#304538")
    out = io.BytesIO()
    picture.save(out, "JPEG")
    raw = out.getvalue()
    relpath = w.assets.store("references", raw)
    with w.db.write() as conn:
        ref = w.assets.attach_reference(conn, person, dict(relpath=relpath, bytes=len(raw), width=320, height=240, sha256=digest(raw)), None, True, '"rev-1"')
        if n == 0:
            conn.execute(update(s.reference_assets).where(s.reference_assets.c.id == ref["asset_id"]).values(source_candidate_key="browser-candidate-0"))
start = clock[0] + 3600000
for index in range(30):
    minute = make_minute(w, camera, start + index * 60000, activity="睡觉" if index < 20 else "吃饭", subject=people[0] if index < 2 else None, identity="matched" if index < 2 else "unknown")
    if index < 2:
        candidate = f"browser-candidate-{index}"
        with w.db.write() as conn:
            conn.execute(update(s.activity_spans).where(s.activity_spans.c.minute_id == minute).values(details_json=dict(observation="合成活动记录", labels=[], object_ids=[], subject_candidate_key=candidate)))
            conn.execute(s.entity_occurrences.insert().values(id=new_id("occurrence"), entity_id=people[0], source_key=candidate, camera_id=camera,
                captured_at_ms=start + index * 60000, evidence_id=f"synthetic-evidence-{index}", crop_json=dict(x=0, y=0, width=1, height=1),
                links_json=[dict(minute_start=f"2026-10-19T01:{index:02d}:00+08:00", role="subject")], manual=False, created_at_ms=clock[0]))
clock[0] = timestamp("2026-10-19T08:00:00+08:00")
with w.db.write() as conn:
    w.ledger.project(conn)
app = create_app(config, w)
if __name__ == "__main__":
    print("Isolated activity UI: http://127.0.0.1:5174 (test data only)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=18181, log_level="warning")
