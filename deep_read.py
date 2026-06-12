"""원문 자동 정밀독해 (deep read) — 정형·기본 본문 파서가 못 잡는 '이벤트 단서'.

라디오의 기계적 태깅(희석·상습·자금난·콜옵션)만으로는 디알텍 사례처럼
"단순 숏 → 실제로는 지배권 통합 이벤트"인 건을 놓친다. 이 모듈은 PM/애널리스트가
원문에서 손으로 잡아야 하는 네 가지를 자동 추출한다.

  1) 지배권 변경 단서   : 기한이익상실(EOD) 카브아웃에 '특정 법인이 단독 최대주주로
                          변경' 같은 예외가 박혀 있으면 → 전략적 인수자 지목.
  2) 앵커 인수자        : 대상자별 발행내역에서 최대 배정처 + 밸류업/PE/경영참여 성격.
  3) 차환 대상 회차     : 채무상환자금으로 어느 구회차를 상환·소각하는지(리스트라이크).
  4) 누적 메자닌 오버행 : 미상환 사채권 표의 '기발행주식총수 대비 비율' = (A+B)/C.

모두 휴리스틱이며 실패 시 None/빈값으로 보수 처리한다. 입력은
DartClient.fetch_document_text 가 만든 평문.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from config import (CONTROL_CHANGE_KEYWORDS, GENERIC_ENTITY_STOP,
                    VALUE_UP_KEYWORDS)
from models import DeepReadResult

logger = logging.getLogger("mezzanine.deepread")

# --------------------------------------------------------------------------- #
# 정규식
# --------------------------------------------------------------------------- #
# "X(㈜/주식회사 등)가 (단독) 최대주주로 변경/가 되는" — 전략적 인수자 지목
_CTRL_ENTITY_PAT = re.compile(
    r"([가-힣A-Za-z0-9()㈜·\-]{2,40}?)\s*(?:가|이)\s*(?:단독\s*)?최대주주\s*(?:로\s*변경|가\s*되)"
)
# 회차 토큰
_SERIES_PAT = re.compile(r"제\s*(\d+)\s*회차")
# 큰 금액(원) — 1억 이상
_AMOUNT_PAT = re.compile(r"([0-9]{1,3}(?:,[0-9]{3}){2,})\s*(?:원)?")
# 미상환 사채권 표: 기발행주식총수 대비 비율(%)
_OVERHANG_PAT = re.compile(
    r"기발행주식총수\s*대비\s*비율[^0-9]*?([0-9]{1,3}(?:\.[0-9]+)?)"
)
# 대상자별 발행내역 한 행: 이름 ... 금액
_SUBSCR_LINE = re.compile(
    r"([가-힣A-Za-z0-9()㈜·\-\s]{3,60}?)\s+([0-9]{1,3}(?:,[0-9]{3}){2,})(?:\s|$)"
)

_CANCEL_PAT = re.compile(r"소각")
_DEBT_SCOPE_PAT = re.compile(r"채무상환")

# 밸류업/PE 성격 키워드를 포함하고 법인 접미사로 끝나는 명칭
_VU_ENTITY_PAT = re.compile(
    r"([가-힣A-Za-z0-9·]+(?:\s[가-힣A-Za-z0-9·]+){0,4}?"
    r"(?:기업가치제고|밸류업|경영참여|사모투자)[가-힣A-Za-z0-9·시너지]*?"
    r"\s*(?:사모투자합자회사|합자회사|유한회사|주식회사|투자조합|조합|회사))"
)


def _clean_amount(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 1) 지배권 변경 단서
# --------------------------------------------------------------------------- #
def detect_control_change(text: str) -> tuple[Optional[str], Optional[str]]:
    """EOD 카브아웃 등에서 '특정 법인이 (단독) 최대주주로 변경' 단서 추출.

    Returns (entity, context) — entity 없으면 (None, None).
    """
    for m in _CTRL_ENTITY_PAT.finditer(text):
        ent = m.group(1).strip(" ·-()")
        # 일반명사·발행회사 자기지칭 제외, 너무 긴 절 제외
        token = ent.split()[-1] if " " in ent else ent
        if token in GENERIC_ENTITY_STOP or "발행회사" in ent or len(ent) < 2:
            continue
        # 회사성 토큰만 채택(㈜/주식회사/임플란트 등 고유명) — 일반 '최대주주' 배제
        if not re.search(r"㈜|주식회사|회사|[가-힣]{2,}(?:임플란트|전자|바이오|제약|"
                         r"홀딩스|그룹|증권|캐피탈|인베스트|파트너스|텍|중공업)", ent):
            # 고유명 휴리스틱 미달이지만 4자 이상 한글 고유명이면 통과
            if not (re.fullmatch(r"[가-힣A-Za-z0-9·\-]{3,20}", ent)):
                continue
        s = max(0, m.start() - 50)
        ctx = re.sub(r"\s+", " ", text[s:m.end() + 30]).strip()
        return ent, ctx[:160]
    # 키워드만 있고 엔티티 못 잡은 경우: 경영권/지배권 언급 여부만 알림
    if any(k in text for k in ("경영권의 양도", "경영권 인수", "지배권")):
        return None, None
    return None, None


# --------------------------------------------------------------------------- #
# 2) 앵커 인수자
# --------------------------------------------------------------------------- #
def detect_anchor_subscriber(
    text: str, face_amount: Optional[float] = None
) -> tuple[Optional[str], Optional[float], Optional[float], bool, list[str]]:
    """대상자별 발행내역에서 최대 배정처와 밸류업/PE 성격 판정.

    Returns (anchor_name, anchor_amount, anchor_pct, anchor_is_value_up, value_up_names).
    """
    anchor = text.find("대상자")
    scope = text[anchor: anchor + 6000] if anchor >= 0 else text[:6000]
    # 미상환 사채권 / 채무상환 표로 범위가 넘치지 않도록 컷
    for stop in ("【채무상환", "채무상환자금의 경우", "【미상환", "미상환 주권",
                 "기발행주식총수"):
        p = scope.find(stop)
        if p > 0:
            scope = scope[:p]

    best_name, best_amt = None, 0.0
    for m in _SUBSCR_LINE.finditer(scope):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" ·-()")
        amt = _clean_amount(m.group(2))
        # 표 헤더·합계·총액·미상환 사채행 배제
        if not amt or amt < 1e8:
            continue
        if any(w in name for w in ("권면", "총액", "합계", "소계", "비율",
                                   "주식총수", "사채권", "회차", "잔액")):
            continue
        if len(name) < 3:
            continue
        if amt > best_amt:
            best_name, best_amt = name, amt

    # 밸류업/PE/경영참여 성격 인수자 수집(앵커 아니어도) — 법인명 단위로 정제
    value_up = []
    for m in _VU_ENTITY_PAT.finditer(text):
        nm = re.sub(r"\s+", " ", m.group(1)).strip(" ·-()")
        nm = re.sub(r"^[\d.,\s]+", "", nm).strip()  # 앞자리 숫자(100.00 등) 제거
        if 4 <= len(nm) <= 50 and nm not in value_up:
            value_up.append(nm)
    # 부분문자열 중복 제거(짧은 쪽이 긴 쪽에 포함되면 긴 쪽만)
    value_up = [n for n in value_up
                if not any(n != o and n in o for o in value_up)][:4]

    anchor_is_vu = bool(best_name and any(
        kw in best_name for kw in VALUE_UP_KEYWORDS)) or bool(
        best_name and value_up and any(best_name.split()[0] in v for v in value_up))
    # 앵커명이 밸류업 키워드를 직접 안 가져도, 본문에 밸류업 SPV가 있으면 표시
    if not anchor_is_vu and value_up:
        anchor_is_vu = True

    pct = (best_amt / face_amount) if (face_amount and best_amt) else None
    return best_name, (best_amt or None), pct, anchor_is_vu, value_up


# --------------------------------------------------------------------------- #
# 3) 차환 대상 회차
# --------------------------------------------------------------------------- #
def detect_refinance(text: str) -> tuple[list[str], bool]:
    """채무상환 섹션에서 상환·소각 대상 구회차 추출 + 소각예정 여부.

    '【채무상환자금의 경우】' 표를 우선 스코프로 잡고, '【미상환' 표로 범위가
    넘치지 않도록 컷한다(미상환 표에는 모든 구회차가 나열되므로 오염 방지).
    """
    start = text.find("채무상환자금의 경우")
    if start < 0:
        m = _DEBT_SCOPE_PAT.search(text)
        if not m:
            return [], False
        start = m.start()
    scope = text[start: start + 1200]
    for stop in ("【미상환", "미상환 주권", "【조달자금", "【시설자금"):
        p = scope.find(stop)
        if p > 0:
            scope = scope[:p]
    series = []
    for s in _SERIES_PAT.finditer(scope):
        tag = f"{int(s.group(1))}회차"
        if tag not in series:
            series.append(tag)
    cancel = bool(_CANCEL_PAT.search(scope))
    return series, cancel


# --------------------------------------------------------------------------- #
# 4) 누적 메자닌 오버행
# --------------------------------------------------------------------------- #
def detect_cumulative_overhang(text: str) -> Optional[float]:
    """미상환 사채권 표의 '기발행주식총수 대비 비율(%)' = (A+B)/C 추출."""
    m = _OVERHANG_PAT.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
        return v if 0 < v < 1000 else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 통합
# --------------------------------------------------------------------------- #
def deep_read(text: str, face_amount: Optional[float] = None) -> DeepReadResult:
    """원문 전체 정밀독해 → DeepReadResult(+사람이 읽는 clues)."""
    res = DeepReadResult()
    if not text:
        return res

    ent, ctx = detect_control_change(text)
    res.control_change = ent
    res.control_change_detail = ctx

    a_name, a_amt, a_pct, a_vu, vu = detect_anchor_subscriber(text, face_amount)
    res.anchor_name = a_name
    res.anchor_amount = a_amt
    res.anchor_pct = a_pct
    res.anchor_is_value_up = a_vu
    res.value_up_subscribers = vu

    series, cancel = detect_refinance(text)
    res.refinance_series = series
    res.refinance_cancel = cancel

    res.cumulative_overhang_pct = detect_cumulative_overhang(text)

    res.clues = _build_clues(res)
    return res


def _build_clues(r: DeepReadResult) -> list[str]:
    """알림 '⚡ Event clues' 섹션에 들어갈 사람이 읽는 줄."""
    out: list[str] = []
    if r.control_change:
        out.append(f"🎯 Control-change carve-out: {r.control_change} → sole-largest-"
                   f"shareholder route anticipated (EOD exception)")
    if r.anchor_name:
        pct = f" ({r.anchor_pct:.0%} of deal)" if r.anchor_pct else ""
        vu = " · VALUE-UP/PE anchor" if r.anchor_is_value_up else ""
        out.append(f"⚓ Anchor subscriber: {r.anchor_name}{pct}{vu}")
    elif r.value_up_subscribers:
        out.append("⚓ Value-up/PE vehicle present: "
                   + "; ".join(r.value_up_subscribers[:2]))
    if r.refinance_series:
        canc = " (cancel/소각)" if r.refinance_cancel else ""
        out.append(f"♻️ Refinances prior {', '.join(r.refinance_series)}{canc} "
                   f"— dilution re-strike lower")
    if r.cumulative_overhang_pct is not None:
        out.append(f"📊 Cumulative mezz overhang: {r.cumulative_overhang_pct:.1f}% "
                   f"of shares (all outstanding CB/BW incl. new)")
    return out


def enrich_deep_read(bond, text: str):
    """bond.deep_read 채우기(제자리). enrich_bond_from_text 이후 호출."""
    if not text:
        return bond
    bond.deep_read = deep_read(text, face_amount=bond.face_amount)
    return bond
