"""데이터 모델 (Pydantic v2) — CB·BW·EB·RCPS 공통 도메인 계층."""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SecurityType(str, Enum):
    CB = "CB"
    BW = "BW"
    EB = "EB"
    RCPS = "RCPS"


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class MezzanineBond(BaseModel):
    """발행결정 공시 1건. 정형 필드는 OpenDART 명세 검증값 기준."""

    # --- 식별 ---
    rcept_no: str
    corp_code: str
    corp_name: str
    stock_code: Optional[str] = None
    corp_cls: Optional[str] = Field(None, description="Y유가 K코스닥 N코넥스 E기타")
    sec_type: SecurityType
    disclosed_date: Optional[dt.date] = Field(None, description="list.json rcept_dt")

    # --- 채권 공통 조건 ---
    series: Optional[str] = None
    face_amount: Optional[float] = Field(None, description="권면총액(원) / RCPS는 미제공")
    coupon_rate: Optional[float] = Field(None, description="표면이자율 %")
    ytm: Optional[float] = Field(None, description="만기이자율(만기보장수익률) %")
    maturity_date: Optional[dt.date] = None
    issuance_method: Optional[str] = Field(None, description="bdis_mthn: 사모/공모 등")
    securities_report: Optional[bool] = Field(None, description="증권신고서 제출대상(rs_sm_atn)")

    # --- 전환/행사/교환 조건 ---
    conversion_price: Optional[float] = Field(None, description="전환·행사·교환가액(원/주)")
    conversion_ratio: Optional[float] = None
    price_determination: Optional[str] = Field(None, description="가액 결정방법(BW/EB)")
    potential_shares: Optional[float] = Field(None, description="발행·교환대상 주식수")
    potential_shares_ratio: Optional[float] = Field(None, description="주식총수 대비 %")
    conv_start: Optional[dt.date] = Field(None, description="청구/행사기간 시작 = 사모 락업해제")
    conv_end: Optional[dt.date] = None

    # --- 리픽싱 (CB/BW 정형 제공, EB는 미제공) ---
    refix_floor_price: Optional[float] = Field(None, description="최저 조정가액")
    refix_floor_basis: Optional[str] = None
    refix_below_70_limit: Optional[float] = Field(None, description="70%미만 조정가능 잔여한도(원)")

    # --- BW 전용 ---
    bw_detachable: Optional[bool] = Field(None, description="분리형 여부(bdwt_div_atn)")

    # --- EB 전용 ---
    eb_target: Optional[str] = Field(None, description="교환대상 종류(extg)")
    eb_target_is_treasury: Optional[bool] = Field(None, description="자기주식 교환 여부")

    # --- RCPS(유상증자) 전용 ---
    new_common_shares: Optional[float] = None
    new_other_shares: Optional[float] = Field(None, description="기타주식(우선주 등) 수")
    pre_total_shares: Optional[float] = None
    ic_method: Optional[str] = Field(None, description="증자방식(제3자배정 등)")
    rcps_confirmed: Optional[bool] = Field(None, description="본문에서 상환+전환 조건 확인")

    # --- 자금사용 목적 ---
    use_facility: Optional[float] = None
    use_operating: Optional[float] = None
    use_debt_repay: Optional[float] = None
    use_other_sec: Optional[float] = None
    use_etc: Optional[float] = None

    # --- 일정 ---
    board_date: Optional[dt.date] = None
    pay_date: Optional[dt.date] = None

    # --- 본문 파싱 항목 (정형 미제공) ---
    has_put_option: Optional[bool] = None
    put_option_dates: list[dt.date] = Field(default_factory=list)
    refixing_schedule: list[dt.date] = Field(default_factory=list)
    refix_period_months: Optional[int] = None
    call_option: Optional[bool] = Field(None, description="최대주주 등 매도청구권(콜옵션)")
    call_option_detail: Optional[str] = None
    subscribers: list[str] = Field(default_factory=list, description="대상자(본문 휴리스틱)")

    # --- 원문 정밀독해 (이벤트 단서) ---
    deep_read: Optional["DeepReadResult"] = None

    # ------------------------------------------------------------------ #
    @property
    def is_private(self) -> bool:
        """사모 여부. CB/BW/EB는 발행방법, RCPS는 제3자배정으로 판정."""
        if self.issuance_method and "사모" in self.issuance_method:
            return True
        if self.ic_method and "제3자" in self.ic_method:
            return True
        return False

    @property
    def anchor_date(self) -> Optional[dt.date]:
        """일정 계산 기준일: 납입일 → 이사회일 → 공시일."""
        return self.pay_date or self.board_date or self.disclosed_date


class DeepReadResult(BaseModel):
    """원문 정밀독해 산출 — 이벤트 단서(정형·기본 본문 파서가 못 잡는 항목)."""

    control_change: Optional[str] = Field(None, description="EOD 카브아웃 전략적 인수자")
    control_change_detail: Optional[str] = None
    anchor_name: Optional[str] = Field(None, description="최대 배정 인수자")
    anchor_amount: Optional[float] = None
    anchor_pct: Optional[float] = Field(None, description="앵커 배정액 / 권면총액")
    anchor_is_value_up: bool = False
    value_up_subscribers: list[str] = Field(default_factory=list)
    refinance_series: list[str] = Field(default_factory=list, description="차환 대상 회차")
    refinance_cancel: bool = False
    cumulative_overhang_pct: Optional[float] = Field(
        None, description="미상환 사채권 기준 (A+B)/C %")
    clues: list[str] = Field(default_factory=list, description="알림용 사람이 읽는 단서")


class ComplianceFlag(BaseModel):
    corp_code: str
    corp_name: str = ""
    flagged: bool = False
    reasons: list[str] = Field(default_factory=list)
    rcept_nos: list[str] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
    stock_code: str
    last_close: Optional[float] = None
    market_cap: Optional[float] = Field(None, description="원")
    shares_outstanding: Optional[float] = None
    short_balance_ratio: Optional[float] = Field(None, description="공매도 잔고비중 %")
    short_as_of: Optional[dt.date] = None


class SignalScore(BaseModel):
    """발행건별 PM 신호 — 태그가 1차 산출물, score는 정렬용."""

    rcept_no: str
    corp_name: str
    stock_code: Optional[str]
    corp_cls: Optional[str] = None
    sec_type: SecurityType
    disclosed_date: Optional[dt.date] = None

    face_amount: Optional[float] = None
    current_price: Optional[float] = None
    conversion_price: Optional[float] = None
    disparity: Optional[float] = Field(None, description="주가/전환가-1")
    market_cap: Optional[float] = None
    proceeds_to_mktcap: Optional[float] = None
    overhang_ratio: Optional[float] = None
    short_balance_ratio: Optional[float] = None
    conv_start: Optional[dt.date] = None
    days_to_unlock: Optional[int] = None
    days_to_refix: Optional[int] = None
    serial_count: Optional[int] = None
    cumulative_overhang_pct: Optional[float] = Field(
        None, description="누적 메자닌 오버행 (deep_read)")

    tags: list[str] = Field(default_factory=list)
    score: float = 0.0
    side: Side = Side.NEUTRAL
    rationale: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class CalendarEvent(BaseModel):
    """PM 이벤트 캘린더 1행."""

    event_date: dt.date
    event_type: str  # PAY / CONV_START / REFIX_EST / REFIX / PUT / MATURITY / UNLOCK_EST
    estimated: bool = False
    stock_code: Optional[str] = None
    corp_name: str = ""
    sec_type: SecurityType = SecurityType.CB
    rcept_no: str = ""
    detail: str = ""


class ExerciseFiling(BaseModel):
    """전환·행사·교환 청구 공시(실물 공급 이벤트) — 거래소공시 채널."""

    rcept_no: str
    corp_code: str
    corp_name: str
    stock_code: Optional[str] = None
    report_nm: str = ""
    rcept_dt: Optional[dt.date] = None


# forward-ref 해소 (MezzanineBond.deep_read → DeepReadResult)
MezzanineBond.model_rebuild()
