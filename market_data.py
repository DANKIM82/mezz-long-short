"""시장 데이터 v2 — 현재가·시가총액·상장주식수·공매도잔고 (pykrx).

동기 pykrx를 asyncio.to_thread로 오프로드. 시총은 '조달액/시총' 산출에,
상장주식수는 RCPS 희석률 검산에 사용한다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

from models import MarketSnapshot

logger = logging.getLogger("mezzanine.market")

try:
    from pykrx import stock as _krx  # type: ignore
    _HAS_PYKRX = True
except Exception:  # pragma: no cover
    _HAS_PYKRX = False
    logger.warning("pykrx 미설치 — 시장데이터 None (pip install pykrx)")


def _yyyymmdd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _window(days: int = 12) -> tuple[str, str]:
    today = dt.date.today()
    return _yyyymmdd(today - dt.timedelta(days=days)), _yyyymmdd(today)


def _snapshot_sync(stock_code: str) -> MarketSnapshot:
    snap = MarketSnapshot(stock_code=stock_code)
    if not _HAS_PYKRX:
        return snap
    bgn, end = _window()
    try:
        ohlcv = _krx.get_market_ohlcv(bgn, end, stock_code)
        if ohlcv is not None and not ohlcv.empty:
            snap.last_close = float(ohlcv["종가"].iloc[-1])
    except Exception as exc:
        logger.error("price fail %s: %s", stock_code, exc)
    try:
        cap = _krx.get_market_cap(bgn, end, stock_code)
        if cap is not None and not cap.empty:
            last = cap.iloc[-1]
            if "시가총액" in cap.columns:
                snap.market_cap = float(last["시가총액"])
            if "상장주식수" in cap.columns:
                snap.shares_outstanding = float(last["상장주식수"])
    except Exception as exc:
        logger.error("mktcap fail %s: %s", stock_code, exc)
    try:
        sb = _krx.get_shorting_balance_by_date(bgn, end, stock_code)
        if sb is not None and not sb.empty:
            last = sb.iloc[-1]
            for col in ("비중", "공매도잔고비중"):
                if col in sb.columns:
                    snap.short_balance_ratio = float(last[col])
                    break
            idx = sb.index[-1]
            snap.short_as_of = idx.date() if hasattr(idx, "date") else None
    except Exception as exc:
        logger.error("short fail %s: %s", stock_code, exc)
    return snap


async def get_snapshot(stock_code: str) -> MarketSnapshot:
    """단일 종목 시장 스냅샷."""
    return await asyncio.to_thread(_snapshot_sync, stock_code)


async def enrich_market_data(
    stock_codes: list[str], max_concurrency: int = 8
) -> dict[str, MarketSnapshot]:
    """종목 집합 동시 수집 → {code: MarketSnapshot}."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(code: str) -> tuple[str, MarketSnapshot]:
        async with sem:
            return code, await get_snapshot(code)

    pairs = await asyncio.gather(*(_one(c) for c in stock_codes if c))
    return dict(pairs)
