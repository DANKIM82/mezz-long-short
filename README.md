# Mezzanine Issuance Radar — KR CB · BW · EB · RCPS

한국 상장사의 메자닌 발행(전환사채·신주인수권부사채·교환사채·상환전환우선주)을
**공시 당일 저녁 알림 → 트레이드 태그 분류 → 이벤트 캘린더**로 연결하는
롱숏 PM용 파이프라인. OpenDART 정형 API(필드 명세 검증 완료) + pykrx 기반.

> 산출물은 리서치 신호이며 매매 권고가 아닙니다.

---

## 1. 운영 모드

```bash
export DART_API_KEY=...           # opendart.fss.or.kr 발급
export TELEGRAM_BOT_TOKEN=...     # 선택 (미설정 시 콘솔 출력)
export TELEGRAM_CHAT_ID=...
export RADAR_LANG=en              # en(기본) / ko

python pipeline.py --mode daily      # 신규 발행 알림 (cron/Actions용)
python pipeline.py --mode scan       # 룩백 365일 풀 스크리닝 → CSV
python pipeline.py --mode calendar   # 누적 발행건 → 향후 60일 이벤트 CSV
```

- **daily**: 최근 3일 윈도우(주말 커버)에서 신규 공시만 처리.
  `state/seen_rcept.json`으로 중복 차단, 발행건은 `state/bonds.jsonl`에 누적.
  본문(zip) 보강 포함 — `--no-body`로 생략 가능.
- **scan**: 주간 리뷰용. 태그 보유 / 큰 괴리율 / 리픽싱·언락 임박 건만 통과.
- **calendar**: PM은 날짜를 거래한다 — 납입·청구개시(락업해제)·리픽싱·풋·만기.

`.github/workflows/daily_radar.yml`: 평일 18:30 KST 자동 실행 + Telegram 발송.
Secrets에 `DART_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록.

---

## 2. 알림 1건의 모양 (영문 기본)

```
🔔 NEW CB — 테스트전자 (123456 KOSDAQ)
Size: KRW 30.0bn (300억) = 12.4% of mktcap · dilution 12.5% of shares
Terms: 0.0% cpn / 3.0% YTM · Private · Mty 2029-06-30
CV px: ₩10,000 (₩9,795, -2.1% OTM)
Refix: floor ₩7,000 · <70% limit n/a · every 3M est.
Conv. window: 2027-06-30 → 2029-05-30 (D-380) — private lock-up release
Use of proceeds: 운영 80% / 차환 20%
Call option (owner): YES · Put: YES from 2028-06-30
Subscribers: OO투자조합, XX자산운용
History: 3 mezz issues in 36M
Tags: DILUTION_SHOCK · SERIAL_ISSUER · DISTRESS_FUNDING
https://dart.fss.or.kr/dsaf001/main.do?rcpNo=2026...
```

사이즈는 절대액이 아니라 **시총 대비 %·주식총수 대비 희석률**로 읽도록 설계.
모든 알림에 DART 뷰어 링크 첨부 — 원문 1클릭.

---

## 3. 트레이드 태그 → 플레이북

| 태그 | 의미 | 전형적 대응 |
|---|---|---|
| `DILUTION_SHOCK` | 잠재주식 ≥5% 또는 조달액/시총 ≥5% | D+1 숏 후보 / 보유 시 리스크 점검 |
| `SERIAL_ISSUER` | 36개월 내 메자닌 ≥3회 | 상습 희석 바스켓 숏 |
| `REFIX_SHORT` | 사모 CB/BW, 하향 여력 + OTM, 리픽싱 임박 | 리픽싱 전 구조적 숏 압력 |
| `REFIX_EXHAUSTED` | 플로어 도달·70%한도 소진 | 추가 희석 제한 → 숏 논리 약화 |
| `UNLOCK_SUPPLY` | 청구 개시(사모 1년 락업 해제) D-45 이내 | 공급 이벤트 전 숏/롱 회피 |
| `ITM_CONVERT_FLOW` | 괴리율 ≥+25% & 청구기간 진행 중 | 전환→매도 플로우, 수급 헤비 |
| `TREASURY_EB` | 자기주식 교환 EB | 자사주 재유출(소각 회피) — 거버넌스 숏/인게이지 후보 |
| `EB_CROSS_HOLDING` | 타법인주식 교환 EB | EB 롱 / 기초주 숏 패리티, 지주 디스카운트 점검 |
| `DISTRESS_FUNDING` | 운영+차환 ≥70% 사모 조달 | 자금난 시그널 |
| `OWNER_CALL_OPTION` | 최대주주 매도청구권 부착 | 지배권 이전·저가 지분 확보 스킴 가능성 |
| `COMPLIANCE_AVOID` | 불성실공시·횡령배임 등 12개월 이력 | 신규 진입 회피 / 숏 바이어스 |
| `SQUEEZE_RISK` | 숏잔고 ≥5% + 깊은 OTM + 리픽싱 소진 | 숏 진입 위험(스퀴즈) |

점수 = Σ 태그 가중치(`config.TagWeights`) → SHORT(≥1.5) / LONG(≤-0.5) / NEUTRAL.
가중치는 백테스트 캘리브레이션 전 초기값.

---

## 4. 데이터 계보 — 무엇이 정형이고 무엇이 본문인가

**검증된 정형 필드** (OpenDART 개발가이드 DS005에서 직접 확인):

| 종목 | 엔드포인트 | 핵심 필드 |
|---|---|---|
| CB | `cvbdIsDecsn` (2020033) | `cv_prc` 전환가, `cvisstk_tisstk_vs` 희석률, `cvrqpd_bgd/edd` 청구기간, `act_mktprcfl_cvprc_lwtrsprc` 리픽싱 플로어, `rmislmt_lt70p` 70%미만 한도, `bdis_mthn` 사모/공모 |
| BW | `bdwtIsDecsn` (2020034) | `ex_prc` 행사가, `nstk_isstk_cnt/_tisstk_vs`, `expd_bgd/edd`, 리픽싱 키는 **CB와 동일 표기**, `bdwt_div_atn` 분리형 |
| EB | `exbdIsDecsn` (2020035) | `ex_prc` 교환가, `extg` **교환대상**(자기주식 판별), `extg_stkcnt/_tisstk_vs`, `exrqpd_bgd/edd` — 리픽싱 정형 필드 없음 |
| RCPS | `piicDecsn` (2020023, 유상증자결정) | `nstk_estk_cnt` 기타주식 수(게이트), `bfic_tisstk_*` 증자전 총주식, `ic_mthn` 증자방식 — 가격·일자·조건 정형 미제공 |

**본문 파싱**(`document.xml` zip → 휴리스틱): 풋옵션 개시, 정기 리픽싱 주기(매 N개월),
최대주주 콜옵션, 대상자(인수자), RCPS 상환+전환 조건 확정.

---

## 5. 정직한 한계 (PM이 알아야 할 것)

1. **리픽싱 일정**: 본문에서 주기를 못 읽으면 사모 관행(매 3개월)으로 *추정*하고
   `REFIX_EST`/`est.` 표기. 추정과 확인을 항상 구분 표시.
2. **풋옵션**: 시장 관행이 다양해 추정 생성 금지 — 본문 확인분만 캘린더에 등재.
3. **RCPS**: 정형 데이터는 "기타주식 증자"까지만 식별. 본문에서 상환+전환 조건이
   확인돼야 `confirmed`, 아니면 `candidate`로 표기. `유무상증자결정(pifricDecsn)`
   경로의 RCPS는 현재 미커버(알려진 갭).
4. **EB 패리티**: 교환대상이 타법인주식이면 기초주 시세·괴리는 수동 점검 필요
   (기초 종목코드 자동 매핑은 차기 과제).
5. **컴플라이언스**: 공시 제목 키워드 매칭 — 처분 결과·경중은 원문 확인 필요.
6. **대상자 추출**: 표 구조가 회사마다 달라 휴리스틱 — 누락 가능, 원문 링크로 보완.
7. **공시 시각**: list.json은 일자 단위 — 장중 실시간이 아닌 당일 저녁 배치 전제.

---

## 6. 요청량 설계 (일 20,000건 한도)

- daily: B/I 목록 수 페이지 + 신규 발행사당 정형 4종 + 본문 1 + 시리얼 3 + 컴플라이언스 1
  ≈ 발행사당 ~9건 → 통상 일 100건 미만.
- scan(365d): 목록 ~수백 페이지 + 발행사(연 300~600사)당 정형/시리얼 — 수천 건 수준, 한도 내.

## 7. 구조

```
config.py        엔드포인트·키워드·임계값·태그가중치·알림설정
models.py        MezzanineBond / SignalScore / CalendarEvent / ExerciseFiling
dart_client.py   비동기 클라이언트 (정형 4종 + list + document.zip)
parsers.py       검증 필드맵 → 모델 (RCPS 기타주식 게이트 포함)
body_parser.py   풋·콜·리픽싱주기·대상자·RCPS 확정 (본문 휴리스틱)
market_data.py   pykrx: 종가·시총·상장주식수·공매도잔고
compliance.py    시장 인덱스(scan) + 발행사 단건 점검(daily)
events.py        이벤트 캘린더 생성 (추정치 명시 원칙)
analytics.py     지표 + 12개 트레이드 태그 + 점수
alerts.py        영문 PM 알림·디지스트·Telegram
pipeline.py      daily / scan / calendar CLI + 상태 관리
```
