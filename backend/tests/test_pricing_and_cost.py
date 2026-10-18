from __future__ import annotations

import json
from decimal import Decimal
import pytest
from sqlalchemy import select, update

from app.analysis.client import format_cost
from app.errors import AppError
from app.observability.status import aggregate_usage, usage_data
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import ProviderPricingConfig, ProviderPricingWrite, ProviderWrite
from app.timeutils import BEIJING, iso
from tests.conftest import make_provider


def test_format_cost():
    assert format_cost(Decimal("0")) == "0"
    assert format_cost(Decimal("0.000000")) == "0"
    assert format_cost(Decimal("3.200000")) == "3.2"
    assert format_cost(Decimal("23.040000")) == "23.04"
    assert format_cost(Decimal("0.00000012")) == "0.00000012"


def test_pricing_validation():
    # 允许合法正数、零及最多6位小数
    cfg = ProviderPricingConfig(
        currency="USD",
        input_price="2.0",
        output_price="6.0",
        cached_input_price="0.500000",
        source_note="官网价格页",
    )
    assert cfg.currency == "USD"

    # 拒绝负数
    with pytest.raises(Exception):
        ProviderPricingConfig(
            currency="USD",
            input_price="-1.0",
            output_price="2.0",
            source_note="test",
        )

    # 拒绝超过6位小数
    with pytest.raises(Exception):
        ProviderPricingConfig(
            currency="USD",
            input_price="1.1234567",
            output_price="2.0",
            source_note="test",
        )


def test_pricing_resource_crud_and_revisions(client, w):
    provider_id = make_provider(w)

    # 1. 默认未配置价格
    res = client.get(f"/api/providers/{provider_id}/pricing")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["provider_id"] == provider_id
    assert data["pricing_revision"] == 1
    assert data["provider_revision"] == 1
    assert data["price_configured"] is False
    assert data["applicable"] is False
    assert data["pricing"] is None
    etag = res.headers["ETag"]
    assert etag == '"rev-1"'

    # 2. 设置价格，提供 If-Match
    pricing_payload = {
        "provider_revision": 1,
        "pricing_revision": 1,
        "currency": "USD",
        "input_price": "2.0",
        "output_price": "6.0",
        "cached_input_price": "0.5",
        "source_note": "测试定价说明",
    }
    # 缺少 If-Match 报 428
    res = client.put(
        f"/api/providers/{provider_id}/pricing",
        json=pricing_payload,
        headers={"Idempotency-Key": "key-no-rev"},
    )
    assert res.status_code == 428

    # 错误的 If-Match 报 412
    res = client.put(
        f"/api/providers/{provider_id}/pricing",
        json=pricing_payload,
        headers={"If-Match": '"rev-99"', "Idempotency-Key": "key-wrong-rev"},
    )
    assert res.status_code == 412

    # 错误的 provider_revision 报 412
    wrong_prov = dict(pricing_payload, provider_revision=99)
    res = client.put(
        f"/api/providers/{provider_id}/pricing",
        json=wrong_prov,
        headers={"If-Match": '"rev-1"', "Idempotency-Key": "key-wrong-prov"},
    )
    assert res.status_code == 412

    # 正常设置
    res = client.put(
        f"/api/providers/{provider_id}/pricing",
        json=pricing_payload,
        headers={"If-Match": '"rev-1"', "Idempotency-Key": "key-put-ok"},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["pricing_revision"] == 2
    assert data["provider_revision"] == 1
    assert data["price_configured"] is True
    assert data["applicable"] is True
    assert data["pricing"]["input_price"] == "2.0"
    assert data["pricing"]["output_price"] == "6.0"
    assert data["pricing"]["cached_input_price"] == "0.5"

    # 确认 Provider 本身版本未被改变
    prov = client.get(f"/api/providers").json()["data"][0]
    assert prov["revision"] == 1

    # 3. 改变 Provider 模型名称后，旧价格保留但停止应用于新调用 (applicable = False)
    patch_payload = {
        "name": prov["name"],
        "base_url": prov["base_url"],
        "model": "new-model-name",
    }
    res = client.patch(
        f"/api/providers/{provider_id}",
        json=patch_payload,
        headers={"If-Match": f'"rev-{prov["revision"]}"', "Idempotency-Key": "key-patch-prov"},
    )
    assert res.status_code == 200

    res = client.get(f"/api/providers/{provider_id}/pricing")
    data = res.json()["data"]
    assert data["price_configured"] is True
    assert data["applicable"] is False  # 模型名称不匹配

    # 4. 清除价格
    clear_payload = {
        "provider_revision": 2,
        "pricing_revision": 2,
        "clear": True,
    }
    res = client.put(
        f"/api/providers/{provider_id}/pricing",
        json=clear_payload,
        headers={"If-Match": '"rev-2"', "Idempotency-Key": "key-clear"},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["price_configured"] is False
    assert data["applicable"] is False
    assert data["pricing_revision"] == 3


def test_cost_calculation_synthetic(w):
    # 核心场景：输入 100 万、其中缓存 20 万、输出 25 万；单价分别为 2／0.5／6 USD，结果为 3.2 USD；合成汇率 7.2 时折算为 23.04 CNY
    provider_id = make_provider(w)
    now = w.now()

    # 设置 USD 价格
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
                output_price="6.0",
                cached_input_price="0.5",
                source_note="合成测试",
            ),
            header='"rev-1"',
        )
        # 写入合成汇率 7.2 (当天日期)
        from datetime import datetime
        today_str = datetime.fromtimestamp(now / 1000, BEIJING).date().isoformat()
        conn.execute(
            s.exchange_rates.insert().values(
                id="usd_cny",
                from_currency="USD",
                to_currency="CNY",
                rate="7.2",
                rate_date=today_str,
                source="ECB",
                fetched_at_ms=now,
            )
        )

    # 模拟一次模型调用
    job = {
        "id": new_id("job"),
        "kind": "sparse_analysis",
        "attempt_count": 0,
        "lease_token": "token_1",
    }
    with w.db.write() as conn:
        conn.execute(
            s.jobs.insert().values(
                id=job["id"],
                created_at_ms=now,
                updated_at_ms=now,
                kind=job["kind"],
                dedup_key="dedup_synthetic",
                payload_json={"provider_id": provider_id},
                state="running",
                phase="running",
                progress_json={},
                priority=10,
                not_before_ms=now,
                cancel_requested=False,
                attempt_count=0,
                result_json={},
                lease_token=job["lease_token"],
                lease_until_ms=now + 60000,
            )
        )

    from unittest.mock import AsyncMock, patch
    from app.analysis.client import AIClient
    from app.schemas import Strict

    class DummyOutput(Strict):
        status: str

    client = AIClient(w)

    mock_usage = {
        "prompt_tokens": 1000000,
        "completion_tokens": 250000,
        "prompt_tokens_details": {"cached_tokens": 200000},
    }
    mock_choice = {
        "finish_reason": "stop",
        "message": {
            "content": json.dumps({"status": "ok"})
        },
    }

    class MockStreamResponse:
        status_code = 200
        headers = {"x-request-id": "req-synthetic"}

        async def aiter_bytes(self):
            yield json.dumps({"choices": [mock_choice], "usage": mock_usage}).encode("utf-8")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    with w.db.read() as conn:
        prov = one(conn, s.providers, provider_id)
        limits = w.effective_limits(w.settings_config(conn), prov)

    with patch.object(client.http, "stream", return_value=MockStreamResponse()):
        import asyncio
        output, attempt_id = asyncio.run(
            client.call(job, prov, {}, DummyOutput, 0, 0, limits)
        )

    # 校验 ai_usage 记录
    with w.db.read() as conn:
        usage_row = conn.execute(
            select(s.ai_usage).where(s.ai_usage.c.attempt_id == attempt_id)
        ).mappings().one()
        assert usage_row["input_tokens"] == 1000000
        assert usage_row["output_tokens"] == 250000
        assert usage_row["cached_tokens"] == 200000
        assert usage_row["estimated_cost"] == "3.2"
        assert usage_row["estimated_cost_cny"] == "23.04"
        assert usage_row["cost_status"] == "converted"
        assert usage_row["cost_reason"] is None
        assert usage_row["price_snapshot_json"]["pricing_revision"] == 2
        assert usage_row["exchange_rate_snapshot_json"]["rate"] == "7.2"


def test_cost_unknown_and_zero_values(w):
    provider_id = make_provider(w)
    now = w.now()

    # 1. 真实零费用正常显示为零
    with w.db.write() as conn:
        prov = one(conn, s.providers, provider_id)
        w.write_provider_pricing(
            conn,
            provider_id,
            ProviderPricingWrite(
                provider_revision=prov["revision"],
                pricing_revision=1,
                currency="CNY",
                input_price="0.0",
                output_price="0.0",
                source_note="免费模型",
            ),
            header='"rev-1"',
        )

    from app.analysis.client import AIClient
    from app.schemas import Strict
    from unittest.mock import patch

    class DummyOutput(Strict):
        status: str

    client = AIClient(w)

    class MockStreamResponseZero:
        status_code = 200
        headers = {}

        async def aiter_bytes(self):
            yield json.dumps({
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"status": "ok"})}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }).encode("utf-8")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    job = {"id": new_id("job"), "kind": "sparse_analysis", "attempt_count": 0, "lease_token": "token_zero"}
    with w.db.write() as conn:
        conn.execute(
            s.jobs.insert().values(
                id=job["id"],
                created_at_ms=now,
                updated_at_ms=now,
                kind=job["kind"],
                dedup_key="dedup_zero",
                payload_json={"provider_id": provider_id},
                state="running",
                phase="running",
                progress_json={},
                priority=10,
                not_before_ms=now,
                cancel_requested=False,
                attempt_count=0,
                result_json={},
                lease_token=job["lease_token"],
                lease_until_ms=now + 60000,
            )
        )
    with w.db.read() as conn:
        prov = one(conn, s.providers, provider_id)
        limits = w.effective_limits(w.settings_config(conn), prov)

    import asyncio
    with patch.object(client.http, "stream", return_value=MockStreamResponseZero()):
        _, attempt_id = asyncio.run(client.call(job, prov, {}, DummyOutput, 0, 0, limits))

    with w.db.read() as conn:
        usage_row = conn.execute(
            select(s.ai_usage).where(s.ai_usage.c.attempt_id == attempt_id)
        ).mappings().one()
        assert usage_row["estimated_cost"] == "0"
        assert usage_row["estimated_cost_cny"] == "0"
        assert usage_row["cost_status"] == "converted"


def test_failed_request_with_usage_keeps_cost(w):
    # 有可靠用量的失败请求仍记录费用
    provider_id = make_provider(w)
    now = w.now()

    with w.db.write() as conn:
        prov = one(conn, s.providers, provider_id)
        w.write_provider_pricing(
            conn,
            provider_id,
            ProviderPricingWrite(
                provider_revision=prov["revision"],
                pricing_revision=1,
                currency="CNY",
                input_price="10.0",
                output_price="20.0",
                source_note="计费测试",
            ),
            header='"rev-1"',
        )

    from app.analysis.client import AIClient
    from app.schemas import VisualOutput
    from unittest.mock import patch

    client = AIClient(w)

    class MockStreamResponseInvalidOutput:
        status_code = 200
        headers = {}

        async def aiter_bytes(self):
            # 模型输出了非法结构化 JSON，引发 INVALID_MODEL_OUTPUT
            yield json.dumps({
                "choices": [{"finish_reason": "stop", "message": {"content": "not-valid-json"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
            }).encode("utf-8")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    job = {"id": new_id("job"), "kind": "sparse_analysis", "attempt_count": 0, "lease_token": "token_fail"}
    with w.db.write() as conn:
        conn.execute(
            s.jobs.insert().values(
                id=job["id"],
                created_at_ms=now,
                updated_at_ms=now,
                kind=job["kind"],
                dedup_key="dedup_fail",
                payload_json={"provider_id": provider_id},
                state="running",
                phase="running",
                progress_json={},
                priority=10,
                not_before_ms=now,
                cancel_requested=False,
                attempt_count=0,
                result_json={},
                lease_token=job["lease_token"],
                lease_until_ms=now + 60000,
            )
        )
    with w.db.read() as conn:
        prov = one(conn, s.providers, provider_id)
        limits = w.effective_limits(w.settings_config(conn), prov)

    import asyncio
    with patch.object(client.http, "stream", return_value=MockStreamResponseInvalidOutput()):
        with pytest.raises(AppError, match="INVALID_MODEL_OUTPUT"):
            asyncio.run(client.call(job, prov, {}, VisualOutput, 0, 0, limits))

    with w.db.read() as conn:
        # 查询刚才失败的 attempt
        attempt_id = conn.execute(
            select(s.job_attempts.c.id).where(s.job_attempts.c.job_id == job["id"])
        ).scalar_one()
        usage_row = conn.execute(
            select(s.ai_usage).where(s.ai_usage.c.attempt_id == attempt_id)
        ).mappings().one()
        assert usage_row["state"] == "failed"
        assert usage_row["input_tokens"] == 1000
        assert usage_row["output_tokens"] == 200
        # 1000 * 10 / 1M + 200 * 20 / 1M = 0.01 + 0.004 = 0.014
        assert usage_row["estimated_cost"] == "0.014"
        assert usage_row["estimated_cost_cny"] == "0.014"
        assert usage_row["cost_status"] == "converted"


def test_usage_aggregation_cost_summary(w):
    now = w.now()
    rows = [
        # 1. 已折算 CNY
        {
            "state": "succeeded",
            "billing_status": "reported",
            "image_count": 1,
            "reference_image_count": 0,
            "input_tokens": 1000,
            "output_tokens": 500,
            "cached_tokens": None,
            "reported_cost": None,
            "currency": None,
            "pricing_revision": 1,
            "price_snapshot_json": {"currency": "CNY"},
            "exchange_rate_snapshot_json": None,
            "estimated_cost": "10.5",
            "estimated_cost_cny": "10.5",
            "cost_status": "converted",
            "cost_reason": None,
        },
        # 2. 已折算 USD
        {
            "state": "succeeded",
            "billing_status": "reported",
            "image_count": 1,
            "reference_image_count": 0,
            "input_tokens": 1000,
            "output_tokens": 500,
            "cached_tokens": None,
            "reported_cost": None,
            "currency": None,
            "pricing_revision": 1,
            "price_snapshot_json": {"currency": "USD"},
            "exchange_rate_snapshot_json": {"rate": "7.0", "rate_date": "2026-10-18"},
            "estimated_cost": "2.0",
            "estimated_cost_cny": "14.0",
            "cost_status": "converted",
            "cost_reason": None,
        },
        # 3. 未折算 (USD 缺少汇率)
        {
            "state": "succeeded",
            "billing_status": "reported",
            "image_count": 1,
            "reference_image_count": 0,
            "input_tokens": 1000,
            "output_tokens": 500,
            "cached_tokens": None,
            "reported_cost": None,
            "currency": None,
            "pricing_revision": 1,
            "price_snapshot_json": {"currency": "USD"},
            "exchange_rate_snapshot_json": None,
            "estimated_cost": "1.0",
            "estimated_cost_cny": None,
            "cost_status": "unconverted",
            "cost_reason": "missing_exchange_rate",
        },
        # 4. 未计价
        {
            "state": "failed",
            "billing_status": "unknown",
            "image_count": 0,
            "reference_image_count": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "reported_cost": None,
            "currency": None,
            "pricing_revision": None,
            "price_snapshot_json": None,
            "exchange_rate_snapshot_json": None,
            "estimated_cost": None,
            "estimated_cost_cny": None,
            "cost_status": "unpriced",
            "cost_reason": "missing_pricing",
        },
    ]
    summary = aggregate_usage(rows)["cost_summary"]
    # 10.5 + 14.0 = 24.5
    assert summary["estimated_cny_subtotal"] == "24.5"
    assert summary["by_currency"] == {"CNY": "10.5", "USD": "3"}
    assert summary["converted_requests"] == 2
    assert summary["unconverted_requests"] == 1
    assert summary["unpriced_requests"] == 1
    assert summary["completeness"] == "partial"
    assert "missing_exchange_rate" in summary["reasons"]
    assert "missing_pricing" in summary["reasons"]
    assert summary["exchange_rate_dates"] == ["2026-10-18"]
    assert summary["applicable_price_revisions"] == [1]
