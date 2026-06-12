"""파이프라인 오케스트레이션 v2 — Mezzanine Issuance Radar.

모드
----
daily    : 최근 N일(기본 3일) 신규 CB/BW/EB/RCPS 공시 → 본문 보강 → 태깅 →
           디지스트 출력/텔레그램 발송. seen 상태로 중복 차단,
           발행건은 bonds.jsonl 에 누적(캘린더의 원천).
scan     : 룩백 365일 전체 스크리닝 → CSV (주간 리뷰용).
calendar : bonds.jsonl 누적분에서 향후 60일 이벤트 캘린더 출력.

흐름(daily)
  list.json(B) 키워드 매칭 ─┬→ 정형 수집(cb/bw/eb/rcps) → 파싱
                            ├→ 본문(document.zip) 보강: 풋·콜·리픽싱주기·대상자
                            ├→ 발행사별 36M 시리얼 카운트 + 12M 컴플라이언스
  list.json(I) 행사공시 ────┘→ 시장스냅샷(pykrx) → 태깅 → 디지스트/알림
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
from typing import Any, Optional

import pandas as pd

from alerts import build_digest, send_telegram
from analytics import primary_filter, score_bond, to_dataframe
from compliance import build_compliance_index, check_corp_flags, get_flag
from config import (EXERCISE_KEYWORDS, REPORT_NAME_KEYWORDS, SETTINGS,
                    Settings)
from dart_client import DartClient
from events import upcoming_calendar
from market_data import enrich_market_data
from models import ExerciseFiling, MezzanineBond, SignalScore
from parsers import clean_date, parse_bond

logger = logging.getLogger("mezzanine.pipeline")

KINDS = ("cb", "bw", "eb", "rcps")


# --------------------------------------------------------------------------- #
# 상태(중복 차단) + 발행건 저장소
# --------------------------------------------------------------------------- #
def load_seen(path: str) -> set[str]:
    """이미 알림 처리한 rcept_no 집합."""
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f).get("seen", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(path: str, seen: set[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen),
                   "updated": dt.datetime.now().isoformat()}, f,
                  ensure_ascii=False)


def append_bonds_store(path: str, bonds: list[MezzanineBond]) -> None:
    """발행건을 jsonl로 누적(캘린더 모드의 원천 데이터)."""
    if not bonds:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for b in bonds:
            f.write(b.model_dump_json() + "\n")


def load_bonds_store(path: str) -> list[MezzanineBond]:
    out: list[MezzanineBond] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(MezzanineBond.model_validate_json(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        pass
    # rcept_no 기준 최신 우선 dedupe
    seen: dict[str, MezzanineBond] = {}
    for b in out:
        seen[b.rcept_no] = b
    return list(seen.values())


# --------------------------------------------------------------------------- #
# 유니버스 빌드
# --------------------------------------------------------------------------- #
def _match_kind(report_nm: str) -> Optional[str]:
    for kind, kws in REPORT_NAME_KEYWORDS.items():
        if any(k in report_nm for k in kws):
            return kind
    return None


def build_universe(
    disclosures: list[dict[str, Any]],
    skip_rcept: set[str] | None = None,
) -> tuple[dict[str, set[str]], dict[str, str], dict[str, dt.date]]:
    """공시목록 → kind별 발행사 집합 + 종목코드 맵 + rcept_no→공시일.

    Returns
    -------
    (corps_by_kind, code_map, rcept_dates)
    """
    skip = skip_rcept or set()
    corps: dict[str, set[str]] = {k: set() for k in KINDS}
    code_map: dict[str, str] = {}
    rcept_dates: dict[str, dt.date] = {}
    for d in disclosures:
        cc = d.get("corp_code")
        if not cc:
            continue
        sc = (d.get("stock_code") or "").strip()
        if sc:
            code_map[cc] = sc
        kind = _match_kind(d.get("report_nm", "") or "")
        if kind is None:
            continue
        rno = d.get("rcept_no", "")
        if rno in skip:
            continue
        corps[kind].add(cc)
        rd = clean_date(d.get("rcept_dt"))
        if rno and rd:
            rcept_dates[rno] = rd
    return corps, code_map, rcept_dates


# --------------------------------------------------------------------------- #
# 시리얼(상습) 발행 카운트 — 발행사별 정형 3종 36개월 합산
# --------------------------------------------------------------------------- #
async def serial_counts(
    client: DartClient, corp_codes: set[str], asof: dt.date,
    window_days: int,
) -> dict[str, int]:
    bgn = (asof - dt.timedelta(days=window_days)).strftime("%Y%m%d")
    end = asof.strftime("%Y%m%d")

    async def _one(cc: str) -> tuple[str, int]:
        total = 0
        for kind in ("cb", "bw", "eb"):
            try:
                total += len(await client.fetch_major_report(kind, cc, bgn, end))
            except Exception:
                continue
        return cc, total

    pairs = await asyncio.gather(*(_one(cc) for cc in corp_codes))
    return dict(pairs)


# --------------------------------------------------------------------------- #
# 공통: 정형 수집 → 파싱
# --------------------------------------------------------------------------- #
async def collect_bonds(
    client: DartClient,
    corps: dict[str, set[str]],
    code_map: dict[str, str],
    rcept_dates: dict[str, dt.date],
    bgn_de: str, end_de: str,
    only_rcept: set[str] | None = None,
) -> list[MezzanineBond]:
    rows: list[dict[str, Any]] = []
    fetches = [client.fetch_major_reports_bulk(k, sorted(corps[k]), bgn_de, end_de)
               for k in KINDS if corps[k]]
    for sub in await asyncio.gather(*fetches):
        rows.extend(sub)

    bonds: list[MezzanineBond] = []
    for row in rows:
        rno = row.get("rcept_no", "")
        if only_rcept is not None and rno not in only_rcept:
            continue
        bond = parse_bond(
            row,
            stock_code=code_map.get(row.get("corp_code", "")),
            disclosed_date=rcept_dates.get(rno),
        )
        if bond:
            bonds.append(bond)
    logger.info("parsed bonds: %d", len(bonds))
    return bonds


async def enrich_bodies(client: DartClient, bonds: list[MezzanineBond]) -> None:
    """본문 보강(베스트에포트). daily 모드의 소수 신규건에만 사용 권장.

    기본 본문 파서(풋·콜·리픽싱주기·대상자·RCPS) 이후, 원문 정밀독해
    (deep_read)로 지배권 단서·앵커 인수자·차환 회차·누적 오버행을 추출한다.
    """
    from body_parser import enrich_bond_from_text
    from deep_read import enrich_deep_read

    async def _one(b: MezzanineBond) -> None:
        text = await client.fetch_document_text(b.rcept_no)
        if text:
            enrich_bond_from_text(b, text)
            enrich_deep_read(b, text)

    await asyncio.gather(*(_one(b) for b in bonds))


# --------------------------------------------------------------------------- #
# DAILY 모드
# --------------------------------------------------------------------------- #
async def run_daily(settings: Settings = SETTINGS,
                    with_body: bool = True) -> str:
    """신규 발행 레이더 1회 실행 → 디지스트 텍스트 반환(+텔레그램)."""
    settings.validate()
    asof = dt.date.today()
    bgn = (asof - dt.timedelta(days=settings.daily_window_days)).strftime("%Y%m%d")
    end = asof.strftime("%Y%m%d")
    seen = load_seen(settings.seen_path)

    async with DartClient(settings) as client:
        # 1) 신규 발행 공시 (주요사항보고 B)
        b_list = await client.search_disclosures(bgn, end, pblntf_ty="B")
        corps, code_map, rcept_dates = build_universe(b_list, skip_rcept=seen)
        new_rcepts = set(rcept_dates.keys())

        # 2) 행사(공급) 공시 모니터 (거래소공시 I)
        i_list = await client.search_disclosures(bgn, end, pblntf_ty="I")
        exercises = [
            ExerciseFiling(
                rcept_no=d.get("rcept_no", ""), corp_code=d.get("corp_code", ""),
                corp_name=d.get("corp_name", ""),
                stock_code=(d.get("stock_code") or "").strip() or None,
                report_nm=d.get("report_nm", ""),
                rcept_dt=clean_date(d.get("rcept_dt")))
            for d in i_list
            if any(k in (d.get("report_nm") or "") for k in EXERCISE_KEYWORDS)
            and d.get("rcept_no") not in seen
        ]

        # 3) 정형 수집 + 파싱 (신규 접수번호만)
        bonds = await collect_bonds(client, corps, code_map, rcept_dates,
                                    bgn, end, only_rcept=new_rcepts)

        # 4) 본문 보강 (풋·콜·리픽싱주기·대상자·RCPS 확정)
        if with_body and bonds:
            await enrich_bodies(client, bonds)

        # 5) 발행사별 시리얼 카운트(36M) + 컴플라이언스(발행일 이전 12M)
        issuer_codes = {b.corp_code for b in bonds}
        serials = await serial_counts(
            client, issuer_codes, asof, settings.thresholds.serial_window_days)
        comp_bgn = (asof - dt.timedelta(days=365)).strftime("%Y%m%d")
        comp_pairs = await asyncio.gather(*(
            check_corp_flags(client, b.corp_code, b.corp_name, comp_bgn,
                             (b.disclosed_date or asof).strftime("%Y%m%d"))
            for b in bonds))
        comp_map = {f.corp_code: f for f in comp_pairs}

    # 6) 시장 스냅샷
    market = await enrich_market_data(
        sorted({b.stock_code for b in bonds if b.stock_code}),
        settings.max_concurrency)

    # 7) 태깅
    items: list[tuple[SignalScore, MezzanineBond]] = []
    for b in bonds:
        snap = market.get(b.stock_code) if b.stock_code else None
        sig = score_bond(b, snap, comp_map.get(b.corp_code),
                         serial_count=serials.get(b.corp_code), asof=asof,
                         th=settings.thresholds, tw=settings.weights)
        items.append((sig, b))

    # 8) 디지스트 + 상태 갱신
    digest = build_digest(items, exercises, asof=asof,
                          lang=settings.alerts.language)
    append_bonds_store(settings.bonds_store_path, bonds)
    seen |= {b.rcept_no for b in bonds}
    seen |= {e.rcept_no for e in exercises}
    save_seen(settings.seen_path, seen)

    sent = await send_telegram(digest)
    logger.info("daily done: new=%d exercises=%d telegram=%s",
                len(items), len(exercises), sent)
    return digest


# --------------------------------------------------------------------------- #
# SCAN 모드 (주간 풀 스크리닝)
# --------------------------------------------------------------------------- #
async def run_scan(settings: Settings = SETTINGS,
                   with_filter: bool = True) -> pd.DataFrame:
    """룩백 전체 발행건 스크리닝 → 점수순 DataFrame."""
    settings.validate()
    asof = dt.date.today()
    bgn = (asof - dt.timedelta(days=settings.lookback_days)).strftime("%Y%m%d")
    end = asof.strftime("%Y%m%d")

    async with DartClient(settings) as client:
        b_list = await client.search_disclosures(bgn, end, pblntf_ty="B")
        corps, code_map, rcept_dates = build_universe(b_list)
        bonds = await collect_bonds(client, corps, code_map, rcept_dates, bgn, end)
        issuer_codes = {b.corp_code for b in bonds}
        serials = await serial_counts(
            client, issuer_codes, asof, settings.thresholds.serial_window_days)
        comp_idx = await build_compliance_index(client, bgn, end)

    market = await enrich_market_data(
        sorted({b.stock_code for b in bonds if b.stock_code}),
        settings.max_concurrency)

    signals = [
        score_bond(b, market.get(b.stock_code) if b.stock_code else None,
                   get_flag(comp_idx, b.corp_code),
                   serial_count=serials.get(b.corp_code), asof=asof,
                   th=settings.thresholds, tw=settings.weights)
        for b in bonds
    ]
    if with_filter:
        signals = primary_filter(signals, settings.thresholds)
    append_bonds_store(settings.bonds_store_path, bonds)
    return to_dataframe(signals)


# --------------------------------------------------------------------------- #
# CALENDAR 모드
# --------------------------------------------------------------------------- #
def run_calendar(settings: Settings = SETTINGS,
                 horizon_days: Optional[int] = None) -> pd.DataFrame:
    """누적 발행건에서 향후 이벤트 캘린더 생성."""
    bonds = load_bonds_store(settings.bonds_store_path)
    return upcoming_calendar(bonds, horizon_days)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    ap = argparse.ArgumentParser(description="Mezzanine Issuance Radar")
    ap.add_argument("--mode", choices=("daily", "scan", "calendar"),
                    default="daily")
    ap.add_argument("--no-body", action="store_true",
                    help="daily에서 본문 보강 생략")
    ap.add_argument("--horizon", type=int, default=None,
                    help="calendar 모드 일수(기본 60)")
    args = ap.parse_args()

    if args.mode == "daily":
        digest = asyncio.run(run_daily(with_body=not args.no_body))
        print(digest)
    elif args.mode == "scan":
        df = asyncio.run(run_scan())
        if df.empty:
            print("신호 없음.")
            return
        cols = ["corp_name", "stock_code", "sec_type", "side", "score", "tags",
                "disparity", "proceeds_to_mktcap", "overhang_ratio",
                "days_to_unlock", "days_to_refix", "serial_count"]
        print(df[cols].to_string(index=False))
        out = f"mezzanine_scan_{dt.date.today():%Y%m%d}.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n저장: {out}")
    else:
        df = run_calendar(horizon_days=args.horizon)
        if df.empty:
            print("향후 이벤트 없음 (daily/scan으로 발행건 누적 필요).")
            return
        print(df.to_string(index=False))
        out = f"mezzanine_calendar_{dt.date.today():%Y%m%d}.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
