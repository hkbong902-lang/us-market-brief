#!/usr/bin/env python3
"""한국 증시 마감 브리핑 — KRX 정규장 마감(15:30 KST) 이후 17:30 KST 발송.

미국 브리핑(main.py)과는 발송 시점도 구성도 독립이다. 구성은 다음으로 고정한다.
  ① 지수·환율
  ② 코스피 당일 상승률 상위 10 (+종목별 1줄)
  ③ 코스닥 당일 상승률 상위 10 (+종목별 1줄)
  ④ 당일 강세 업종 2개 (+업종 내 최강 3종목, 종목별 1줄)
  ⑤ 당일 약세 업종 1개 (+업종 내 최약 3종목, 종목별 1줄)
  ⑥ 총평

종목 유니버스를 손으로 고르지 않는다. '상승률 상위 10'과 '가장 강한 업종'은 시장 전체를
봐야만 답할 수 있는 질문이고, 손으로 고른 리스트로 답하면 '내가 고른 것 중 1위'가 나오면서
겉모습은 정답과 똑같다. 그래서 네이버 금융 모바일 JSON API 에서 시장 전체의 등락률 순위와 업종 시세를 그대로
받아 쓴다. 추세 지표(20일·200일선)만 선정된 종목에 한해 yfinance 로 덧붙인다.

수집 실패는 조용히 넘기지 않는다. 부분 브리핑은 정상 브리핑과 겉모습이 같아서, 장애가
'기능이 좀 빠진 것'으로 읽히고 원인 조사가 늦어진다.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import requests

from main import (
    KST,
    HISTORY_DAYS,
    download_history,
    trend_metrics,
    strip_none,
    run_claude_brief,
    send_telegram,
)

KR_INDICES = {
    "^KS11": "코스피",
    "^KQ11": "코스닥",
    "KRW=X": "원/달러 환율",
}
BENCHMARK_KR = "^KS11"

# 네이버 금융의 HTML 순위·업종 페이지는 2026-09-10 시점에 표가 사라졌다(HTTP 200 이지만
# <table> 0개). pandas.read_html 은 lxml 로 표를 못 찾자 bs4/html5lib 경로로 넘어가
# "Import html5lib failed" 로 죽었는데, 그 메시지는 증상일 뿐 원인은 소스가 바뀐 것이다.
#
# 그래서 모바일 JSON API 로 옮긴다. HTML 파싱보다 나은 것이 표 유무만이 아니다.
#  - itemCode 가 stockName 과 같은 객체에 들어 있다 → 순번으로 코드를 짝지을 일이 없다
#    (HTML 시절 액스비스에 광전자의 코드가 붙던 부류의 오류가 구조적으로 불가능해진다).
#  - stockExchangeType.code 가 KS/KQ 를 알려준다 → 접미사를 추측하지 않는다.
#  - localTradedAt / marketStatus 가 기준 시각과 장 상태를 준다 → 세션 날짜를 다른 소스에서
#    빌려오지 않는다.
NAVER_API = "https://m.stock.naver.com/api"
UA = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://m.stock.naver.com/",
}
TOP_N = int(os.environ.get("KR_TOP_N", "10"))
# 상승률 상위는 상한가 소형주가 대부분을 차지한다. 그 자체가 그날의 사실이므로 기본값은
# 필터 없음(0)이다. 대형주 위주로 보고 싶으면 억 단위 하한을 넣는다(예: 5000 = 5,000억).
MIN_CAP_EOK = int(os.environ.get("KR_MIN_MARKET_CAP_EOK", "0"))
# ETN·ETF 는 지수를 배수로 추종하는 상품이라 '오늘 오른 이유'를 물을 대상이 아니다.
# API 가 stockEndType 으로 종류를 알려주므로 이름 정규식으로 추측하지 않는다.
EXCLUDE_ETP = os.environ.get("KR_INCLUDE_ETP", "0") != "1"
STOCK_END_TYPES = {"stock"}          # etf, etn, elw 등은 제외
PAGE_MAX = 100                       # API 가 허용하는 pageSize 상한(101 이상은 400)


def _api(path, **params):
    """모바일 JSON API 호출. 실패는 조용히 빈 값으로 넘기지 않고 예외로 올린다."""
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{NAVER_API}/{path}" + (f"?{q}" if q else "")
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _num(v):
    """'+23.99' / '55,700' → float. 파싱 불가면 None."""
    if v is None:
        return None
    s = str(v).replace("%", "").replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _stock_row(x, market=None):
    """API 종목 객체 → 브리핑용 행. 코드·거래소가 같은 객체에서 나온다."""
    chg = _num(x.get("fluctuationsRatio"))
    if chg is None or not x.get("itemCode"):
        return None
    ex = (x.get("stockExchangeType") or {}).get("code")
    return strip_none({
        "name": (x.get("stockName") or "").strip(),
        "code": x["itemCode"],
        "market": market or ("KOSDAQ" if ex == "KQ" else "KOSPI"),
        "suffix": ".KQ" if ex == "KQ" else ".KS",
        "chg_pct_d": round(chg, 2),
        "volume": int(_num(x.get("accumulatedTradingVolume")) or 0) or None,
        "market_cap_eok": int(_num(x.get("marketValue")) or 0) or None,
    })


def naver_index(code):
    """지수 현재값·등락률·기준 시각. code 는 KOSPI 또는 KOSDAQ."""
    j = _api(f"index/{code}/basic")
    return {
        "close": _num(j.get("closePrice")),
        "chg_pct_d": _num(j.get("fluctuationsRatio")),
        "market_status": j.get("marketStatus"),
        "traded_at": j.get("localTradedAt"),
    }


def naver_gainers(market, want=TOP_N):
    """당일 상승률 상위. 시장 전체가 모집단이며 API 가 등락률 내림차순으로 준다.

    ETP 와 시총 하한으로 걸러낸 뒤 want 개를 채워야 하므로 넉넉히 받아 자른다.
    """
    out, page, universe = [], 1, None
    while len(out) < want and page <= 5:
        j = _api(f"stocks/up/{market}", page=page, pageSize=PAGE_MAX)
        universe = j.get("totalCount", universe)
        items = j.get("stocks") or []
        if not items:
            break
        for x in items:
            if EXCLUDE_ETP and x.get("stockEndType") not in STOCK_END_TYPES:
                continue
            r = _stock_row(x, market)
            if not r:
                continue
            if MIN_CAP_EOK > 0 and (r.get("market_cap_eok") or 0) < MIN_CAP_EOK:
                continue
            out.append(r)
            if len(out) >= want:
                break
        page += 1
    if not out:
        raise RuntimeError(f"{market} 상승률 상위를 하나도 받지 못했습니다")
    return out, universe


def naver_sectors():
    """업종 전체를 당일 등락률 내림차순으로. [(no, 이름, 등락률, 상승수, 하락수, 종목수)]"""
    j = _api("stocks/industry", page=1, pageSize=PAGE_MAX)   # 업종은 79개라 한 페이지
    rows = []
    for g in j.get("groups") or []:
        chg = _num(g.get("changeRate"))
        if chg is None:
            continue
        rows.append((g["no"], g["name"], round(chg, 2),
                     g.get("riseCount", 0), g.get("fallCount", 0), g.get("totalCount", 0)))
    if not rows:
        raise RuntimeError("업종 시세를 받지 못했습니다")
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


def naver_sector_members(no):
    """업종 구성종목 전체를 등락률 내림차순으로.

    구성종목이 100개를 넘는 업종이 있는데 pageSize 상한이 100이라 페이징한다.
    한 페이지만 받고 끝내면 뒤쪽 종목이 통째로 빠지고, 그 업종의 '가장 많이 내린 3종목'이
    실제 최하위가 아니게 된다 — 결과는 정상적인 모습을 하고 있어서 드러나지 않는다.
    """
    rows, page, total = [], 1, None
    while page <= 10:
        j = _api(f"stocks/industry/{no}", page=page, pageSize=PAGE_MAX)
        total = j.get("totalCount", total)
        items = j.get("stocks") or []
        rows += [r for r in (_stock_row(x) for x in items) if r]
        if len(items) < PAGE_MAX or (total is not None and len(rows) >= total):
            break
        page += 1
    if total is not None and len(rows) < total:
        print(f"업종 {no}: 구성종목 {total}개 중 {len(rows)}개만 받았습니다", file=sys.stderr)
    rows.sort(key=lambda x: x["chg_pct_d"], reverse=True)
    return rows


def attach_trend(rows, bench):
    """선정된 종목에만 20일·200일선 등 추세 지표를 붙인다.

    거래소 접미사는 API 가 stockExchangeType 으로 알려주므로 추측하지 않는다.
    받지 못한 종목은 조용히 넘기지 않고 이름을 남긴다 — 잘못된 심볼은 예외가 아니라
    빈 시계열로 나타나서, 세지 않으면 지표만 사라지고 아무도 모른다.
    """
    want = [r for r in rows if r.get("code")]
    if not want:
        return
    start = (datetime.now(KST) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    syms = [r["code"] + r.get("suffix", ".KS") for r in want]
    hist = download_history(syms, start=start)
    missing = []
    for r in want:
        h = hist.get(r["code"] + r.get("suffix", ".KS"))
        if not h or len(h["close"]) < 2:
            missing.append(f'{r["name"]}({r["code"]})')
            continue
        m = trend_metrics(h["close"], h.get("volume"))
        for k in ("ret_5d", "ret_20d", "vs_ma50", "vs_ma200", "pct_from_52w_high",
                  "vol_x_avg20"):
            if m.get(k) is not None:
                r[k] = m[k]
        if m.get("ret_20d") is not None and bench.get("ret_20d") is not None:
            r["rs_20d_vs_kospi"] = round(m["ret_20d"] - bench["ret_20d"], 2)
    if missing:
        print(f"추세 지표를 받지 못한 종목 {len(missing)}개 → {', '.join(missing[:8])}",
              file=sys.stderr)


def collect_kr_market_data():
    now_kst = datetime.now(KST)
    start = (now_kst - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")

    # 1) 지수 — 종가·등락률·기준 세션은 네이버에서, 추세 지표만 yfinance 시계열에서.
    #    세션 날짜를 지수 시계열이 아니라 거래소 상태(marketStatus/localTradedAt)에서
    #    뽑는다. 라벨과 데이터가 다른 소스에서 나오면 어긋나도 오류가 안 난다.
    kospi = naver_index("KOSPI")
    if kospi.get("traded_at") is None:
        raise RuntimeError("코스피 지수 상태를 받지 못했습니다")
    t_date = datetime.fromisoformat(kospi["traded_at"]).date()
    market_status = kospi.get("market_status")

    hist_idx = download_history(list(KR_INDICES.keys()), start=start)
    bench_close = hist_idx.get(BENCHMARK_KR, {}).get("close")
    if bench_close is None or len(bench_close) < 2:
        raise RuntimeError("코스피 시계열 수집 실패(^KS11)")
    bench = trend_metrics(bench_close)
    t1_date = bench_close.index[-2].date() if len(bench_close) >= 2 else None

    live = {"^KS11": kospi, "^KQ11": naver_index("KOSDAQ")}
    indices = []
    for ticker, name in KR_INDICES.items():
        h = hist_idx.get(ticker)
        m = trend_metrics(h["close"]) if h is not None and len(h["close"]) >= 2 else {}
        row = {"ticker": ticker, "name": name,
               "ret_5d": m.get("ret_5d"), "ret_20d": m.get("ret_20d"),
               "vs_ma200": m.get("vs_ma200"),
               "pct_from_52w_high": m.get("pct_from_52w_high")}
        if ticker in live:                      # 지수는 네이버 실측값을 쓴다
            row["close"] = live[ticker]["close"]
            row["chg_pct_d"] = live[ticker]["chg_pct_d"]
        elif h is not None and len(h["close"]) >= 2:
            row["close"] = round(float(h["close"].iloc[-1]), 2)
            row["chg_pct_d"] = m.get("chg_pct_d")
            row["as_of"] = str(h["close"].index[-1].date())   # 환율은 갱신 시점이 다르다
        else:
            row["error"] = "N/A"
        indices.append(strip_none(row))

    # 2)(3) 시장별 당일 상승률 상위 — 시장 전체가 모집단이다.
    tops = {}
    for market, key in (("KOSPI", "kospi_top_gainers"), ("KOSDAQ", "kosdaq_top_gainers")):
        picked, universe = naver_gainers(market, TOP_N)
        attach_trend(picked, bench)
        tops[key] = picked
        tops[key + "_universe"] = universe

    # 4)(5) 업종 — 강세 2개 / 약세 1개, 각 업종 구성종목 전체에서 최강·최약 3개.
    sectors = naver_sectors()
    strong, weak = [], None
    for no, name, chg, up, down, total in sectors[:2]:
        members = naver_sector_members(no)
        top3 = members[:3]
        attach_trend(top3, bench)
        strong.append({"sector": name, "chg_pct_d": chg, "members": total or len(members),
                       "up": up, "down": down, "top3_by_daily_change": top3})
    if sectors:
        no, name, chg, up, down, total = sectors[-1]
        members = naver_sector_members(no)
        bottom3 = members[-3:][::-1]
        attach_trend(bottom3, bench)
        weak = {"sector": name, "chg_pct_d": chg, "members": total or len(members),
                "up": up, "down": down, "bottom3_by_daily_change": bottom3}

    return {
        "market": "KR",
        "session_date": str(t_date),
        "prev_session_date": str(t1_date),
        "generated_at_kst": now_kst.strftime("%Y-%m-%d %H:%M"),
        "market_status": market_status,
        "benchmark": strip_none({"ticker": BENCHMARK_KR, "name": "코스피(상대강도 기준)", **bench}),
        "indices": indices,
        **tops,
        "sector_universe": len(sectors),
        "strongest_sectors": strong,
        "weakest_sector": weak,
        "min_market_cap_filter_eok": MIN_CAP_EOK,
        "field_guide": {
            "chg_pct_d": "당일 등락률(%). 종목은 네이버 금융 시세, 업종은 업종지수 기준.",
            "ret_5d / ret_20d": "5·20 거래일 전 종가 대비 수익률(%)",
            "vs_ma50 / vs_ma200": "50일·200일 이동평균 대비 이격도(%). 양수면 추세 위.",
            "pct_from_52w_high": "52주 고점 대비 위치(%). 0에 가까울수록 신고가권.",
            "vol_x_avg20": "당일 거래량 ÷ 직전 20일 평균 거래량. 2 이상이면 이벤트 신호.",
            "rs_20d_vs_kospi": "20일 수익률에서 코스피 20일 수익률을 뺀 값(%p). 상대강도.",
            "*_universe": "순위를 매긴 모집단 크기(그 시장의 전 종목 수).",
            "min_market_cap_filter_eok": "0이면 시가총액 필터 없이 순수 등락률 순위다.",
        },
        "note": ("kospi_top_gainers / kosdaq_top_gainers는 시가총액 상위가 아니라 "
                 "'당일 상승률 상위'다. 시장 전체를 모집단으로 하므로 소형주·상한가 종목이 "
                 "많이 포함되는 것이 정상이며, 그 사실 자체가 그날 시장의 성격을 말해준다. "
                 "추세 지표(ret_20d 등)는 선정된 종목에만 붙어 있고, 신규상장 등으로 이력이 "
                 "짧으면 아예 없다. 없는 지표는 언급하지 말 것."),
    }


# ── 브리핑 생성 ──────────────────────────────────────────────────────────────
KR_SYSTEM_PROMPT = """당신은 한국 은행 자금부의 시니어 마켓 데스크입니다. 독자는 21년차 뱅커이자
추세추종·섹터 상대강도 전략을 쓰는 개인투자자입니다. 제공된 실측 데이터(JSON)로 오늘
한국 증시(코스피·코스닥) 정규장 마감 브리핑을 작성하세요.

이 브리핑의 핵심 가치는 '어느 종목이 몇 % 올랐는가'가 아니라 '왜 올랐는가'입니다.
등락률만 나열하고 이유가 비어 있는 브리핑은 실패입니다.

규칙:
- 반드시 한국어. 텔레그램 발송용이므로 마크다운 표 대신 줄단위 텍스트/이모지 사용.
- HTML 태그는 <b>, <i>만 사용 가능(텔레그램 HTML 모드). 다른 태그 금지.
- 종목은 <b>종목명</b> 형식으로 쓰고 종목코드·주가(현재가 금액)는 쓰지 않는다.
  등락률과 추세 수치만 표기한다. 지수와 환율의 수치는 ①에 그대로 표기한다.
- 수치 표기 약속: 1일 등락률은 +2.1%, 20일 수익률은 20d +8.4%,
  200일선 대비는 200일선 +12%, 상대강도는 RS +5.2%p, 거래량은 거래량 2.3배.
- ★1줄 브리핑의 정의: 그 종목이 오늘 그렇게 움직인 '이유'를 한 문장으로 쓴 것이다.
  실적·수주·공시·증권사 리포트·수급·업황·테마 등 구체적 사건을 밝힌다. 상승률 상위에는
  이름이 낯선 중소형주가 많고 바로 그 종목들이야말로 이유가 궁금한 종목이므로,
  웹검색은 대형주가 아니라 이런 종목에 우선 쓸 것.
  검색으로 확인되지 않으면 지어내지 말고 대신 관찰된 사실을 한 문장으로 쓴다
  (예: "재료가 확인되지 않은 채 거래량만 급증한 상한가"). 어느 경우에도 빈칸으로 두지 않는다.
- ★검색 결과의 영어 원문을 그대로 옮기지 말 것. 반드시 한국어로 소화해 서술한다.

구성(이 순서를 그대로 지킬 것):

  ① 오늘의 시장 요약 (3~4문장)
     코스피·코스닥 지수 등락률과 원/달러 환율을 수치로 밝히고, 두 시장이 같은 방향이었는지
     엇갈렸는지, 오늘의 움직임이 20일 추세의 연장인지 되돌림인지를 200일선 대비 위치를
     근거로 한 문장으로 못박는다. 지수는 내렸는데 상승률 상위에 상한가가 즐비한 식으로
     지수와 개별 종목의 온도가 다르면 그 점을 반드시 짚는다.

  ② 코스피 당일 상승률 상위 10
     kospi_top_gainers를 순서대로 10개, 한 줄씩. 형식:
       n. <b>종목명</b> +23.99% (20d +5.4%) — 1줄 브리핑
     20d 등 추세 지표가 데이터에 없는 종목은 등락률만 쓰고 넘어간다(없는 수치를 만들지 말 것).
     ★이것은 시가총액 순위가 아니라 상승률 순위다. 모집단은 그 시장의 전 종목이다.

  ③ 코스닥 당일 상승률 상위 10
     ②와 완전히 같은 형식으로 kosdaq_top_gainers 10개.

  ④ 오늘 가장 강했던 업종 2개
     strongest_sectors의 2개를 순서대로. 각 업종마다:
       (a) <b>업종명</b> 헤더 한 줄 — 업종지수 등락률, 상승/하락 종목 수(전체 구성종목 수)
       (b) 그 업종에서 가장 많이 오른 종목 3개(top3_by_daily_change)를 한 줄씩,
           ②와 같은 '종목명 등락률 (20d) — 1줄 브리핑' 형식으로
       (c) 이 업종이 오늘 왜 강했는지 2~3문장. 마지막 문장은 반드시 추세 판단
           (20일 추세의 연장인가, 낙폭과대 반등인가)으로 끝낸다.

  ⑤ 오늘 가장 약했던 업종 1개
     weakest_sector를 ④와 똑같은 (a)(b)(c) 구조로. 단 (b)는 그 업종에서 가장 많이 내린
     종목 3개(bottom3_by_daily_change)를 쓴다. (c)에서는 왜 같은 날 같은 방향으로 팔렸는지를
     업황·수급·자금 이동으로 설명하고, 이 하락이 '추세 안의 조정'인지 '추세 이탈'인지를
     200일선 대비 위치와 20d 수익률로 구분해 명시한다.

  ⑥ 한 줄 총평
     오늘 자금이 어느 업종에서 어느 업종으로 옮겨갔는지 한 문장.

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


def build_kr_fallback_brief(data, reason=None):
    """Claude 호출이 실패했을 때의 수치 위주 브리핑.

    머리에 반드시 경고를 붙인다. 축약본이 정상 브리핑과 같은 모습으로 도착하면
    장애가 '해설이 좀 빠진 것'으로 읽히고, 원인(예: API 크레딧 소진)을 찾기까지
    시간이 걸린다. 겉모습으로 구분되게 하는 것이 이 한 줄의 목적이다.
    """
    L = [f"⚠️ <b>AI 해설 생성에 실패해 수치 요약만 발송합니다.</b>"]
    if reason:
        L.append(f"<i>사유: {reason}</i>")
    L += ["", f"🇰🇷 <b>한국 증시 마감 브리핑 — {data['session_date']}</b>", ""]

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

    for key, title in (("kospi_top_gainers", "② 코스피 상승률 상위"),
                       ("kosdaq_top_gainers", "③ 코스닥 상승률 상위")):
        rows = data.get(key) or []
        if not rows:
            continue
        n = data.get(key + "_universe")
        L.append(f"<b>{title} {len(rows)}</b> <i>(전 종목 {n}개 중)</i>" if n
                 else f"<b>{title} {len(rows)}</b>")
        for i, r in enumerate(rows, 1):
            L.append(f"{i}. {_line(r)}")
        L.append("")

    for i, s in enumerate(data.get("strongest_sectors") or [], 1):
        L.append(f"<b>④-{i} 강세 업종: {s['sector']}</b> {s['chg_pct_d']:+.2f}% "
                 f"(상승 {s['up']}/하락 {s['down']}, 전체 {s['members']})")
        for r in s["top3_by_daily_change"]:
            L.append(f"   · {_line(r)}")
        L.append("")

    w = data.get("weakest_sector")
    if w:
        L.append(f"<b>⑤ 약세 업종: {w['sector']}</b> {w['chg_pct_d']:+.2f}% "
                 f"(상승 {w['up']}/하락 {w['down']}, 전체 {w['members']})")
        for r in w["bottom3_by_daily_change"]:
            L.append(f"   · {_line(r)}")
        L.append("")

    L.append("<i>본 내용은 투자 권유가 아닌 정보 제공 목적입니다.</i>")
    return "\n".join(L)


def main():
    now_kst = datetime.now(KST)
    print(f"run at KST={now_kst:%Y-%m-%d %H:%M}")

    try:
        data = collect_kr_market_data()
    except Exception as e:
        # 수집 실패는 조용히 넘기지 않는다. 빈 브리핑보다 실패 통지가 낫다.
        print(f"수집 실패: {e}", file=sys.stderr)
        if os.environ.get("DRY_RUN") != "1":
            send_telegram(f"🇰🇷 <b>한국 증시 브리핑 생성 실패</b>\n<i>{e}</i>")
        raise

    session = datetime.strptime(data["session_date"], "%Y-%m-%d").date()

    # 17:30 KST 실행이므로 정상 거래일이면 최근 세션은 'KST 기준 오늘'이어야 한다.
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
        print(build_kr_fallback_brief(data, "DRY_RUN"))
        return

    brief, reason = None, None
    try:
        brief = build_kr_brief_with_claude(data)
        if not brief:
            reason = "ANTHROPIC_API_KEY 미설정 또는 빈 응답"
    except Exception as e:
        # requests가 raise_for_status로 올린 예외에는 응답 객체가 붙어 있고, 사유는
        # 상태줄이 아니라 그 본문에 있다("credit balance is too low" 등). 상태코드만
        # 보고하면 결제 문제와 요청 오류가 같은 문장으로 보여 진단이 한 단계 늦어진다.
        body = getattr(getattr(e, "response", None), "text", "") or ""
        try:
            body = json.loads(body).get("error", {}).get("message", "") or body
        except Exception:
            pass
        reason = (f"{e} — {body}" if body else str(e))[:400]
        print(f"Claude API 실패 → 기본 브리핑으로 대체: {reason}", file=sys.stderr)
    if not brief:
        brief = build_kr_fallback_brief(data, reason)

    send_telegram(brief)
    print("텔레그램 발송 완료")


if __name__ == "__main__":
    main()
