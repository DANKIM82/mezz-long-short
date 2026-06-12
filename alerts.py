"""알림 계층 — 글로벌 PM이 그대로 읽을 수 있는 발행 알림 포맷.

기본 영어(한국 종목명·기관명은 원문 유지), RADAR_LANG=ko 로 국문 전환.
Telegram 환경변수(TELEGRAM_BOT_TOKEN/CHAT_ID) 설정 시 자동 발송,
미설정 시 콘솔 출력만 한다.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Optional

import httpx

from config import SETTINGS
from dart_client import viewer_url
from models import ExerciseFiling, MezzanineBond, SignalScore

logger = logging.getLogger("mezzanine.alerts")

_MKT = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": "ETC"}


# --------------------------------------------------------------------------- #
# 포맷 유틸
# --------------------------------------------------------------------------- #
def fmt_krw(amount: Optional[float]) -> str:
    """원화 금액 → 'KRW 30.0bn (300억)' 형태."""
    if amount is None:
        return "n/a"
    eok = amount / 1e8
    bn = amount / 1e9
    if bn >= 1:
        return f"KRW {bn:,.1f}bn ({eok:,.0f}억)"
    return f"KRW {amount/1e6:,.0f}mn ({eok:,.1f}억)"


def fmt_pct(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def fmt_px(x: Optional[float]) -> str:
    return f"₩{x:,.0f}" if x is not None else "n/a"


def _funding_mix(b: MezzanineBond) -> str:
    parts = [("운영", b.use_operating), ("차환", b.use_debt_repay),
             ("시설", b.use_facility), ("타법인", b.use_other_sec),
             ("기타", b.use_etc)]
    total = sum(v for _, v in parts if v)
    if not total:
        return "n/a"
    return " / ".join(f"{k} {v/total:.0%}" for k, v in parts if v)


# --------------------------------------------------------------------------- #
# 발행 알림 (건별)
# --------------------------------------------------------------------------- #
def build_alert(sig: SignalScore, bond: MezzanineBond, lang: str = "en") -> str:
    """단일 발행건 알림 메시지(여러 줄 문자열)."""
    mkt = _MKT.get(sig.corp_cls or "", "")
    code = f"{sig.stock_code} {mkt}".strip() if sig.stock_code else "unlisted"
    method = "Private" if bond.is_private else (bond.issuance_method or "n/a")
    if lang == "ko":
        method = "사모" if bond.is_private else (bond.issuance_method or "미상")

    lines = [f"🔔 NEW {sig.sec_type.value} — {sig.corp_name} ({code})"]

    size = fmt_krw(sig.face_amount)
    extras = []
    if sig.proceeds_to_mktcap is not None:
        extras.append(f"{fmt_pct(sig.proceeds_to_mktcap)} of mktcap")
    if sig.overhang_ratio is not None:
        extras.append(f"dilution {fmt_pct(sig.overhang_ratio)} of shares")
    lines.append(f"Size: {size}" + (f" = {' · '.join(extras)}" if extras else ""))

    if sig.sec_type.value != "RCPS":
        lines.append(
            f"Terms: {bond.coupon_rate if bond.coupon_rate is not None else 'n/a'}% cpn"
            f" / {bond.ytm if bond.ytm is not None else 'n/a'}% YTM"
            f" · {method} · Mty {bond.maturity_date or 'n/a'}")
    else:
        n_other = int(bond.new_other_shares or 0)
        conf = "confirmed" if bond.rcps_confirmed else "candidate (body unconfirmed)"
        lines.append(f"Pref shares: {n_other:,} · {bond.ic_method or method} · RCPS {conf}")

    if sig.conversion_price is not None:
        mny = (f" ({fmt_px(sig.current_price)}, {fmt_pct(sig.disparity, signed=True)} "
               f"{'ITM' if (sig.disparity or 0) >= 0 else 'OTM'})"
               if sig.disparity is not None else "")
        label = {"CB": "CV px", "BW": "Strike", "EB": "Exch px", "RCPS": "Conv px"}[sig.sec_type.value]
        lines.append(f"{label}: {fmt_px(sig.conversion_price)}{mny}")

    if bond.refix_floor_price is not None or bond.refix_below_70_limit is not None:
        floor = fmt_px(bond.refix_floor_price)
        lim = ("exhausted" if bond.refix_below_70_limit == 0
               else fmt_krw(bond.refix_below_70_limit)
               if bond.refix_below_70_limit else "n/a")
        per = bond.refix_period_months or SETTINGS.thresholds.refix_default_period_m
        est = "" if bond.refixing_schedule else " est."
        lines.append(f"Refix: floor {floor} · <70% limit {lim} · every {per}M{est}")

    if bond.sec_type.value == "EB" and bond.eb_target:
        treasury = " ⚠️ TREASURY SHARES" if bond.eb_target_is_treasury else ""
        lines.append(f"Exchange into: {bond.eb_target}{treasury}")

    if bond.conv_start:
        unlock = f" (D-{sig.days_to_unlock})" if sig.days_to_unlock is not None else ""
        lock = " — private lock-up release" if bond.is_private else ""
        lines.append(f"Conv. window: {bond.conv_start} → {bond.conv_end or 'mty'}{unlock}{lock}")

    lines.append(f"Use of proceeds: {_funding_mix(bond)}")

    opt_bits = []
    if bond.call_option:
        opt_bits.append("Call option (owner): YES")
    if bond.has_put_option:
        first_put = bond.put_option_dates[0] if bond.put_option_dates else None
        opt_bits.append(f"Put: YES{f' from {first_put}' if first_put else ''}")
    if opt_bits:
        lines.append(" · ".join(opt_bits))
    if bond.subscribers:
        lines.append("Subscribers: " + ", ".join(bond.subscribers[:5]))

    if sig.serial_count is not None and sig.serial_count >= 2:
        lines.append(f"History: {sig.serial_count} mezz issues in 36M")
    if sig.tags:
        lines.append("Tags: " + " · ".join(sig.tags))
    if sig.flags:
        lines.append("⚠️ " + " | ".join(sig.flags))
    lines.append(viewer_url(sig.rcept_no))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 데일리 디지스트
# --------------------------------------------------------------------------- #
def build_digest(
    items: list[tuple[SignalScore, MezzanineBond]],
    exercises: list[ExerciseFiling],
    asof: dt.date | None = None,
    lang: str = "en",
) -> str:
    """하루치 통합 메시지: 신규 발행(점수순) + 청구권 행사 모니터."""
    asof = asof or dt.date.today()
    head = f"🇰🇷 Mezzanine Issuance Radar — {asof.isoformat()}"
    if not items and not exercises:
        return head + "\nNo new CB/BW/EB/RCPS filings."
    parts = [head + f"\nNew filings: {len(items)}"]
    for sig, bond in sorted(items, key=lambda t: t[0].score, reverse=True):
        parts.append(build_alert(sig, bond, lang=lang))
    if exercises:
        ex_lines = [f"📥 Exercise filings (supply prints): {len(exercises)}"]
        for e in exercises[:15]:
            code = f" ({e.stock_code})" if e.stock_code else ""
            ex_lines.append(f"· {e.corp_name}{code} — {e.report_nm}  {viewer_url(e.rcept_no)}")
        parts.append("\n".join(ex_lines))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Telegram 발송
# --------------------------------------------------------------------------- #
async def send_telegram(text: str) -> bool:
    """TELEGRAM_BOT_TOKEN/CHAT_ID 설정 시 발송. 4096자 분할 처리.

    Returns
    -------
    bool — 발송 성공(또는 채널 미설정으로 스킵 시 False).
    """
    token = SETTINGS.alerts.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = SETTINGS.alerts.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        logger.info("telegram unset — console only")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for ch in chunks:
                r = await client.post(url, json={
                    "chat_id": chat, "text": ch,
                    "disable_web_page_preview": True})
                r.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("telegram send fail: %s", exc)
        return False
