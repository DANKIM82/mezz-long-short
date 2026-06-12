"""분석 엔진 v2 — PM 트레이드 태그 + 정렬용 점수.

태그가 1차 산출물이다. 각 태그는 한국 메자닌 시장의 검증된 트레이드 패턴에
대응하며, 점수는 TagWeights 가산으로 디지스트 정렬에만 쓴다.
점수·태그는 리서치 신호이지 매매 권고가 아니다.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from config import SETTINGS, TagWeights, Thresholds
from events import next_event_days
from models import (ComplianceFlag, MarketSnapshot, MezzanineBond,
                    SecurityType, Side, SignalScore)

logger = logging.getLogger("mezzanine.analytics")


# --------------------------------------------------------------------------- #
# 단위 지표
# --------------------------------------------------------------------------- #
def conversion_disparity(price: Optional[float],
                         conv_price: Optional[float]) -> Optional[float]:
    """주가/전환가 - 1 (양수=ITM)."""
    if not price or not conv_price:
        return None
    return price / conv_price - 1.0


def proceeds_to_mktcap(bond: MezzanineBond,
                       snap: Optional[MarketSnapshot]) -> Optional[float]:
    """조달액 / 시가총액."""
    if not bond.face_amount or not snap or not snap.market_cap:
        return None
    return bond.face_amount / snap.market_cap


def overhang_ratio(bond: MezzanineBond) -> Optional[float]:
    """잠재(교환대상)주식 / 총주식 — 정형 % 필드를 소수로."""
    if bond.potential_shares_ratio is None:
        return None
    return bond.potential_shares_ratio / 100.0


def funding_distress_share(bond: MezzanineBond) -> Optional[float]:
    """운영자금+채무상환 비중(조달목적 합 대비)."""
    parts = [bond.use_facility, bond.use_operating, bond.use_debt_repay,
             bond.use_other_sec, bond.use_etc]
    total = sum(p for p in parts if p)
    if not total:
        return None
    distress = (bond.use_operating or 0) + (bond.use_debt_repay or 0)
    return distress / total


def refix_room(bond: MezzanineBond, price: Optional[float]) -> Optional[bool]:
    """리픽싱 하향 여력: 현 주가가 플로어 대비 유의미하게 위인가."""
    if bond.refix_floor_price is None or not price:
        return None
    return price > bond.refix_floor_price * 1.05


# --------------------------------------------------------------------------- #
# 태깅 + 스코어링
# --------------------------------------------------------------------------- #
def score_bond(
    bond: MezzanineBond,
    snap: Optional[MarketSnapshot],
    compliance: Optional[ComplianceFlag],
    serial_count: Optional[int] = None,
    asof: Optional[dt.date] = None,
    th: Thresholds = SETTINGS.thresholds,
    tw: TagWeights = SETTINGS.weights,
) -> SignalScore:
    """발행건 1건 → 태그·점수·방향.

    Parameters
    ----------
    bond : MezzanineBond
        본문 보강이 끝났으면 풋/리픽싱 일정·콜옵션도 반영된다.
    snap : MarketSnapshot | None
        현재가·시총·공매도잔고.
    compliance : ComplianceFlag | None
        공시신뢰성 점검 결과.
    serial_count : int | None
        36개월 내 메자닌 발행 횟수(현재 건 포함).

    Returns
    -------
    SignalScore
    """
    asof = asof or dt.date.today()
    price = snap.last_close if snap else None
    disparity = conversion_disparity(price, bond.conversion_price)
    oh = overhang_ratio(bond)
    p2m = proceeds_to_mktcap(bond, snap)
    sbr = snap.short_balance_ratio if snap else None
    d_unlock = ((bond.conv_start - asof).days
                if bond.conv_start and bond.conv_start >= asof else None)
    d_refix = next_event_days(bond, ("REFIX", "REFIX_EST"), asof)
    room = refix_room(bond, price)
    distress = funding_distress_share(bond)

    tags: list[str] = []
    rationale: list[str] = []
    flags: list[str] = []

    def tag(name: str, why: str) -> None:
        tags.append(name)
        rationale.append(f"[{name}] {why}")

    # 1) 희석 쇼크 — 신규 발행 자체의 사이즈
    if (oh is not None and oh * 100 >= th.dilution_major_pct) or \
       (p2m is not None and p2m >= th.proceeds_mktcap_major):
        parts = []
        if oh is not None:
            parts.append(f"잠재주식 {oh:.1%}")
        if p2m is not None:
            parts.append(f"조달액/시총 {p2m:.1%}")
        tag("DILUTION_SHOCK", " · ".join(parts))

    # 2) 상습 발행사
    if serial_count is not None and serial_count >= th.serial_count:
        tag("SERIAL_ISSUER", f"36개월 내 메자닌 {serial_count}회")

    # 3) 리픽싱 숏 구조 (CB/BW 사모)
    if bond.sec_type in (SecurityType.CB, SecurityType.BW) and bond.is_private:
        if room and (disparity is None or disparity < 0.05) and d_refix is not None:
            est = "추정" if not bond.refixing_schedule else "확인"
            tag("REFIX_SHORT",
                f"하향 여력 있음 · 다음 리픽싱 D-{d_refix}({est})"
                + (f" · 괴리율 {disparity:+.1%}" if disparity is not None else ""))
        elif room is False and disparity is not None and disparity <= th.deep_otm:
            tag("REFIX_EXHAUSTED",
                f"플로어 도달·괴리율 {disparity:+.1%} → 추가 희석 제한")

    # 4) 락업 해제(청구 개시) 임박 — 공급 이벤트
    if d_unlock is not None and d_unlock <= th.unlock_window_days:
        mny = f" · 괴리율 {disparity:+.1%}" if disparity is not None else ""
        tag("UNLOCK_SUPPLY", f"청구 개시 D-{d_unlock}{mny}")

    # 5) 깊은 ITM + 청구기간 개방 → 전환 후 매도 플로우
    window_open = (bond.conv_start is not None and bond.conv_start <= asof
                   and (bond.conv_end is None or asof <= bond.conv_end))
    if disparity is not None and disparity >= th.deep_itm and window_open:
        tag("ITM_CONVERT_FLOW", f"괴리율 {disparity:+.1%} · 청구기간 진행 중")

    # 6) EB 구분 — 자기주식 vs 타법인주식
    if bond.sec_type == SecurityType.EB:
        if bond.eb_target_is_treasury:
            tag("TREASURY_EB",
                f"자기주식 교환 — 자사주 재유출(소각 회피){' · ' + bond.eb_target if bond.eb_target else ''}")
        elif bond.eb_target:
            tag("EB_CROSS_HOLDING",
                f"교환대상: {bond.eb_target} → 패리티/지주 디스카운트 점검")

    # 7) 자금난성 조달
    if distress is not None and distress >= 0.7 and bond.is_private:
        tag("DISTRESS_FUNDING", f"운영·차환자금 비중 {distress:.0%}")

    # 8) 최대주주 콜옵션
    if bond.call_option:
        tag("OWNER_CALL_OPTION",
            bond.call_option_detail or "매도청구권(콜옵션) 부착")

    # 9) 컴플라이언스
    if compliance and compliance.flagged:
        tag("COMPLIANCE_AVOID", ", ".join(compliance.reasons))
        flags.append("공시신뢰성 리스크: " + ", ".join(compliance.reasons))

    # 10) 숏 과밀 스퀴즈
    if (sbr is not None and sbr >= th.short_crowded_pct
            and disparity is not None and disparity <= th.deep_otm
            and room is False):
        tag("SQUEEZE_RISK",
            f"숏잔고 {sbr:.1f}% · 깊은 OTM · 리픽싱 소진 → 숏 진입 위험")

    # 11) 원문 정밀독해 이벤트 단서 (지배권·앵커·차환·누적오버행)
    dr = bond.deep_read
    if dr is not None:
        if dr.control_change:
            tag("CONTROL_CHANGE_SIGNAL",
                f"EOD 카브아웃: {dr.control_change} 단독 최대주주 변경 상정 "
                f"→ 단순 숏 위험(이벤트/스퀴즈)")
            flags.append(f"지배권 단서: {dr.control_change} 단독 최대주주 변경 가능")
        if dr.anchor_is_value_up:
            anc = dr.anchor_name or (dr.value_up_subscribers[0]
                                     if dr.value_up_subscribers else "밸류업/PE")
            pct = f" ({dr.anchor_pct:.0%})" if dr.anchor_pct else ""
            tag("ANCHOR_VALUE_UP", f"앵커 인수자 {anc}{pct} — 밸류업/PE 성격 → 숏 약화")
        if (dr.cumulative_overhang_pct is not None
                and dr.cumulative_overhang_pct >= th.cumulative_overhang_heavy_pct):
            tag("MEZZ_OVERHANG_HEAVY",
                f"누적 메자닌 오버행 {dr.cumulative_overhang_pct:.1f}% (미상환 전체)")
        if dr.refinance_series:
            canc = " · 소각" if dr.refinance_cancel else ""
            tag("REFI_RESTRIKE",
                f"구 {', '.join(dr.refinance_series)} 차환{canc} → 전환가 하향 재설정")

    # 정보 부족 플래그
    if bond.sec_type in (SecurityType.CB, SecurityType.BW) \
            and not bond.refixing_schedule and bond.refix_period_months is None:
        flags.append("리픽싱 주기 미확인(관행 3개월 추정 적용)")
    if bond.sec_type == SecurityType.RCPS and not bond.rcps_confirmed:
        flags.append("RCPS 후보(기타주식 증자) — 본문 미확정")

    score = round(sum(tw.weight(t) for t in tags), 3)
    side = Side.SHORT if score >= 1.5 else Side.LONG if score <= -0.5 else Side.NEUTRAL

    return SignalScore(
        rcept_no=bond.rcept_no, corp_name=bond.corp_name,
        stock_code=bond.stock_code, corp_cls=bond.corp_cls,
        sec_type=bond.sec_type, disclosed_date=bond.disclosed_date,
        face_amount=bond.face_amount, current_price=price,
        conversion_price=bond.conversion_price, disparity=disparity,
        market_cap=snap.market_cap if snap else None,
        proceeds_to_mktcap=p2m, overhang_ratio=oh,
        short_balance_ratio=sbr, conv_start=bond.conv_start,
        days_to_unlock=d_unlock, days_to_refix=d_refix,
        serial_count=serial_count, tags=tags, score=score, side=side,
        cumulative_overhang_pct=(bond.deep_read.cumulative_overhang_pct
                                 if bond.deep_read else None),
        rationale=rationale, flags=flags,
    )


def primary_filter(signals: list[SignalScore],
                   th: Thresholds = SETTINGS.thresholds) -> list[SignalScore]:
    """scan 모드 게이트: 태그 보유 OR 큰 괴리율 OR 리픽싱/언락 임박만 통과."""
    kept = []
    for s in signals:
        big = s.disparity is not None and (
            s.disparity >= th.deep_itm or s.disparity <= th.deep_otm)
        soon = (s.days_to_refix is not None and s.days_to_refix <= th.refix_imminent_days) \
            or (s.days_to_unlock is not None and s.days_to_unlock <= th.unlock_window_days)
        if s.tags or big or soon:
            kept.append(s)
    logger.info("primary_filter: %d → %d", len(signals), len(kept))
    return kept


def to_dataframe(signals: list[SignalScore]) -> pd.DataFrame:
    """점수 내림차순 테이블."""
    if not signals:
        return pd.DataFrame()
    df = pd.DataFrame([s.model_dump() for s in signals])
    for col in ("tags", "rationale", "flags"):
        df[col] = df[col].apply(lambda xs: " | ".join(xs))
    return df.sort_values("score", ascending=False).reset_index(drop=True)
