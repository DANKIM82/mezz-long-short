"""공시 본문 파서 v2 — 정형 API 미제공 항목 추출.

대상: 풋옵션(조기상환청구권) 개시일·주기, 정기 리픽싱 주기,
최대주주 콜옵션(매도청구권), 대상자(인수자), RCPS 확정(상환+전환 조건).

DartClient.fetch_document_text(rcept_no)가 만든 평문 텍스트를 입력으로 받는다.
규칙은 한국 사모 메자닌 표준 문안 기준의 휴리스틱이며, 실패 시 None 유지
(분석 엔진은 '미상'으로 보수 처리).
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from events import add_months
from models import MezzanineBond

logger = logging.getLogger("mezzanine.body")

_PUT_PAT = re.compile(r"조기상환청구권|풋\s*옵션|put\s*option", re.IGNORECASE)
_CALL_PAT = re.compile(r"매도청구권|콜\s*옵션|call\s*option", re.IGNORECASE)
_CALL_OWNER_PAT = re.compile(r"최대주주|발행회사[가-힣\s]{0,8}지정", re.IGNORECASE)
_REFIX_PERIOD_PAT = re.compile(r"매\s*(\d+)\s*개월|(\d+)\s*개월\s*마다")
_PUT_START_PAT = re.compile(
    r"(?:발행일|납입일)[로으]?부터\s*(\d+)\s*(년|개월)[이가\s]*(?:되는|경과)")
_DATE_PAT = re.compile(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})")
# 대상자(인수자) 휴리스틱: 조합/운용/캐피탈/인베스트/증권/저축은행 등 기관성 토큰
_SUBSCRIBER_PAT = re.compile(
    r"[\(\)가-힣A-Za-z0-9&\.\-]{2,30}?"
    r"(?:투자조합|조합|자산운용|운용|캐피탈|인베스트먼트|인베스트|파트너스|"
    r"증권|저축은행|신기술|벤처투자|유한회사|주식회사)")
_RCPS_PAT_REDEEM = re.compile(r"상환[권조건에관한사항\s]")
_RCPS_PAT_CONVERT = re.compile(r"전환[권조건에관한사항\s]")
_RCPS_PAT_PREF = re.compile(r"우선주")


def detect_refix_period_months(text: str) -> int | None:
    """'매 3개월' / '3개월마다' 류에서 리픽싱 주기(월) 추출."""
    m = _REFIX_PERIOD_PAT.search(text)
    if not m:
        return None
    g = m.group(1) or m.group(2)
    try:
        v = int(g)
        return v if 1 <= v <= 12 else None
    except (TypeError, ValueError):
        return None


def detect_put(text: str, anchor: dt.date | None) -> tuple[bool, list[dt.date]]:
    """풋옵션 존재 + 개시일 추정.

    '발행일로부터 N년/개월이 되는 날' 패턴이 있으면 anchor 기준으로 개시일 산출.
    이후 3개월 주기 행사 가정의 구체 일정은 events.build_events에서 생성.
    """
    if not _PUT_PAT.search(text):
        return False, []
    dates: list[dt.date] = []
    m = _PUT_START_PAT.search(text)
    if m and anchor:
        n, unit = int(m.group(1)), m.group(2)
        months = n * 12 if unit == "년" else n
        try:
            dates.append(add_months(anchor, months))
        except Exception:
            pass
    return True, dates


def detect_call_option(text: str) -> tuple[bool | None, str | None]:
    """최대주주 등 매도청구권(콜옵션) 존재 여부 + 짧은 컨텍스트."""
    m = _CALL_PAT.search(text)
    if not m:
        return None, None
    s = max(0, m.start() - 40)
    ctx = re.sub(r"\s+", " ", text[s:m.end() + 60]).strip()
    is_owner = bool(_CALL_OWNER_PAT.search(ctx))
    return True, (ctx[:120] + ("…" if len(ctx) > 120 else "")) if is_owner or True else None


def extract_subscribers(text: str, limit: int = 8) -> list[str]:
    """'특정인에 대한 대상자별 사채발행내역' 인근에서 인수자명 휴리스틱 추출."""
    anchor = text.find("대상자")
    scope = text[anchor: anchor + 4000] if anchor >= 0 else text[:4000]
    names: list[str] = []
    for m in _SUBSCRIBER_PAT.finditer(scope):
        nm = m.group(0).strip("()· ").strip()
        if 2 <= len(nm) <= 30 and nm not in names:
            names.append(nm)
        if len(names) >= limit:
            break
    return names


def confirm_rcps(text: str) -> bool:
    """우선주 + 상환 조건 + 전환 조건 동시 존재 → RCPS로 확정."""
    return bool(_RCPS_PAT_PREF.search(text)
                and _RCPS_PAT_REDEEM.search(text)
                and _RCPS_PAT_CONVERT.search(text))


def enrich_bond_from_text(bond: MezzanineBond, text: str) -> MezzanineBond:
    """본문 텍스트로 bond의 미정형 항목 일괄 보강(제자리 갱신 후 반환)."""
    if not text:
        return bond
    bond.refix_period_months = detect_refix_period_months(text) or bond.refix_period_months
    has_put, put_dates = detect_put(text, bond.anchor_date)
    bond.has_put_option = has_put if bond.has_put_option is None else bond.has_put_option
    if put_dates:
        bond.put_option_dates = sorted(set(bond.put_option_dates + put_dates))
    call, detail = detect_call_option(text)
    if call is not None:
        bond.call_option = call
        bond.call_option_detail = detail
    subs = extract_subscribers(text)
    if subs:
        bond.subscribers = subs
    if bond.sec_type.value == "RCPS" and bond.rcps_confirmed is None:
        bond.rcps_confirmed = confirm_rcps(text)
    return bond
