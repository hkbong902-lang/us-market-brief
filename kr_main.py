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
겉모습은 정답과 똑같다. 그래서 네이버 금융의 등락률 순위·업종 시세를 그대로 받아 쓴다
(코스피 789 / 코스닥 506 종목, 업종 79개 기준). 추세 지표(20일·200일선)만 선정된
종목에 한해 yfinance로 덧붙인다.

수집 실패는 조용히 넘기지 않는다. 부분 브리핑은 정상 브리핑과 겉모습이 같아서, 장애가
'기능이 좀 빠진 것'으로 읽히고 원인 조사가 늦어진다.
"""
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests
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

KR_INDICES = {
    "^KS11": "코스피",
    "^KQ11": "코스닥",
    "KRW=X": "원/달러 환율",
}
BENCHMARK_KR = "^KS11"

NAVER = "https://finance.naver.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TOP_N = int(os.environ.get("KR_TOP_N", "10"))
# 상승률 상위는 상한가 소형주가 대부분을 차지한다. 그 자체가 그날의 사실이므로 기본값은
# 필터 없음(0)이다. 대형주 위주로 보고 싶으면 억 단위 하한을 넣는다(예: 5000 = 5,000억).
MIN_CAP_EOK = int(os.environ.get("KR_MIN_MARKET_CAP_EOK", "0"))
# 네이버 등락률 순위표에는 ETN·ETF·리츠가 종목과 섞여 실린다. ETN은 지수를 추종하는
# 상장지수증권이라 '오늘 오른 종목'의 이유를 물을 대상이 아니다(레버리지 배수만큼 오른다).
# KR_INCLUDE_ETP=1 로 두면 필터를 끈다.
EXCLUDE_ETP = os.environ.get("KR_INCLUDE_ETP", "0") != "1"
ETP_PAT = re.compile(r"\bETN\b|\bETF\b|레버리지|인버스|선물\s*ETN")


def _get(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    r.encoding = "euc-kr"
    return r.text


def _tables(html):
    return pd.read_html(io.StringIO(html))


def _num(v):
    """'+23.99%' → 23.99 / '-1.25%' → -1.25. 파싱 불가면 None."""
    if v is None:
        return None
    s = str(v).replace("%", "").replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def naver_gainers(market):
    """네이버 등락률 순위에서 시장 전체를 등락률 내림차순으로 받는다."""
    sosok = 0 if market == "KOSPI" else 1
    html = _get(f"{NAVER}/sise/sise_rise.naver?sosok={sosok}")
    codes = re.findall(r"/item/main\.naver\?code=(\d{6})", html)
    tbl = None
    for t in _tables(html):
        cols = [str(c) for c in t.columns]
        if any("종목명" in c for c in cols) and any("등락률" in c for c in cols):
            tbl = t.dropna(how="all").dropna(axis=1, how="all")
            break
    if tbl is None:
        raise RuntimeError(f"네이버 등락률 순위 표를 찾지 못했습니다({market})")

    rows, skipped = [], []
    for i, (_, r) in enumerate(tbl.iterrows()):
        chg = _num(r.get("등락률"))
        name = str(r.get("종목명", "")).strip()
        if chg is None or not name or name == "nan":
            continue
        # 코드는 표의 행 순서와 같은 순서로 뽑히므로, 걸러낸 행도 자리를 소비해야
        # 이후 종목과 코드가 어긋나지 않는다. 필터는 코드를 짝지은 뒤에 적용한다.
        code = codes[len(rows) + len(skipped)] if (len(rows) + len(skipped)) < len(codes) else None
        if EXCLUDE_ETP and ETP_PAT.search(name):
            skipped.append(name)
            continue
        rows.append({
            "name": name.rstrip(" *"),
            "code": code,
            "market": market,
            "chg_pct_d": round(chg, 2),
            "volume": int(r["거래량"]) if pd.notna(r.get("거래량")) else None,
        })
    if skipped:
        print(f"{market}: ETN/ETF {len(skipped)}개 제외 → {', '.join(skipped[:5])}",
              file=sys.stderr)
    if not rows:
        raise RuntimeError(f"네이버 등락률 순위가 비어 있습니다({market})")
    return rows


def naver_sectors():
    """업종별 당일 등락률. [(no, 업종명, 등락률)] 를 등락률 내림차순으로."""
    html = _get(f"{NAVER}/sise/sise_group.naver?type=upjong")
    links = re.findall(
        r"sise_group_detail\.naver\?type=upjong&no=(\d+)\">([^<]+)</a>", html)
    chg_by_name = {}
    for t in _tables(html):
        cols = [str(c) for c in t.columns]
        if any("업종명" in c for c in cols):
            t = t.dropna(how="all")
            namecol = [c for c in t.columns if "업종명" in str(c)][0]
            chgcol = [c for c in t.columns if "전일대비" in str(c)][0]
            for _, r in t.iterrows():
                v = _num(r[chgcol])
                if v is not None:
                    chg_by_name[str(r[namecol]).strip()] = v
            break
    out = [(no, nm, chg_by_name[nm]) for no, nm in links if nm in chg_by_name]
    if not out:
        raise RuntimeError("네이버 업종 시세를 파싱하지 못했습니다")
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def naver_sector_members(no):
    """업종 구성종목 전체를 등락률 내림차순으로."""
    html = _get(f"{NAVER}/sise/sise_group_detail.naver?type=upjong&no={no}")
    codes = re.findall(r"/item/main\.naver\?code=(\d{6})", html)
    tbl = None
    for t in _tables(html):
        cols = [str(c) for c in t.columns]
        if any("종목명" in c for c in cols) and any("등락률" in c for c in cols):
            tbl = t.dropna(how="all").dropna(axis=1, how="all")
            break
    if tbl is None:
        return []
    rows = []
    for _, r in tbl.iterrows():
        chg = _num(r.get("등락률"))
        name = str(r.get("종목명", "")).strip()
        if chg is None or not name or name == "nan":
            continue
        rows.append({
            # 네이버는 업종 구성종목 중 코스닥 종목에 ' *'를 붙인다.
            "name": name.rstrip(" *"),
            "code": codes[len(rows)] if len(rows) < len(codes) else None,
            "market": "KOSDAQ" if name.endswith("*") else "KOSPI",
            "chg_pct_d": round(chg, 2),
            "volume": int(r["거래량"]) if pd.notna(r.get("거래량")) else None,
        })
    rows.sort(key=lambda x: x["chg_pct_d"], reverse=True)
    return rows


def attach_trend(rows, bench):
    """선정된 종목에만 20일·200일선 등 추세 지표를 붙인다.

    거래소 접미사를 확신할 수 없는 종목이 있으므로 .KS/.KQ 양쪽을 한 번에 받아
    데이터가 있는 쪽을 쓴다. 잘못된 접미사는 예외가 아니라 빈 시계열로 나타나기 때문에,
    한쪽만 시도하면 지표가 조용히 빠진다.
    """
    codes = [r["code"] for r in rows if r.get("code")]
    if not codes:
        return
    start = (datetime.now(KST) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    syms = [c + s for c in codes for s in (".KS", ".KQ")]
    hist = download_history(syms, start=start)
    for r in rows:
        c = r.get("code")
        if not c:
            continue
        h = hist.get(c + ".KS") or hist.get(c + ".KQ")
        if not h or len(h["close"]) < 2:
            continue
        m = trend_metrics(h["close"], h.get("volume"))
        for k in ("ret_5d", "ret_20d", "vs_ma50", "vs_ma200", "pct_from_52w_high",
                  "vol_x_avg20"):
            if m.get(k) is not None:
                r[k] = m[k]
        if m.get("ret_20d") is not None and bench.get("ret_20d") is not None:
            r["rs_20d_vs_kospi"] = round(m["ret_20d"] - bench["ret_20d"], 2)


def fetch_caps_eok(rows):
    """시총 하한 필터를 켠 경우에만 쓰는 시가총액(억원) 조회."""
    import concurrent.futures as cf

    def one(r):
        c = r.get("code")
        if not c:
            return r, None
        for suf in (".KS", ".KQ"):
            try:
                mc = yf.Ticker(c + suf).fast_info.get("marketCap")
                if mc:
                    return r, mc / 1e8
            except Exception:
                continue
        return r, None

    with cf.ThreadPoolExecutor(12) as ex:
        for r, cap in ex.map(one, rows):
            if cap:
                r["market_cap_eok"] = round(cap)


def collect_kr_market_data():
    now_kst = datetime.now(KST)
    start = (now_kst - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")

    # 1) 지수·환율 — 추세 지표가 필요하므로 여기만 yfinance 시계열을 쓴다.
    idx = download_history(list(KR_INDICES.keys()), start=start)
    if BENCHMARK_KR not in idx or len(idx[BENCHMARK_KR]["close"]) < 2:
        raise RuntimeError("지수 데이터 수집 실패(^KS11)")
    bench_close = idx[BENCHMARK_KR]["close"]
    bench = trend_metrics(bench_close)
    dates = list(bench_close.index)
    t_date, t1_date = dates[-1].date(), (dates[-2].date() if len(dates) >= 2 else None)

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
        if ticker == "KRW=X":      # 24시간 거래라 당일 봉 확정이 늦을 수 있다
            row["as_of"] = str(s.index[-1].date())
        indices.append(strip_none(row))

    # 2)(3) 시장별 당일 상승률 상위 — 시장 전체를 받아 상위 N개를 자른다.
    tops = {}
    for market, key in (("KOSPI", "kospi_top_gainers"), ("KOSDAQ", "kosdaq_top_gainers")):
        allrows = naver_gainers(market)
        picked = allrows
        if MIN_CAP_EOK > 0:
            head = allrows[:TOP_N * 6]      # 필터 통과분을 채우기 위한 여유분만 조회
            fetch_caps_eok(head)
            picked = [r for r in head if (r.get("market_cap_eok") or 0) >= MIN_CAP_EOK]
        picked = picked[:TOP_N]
        attach_trend(picked, bench)
        tops[key] = picked
        tops[key + "_universe"] = len(allrows)

    # 4)(5) 업종 — 강세 2개 / 약세 1개, 각 업종의 구성종목 전체에서 최강·최약 3개.
    sectors = naver_sectors()
    strong, weak = [], None
    for no, name, chg in sectors[:2]:
        members = naver_sector_members(no)
        top3 = members[:3]
        attach_trend(top3, bench)
        strong.append({"sector": name, "chg_pct_d": chg, "members": len(members),
                       "up": sum(1 for m in members if m["chg_pct_d"] > 0),
                       "down": sum(1 for m in members if m["chg_pct_d"] < 0),
                       "top3_by_daily_change": top3})
    if sectors:
        no, name, chg = sectors[-1]
        members = naver_sector_members(no)
        bottom3 = members[-3:][::-1]
        attach_trend(bottom3, bench)
        weak = {"sector": name, "chg_pct_d": chg, "members": len(members),
                "up": sum(1 for m in members if m["chg_pct_d"] > 0),
                "down": sum(1 for m in members if m["chg_pct_d"] < 0),
                "bottom3_by_daily_change": bottom3}

    return {
        "market": "KR",
        "session_date": str(t_date),
        "prev_session_date": str(t1_date),
        "generated_at_kst": now_kst.strftime("%Y-%m-%d %H:%M"),
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
        reason = str(e)[:300]
        print(f"Claude API 실패 → 기본 브리핑으로 대체: {e}", file=sys.stderr)
    if not brief:
        brief = build_kr_fallback_brief(data, reason)

    send_telegram(brief)
    print("텔레그램 발송 완료")


if __name__ == "__main__":
    main()
