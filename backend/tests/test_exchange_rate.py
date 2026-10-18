from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import ProviderPricingWrite
from app.services.exchange_rate import ExchangeRateService, parse_ecb_xml
from app.timeutils import BEIJING, now_ms
from tests.conftest import make_provider

SAMPLE_ECB_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-02-23" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
    <gesmes:subject>Reference rates</gesmes:subject>
    <gesmes:Sender>
        <gesmes:name>European Central Bank</gesmes:name>
    </gesmes:Sender>
    <Cube>
        <Cube time='2026-10-16'>
            <Cube currency='USD' rate='1.0850'/>
            <Cube currency='JPY' rate='162.50'/>
            <Cube currency='CNY' rate='7.8500'/>
        </Cube>
    </Cube>
</gesmes:Envelope>"""


def test_parse_ecb_xml_success():
    rate_date, usd_to_cny, raw_data = parse_ecb_xml(SAMPLE_ECB_XML)
    assert rate_date == "2026-10-16"
    # 7.8500 / 1.0850 = 7.2350229... -> 7.2350
    assert usd_to_cny == Decimal("7.2350")
    assert raw_data["eur_usd"] == "1.0850"
    assert raw_data["eur_cny"] == "7.8500"


def test_parse_ecb_xml_invalid():
    # 1. 缺少币种
    xml_missing_cny = b"""<Envelope><Cube><Cube time='2026-10-16'>
        <Cube currency='USD' rate='1.0850'/>
    </Cube></Cube></Envelope>"""
    with pytest.raises(AppError, match="EXCHANGE_RATE_PARSE_FAILED"):
        parse_ecb_xml(xml_missing_cny)

    # 2. 负数或零汇率
    xml_zero = b"""<Envelope><Cube><Cube time='2026-10-16'>
        <Cube currency='USD' rate='0'/>
        <Cube currency='CNY' rate='7.8500'/>
    </Cube></Cube></Envelope>"""
    with pytest.raises(AppError, match="EXCHANGE_RATE_PARSE_FAILED"):
        parse_ecb_xml(xml_zero)

    # 3. 日期格式异常
    xml_bad_date = b"""<Envelope><Cube><Cube time='not-a-date'>
        <Cube currency='USD' rate='1.0850'/>
        <Cube currency='CNY' rate='7.8500'/>
    </Cube></Cube></Envelope>"""
    with pytest.raises(AppError, match="EXCHANGE_RATE_PARSE_FAILED"):
        parse_ecb_xml(xml_bad_date)


def test_exchange_rate_service_refresh_and_regression(w):
    service = ExchangeRateService(w)

    # 第一次写入 2026-10-16 汇率
    with patch("app.services.exchange_rate.fetch_ecb_rate", return_value=("2026-10-16", Decimal("7.2350"), {"eur_usd": "1.0850", "eur_cny": "7.8500"})):
        res = asyncio.run(service.refresh())
        assert res["rate"] == "7.2350"
        assert res["rate_date"] == "2026-10-16"

    with w.db.read() as conn:
        row = conn.execute(select(s.exchange_rates).where(s.exchange_rates.c.id == "usd_cny")).mappings().one()
        assert row["rate"] == "7.2350"
        assert row["rate_date"] == "2026-10-16"

    # 周末/节假日返回同一天 2026-10-16：成功更新
    with patch("app.services.exchange_rate.fetch_ecb_rate", return_value=("2026-10-16", Decimal("7.2350"), {})):
        res2 = asyncio.run(service.refresh())
        assert res2["rate_date"] == "2026-10-16"

    # 日期倒退（2026-10-15 < 2026-10-16）：拒绝并抛出不可重试的异常
    with patch("app.services.exchange_rate.fetch_ecb_rate", return_value=("2026-10-15", Decimal("7.2000"), {})):
        with pytest.raises(AppError) as exc_info:
            asyncio.run(service.refresh())
        assert exc_info.value.code == "EXCHANGE_RATE_DATE_REGRESSION"
        assert exc_info.value.retryable is False


def test_queue_fail_retry_delays_for_exchange_rate(w):
    now = w.now()
    from app.worker.queue import enqueue

    with w.db.write() as conn:
        job = enqueue(
            conn,
            now,
            "exchange_rate_refresh",
            {"trigger": "test"},
            "test_retry_key",
        )
    # Claim job to make it running
    claimed = w.queue.claim("worker_test")
    assert claimed is not None
    assert claimed["id"] == job["id"]

    err = AppError(503, "EXCHANGE_RATE_TIMEOUT", "超时", retryable=True)

    # 1. 第一次失败：重试间隔 1 分钟 (60,000 ms)
    w.queue.fail(claimed, err)
    with w.db.read() as conn:
        j1 = one(conn, s.jobs, job["id"])
        assert j1["state"] == "queued"
        assert j1["queue_reason"] == "retry_wait"
        assert j1["failure_count"] == 1
        assert j1["not_before_ms"] == now + 60000

    # 2. 第二次失败：重试间隔 5 分钟 (300,000 ms)
    with w.db.write() as conn:
        conn.execute(
            s.jobs.update().where(s.jobs.c.id == job["id"]).values(not_before_ms=now)
        )
    c2 = w.queue.claim("worker_test")
    w.queue.fail(c2, err)
    with w.db.read() as conn:
        j2 = one(conn, s.jobs, job["id"])
        assert j2["state"] == "queued"
        assert j2["failure_count"] == 2
        assert j2["not_before_ms"] == now + 300000

    # 3. 第三次失败：重试间隔 15 分钟 (900,000 ms)
    with w.db.write() as conn:
        conn.execute(
            s.jobs.update().where(s.jobs.c.id == job["id"]).values(not_before_ms=now)
        )
    c3 = w.queue.claim("worker_test")
    w.queue.fail(c3, err)
    with w.db.read() as conn:
        j3 = one(conn, s.jobs, job["id"])
        assert j3["state"] == "queued"
        assert j3["failure_count"] == 3
        assert j3["not_before_ms"] == now + 900000

    # 4. 第四次失败：超出 3 次重试，转为终态 failed
    with w.db.write() as conn:
        conn.execute(
            s.jobs.update().where(s.jobs.c.id == job["id"]).values(not_before_ms=now)
        )
    c4 = w.queue.claim("worker_test")
    w.queue.fail(c4, err)
    with w.db.read() as conn:
        j4 = one(conn, s.jobs, job["id"])
        assert j4["state"] == "failed"
        assert j4["failure_count"] == 4


def test_rate_validity_and_cache_7_days(w):
    now = w.now()
    today = datetime.fromtimestamp(now / 1000, BEIJING).date()

    # 今天
    assert w.is_rate_valid(str(today)) is True
    # 7 天前（仍有效）
    assert w.is_rate_valid(str(today - timedelta(days=7))) is True
    # 8 天前（已过期）
    assert w.is_rate_valid(str(today - timedelta(days=8))) is False
    # None
    assert w.is_rate_valid(None) is False
    # 非法日期
    assert w.is_rate_valid("invalid-date") is False


def test_pricing_usd_triggers_exchange_rate_refresh(w):
    provider_id = make_provider(w)
    now = w.now()

    # 初始状态没有汇率
    with w.db.read() as conn:
        assert w.has_valid_rate(conn) is False

    # 配置 USD 价格 -> 触发立即刷新任务
    with w.db.write() as conn:
        prov = one(conn, s.providers, provider_id)
        w.write_provider_pricing(
            conn,
            provider_id,
            ProviderPricingWrite(
                provider_revision=prov["revision"],
                pricing_revision=1,
                currency="USD",
                input_price="2.0",
                output_price="5.0",
                source_note="USD 模型",
            ),
            header='"rev-1"',
        )

    with w.db.read() as conn:
        job = conn.execute(
            select(s.jobs).where(s.jobs.c.kind == "exchange_rate_refresh")
        ).mappings().first()
        assert job is not None
        assert job["payload_json"]["reason"] == "initial_usd_pricing"


def test_daily_2315_scheduler(w):
    provider_id = make_provider(w)

    # 1. 没有 USD 价格时不安排刷新
    from app.worker_main import Worker
    worker = Worker(w)
    with w.db.write() as conn:
        worker.schedule_exchange_rate(conn)
        jobs = conn.execute(select(s.jobs).where(s.jobs.c.kind == "exchange_rate_refresh")).mappings().all()
        assert len(jobs) == 0

    # 2. 配置 USD 价格
    with w.db.write() as conn:
        prov = one(conn, s.providers, provider_id)
        w.write_provider_pricing(
            conn,
            provider_id,
            ProviderPricingWrite(
                provider_revision=prov["revision"],
                pricing_revision=1,
                currency="USD",
                input_price="2.0",
                output_price="5.0",
                source_note="USD 模型",
            ),
            header='"rev-1"',
        )

    # 3. 模拟时间不是 23:15
    with patch.object(w, "now", return_value=int(datetime(2026, 10, 18, 12, 0, tzinfo=BEIJING).timestamp() * 1000)):
        with w.db.write() as conn:
            # 清理之前的 pricing 触发 job
            conn.execute(s.jobs.delete().where(s.jobs.c.kind == "exchange_rate_refresh"))
            worker.schedule_exchange_rate(conn)
            jobs = conn.execute(select(s.jobs).where(s.jobs.c.kind == "exchange_rate_refresh")).mappings().all()
            assert len(jobs) == 0

    # 4. 模拟时间是 23:15 -> 入队 exchange_rate_daily:2026-10-18
    with patch.object(w, "now", return_value=int(datetime(2026, 10, 18, 23, 15, 30, tzinfo=BEIJING).timestamp() * 1000)):
        with w.db.write() as conn:
            worker.schedule_exchange_rate(conn)
            daily_job = conn.execute(
                select(s.jobs).where(s.jobs.c.kind == "exchange_rate_refresh", s.jobs.c.dedup_key == "exchange_rate_daily:2026-10-18")
            ).mappings().one()
            assert daily_job is not None
            assert daily_job["payload_json"]["trigger"] == "daily"


def test_exchange_rate_api_endpoints(w, client):
    # 1. 初始状态
    resp = client.get("/api/billing/exchange-rate")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "missing"
    assert data["rate"] is None
    assert data["is_valid"] is False
    assert data["has_usd_provider"] is False

    # 2. 触发刷新
    refresh_resp = client.post(
        "/api/billing/exchange-rate/refresh",
        headers={"Idempotency-Key": "idemp-refresh-1"},
    )
    assert refresh_resp.status_code == 200
    r_data = refresh_resp.json()["data"]
    assert r_data["status"] == "queued"
    job_id = r_data["job_id"]
    assert job_id.startswith("job_")

    # 3. 再次触发（排队中重复触发返回当前任务）
    dup_resp = client.post(
        "/api/billing/exchange-rate/refresh",
        headers={"Idempotency-Key": "idemp-refresh-2"},
    )
    assert dup_resp.status_code == 200
    assert dup_resp.json()["data"]["job_id"] == job_id

    # 4. 模拟写入汇率数据
    today_str = datetime.fromtimestamp(w.now() / 1000, BEIJING).strftime("%Y-%m-%d")
    with w.db.write() as conn:
        conn.execute(
            s.exchange_rates.insert().values(
                id="usd_cny",
                from_currency="USD",
                to_currency="CNY",
                rate="7.2350",
                rate_date=today_str,
                source="ECB",
                fetched_at_ms=w.now(),
                raw_data_json={},
            )
        )

    get_resp2 = client.get("/api/billing/exchange-rate")
    assert get_resp2.status_code == 200
    data2 = get_resp2.json()["data"]
    assert data2["status"] == "valid"
    assert data2["rate"] == "7.2350"
    assert data2["rate_date"] == today_str
    assert data2["is_valid"] is True

