"""중앙 설정 — Mezzanine Issuance Radar.

모든 엔드포인트는 OpenDART 공식 개발가이드(DS005)에서 검증된 값이다.
  CB   : cvbdIsDecsn  (apiId 2020033)
  BW   : bdwtIsDecsn  (apiId 2020034)
  EB   : exbdIsDecsn  (apiId 2020035)
  RCPS : piicDecsn    (apiId 2020023, 유상증자결정 — 기타주식 발행분에서 탐지)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# OpenDART 엔드포인트 (검증 완료)
# --------------------------------------------------------------------------- #
DART_BASE = "https://opendart.fss.or.kr/api"

DART_ENDPOINTS = {
    "list": f"{DART_BASE}/list.json",
    "cb": f"{DART_BASE}/cvbdIsDecsn.json",
    "bw": f"{DART_BASE}/bdwtIsDecsn.json",
    "eb": f"{DART_BASE}/exbdIsDecsn.json",
    "rcps": f"{DART_BASE}/piicDecsn.json",     # 유상증자결정
    "document": f"{DART_BASE}/document.xml",   # 공시서류 원본(zip)
}

DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

# list.json pblntf_ty
PBLNTF_TY = {"주요사항보고": "B", "거래소공시": "I"}

# 공시명 매칭 키워드 — 발행 유니버스
REPORT_NAME_KEYWORDS: dict[str, list[str]] = {
    "cb": ["전환사채권 발행결정", "전환사채권발행결정"],
    "bw": ["신주인수권부사채권 발행결정", "신주인수권부사채권발행결정"],
    "eb": ["교환사채권 발행결정", "교환사채권발행결정"],
    "rcps": ["유상증자 결정", "유상증자결정"],  # 기타주식(우선주) 발행분만 파서에서 통과
}

# 권리행사(실물 공급) 모니터 — 거래소공시(I) 채널
EXERCISE_KEYWORDS = ["전환청구권행사", "신주인수권행사", "교환청구권행사", "전환가액의조정", "전환가액의 조정"]

# 원문 정밀독해 키워드
# 지배권 변경 단서(EOD 카브아웃 등)
CONTROL_CHANGE_KEYWORDS = [
    "단독 최대주주", "최대주주로 변경", "최대주주가 되", "경영권의 양도",
    "경영권 인수", "지배권",
]
# 밸류업/PE/경영참여 성격 인수자 — 단순 코스닥벤처펀드와 구별
VALUE_UP_KEYWORDS = [
    "기업가치제고", "밸류업", "value-up", "value up", "사모투자합자",
    "경영참여", "그로쓰", "프라이빗에쿼티", "PEF", "사모투자",
]
# 지배권 단서에서 제외할 일반명사(자기지칭 등)
GENERIC_ENTITY_STOP = {
    "발행회사", "회사", "최대주주", "당사", "본건", "동사", "그", "이",
    "인수인", "사채권자", "대주주",
}

# 공시신뢰성 리스크 키워드
COMPLIANCE_KEYWORDS = [
    "불성실공시법인", "공시불이행", "공시번복", "공시변경",
    "횡령", "배임", "상장적격성 실질심사", "감사의견 거절", "감사의견거절",
    "매매거래정지", "거래정지",
]


# --------------------------------------------------------------------------- #
# 전략 임계값
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Thresholds:
    """필터·태깅 임계값 — 백테스트 캘리브레이션 대상."""

    deep_itm: float = 0.25            # 주가/전환가-1 ≥ 25% → 전환차익 매도 압력
    deep_otm: float = -0.20           # ≤ -20% → 리픽싱 트리거권
    dilution_major_pct: float = 5.0   # 잠재주식/총주식 ≥ 5% → 희석 쇼크
    proceeds_mktcap_major: float = 0.05  # 조달액/시총 ≥ 5%
    refix_imminent_days: int = 30
    unlock_window_days: int = 45      # 전환청구 개시(락업해제) 임박 윈도우
    short_crowded_pct: float = 5.0
    serial_count: int = 3             # 36개월 내 메자닌 n회 이상 → 상습 발행
    serial_window_days: int = 365 * 3
    refix_default_period_m: int = 3   # 사모 관행: 매 3개월 리픽싱 (추정치)
    calendar_horizon_days: int = 60
    cumulative_overhang_heavy_pct: float = 30.0  # 누적 메자닌 오버행 과중 기준


# --------------------------------------------------------------------------- #
# 트레이드 태그 가중치 (양수=SHORT 편향, 음수=숏 회피/LONG 편향)
# 태그 자체가 1차 산출물이고 점수는 정렬용 보조 지표다.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TagWeights:
    DILUTION_SHOCK: float = 1.5      # 시총/주식수 대비 대형 발행
    SERIAL_ISSUER: float = 1.0       # 상습 메자닌 발행사
    REFIX_SHORT: float = 1.25        # 리픽싱 여력 + OTM 구조
    UNLOCK_SUPPLY: float = 1.5       # 전환청구 개시(락업해제) 임박
    ITM_CONVERT_FLOW: float = 1.0    # 깊은 ITM + 청구기간 개방
    TREASURY_EB: float = 1.25        # 자기주식 교환 EB (자사주 재유출)
    EB_CROSS_HOLDING: float = 0.0    # 타법인주식 EB — 패리티 기회(정보성)
    DISTRESS_FUNDING: float = 0.75   # 운영/차환자금 위주 사모 조달
    OWNER_CALL_OPTION: float = 0.5   # 최대주주 콜옵션 부착
    COMPLIANCE_AVOID: float = 2.0    # 불성실공시·횡령배임 이력
    SQUEEZE_RISK: float = -0.75      # 숏 과밀 + 리픽싱 소진 → 숏 위험
    REFIX_EXHAUSTED: float = -0.5    # 70%룰 한도 소진 → 추가 희석 제한
    # --- 원문 정밀독해 이벤트 단서 ---
    CONTROL_CHANGE_SIGNAL: float = -0.75  # 지배권 변경/전략적 인수 단서 → 단순숏 위험(이벤트)
    ANCHOR_VALUE_UP: float = -0.5         # 밸류업/PE 앵커 인수자 → 숏 약화
    MEZZ_OVERHANG_HEAVY: float = 1.0      # 누적 메자닌 오버행 과중
    REFI_RESTRIKE: float = 0.5            # 구회차 차환·소각 = 전환가 재스트라이크(하향)

    def weight(self, tag: str) -> float:
        return float(getattr(self, tag, 0.0))


@dataclass
class AlertConfig:
    """알림 채널 설정 (Telegram 미설정 시 콘솔 출력만)."""

    telegram_bot_token: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))
    language: str = field(default_factory=lambda: os.environ.get("RADAR_LANG", "en"))


@dataclass
class Settings:
    """런타임 설정 — 환경변수 우선."""

    dart_api_key: str = field(default_factory=lambda: os.environ.get("DART_API_KEY", ""))
    lookback_days: int = 365          # scan 모드 기본 윈도우
    daily_window_days: int = 3        # daily 모드: 주말 커버용 3일
    max_concurrency: int = 8
    request_timeout: float = 20.0
    max_retries: int = 3
    backoff_base: float = 0.8
    inter_request_delay: float = 0.05

    state_dir: str = "state"
    thresholds: Thresholds = field(default_factory=Thresholds)
    weights: TagWeights = field(default_factory=TagWeights)
    alerts: AlertConfig = field(default_factory=AlertConfig)

    @property
    def seen_path(self) -> str:
        return os.path.join(self.state_dir, "seen_rcept.json")

    @property
    def bonds_store_path(self) -> str:
        return os.path.join(self.state_dir, "bonds.jsonl")

    def validate(self) -> None:
        if not self.dart_api_key:
            raise RuntimeError(
                "DART_API_KEY 환경변수가 비어 있습니다 (opendart.fss.or.kr에서 발급)."
            )


SETTINGS = Settings()
