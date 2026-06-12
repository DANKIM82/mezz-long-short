"""컴플라이언스 v2 — 불성실공시·횡령배임 탐지.

scan 모드 : 시장 전체 인덱스(B+I 채널 키워드 매칭)
daily 모드: 신규 발행사별 단건 점검(list.json corp_code 검색, 요청 1~2건)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from config import COMPLIANCE_KEYWORDS, PBLNTF_TY
from dart_client import DartClient
from models import ComplianceFlag

logger = logging.getLogger("mezzanine.compliance")


def _hits(report_nm: str) -> list[str]:
    return [kw for kw in COMPLIANCE_KEYWORDS if kw in (report_nm or "")]


async def build_compliance_index(
    client: DartClient, bgn_de: str, end_de: str
) -> dict[str, ComplianceFlag]:
    """기간 내 전체 시장 위험공시 인덱스 (scan 모드용).

    Returns
    -------
    dict[corp_code, ComplianceFlag]
    """
    disclosures: list[dict[str, Any]] = []
    for ty in (PBLNTF_TY["거래소공시"], PBLNTF_TY["주요사항보고"]):
        disclosures.extend(await client.search_disclosures(bgn_de, end_de, pblntf_ty=ty))

    index: dict[str, ComplianceFlag] = {}
    reasons: dict[str, set[str]] = defaultdict(set)
    rcepts: dict[str, set[str]] = defaultdict(set)
    for d in disclosures:
        hits = _hits(d.get("report_nm", ""))
        if not hits:
            continue
        cc = d.get("corp_code", "")
        if not cc:
            continue
        reasons[cc].update(hits)
        rcepts[cc].add(d.get("rcept_no", ""))
        index.setdefault(cc, ComplianceFlag(
            corp_code=cc, corp_name=d.get("corp_name", ""), flagged=True))
    for cc, flag in index.items():
        flag.reasons = sorted(reasons[cc])
        flag.rcept_nos = sorted(r for r in rcepts[cc] if r)
    logger.info("compliance index: %d corps flagged", len(index))
    return index


async def check_corp_flags(
    client: DartClient, corp_code: str, corp_name: str, bgn_de: str, end_de: str
) -> ComplianceFlag:
    """단일 회사 위험공시 점검 (daily 모드 — 발행사당 요청 1건 수준).

    발행 공시 '이전' 이력만 보는 것이 시점 정합적이므로
    호출측에서 end_de를 발행 공시일로 제한해 사용해도 된다.
    """
    flag = ComplianceFlag(corp_code=corp_code, corp_name=corp_name)
    try:
        rows = await client.search_disclosures(bgn_de, end_de, corp_code=corp_code)
    except Exception as exc:
        logger.error("compliance check fail corp=%s err=%s", corp_code, exc)
        return flag
    reasons: set[str] = set()
    for d in rows:
        hs = _hits(d.get("report_nm", ""))
        if hs:
            reasons.update(hs)
            flag.rcept_nos.append(d.get("rcept_no", ""))
    if reasons:
        flag.flagged = True
        flag.reasons = sorted(reasons)
    return flag


def get_flag(index: dict[str, ComplianceFlag], corp_code: str) -> ComplianceFlag:
    """인덱스 조회(없으면 비플래그)."""
    return index.get(corp_code, ComplianceFlag(corp_code=corp_code))
