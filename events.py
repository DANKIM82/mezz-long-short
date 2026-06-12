"""이벤트 캘린더 — PM은 '날짜'를 거래한다.

생성 이벤트
  PAY        납입일 (정형)
  CONV_START 전환·행사·교환 청구 개시 (정형) — 사모는 실질 락업 해제일
  UNLOCK_EST RCPS 제3자배정 1년 의무보유 해제 추정일
  REFIX      본문에서 확인된 리픽싱 일정
  REFIX_EST  사모 관행(매 3개월) 기반 추정 리픽싱 일정 — estimated=True
  PUT        본문에서 확인된 풋옵션 개시일
  MATURITY   만기

원칙: 모르는 것은 만들어내지 않는다. 추정치는 estimated=True로 명시하고,
풋옵션은 본문 확인분만 싣는다(시장 관행이 다양해 추정 금지).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from config import SETTINGS, Thresholds
from models import CalendarEvent, MezzanineBond, SecurityType


def add_months(d: dt.date, months: int) -> dt.date:
    """월 가산(말일 안전: 일자는 28일로 클램프)."""
    m = d.month - 1 + months
    y = d.year + m // 12
    return dt.date(y, m % 12 + 1, min(d.day, 28))


def _refix_capable(bond: MezzanineBond) -> bool:
    """리픽싱 가능 구조 판단: 정형 플로어/한도 필드 존재(CB·BW)."""
    return bond.sec_type in (SecurityType.CB, SecurityType.BW) and (
        bond.refix_floor_price is not None
        or bond.refix_below_70_limit is not None
    )


def build_events(
    bond: MezzanineBond, thresholds: Thresholds = SETTINGS.thresholds
) -> list[CalendarEvent]:
    """발행건 1개의 전체 이벤트 목록 생성."""
    ev: list[CalendarEvent] = []

    def _add(date: dt.date | None, etype: str, detail: str = "",
             estimated: bool = False) -> None:
        if date is None:
            return
        ev.append(CalendarEvent(
            event_date=date, event_type=etype, estimated=estimated,
            stock_code=bond.stock_code, corp_name=bond.corp_name,
            sec_type=bond.sec_type, rcept_no=bond.rcept_no, detail=detail))

    _add(bond.pay_date, "PAY", "납입일")
    lock_note = " (사모 락업 해제)" if bond.is_private else ""
    _add(bond.conv_start, "CONV_START",
         f"{bond.sec_type.value} 청구/행사 개시{lock_note}")
    _add(bond.maturity_date, "MATURITY", "만기")

    # RCPS 제3자배정: 신주 1년 의무보유 해제 추정
    if bond.sec_type == SecurityType.RCPS and bond.is_private and bond.anchor_date:
        _add(add_months(bond.anchor_date, 12), "UNLOCK_EST",
             "제3자배정 신주 1년 의무보유 해제(추정)", estimated=True)

    # 리픽싱: 본문 확인분 우선, 없으면 사모 관행 추정
    if bond.refixing_schedule:
        for d in bond.refixing_schedule:
            _add(d, "REFIX", "리픽싱(본문 확인)")
    elif _refix_capable(bond) and bond.is_private and bond.anchor_date \
            and bond.maturity_date:
        period = bond.refix_period_months or thresholds.refix_default_period_m
        cur = bond.anchor_date
        while True:
            cur = add_months(cur, period)
            if cur >= bond.maturity_date:
                break
            _add(cur, "REFIX_EST", f"리픽싱 추정(매 {period}개월 관행)",
                 estimated=True)

    for d in bond.put_option_dates:
        _add(d, "PUT", "풋옵션 행사 개시(본문 확인)")

    return sorted(ev, key=lambda e: e.event_date)


def next_event_days(
    bond: MezzanineBond,
    types: tuple[str, ...],
    asof: dt.date | None = None,
) -> int | None:
    """지정 유형 중 가장 가까운 향후 이벤트까지 잔여일."""
    asof = asof or dt.date.today()
    future = [e.event_date for e in build_events(bond)
              if e.event_type in types and e.event_date >= asof]
    return (min(future) - asof).days if future else None


def upcoming_calendar(
    bonds: list[MezzanineBond],
    horizon_days: int | None = None,
    asof: dt.date | None = None,
) -> pd.DataFrame:
    """여러 발행건 → 향후 horizon 내 이벤트 테이블(날짜순)."""
    asof = asof or dt.date.today()
    horizon = horizon_days or SETTINGS.thresholds.calendar_horizon_days
    end = asof + dt.timedelta(days=horizon)
    rows = [e.model_dump() for b in bonds for e in build_events(b)
            if asof <= e.event_date <= end]
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            .sort_values(["event_date", "corp_name"])
            .reset_index(drop=True))
