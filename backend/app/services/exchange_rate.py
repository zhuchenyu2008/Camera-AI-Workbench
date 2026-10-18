from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.errors import AppError
from app.repositories import schema as s

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_ecb_xml(xml_bytes: bytes) -> tuple[str, Decimal, dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        raise AppError(503, "EXCHANGE_RATE_PARSE_FAILED", "ECB 汇率 XML 格式错误", retryable=True) from exc

    rate_date = None
    usd_str = None
    cny_str = None

    for elem in root.iter():
        if elem.tag.endswith("Cube"):
            if "time" in elem.attrib:
                rate_date = elem.attrib["time"]
            curr = elem.attrib.get("currency")
            if curr == "USD":
                usd_str = elem.attrib.get("rate")
            elif curr == "CNY":
                cny_str = elem.attrib.get("rate")

    if not rate_date or not DATE_RE.match(rate_date):
        raise AppError(503, "EXCHANGE_RATE_PARSE_FAILED", "ECB 汇率缺少有效报价日期", retryable=True)

    try:
        d = date.fromisoformat(rate_date)
    except Exception:
        raise AppError(503, "EXCHANGE_RATE_PARSE_FAILED", "ECB 汇率日期格式非法", retryable=True)

    if not usd_str or not cny_str:
        raise AppError(503, "EXCHANGE_RATE_PARSE_FAILED", "ECB 汇率缺少 USD 或 CNY 报价", retryable=True)

    try:
        usd_dec = Decimal(usd_str)
        cny_dec = Decimal(cny_str)
    except Exception:
        raise AppError(503, "EXCHANGE_RATE_PARSE_FAILED", "ECB 汇率数值格式非法", retryable=True)

    if usd_dec <= 0 or cny_dec <= 0:
        raise AppError(503, "EXCHANGE_RATE_PARSE_FAILED", "ECB 汇率数值必须为正数", retryable=True)

    # USD -> CNY: 人民币/欧元 ÷ 美元/欧元
    usd_to_cny = (cny_dec / usd_dec).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    raw_data = {
        "eur_usd": usd_str,
        "eur_cny": cny_str,
        "rate_date": rate_date,
    }
    return rate_date, usd_to_cny, raw_data


async def fetch_ecb_rate(timeout: float = 10.0, transport=None) -> tuple[str, Decimal, dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            resp = await client.get(ECB_URL)
            if resp.status_code != 200:
                raise AppError(
                    503,
                    "EXCHANGE_RATE_FETCH_FAILED",
                    f"ECB 汇率服务返回异常状态码 {resp.status_code}",
                    retryable=True,
                )
            body = resp.content
    except AppError:
        raise
    except httpx.TimeoutException as exc:
        raise AppError(503, "EXCHANGE_RATE_TIMEOUT", "获取 ECB 汇率超时", retryable=True) from exc
    except Exception as exc:
        raise AppError(503, "EXCHANGE_RATE_FETCH_FAILED", "获取 ECB 汇率网络失败", retryable=True) from exc

    return parse_ecb_xml(body)


class ExchangeRateService:
    def __init__(self, workbench, transport=None):
        self.w = workbench
        self.transport = transport

    async def refresh(self, job: dict | None = None) -> dict:
        rate_date, rate, raw_data = await fetch_ecb_rate(timeout=10.0, transport=self.transport)

        now = self.w.now()
        with self.w.db.write() as conn:
            existing = conn.execute(
                select(s.exchange_rates).where(s.exchange_rates.c.id == "usd_cny")
            ).mappings().first()

            if existing and existing["rate_date"]:
                existing_date = date.fromisoformat(existing["rate_date"])
                new_date = date.fromisoformat(rate_date)
                if new_date < existing_date:
                    raise AppError(
                        503,
                        "EXCHANGE_RATE_DATE_REGRESSION",
                        f"汇率报价日期倒退: {rate_date} < {existing['rate_date']}",
                        retryable=False,
                    )

            conn.execute(
                sqlite_insert(s.exchange_rates)
                .values(
                    id="usd_cny",
                    from_currency="USD",
                    to_currency="CNY",
                    rate=str(rate),
                    rate_date=rate_date,
                    source="ECB",
                    fetched_at_ms=now,
                    raw_data_json=raw_data,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "rate": str(rate),
                        "rate_date": rate_date,
                        "source": "ECB",
                        "fetched_at_ms": now,
                        "raw_data_json": raw_data,
                    },
                )
            )

        return {"rate": str(rate), "rate_date": rate_date, "source": "ECB"}
