#!/usr/bin/env python3
"""한국 증시 마감 브리핑 — KRX 정규장 마감(15:30 KST) 이후 17:30 KST 발송.

미국 브리핑(main.py)과는 발송 시점도 구성도 독립이다. 미국편이 '밤사이 무슨 일이
있었나'를 서사로 푸는 글이라면, 이 브리핑은 '오늘 한국장에서 무엇이 움직였나'를
종목 단위로 훑는 글이다. 그래서 섹터 3블록·매크로 서사 대신
  시총 상위 10(코스피/코스닥) → 강한 섹터 2 → 약한 섹터 1
로 메뉴를 고정하고, 모든 종목에 '왜 움직였는가' 1줄을 붙이는 데 지면을 쓴다.

수집·추세계산·Claude 호출·텔레그램 발송은 main.py의 검증된 부품을 그대로 쓴다.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import yfinance as yf

from main import (
    KST,
    HISTORY_DAYS,
    download_history,
    trend_metrics,
    strip_none,
    run_claude_brief,
    send_telegram,
)

# ── 수집 대상 ────────────────────────────────────────────────────────────────
KR_INDICES = {
    "^KS11": "코스피",
    "^KQ11": "코스닥",
    "KRW=X": "원/달러 환율",
}
BENCHMARK_KR = "^KS11"        # 상대강도(RS)의 기준 지수

# 시총 상위 10을 '뽑기 위한' 후보 풀. 시총은 매일 바뀌므로 순위는 실행 시점에 계산한다.
# 우선주는 보통주와 중복되어 상위 10을 잠식하므로 후보에 넣지 않는다.
KOSPI_MCAP_CANDIDATES = [
    "005930", "000660", "373220", "207940", "005380", "000270", "068270", "005490",
    "035420", "028260", "105560", "055550", "012330", "051910", "035720", "006400",
    "012450", "329180", "034020", "032830", "086790", "259960", "010130", "015760",
    "402340", "138040", "066570", "003670", "042660", "000810", "316140", "096770",
]
KOSDAQ_MCAP_CANDIDATES = [
    "196170", "247540", "086520", "028300", "141080", "000250", "214150", "277810",
    "263750", "068760", "214450", "257720", "058470", "005290", "348370", "145020",
    "240810", "403870", "095340", "041510",
]

# 섹터(테마) 그룹 — yfinance의 sector 메타데이터는 삼성전자를 'Consumer Electronics'로
# 분류하는 등 한국 시장의 테마 서사와 맞지 않아 쓰지 않는다. 미국편과 같은 방식으로
# 직접 묶는다. 각 그룹은 '가장 강한/약한 종목 탑3'을 뽑아야 하므로 최소 5종목 이상 둔다.
KR_SECTOR_GROUPS = {
    "반도체": ["005930", "000660", "042700", "058470", "005290", "240810", "403870", "095340"],
    "2차전지": ["373220", "006400", "051910", "247540", "086520", "066970", "348370", "003670"],
    "바이오·헬스케어": ["207940", "068270", "196170", "141080", "028300", "214450", "145020", "000250"],
    "자동차·부품": ["005380", "000270", "012330", "204320", "011210"],
    "인터넷·게임·엔터": ["035420", "035720", "259960", "263750", "036570", "352820", "041510"],
    "금융": ["105560", "055550", "086790", "316140", "138040", "032830", "000810"],
    "조선·방산·기계": ["012450", "329180", "042660", "010140", "064350", "272210"],
    "소재·에너지·유틸": ["005490", "010130", "096770", "015760", "011200", "004020"],
}

# 코스닥 종목은 .KQ, 그 외는 .KS. 여기에 없는 코드는 .KS로 간주한다.
# 이전상장에 주의할 것: 엘앤에프(066970)는 코스닥에서 코스피로 옮겨 .KS로만 조회된다.
# 잘못된 접미사는 예외가 아니라 '데이터 없음'으로 조용히 빠지므로, 종목을 추가하면
# DRY_RUN=1로 한 번 돌려 "시세를 받지 못한 종목" 경고가 없는지 확인한다.
KOSDAQ_CODES = {
    "196170", "247540", "086520", "028300", "141080", "000250", "214150", "277810",
    "263750", "068760", "214450", "257720", "058470", "005290", "348370", "145020",
    "240810", "403870", "095340", "041510",
}

KR_NAMES = {
    "005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스", "005380": "현대차", "000270": "기아",
    "068270": "셀트리온", "005490": "POSCO홀딩스", "035420": "NAVER",
    "028260": "삼성물산", "105560": "KB금융", "055550": "신한지주",
    "012330": "현대모비스", "051910": "LG화학", "035720": "카카오",
    "006400": "삼성SDI", "012450": "한화에어로스페이스", "329180": "HD현대중공업",
    "034020": "두산에너빌리티", "032830": "삼성생명", "086790": "하나금융지주",
    "259960": "크래프톤", "010130": "고려아연", "015760": "한국전력",
    "402340": "SK스퀘어", "138040": "메리츠금융지주", "066570": "LG전자",
    "003670": "포스코퓨처엠", "042660": "한화오션", "000810": "삼성화재",
    "316140": "우리금융지주", "096770": "SK이노베이션", "042700": "한미반도체",
    "204320": "HL만도", "011210": "현대위아", "036570": "엔씨소프트",
    "352820": "하이브", "010140": "삼성중공업", "064350": "현대로템",
    "272210": "한화시스템", "011200": "HMM", "004020": "현대제철",
    "196170": "알테오젠", "247540": "에코프로비엠", "086520": "에코프로",
    "028300": "HLB", "141080": "리가켐바이오", "000250": "삼천당제약",
    "214150": "클래시스", "277810": "레인보우로보틱스", "263750": "펄어비스",
    "068760": "셀트리온제약", "214450": "파마리서치", "257720": "실리콘투",
    "058470": "리노공업", "005290": "동진쎄미켐", "348370": "엔켐",
    "145020": "휴젤", "066970": "엘앤에프", "240810": "원익IPS",
    "403870": "HPSP", "095340": "ISC", "041510": "에스엠",
}


def yf_symbol(code):
    return code + (".KQ" if code in KOSDAQ_CODES else ".KS")


def kr_name(code):
    return KR_NAMES.get(code, code)


def fetch_market_caps(codes):
    """{code: marketCap}. 조회 실패한 종목은 키 자체를 넣지 않는다."""
    import concurrent.futures as cf

    def one(code):
        try:
            return code, yf.Ticker(yf_symbol(code)).fast_info.get("marketCap")
        except Exception:
            return code, None

    out = {}
    with cf.ThreadPoolExecutor(12) as ex:
        for code, mc in ex.map(one, codes):
            if mc:
                out[code] = mc
    return out


def collect_kr_market_data():
    now_kst = datetime.now(KST)
    start = (now_kst - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")

    # 1) 지수·환율
    idx = download_history(list(KR_INDICES.keys()), start=start)
    if BENCHMARK_KR not in idx or len(idx[BENCHMARK_KR]["close"]) < 2:
        raise RuntimeError("지수 데이터 수집 실패(^KS11)")
    bench_close = idx[BENCHMARK_KR]["close"]
    bench = trend_metrics(bench_close)
    dates = list(bench_close.index)
    t_date = dates[-1].date()
    t1_date = dates[-2].date() if len(dates) >= 2 else None

    indices = []
    for ticker, name in KR_INDICES.items():
        h = idx.get(ticker)
        if h is None or len(h["close"]) < 2:
            indices.append({"ticker": ticker, "name": name, "error": "N/A"})
            continue
        s = h["close"]
        m = trend_metrics(s)
        row = {
            "ticker": ticker, "name": name, "close": round(float(s.iloc[-1]), 2),
            "chg_pct_d": m.get("chg_pct_d"), "ret_5d": m.get("ret_5d"),
            "ret_20d": m.get("ret_20d"), "vs_ma200": m.get("vs_ma200"),
            "pct_from_52w_high": m.get("pct_from_52w_high"),
        }
        # 환율 일봉은 24시간 거래라 당일 봉이 늦게 확정된다. 지수와 날짜가 다르면 밝힌다.
        if ticker == "KRW=X":
            row["as_of"] = str(s.index[-1].date())
        indices.append(strip_none(row))

    # 2) 종목 시세 — 후보 풀과 섹터 구성종목을 한 번에 받는다.
    universe = sorted(
        set(KOSPI_MCAP_CANDIDATES) | set(KOSDAQ_MCAP_CANDIDATES)
        | {c for cs in KR_SECTOR_GROUPS.values() for c in cs}
    )
    hist = download_history([yf_symbol(c) for c in universe], start=start)
    metrics, missing = {}, []
    for code in universe:
        h = hist.get(yf_symbol(code))
        if not h or len(h["close"]) < 2:
            missing.append(f"{kr_name(code)}({code})")
            continue
        m = trend_metrics(h["close"], h.get("volume"))
        if m.get("chg_pct_d") is None:
            missing.append(f"{kr_name(code)}({code})")
            continue
        metrics[code] = m
    # 조용히 빠지면 섹터 구성이 줄어든 것을 아무도 모른다. 로그로 드러낸다.
    if missing:
        print(f"경고: 시세를 받지 못한 종목 {len(missing)}개 -> {', '.join(missing)}",
              file=sys.stderr)

    def rs20(m):
        """코스피 대비 20일 초과수익(%p)."""
        if m.get("ret_20d") is None or bench.get("ret_20d") is None:
            return None
        return round(m["ret_20d"] - bench["ret_20d"], 2)

    def row_of(code, mcap=None):
        m = metrics[code]
        row = {"code": code, "name": kr_name(code), "ticker": yf_symbol(code)}
        if mcap:
            row["market_cap_krw"] = mcap
        row.update(m)
        row["rs_20d_vs_kospi"] = rs20(m)
        row.pop("pct_above_52w_low", None)
        return strip_none(row)

    # 3) 시총 상위 10 — 코스피/코스닥 각각
    caps = fetch_market_caps([c for c in universe if c in metrics])
    tops = {}
    for label, cands in (("kospi_top10_by_mcap", KOSPI_MCAP_CANDIDATES),
                         ("kosdaq_top10_by_mcap", KOSDAQ_MCAP_CANDIDATES)):
        ranked = sorted(
            [(c, caps[c]) for c in set(cands) if c in caps and c in metrics],
            key=lambda x: x[1], reverse=True,
        )[:10]
        tops[label] = [row_of(c, mc) for c, mc in ranked]

    # 4) 섹터 집계 — 강한 2개 / 약한 1개를 고르기 위한 근거를 모두 싣는다.
    sectors = []
    for sector, codes in KR_SECTOR_GROUPS.items():
        members = [c for c in codes if c in metrics]
        if len(members) < 3:      # 탑3을 못 뽑는 섹터는 순위 경쟁에서 제외
            continue

        def avg(field, _ms=members):
            vals = [metrics[c][field] for c in _ms if metrics[c].get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        by_day = sorted(members, key=lambda c: metrics[c]["chg_pct_d"], reverse=True)
        avg20 = avg("ret_20d")
        sectors.append(strip_none({
            "sector": sector,
            "members": len(members),
            "avg_chg_pct_d": avg("chg_pct_d"),
            "avg_ret_5d": avg("ret_5d"),
            "avg_ret_20d": avg20,
            "avg_rs_20d_vs_kospi": (
                round(avg20 - bench["ret_20d"], 2)
                if avg20 is not None and bench.get("ret_20d") is not None else None
            ),
            "up": sum(1 for c in members if metrics[c]["chg_pct_d"] > 0),
            "down": sum(1 for c in members if metrics[c]["chg_pct_d"] < 0),
            "top3_by_daily_change": [row_of(c) for c in by_day[:3]],
            "bottom3_by_daily_change": [row_of(c) for c in reversed(by_day[-3:])],
            "all_members": [row_of(c) for c in by_day],
        }))
    sectors.sort(key=lambda s: s.get("avg_chg_pct_d") or 0, reverse=True)

    return {
        "market": "KR",
        "session_date": str(t_date),
        "prev_session_date": str(t1_date),
        "generated_at_kst": now_kst.strftime("%Y-%m-%d %H:%M"),
        "benchmark": strip_none({"ticker": BENCHMARK_KR, "name": "코스피(상대강도 기준)", **bench}),
        "indices": indices,
        **tops,
        "sectors_by_daily_change": sectors,
        "strongest_sectors": sectors[:2],
        "weakest_sector": sectors[-1] if sectors else None,
        "field_guide": {
            "chg_pct_d": "당일 종가 기준 1일 등락률(%)",
            "ret_5d / ret_20d": "5·20 거래일 전 종가 대비 수익률(%)",
            "vs_ma50 / vs_ma200": "50일·200일 이동평균 대비 이격도(%). 양수면 추세 위.",
            "pct_from_52w_high": "52주 고점 대비 위치(%). 0에 가까울수록 신고가권.",
            "vol_x_avg20": "당일 거래량 ÷ 직전 20일 평균 거래량. 2 이상이면 이벤트 신호.",
            "rs_20d_vs_kospi": "20일 수익률에서 코스피 20일 수익률을 뺀 값(%p). 상대강도.",
            "market_cap_krw": "시가총액(원). 상위 10 선정 기준.",
            "strongest_sectors / weakest_sector":
                "sectors_by_daily_change를 당일 평균 등락률로 정렬해 뽑아둔 것. 서사는 이 셋에만 쓴다.",
        },
        "note": ("강한 섹터 2개와 약한 섹터 1개는 strongest_sectors / weakest_sector에 이미 뽑혀 있다. "
                 "각 섹터의 top3_by_daily_change / bottom3_by_daily_change가 그 섹터에서 "
                 "가장 강했던·약했던 종목 3개다. all_members는 판단 근거이니 참고만 하고 "
                 "브리핑에 전부 나열하지 말 것."),
    }


# ── 브리핑 생성 ──────────────────────────────────────────────────────────────
KR_SYSTEM_PROMPT = """당신은 한국 은행 자금부의 시니어 마켓 데스크입니다. 독자는 21년차 뱅커이자
추세추종·섹터 상대강도 전략을 쓰는 개인투자자입니다. 제공된 실측 데이터(JSON)로 오늘
한국 증시(코스피·코스닥) 정규장 마감 브리핑을 작성하세요.

이 브리핑의 핵심 가치는 '어느 종목이 몇 % 움직였는가'가 아니라 '왜 움직였는가'입니다.
등락률만 나열하고 이유가 비어 있는 브리핑은 실패입니다.

규칙:
- 반드시 한국어. 텔레그램 발송용이므로 마크다운 표 대신 줄단위 텍스트/이모지 사용.
- HTML 태그는 <b>, <i>만 사용 가능(텔레그램 HTML 모드). 다른 태그 금지.
- 종목은 <b>종목명</b> 형식으로 쓰고 종목코드·주가(종가 금액)는 쓰지 않는다.
  등락률과 추세 수치만 표기한다. 지수와 환율의 수치는 ①에 그대로 표기한다.
- 수치 표기 약속: 1일 등락률은 +2.1%, 20일 수익률은 20d +8.4%,
  200일선 대비는 200일선 +12%, 상대강도는 RS +5.2%p, 거래량은 거래량 2.3배.
- ★1줄 브리핑의 정의: 그 종목이 오늘 그렇게 움직인 '이유'를 한 문장으로 쓴 것이다.
  실적·수주·공시·증권사 리포트·수급·업황·모회사/자회사 이슈 등 구체적 사건을 밝힌다.
  웹검색으로 확인되지 않으면 지어내지 말고, 대신 추세 관점에서 한 문장을 쓴다
  (예: "특별한 재료 없이 20일 추세를 그대로 이어간 흐름"). 어느 경우에도 빈칸으로 두지 않는다.
- ★검색 결과의 영어 원문을 그대로 옮기지 말 것. 반드시 한국어로 소화해 서술한다.

구성(이 순서를 그대로 지킬 것):

  ① 오늘의 시장 요약 (3~4문장)
     코스피·코스닥 지수 등락률과 원/달러 환율을 수치로 밝히고, 두 시장이 같은 방향이었는지
     엇갈렸는지, 그리고 오늘의 움직임이 20일 추세의 연장인지 되돌림인지를 200일선 대비
     위치를 근거로 한 문장으로 못박는다.

  ② 코스피 시가총액 상위 10 종목
     시총 순위대로 10개를 한 줄씩. 형식:
       n. <b>종목명</b> +1.2% (20d +5.4%) — 1줄 브리핑
     10개 모두 빠짐없이 쓴다.

  ③ 코스닥 시가총액 상위 10 종목
     ②와 완전히 같은 형식으로 10개.

  ④ 오늘 가장 강했던 섹터 2개
     strongest_sectors의 2개를 순서대로. 각 섹터마다:
     ★시장이 전반적으로 하락한 날에는 1위 섹터의 avg_chg_pct_d도 음수일 수 있다.
       그때 '강세 섹터'라고 쓰면 사실과 어긋난다. 평균이 음수면 '가장 덜 빠진 섹터'로
       정확히 부르고, 방어적 흐름이었다는 점을 (c)에서 밝힐 것.
       (a) <b>섹터명</b> 헤더 한 줄 — 평균 등락률, 상승/하락 종목 수, 20d 평균, RS ±%p
       (b) 그 섹터에서 가장 강했던 종목 3개(top3_by_daily_change)를 한 줄씩,
           ②와 같은 '종목명 등락률 (20d) — 1줄 브리핑' 형식으로
       (c) 이 섹터가 오늘 왜 강했는지 2~3문장. 마지막 문장은 반드시 추세 판단
           (20일 추세의 연장인가, 낙폭과대 반등인가)으로 끝낸다.

  ⑤ 오늘 가장 약했던 섹터 1개
     weakest_sector를 ④와 똑같은 (a)(b)(c) 구조로. 단 (b)는 그 섹터에서 가장 약했던
     종목 3개(bottom3_by_daily_change)를 쓴다. (c)에서는 왜 같은 날 같은 방향으로
     팔렸는지를 자금 이동·업황·수급으로 설명하고, 이 하락이 '추세 안의 조정'인지
     '추세 이탈'인지를 200일선 대비 위치와 20d 수익률로 구분해 명시한다.

  ⑥ 한 줄 총평
     오늘 자금이 어느 섹터에서 어느 섹터로 옮겨갔는지 한 문장.

- 제공된 수치를 임의로 바꾸지 말 것. 없는 수치는 만들지 말 것.
- 마지막에 '투자 권유가 아닌 정보 제공 목적' 1줄.
- 전체 길이는 텔레그램 2~4개 메시지 이내(약 6,000자 이내).
- ★①부터 ⑥까지와 면책 문구를 반드시 완결할 것. 분량이 부족하면 ④⑤의 (c)를 줄이되,
  ②③의 종목을 빠뜨리거나 문장 중간에서 멈추지 말 것."""


def build_kr_brief_with_claude(data):
    return run_claude_brief(
        KR_SYSTEM_PROMPT,
        f"기준 세션: {data['session_date']} (직전 세션 {data['prev_session_date']}).\n"
        "아래 실측 데이터로 한국 증시 마감 브리핑을 작성해 주세요.\n\n"
        + json.dumps(data, ensure_ascii=False),
        max_uses=int(os.environ.get("KR_WEB_SEARCH_MAX_USES", "10")),
        default_max_tokens="24000",
    )


def _line(r):
    d20 = f" (20d {r['ret_20d']:+.2f}%)" if r.get("ret_20d") is not None else ""
    return f"<b>{r['name']}</b> {r['chg_pct_d']:+.2f}%{d20}"


def build_kr_fallback_brief(data):
    """Claude 호출이 실패했을 때의 수치 위주 브리핑(1줄 브리핑 없음)."""
    L = [f"🇰🇷 <b>한국 증시 마감 브리핑 — {data['session_date']}</b>", ""]
    L.append("<b>① 지수·환율</b>")
    for r in data["indices"]:
        if "error" in r:
            L.append(f"· {r['name']}: N/A")
            continue
        d20 = f"{r['ret_20d']:+.2f}%" if r.get("ret_20d") is not None else "N/A"
        ma = f"  <i>200일선 {r['vs_ma200']:+.1f}%</i>" if r.get("vs_ma200") is not None else ""
        asof = f"  <i>({r['as_of']} 기준)</i>" if r.get("as_of") else ""
        L.append(f"· {r['name']}: {r['close']:,.2f}  "
                 f"{r.get('chg_pct_d', 0):+.2f}% / 20d {d20}{ma}{asof}")
    L.append("")

    for key, title in (("kospi_top10_by_mcap", "② 코스피 시총 상위 10"),
                       ("kosdaq_top10_by_mcap", "③ 코스닥 시총 상위 10")):
        rows = data.get(key) or []
        if not rows:
            continue
        L.append(f"<b>{title}</b>")
        for i, r in enumerate(rows, 1):
            L.append(f"{i}. {_line(r)}")
        L.append("")

    for i, s in enumerate(data.get("strongest_sectors") or [], 1):
        L.append(f"<b>④-{i} 강세 섹터: {s['sector']}</b> "
                 f"평균 {s.get('avg_chg_pct_d', 0):+.2f}% (상승 {s['up']}/하락 {s['down']})")
        for r in s["top3_by_daily_change"]:
            L.append(f"   · {_line(r)}")
        L.append("")

    w = data.get("weakest_sector")
    if w:
        L.append(f"<b>⑤ 약세 섹터: {w['sector']}</b> "
                 f"평균 {w.get('avg_chg_pct_d', 0):+.2f}% (상승 {w['up']}/하락 {w['down']})")
        for r in w["bottom3_by_daily_change"]:
            L.append(f"   · {_line(r)}")
        L.append("")

    L.append("<i>본 내용은 투자 권유가 아닌 정보 제공 목적입니다.</i>")
    return "\n".join(L)


def main():
    now_kst = datetime.now(KST)
    print(f"run at KST={now_kst:%Y-%m-%d %H:%M}")

    data = collect_kr_market_data()
    session = datetime.strptime(data["session_date"], "%Y-%m-%d").date()

    # 17:30 KST 실행이므로 정상 거래일이면 최근 세션은 'KST 기준 오늘'이어야 한다.
    # 아니라면 오늘 KRX는 휴장이었다는 뜻. FORCE_SEND=1이면 직전 세션으로 강제 발송한다.
    if session != now_kst.date():
        if os.environ.get("FORCE_SEND") == "1":
            print(f"FORCE_SEND=1 → 휴장 판정을 무시하고 직전 세션({session}) 기준으로 발송")
        elif os.environ.get("SKIP_ON_HOLIDAY", "1") == "1":
            print(f"휴장 감지(최근 세션 {session}) → 발송 생략")
            return
        else:
            send_telegram(f"🇰🇷 오늘({now_kst:%m/%d})은 한국 증시 휴장일이었습니다. "
                          f"직전 거래일은 {session}입니다.")
            return

    if os.environ.get("DRY_RUN") == "1":
        print(build_kr_fallback_brief(data))
        return

    brief = None
    try:
        brief = build_kr_brief_with_claude(data)
    except Exception as e:
        print(f"Claude API 실패 → 기본 브리핑으로 대체: {e}", file=sys.stderr)
    if not brief:
        brief = build_kr_fallback_brief(data)

    send_telegram(brief)
    print("텔레그램 발송 완료")


if __name__ == "__main__":
    main()
