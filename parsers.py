"""파싱 계층 v2 — OpenDART 정형 응답(검증 명세) → MezzanineBond.

필드맵은 전부 공식 개발가이드에서 확인한 키:
  CB  (cvbdIsDecsn) : cv_prc, cv_rt, cvisstk_cnt, cvisstk_tisstk_vs,
                      cvrqpd_bgd/edd, act_mktprcfl_cvprc_lwtrsprc, rmislmt_lt70p
  BW  (bdwtIsDecsn) : ex_prc, ex_rt, nstk_isstk_cnt, nstk_isstk_tisstk_vs,
                      expd_bgd/edd, act_mktprcfl_cvprc_lwtrsprc(키 CB와 동일),
                      rmislmt_lt70p, bdwt_div_atn(분리형), ex_prc_dmth
  EB  (exbdIsDecsn) : ex_prc, ex_rt, extg(교환대상), extg_stkcnt,
                      extg_tisstk_vs, exrqpd_bgd/edd, ex_prc_dmth
                      ※ 리픽싱 정형 필드 없음
  RCPS(piicDecsn)   : nstk_ostk_cnt, nstk_estk_cnt, fv_ps,
                      bfic_tisstk_ostk/estk, ic_mthn
                      ※ 발행가·전환/상환조건·납입일은 본문 파싱 필요
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Optional

from models import MezzanineBond, SecurityType

logger = logging.getLogger("mezzanine.parser")

_EMPTY = {"", "-", "해당사항없음", "해당없음", None}
_DATE_RE = re.compile(r"(\d{4})\D*?(\d{1,2})\D*?(\d{1,2})")


# --------------------------------------------------------------------------- #
# 값 클리너
# --------------------------------------------------------------------------- #
def clean_number(raw: Any) -> Optional[float]:
    """'9,999,999,999' / '2.5' / '-' / '(1,234)' → float | None."""
    if raw in _EMPTY:
        return None
    s = str(raw).strip().replace(",", "").replace("%", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s in _EMPTY:
        return None
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def clean_date(raw: Any) -> Optional[dt.date]:
    """'2027년 06월 30일' / '2027-06-30' / '20270630' → date | None."""
    if raw in _EMPTY:
        return None
    m = _DATE_RE.search(str(raw))
    if not m:
        return None
    try:
        y, mo, d = (int(g) for g in m.groups())
        return dt.date(y, mo, d)
    except ValueError:
        return None


def clean_bool(raw: Any) -> Optional[bool]:
    """'예/아니오/대상/미대상/분리/비분리' → bool | None."""
    if raw in _EMPTY:
        return None
    s = str(raw).strip()
    if any(k in s for k in ("예", "대상", "있음", "분리형", "Y")) and "미" not in s and "비" not in s and "아니" not in s:
        return True
    if any(k in s for k in ("아니", "미대상", "없음", "비분리", "면제", "N")):
        return False
    return None


# --------------------------------------------------------------------------- #
# 필드 매핑 (모델필드 → DART 키)
# --------------------------------------------------------------------------- #
_BOND_COMMON = {
    "rcept_no": "rcept_no", "corp_code": "corp_code", "corp_name": "corp_name",
    "corp_cls": "corp_cls", "series": "bd_tm", "face_amount": "bd_fta",
    "coupon_rate": "bd_intr_ex", "ytm": "bd_intr_sf", "maturity_date": "bd_mtd",
    "issuance_method": "bdis_mthn", "securities_report": "rs_sm_atn",
    "use_facility": "fdpp_fclt", "use_operating": "fdpp_op",
    "use_debt_repay": "fdpp_dtrp", "use_other_sec": "fdpp_ocsa",
    "use_etc": "fdpp_etc", "board_date": "bddd", "pay_date": "pymd",
}

_CB_MAP = {
    **_BOND_COMMON,
    "conversion_price": "cv_prc",
    "conversion_ratio": "cv_rt",
    "potential_shares": "cvisstk_cnt",
    "potential_shares_ratio": "cvisstk_tisstk_vs",
    "conv_start": "cvrqpd_bgd",
    "conv_end": "cvrqpd_edd",
    "refix_floor_price": "act_mktprcfl_cvprc_lwtrsprc",
    "refix_floor_basis": "act_mktprcfl_cvprc_lwtrsprc_bs",
    "refix_below_70_limit": "rmislmt_lt70p",
}

_BW_MAP = {
    **_BOND_COMMON,
    "conversion_price": "ex_prc",
    "conversion_ratio": "ex_rt",
    "price_determination": "ex_prc_dmth",
    "bw_detachable": "bdwt_div_atn",
    "potential_shares": "nstk_isstk_cnt",
    "potential_shares_ratio": "nstk_isstk_tisstk_vs",
    "conv_start": "expd_bgd",
    "conv_end": "expd_edd",
    # BW도 응답키는 'cvprc' 표기를 그대로 사용함(명세 확인)
    "refix_floor_price": "act_mktprcfl_cvprc_lwtrsprc",
    "refix_floor_basis": "act_mktprcfl_cvprc_lwtrsprc_bs",
    "refix_below_70_limit": "rmislmt_lt70p",
}

_EB_MAP = {
    **_BOND_COMMON,
    "conversion_price": "ex_prc",
    "conversion_ratio": "ex_rt",
    "price_determination": "ex_prc_dmth",
    "eb_target": "extg",
    "potential_shares": "extg_stkcnt",
    "potential_shares_ratio": "extg_tisstk_vs",
    "conv_start": "exrqpd_bgd",
    "conv_end": "exrqpd_edd",
}

_RCPS_MAP = {
    "rcept_no": "rcept_no", "corp_code": "corp_code", "corp_name": "corp_name",
    "corp_cls": "corp_cls",
    "new_common_shares": "nstk_ostk_cnt",
    "new_other_shares": "nstk_estk_cnt",
    "ic_method": "ic_mthn",
    "use_facility": "fdpp_fclt", "use_operating": "fdpp_op",
    "use_debt_repay": "fdpp_dtrp", "use_other_sec": "fdpp_ocsa",
    "use_etc": "fdpp_etc",
}

_NUMERIC = {
    "face_amount", "coupon_rate", "ytm", "conversion_price", "conversion_ratio",
    "potential_shares", "potential_shares_ratio", "refix_floor_price",
    "refix_below_70_limit", "use_facility", "use_operating", "use_debt_repay",
    "use_other_sec", "use_etc", "new_common_shares", "new_other_shares",
}
_DATES = {"maturity_date", "conv_start", "conv_end", "board_date", "pay_date"}
_BOOLS = {"securities_report", "bw_detachable"}

_KIND_TO_TYPE = {
    "cb": SecurityType.CB, "bw": SecurityType.BW,
    "eb": SecurityType.EB, "rcps": SecurityType.RCPS,
}
_KIND_TO_MAP = {"cb": _CB_MAP, "bw": _BW_MAP, "eb": _EB_MAP, "rcps": _RCPS_MAP}


def parse_bond(
    row: dict[str, Any],
    stock_code: Optional[str] = None,
    disclosed_date: Optional[dt.date] = None,
) -> Optional[MezzanineBond]:
    """정형 레코드 1건 → MezzanineBond.

    Parameters
    ----------
    row : dict
        fetch_major_report 결과('_sec_kind' 포함).
    stock_code : str, optional
        list.json에서 조인한 6자리 종목코드.
    disclosed_date : date, optional
        list.json rcept_dt (RCPS는 정형에 일자 필드가 없어 기준일로 사용).

    Returns
    -------
    MezzanineBond | None
        RCPS의 경우 기타주식(우선주) 발행이 없으면 None(보통주 증자 제외).
    """
    kind = row.get("_sec_kind", "cb")
    sec_type = _KIND_TO_TYPE[kind]
    fmap = _KIND_TO_MAP[kind]

    parsed: dict[str, Any] = {
        "sec_type": sec_type,
        "stock_code": stock_code,
        "disclosed_date": disclosed_date,
    }
    for mf, dk in fmap.items():
        raw = row.get(dk)
        if mf in _NUMERIC:
            parsed[mf] = clean_number(raw)
        elif mf in _DATES:
            parsed[mf] = clean_date(raw)
        elif mf in _BOOLS:
            parsed[mf] = clean_bool(raw)
        else:
            parsed[mf] = None if raw in _EMPTY else str(raw).strip()

    if not parsed.get("rcept_no"):
        return None

    # --- 종류별 후처리 -------------------------------------------------- #
    if kind == "eb":
        tgt = parsed.get("eb_target") or ""
        parsed["eb_target_is_treasury"] = ("자기주식" in tgt) or ("자사주" in tgt)

    if kind == "rcps":
        other = parsed.get("new_other_shares") or 0
        if other <= 0:
            return None  # 보통주만 발행 → 메자닌 아님
        pre = (clean_number(row.get("bfic_tisstk_ostk")) or 0) + \
              (clean_number(row.get("bfic_tisstk_estk")) or 0)
        parsed["pre_total_shares"] = pre or None
        parsed["potential_shares"] = other
        if pre:
            parsed["potential_shares_ratio"] = other / pre * 100.0

    try:
        return MezzanineBond(**parsed)
    except Exception as exc:
        logger.error("validation fail corp=%s err=%s", parsed.get("corp_name"), exc)
        return None
