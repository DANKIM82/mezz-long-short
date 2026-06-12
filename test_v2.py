"""v2 단위 테스트 — DART_API_KEY 불필요(네트워크 미사용)."""
import datetime as dt
import json
import os
import sys

os.environ.setdefault("DART_API_KEY", "TESTKEY")

from analytics import score_bond
from alerts import build_alert, build_digest, fmt_krw
from body_parser import (confirm_rcps, detect_call_option, detect_put,
                         detect_refix_period_months, enrich_bond_from_text,
                         extract_subscribers)
from events import add_months, build_events, upcoming_calendar
from models import ComplianceFlag, ExerciseFiling, MarketSnapshot, SecurityType
from parsers import parse_bond
from pipeline import (append_bonds_store, build_universe, load_bonds_store,
                      load_seen, save_seen)

PASS = 0
def ok(cond, msg):
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ✓ {msg}")

today = dt.date(2026, 6, 11)

# ---------------------------------------------------------------- CB (검증키)
cb_row = {
    "_sec_kind": "cb", "rcept_no": "20260610000001", "corp_code": "00111111",
    "corp_name": "테스트전자", "corp_cls": "K", "bd_tm": "5", "bd_fta": "30,000,000,000",
    "bd_intr_ex": "0.0", "bd_intr_sf": "3.0", "bd_mtd": "2029년 06월 30일",
    "bdis_mthn": "사모", "rs_sm_atn": "아니오",
    "cv_prc": "10,000", "cv_rt": "100", "cvisstk_cnt": "3,000,000",
    "cvisstk_tisstk_vs": "12.50", "cvrqpd_bgd": "2027-06-30", "cvrqpd_edd": "2029-05-30",
    "act_mktprcfl_cvprc_lwtrsprc": "7,000", "act_mktprcfl_cvprc_lwtrsprc_bs": "발행가의 70%",
    "rmislmt_lt70p": "-", "fdpp_op": "24,000,000,000", "fdpp_dtrp": "6,000,000,000",
    "bddd": "2026-06-10", "pymd": "2026-06-30",
}
cb = parse_bond(cb_row, stock_code="123456", disclosed_date=dt.date(2026, 6, 10))
ok(cb and cb.sec_type == SecurityType.CB and cb.conversion_price == 10000, "CB 파싱 (cv_prc)")
ok(cb.is_private and cb.refix_floor_price == 7000, "CB 사모·리픽싱 플로어")
ok(cb.potential_shares_ratio == 12.5 and cb.conv_start == dt.date(2027, 6, 30), "CB 희석률·청구개시")

# ---------------------------------------------------------------- BW (검증키)
bw_row = {
    "_sec_kind": "bw", "rcept_no": "20260610000002", "corp_code": "00222222",
    "corp_name": "테스트바이오", "corp_cls": "K", "bd_fta": "10,000,000,000",
    "bd_intr_ex": "1.0", "bd_intr_sf": "4.0", "bd_mtd": "2029-06-30", "bdis_mthn": "사모",
    "ex_prc": "5,000", "ex_rt": "100", "ex_prc_dmth": "청약일 전 가중산술평균",
    "bdwt_div_atn": "분리형", "nstk_isstk_cnt": "2,000,000", "nstk_isstk_tisstk_vs": "8.00",
    "expd_bgd": "2027-06-30", "expd_edd": "2029-05-30",
    "act_mktprcfl_cvprc_lwtrsprc": "3,500", "rmislmt_lt70p": "0",
    "fdpp_fclt": "10,000,000,000", "pymd": "2026-06-30",
}
bw = parse_bond(bw_row, stock_code="234567")
ok(bw and bw.conversion_price == 5000 and bw.potential_shares == 2_000_000,
   "BW 파싱 (ex_prc / nstk_isstk_cnt — 수정된 검증키)")
ok(bw.bw_detachable is True and bw.refix_floor_price == 3500, "BW 분리형·리픽싱(CB동일키)")
ok(bw.refix_below_70_limit == 0, "BW 70%한도 소진(=0) 인식")

# ---------------------------------------------------------------- EB (검증키)
eb_row = {
    "_sec_kind": "eb", "rcept_no": "20260610000003", "corp_code": "00333333",
    "corp_name": "테스트홀딩스", "corp_cls": "Y", "bd_fta": "100,000,000,000",
    "bd_intr_ex": "0.0", "bd_intr_sf": "1.5", "bd_mtd": "2031-06-30", "bdis_mthn": "사모",
    "ex_prc": "50,000", "ex_rt": "100", "extg": "자기주식(보통주)",
    "extg_stkcnt": "2,000,000", "extg_tisstk_vs": "4.20",
    "exrqpd_bgd": "2026-07-30", "exrqpd_edd": "2031-05-30",
    "fdpp_ocsa": "100,000,000,000", "pymd": "2026-06-30",
}
eb = parse_bond(eb_row, stock_code="345678")
ok(eb and eb.sec_type == SecurityType.EB and eb.eb_target_is_treasury is True,
   "EB 파싱 + 자기주식 교환 판별 (extg)")
ok(eb.refix_floor_price is None, "EB 리픽싱 정형필드 부재 처리")

# ---------------------------------------------------------------- RCPS (검증키)
rcps_row = {
    "_sec_kind": "rcps", "rcept_no": "20260610000004", "corp_code": "00444444",
    "corp_name": "테스트제약", "corp_cls": "K",
    "nstk_ostk_cnt": "0", "nstk_estk_cnt": "1,500,000",
    "bfic_tisstk_ostk": "20,000,000", "bfic_tisstk_estk": "0",
    "ic_mthn": "제3자배정증자", "fdpp_op": "15,000,000,000",
}
rcps = parse_bond(rcps_row, stock_code="456789", disclosed_date=dt.date(2026, 6, 10))
ok(rcps and rcps.sec_type == SecurityType.RCPS and rcps.is_private,
   "RCPS 후보 파싱 (nstk_estk_cnt>0, 제3자배정)")
ok(abs(rcps.potential_shares_ratio - 7.5) < 1e-9, "RCPS 희석률 산출 (1.5M/20M)")
common_only = parse_bond({**rcps_row, "rcept_no": "x", "nstk_estk_cnt": "0",
                          "nstk_ostk_cnt": "1,000,000"})
ok(common_only is None, "보통주만 증자 → RCPS 게이트 차단")

# ---------------------------------------------------------------- 본문 파서
body = """
사채의 조기상환청구권에 관한 사항: 사채권자는 발행일로부터 2년이 되는 날부터
매 3개월마다 조기상환을 청구할 수 있다. 전환가액은 매 3개월마다 조정한다.
발행회사 또는 발행회사가 지정하는 자(최대주주 포함)는 매도청구권(콜옵션)을 가진다.
특정인에 대한 대상자별 사채발행내역: 에이비씨투자조합, 가나다자산운용 주식회사
"""
ok(detect_refix_period_months(body) == 3, "본문: 리픽싱 주기(매 3개월)")
has_put, put_dates = detect_put(body, dt.date(2026, 6, 30))
ok(has_put and put_dates == [dt.date(2028, 6, 28)], "본문: 풋옵션 + 개시일(발행 2년)")
call, detail = detect_call_option(body)
ok(call is True and detail, "본문: 콜옵션 + 컨텍스트")
subs = extract_subscribers(body)
ok("에이비씨투자조합" in subs and any("가나다자산운용" in s for s in subs), "본문: 대상자 추출")
rcps_body = "우선주의 상환에 관한 사항 ... 전환에 관한 사항 ..."
ok(confirm_rcps(rcps_body) is True, "본문: RCPS 확정(우선주+상환+전환)")
enrich_bond_from_text(cb, body)
ok(cb.refix_period_months == 3 and cb.call_option is True and cb.has_put_option,
   "본문 보강 → bond 반영")

# ---------------------------------------------------------------- 캘린더
evs = build_events(cb)
types = [e.event_type for e in evs]
ok("PAY" in types and "CONV_START" in types and "MATURITY" in types and "PUT" in types,
   "캘린더: PAY/CONV_START/MATURITY/PUT 생성")
refix_est = [e for e in evs if e.event_type == "REFIX_EST"]
ok(len(refix_est) >= 8 and all(e.estimated for e in refix_est),
   f"캘린더: 리픽싱 추정 {len(refix_est)}건(매3개월, estimated=True)")
ok(add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28), "월가산 말일 클램프")
rcps_evs = build_events(rcps)
ok(any(e.event_type == "UNLOCK_EST" for e in rcps_evs), "캘린더: RCPS 1년 의무보유 해제 추정")
cal_df = upcoming_calendar([cb, eb, rcps], horizon_days=400, asof=today)
ok(not cal_df.empty and "event_date" in cal_df.columns, f"캘린더 DF {len(cal_df)}행")

# ---------------------------------------------------------------- 태깅
snap = MarketSnapshot(stock_code="123456", last_close=9795.0,
                      market_cap=2.42e11, shares_outstanding=24_000_000,
                      short_balance_ratio=1.2)
comp = ComplianceFlag(corp_code="00111111", flagged=True, reasons=["불성실공시법인"])
sig = score_bond(cb, snap, comp, serial_count=3, asof=today)
ok("DILUTION_SHOCK" in sig.tags and "SERIAL_ISSUER" in sig.tags
   and "COMPLIANCE_AVOID" in sig.tags and "DISTRESS_FUNDING" in sig.tags
   and "OWNER_CALL_OPTION" in sig.tags, f"CB 태그: {sig.tags}")
ok(sig.side.value == "SHORT" and abs(sig.disparity - (-0.0205)) < 1e-3,
   f"CB SHORT (score={sig.score}) · 괴리율 {sig.disparity:+.2%}")

eb_snap = MarketSnapshot(stock_code="345678", last_close=52000.0, market_cap=2.4e12)
eb_sig = score_bond(eb, eb_snap, None, serial_count=1, asof=today)
ok("TREASURY_EB" in eb_sig.tags, f"EB 태그: {eb_sig.tags}")
ok("ITM_CONVERT_FLOW" not in eb_sig.tags, "EB ITM 4% < 25% → 전환플로우 미태깅")

rcps_sig = score_bond(rcps, None, None, serial_count=0, asof=today)
ok(any("RCPS 후보" in f for f in rcps_sig.flags), "RCPS 미확정 플래그")

# ---------------------------------------------------------------- 알림 포맷
ok(fmt_krw(3e10) == "KRW 30.0bn (300억)", "금액 포맷")
alert = build_alert(sig, cb, lang="en")
print("\n" + "-" * 60 + "\n" + alert + "\n" + "-" * 60)
for token in ("NEW CB", "테스트전자", "12.4%", "₩10,000", "floor ₩7,000",
              "운영 80%", "Call option", "Put: YES from 2028-06-28",
              "에이비씨투자조합", "3 mezz issues", "DILUTION_SHOCK",
              "rcpNo=20260610000001"):
    ok(token in alert, f"알림 포함: {token}")
ex = ExerciseFiling(rcept_no="20260611000009", corp_code="00555555",
                    corp_name="테스트머티리얼", stock_code="567890",
                    report_nm="전환청구권행사(신주인수권행사 등)")
digest = build_digest([(sig, cb), (eb_sig, eb)], [ex], asof=today)
ok("Mezzanine Issuance Radar — 2026-06-11" in digest
   and "New filings: 2" in digest and "Exercise filings" in digest, "디지스트 구성")

# ---------------------------------------------------------------- 유니버스/상태
listing = [
    {"corp_code": "00111111", "stock_code": "123456", "corp_name": "테스트전자",
     "report_nm": "주요사항보고서(전환사채권발행결정)", "rcept_no": "20260610000001",
     "rcept_dt": "20260610"},
    {"corp_code": "00333333", "stock_code": "345678", "corp_name": "테스트홀딩스",
     "report_nm": "주요사항보고서(교환사채권 발행결정)", "rcept_no": "20260610000003",
     "rcept_dt": "20260610"},
    {"corp_code": "00444444", "stock_code": "456789", "corp_name": "테스트제약",
     "report_nm": "주요사항보고서(유상증자결정)", "rcept_no": "20260610000004",
     "rcept_dt": "20260610"},
    {"corp_code": "00666666", "stock_code": "678901", "corp_name": "무관회사",
     "report_nm": "주요사항보고서(유무상증자결정)", "rcept_no": "20260610000099",
     "rcept_dt": "20260610"},
]
corps, code_map, rd = build_universe(listing)
ok(corps["cb"] == {"00111111"} and corps["eb"] == {"00333333"}
   and corps["rcps"] == {"00444444"}, "유니버스 kind 분류")
ok("00666666" not in corps["rcps"], "유무상증자는 유상증자 키워드 미매칭(알려진 갭)")
corps2, _, rd2 = build_universe(listing, skip_rcept={"20260610000001"})
ok(not corps2["cb"] and "20260610000001" not in rd2, "seen 스킵")

save_seen("state/_t_seen.json", {"a", "b"})
ok(load_seen("state/_t_seen.json") == {"a", "b"}, "seen 저장/복원")
append_bonds_store("state/_t_bonds.jsonl", [cb, eb, rcps])
append_bonds_store("state/_t_bonds.jsonl", [cb])  # 중복
loaded = load_bonds_store("state/_t_bonds.jsonl")
ok(len(loaded) == 3 and loaded[0].refix_period_months == 3,
   "bonds.jsonl 누적 + dedupe + 본문보강값 직렬화 보존")
os.remove("state/_t_seen.json"); os.remove("state/_t_bonds.jsonl")

# ---------------------------------------------------------------- 원문 정밀독해
from deep_read import (deep_read, detect_control_change, detect_anchor_subscriber,
                       detect_refinance, detect_cumulative_overhang,
                       enrich_deep_read)

_FIX = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "drtech_cb9.txt")
drtext = open(_FIX, encoding="utf-8").read() if os.path.exists(_FIX) else ""
if drtext:
    ent, ctx = detect_control_change(drtext)
    ok(ent == "오스템임플란트㈜", f"deep: 지배권 단서 추출 ({ent})")
    a_name, a_amt, a_pct, a_vu, vu = detect_anchor_subscriber(drtext, 27_000_000_000)
    ok(a_name == "케이메디컬밸류업 유한회사" and a_amt == 15_000_000_000,
       "deep: 앵커 인수자 = 최대 배정처(미상환표 오염 없음)")
    ok(abs(a_pct - 15/27) < 1e-6 and a_vu, f"deep: 앵커 비중 {a_pct:.0%}·밸류업 판정")
    series, cancel = detect_refinance(drtext)
    ok(series == ["6회차"] and cancel,
       "deep: 차환 대상=6회차만(7·8회차 미상환표 오염 없음)·소각")
    ok(detect_cumulative_overhang(drtext) == 54.87, "deep: 누적 오버행 54.87%")

    dr = deep_read(drtext, face_amount=27_000_000_000)
    ok(len(dr.clues) == 4, f"deep: clues 4종 생성 ({len(dr.clues)})")
    ok(any("오스템임플란트" in c for c in dr.clues)
       and any("56% of deal" in c for c in dr.clues)
       and any("6회차" in c for c in dr.clues)
       and any("54.9%" in c for c in dr.clues), "deep: clues 내용 정확")

    # bond 통합 + 태깅 + 알림
    drb = parse_bond({
        "_sec_kind": "cb", "rcept_no": "20260611000651", "corp_code": "00214680",
        "corp_name": "디알텍", "corp_cls": "K", "bd_fta": "27,000,000,000",
        "bd_intr_ex": "0.0", "bd_intr_sf": "1.0", "bd_mtd": "2031-06-23",
        "bdis_mthn": "사모", "cv_prc": "1,360", "cvisstk_cnt": "19,852,941",
        "cvisstk_tisstk_vs": "19.21", "cvrqpd_bgd": "2027-06-23",
        "cvrqpd_edd": "2031-05-23", "act_mktprcfl_cvprc_lwtrsprc": "952",
        "fdpp_fclt": "7,000,000,000", "fdpp_op": "8,000,000,000",
        "fdpp_dtrp": "12,000,000,000", "pymd": "2026-06-23", "bddd": "2026-06-11",
    }, stock_code="214680", disclosed_date=dt.date(2026, 6, 11))
    enrich_deep_read(drb, drtext)
    ok(drb.deep_read is not None and drb.deep_read.cumulative_overhang_pct == 54.87,
       "deep: bond.deep_read 부착·직렬화 대상")
    drsnap = MarketSnapshot(stock_code="214680", last_close=1273.0,
                            market_cap=106e9, shares_outstanding=83_477_056)
    drsig = score_bond(drb, drsnap, None, serial_count=4, asof=today)
    for t in ("CONTROL_CHANGE_SIGNAL", "ANCHOR_VALUE_UP", "MEZZ_OVERHANG_HEAVY",
              "REFI_RESTRIKE"):
        ok(t in drsig.tags, f"deep: 태그 {t}")
    ok(drsig.cumulative_overhang_pct == 54.87, "deep: SignalScore 누적오버행 전파")
    dralert = build_alert(drsig, drb, lang="en")
    ok("⚡ Event clues:" in dralert and "오스템임플란트㈜" in dralert,
       "deep: 알림 Event clues 섹션 렌더")
    # 직렬화 라운드트립 (deep_read 보존)
    append_bonds_store("state/_t_dr.jsonl", [drb])
    rb = load_bonds_store("state/_t_dr.jsonl")[0]
    ok(rb.deep_read and rb.deep_read.control_change == "오스템임플란트㈜",
       "deep: bonds.jsonl 라운드트립 시 deep_read 보존")
    os.remove("state/_t_dr.jsonl")

    # 음성 케이스: 일반 발행문(단서 없음)
    plain = "본 사채는 운영자금 조달 목적의 공모 전환사채이며 특이사항 없음."
    ndr = deep_read(plain)
    ok(not ndr.control_change and not ndr.clues, "deep: 단서 없는 본문 → 빈 결과")
else:
    print("  (skip) drtech fixture 없음")

print(f"\n전체 {PASS}개 검증 통과 ✓")
