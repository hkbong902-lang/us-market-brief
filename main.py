# -*- coding: utf-8 -*-
"""
US Market Daily Brief -> Telegram
- 직전 미국 정규장 마감 기준 데이터를 수집하고,
- Claude API로 서사형 한국어 브리핑을 작성한 뒤,
- 텔레그램 봇으로 발송한다.

필수 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수: ANTHROPIC_API_KEY (없으면 수치 위주 기본 브리핑으로 발송)
              CLAUDE_MODEL (기본: claude-sonnet-5)
              CLAUDE_MAX_TOKENS (한 응답의 생성 토큰 상한. 기본 32000.
                                 사고·웹검색·본문이 이 한 예산을 공유한다)
              USE_WEB_SEARCH ("0"이면 뉴스 웹검색 비활성, 기본 활성)
              WEB_SEARCH_TOOL_TYPE (기본: web_search_20260209.
                                    구형 모델을 쓰면 web_search_20250305)
              SKIP_ON_HOLIDAY ("1"이면 휴장일에 아무것도 안 보냄, 기본은 휴장 안내 발송)
              FORCE_SEND ("1"이면 휴장 판정을 무시하고 직전 마감 세션 브리핑을 발송. 테스트용)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# 윈도우 콘솔(cp949)에서 한국어·기호 출력이 UnicodeEncodeError로 죽지 않도록 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

# ── 수집 대상 ────────────────────────────────────────────────────────────────
INDICES = {
    "^TNX": "미국채 10년물(%)",
    "DX-Y.NYB": "달러인덱스(DXY)",   # DX=F(선물)는 야후에서 404 → 현물 지수를 쓴다
    "CL=F": "WTI($)",
    "^DJI": "다우",
    "^GSPC": "S&P500",
    "^IXIC": "나스닥",
    "^RUT": "러셀2000",
    "^VIX": "VIX(변동성)",
}

SECTOR_ETFS = {
    "XLK": "기술", "XLF": "금융", "XLE": "에너지", "XLV": "헬스케어",
    "XLY": "경기소비재", "XLP": "필수소비재", "XLI": "산업재", "XLB": "소재",
    "XLU": "유틸리티", "XLRE": "리츠", "XLC": "커뮤니케이션", "SMH": "반도체(SMH)",
}

# 나스닥 메가캡 후보군(시총 상위 10 선별용) — 필요시 자유롭게 수정
NASDAQ_MEGACAP_CANDIDATES = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AVGO", "TSLA",
    "AMD", "ASML", "NFLX", "COST", "PLTR", "CSCO", "PEP", "QCOM", "ADBE",
]

# 급등락 스캔용 워치리스트 — 테마별로 묶어 '진앙지 → 확산 → 반대편' 서사에 쓴다.
# 티커를 추가할 때는 TICKER_NAMES에 회사명도 함께 넣어야 브리핑에 종목명이 정확히 나온다.
MOVER_WATCHLIST_GROUPS = {
    "반도체·반도체장비": [
        "NVDA", "AVGO", "AMD", "ASML", "MU", "TSM", "INTC", "QCOM", "ARM", "LRCX", "AMAT",
        "KLAC", "TXN"
    ],
    "메가캡 플랫폼": [
        "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA"
    ],
    "AI 인프라·메모리·스토리지·하드웨어": [
        "SNDK", "SKHY", "WDC", "STX", "MRVL", "NVTS", "SMCI", "DELL", "VRT", "BE", "LITE",
        "COHR", "ANET", "CRDO"
    ],
    "소프트웨어·SaaS·인터넷": [
        "PLTR", "CRM", "ORCL", "NOW", "SNOW", "NFLX", "SHOP", "UBER", "PANW", "CRWD", "ADBE",
        "TEAM", "MDB", "RBLX", "FICO", "EFX", "GWRE", "ZS", "DOCU"
    ],
    "금융": [
        "JPM", "BAC", "GS", "MS", "WFC", "BLK", "BRK-B", "V", "MA", "COIN"
    ],
    "에너지": [
        "XOM", "CVX", "COP", "SLB", "MPC", "VLO", "OXY", "EOG", "HAL", "PSX"
    ],
    "헬스케어·소비·산업": [
        "LLY", "UNH", "JNJ", "PFE", "MRK", "ABBV", "WMT", "COST", "HD", "NKE", "LULU", "MCD",
        "SBUX", "DIS", "BA", "CAT", "GE", "LMT", "DE", "HON"
    ],
    "기타(원자재·밈주)": [
        "MP", "AMC"
    ],
}

MOVER_WATCHLIST = [t for ts in MOVER_WATCHLIST_GROUPS.values() for t in ts]
TICKER_GROUP = {t: g for g, ts in MOVER_WATCHLIST_GROUPS.items() for t in ts}

# 브리핑에 종목명을 정확히 표기하기 위한 매핑(yfinance longName 기준).
# 없는 티커는 티커 문자열이 그대로 쓰인다.
TICKER_NAMES = {
    "AAPL": "Apple Inc.",
    "ABBV": "AbbVie Inc.",
    "ADBE": "Adobe Inc.",
    "AMAT": "Applied Materials, Inc.",
    "AMC": "AMC Entertainment Holdings, Inc.",
    "AMD": "Advanced Micro Devices, Inc.",
    "AMZN": "Amazon.com, Inc.",
    "ANET": "Arista Networks, Inc.",
    "ARM": "Arm Holdings plc",
    "ASML": "ASML Holding N.V.",
    "AVGO": "Broadcom Inc.",
    "BA": "The Boeing Company",
    "BAC": "Bank of America Corporation",
    "BE": "Bloom Energy Corporation",
    "BLK": "BlackRock, Inc.",
    "BRK-B": "Berkshire Hathaway Inc.",
    "CAT": "Caterpillar Inc.",
    "COHR": "Coherent Corp.",
    "COIN": "Coinbase Global, Inc.",
    "COP": "ConocoPhillips",
    "COST": "Costco Wholesale Corporation",
    "CRDO": "Credo Technology Group Holding Ltd",
    "CRM": "Salesforce, Inc.",
    "CRWD": "CrowdStrike Holdings, Inc.",
    "CSCO": "Cisco Systems, Inc.",
    "CVX": "Chevron Corporation",
    "DE": "Deere & Company",
    "DELL": "Dell Technologies Inc.",
    "DIS": "The Walt Disney Company",
    "DOCU": "DocuSign, Inc.",
    "EFX": "Equifax Inc.",
    "EOG": "EOG Resources, Inc.",
    "FICO": "Fair Isaac Corporation",
    "GE": "GE Aerospace",
    "GOOGL": "Alphabet Inc.",
    "GS": "The Goldman Sachs Group, Inc.",
    "GWRE": "Guidewire Software, Inc.",
    "HAL": "Halliburton Company",
    "HD": "The Home Depot, Inc.",
    "HON": "Honeywell International Inc.",
    "INTC": "Intel Corporation",
    "JNJ": "Johnson & Johnson",
    "JPM": "JPMorgan Chase & Co.",
    "KLAC": "KLA Corporation",
    "LITE": "Lumentum Holdings Inc.",
    "LLY": "Eli Lilly and Company",
    "LMT": "Lockheed Martin Corporation",
    "LRCX": "Lam Research Corporation",
    "LULU": "lululemon athletica inc.",
    "MA": "Mastercard Incorporated",
    "MCD": "McDonald's Corporation",
    "MDB": "MongoDB, Inc.",
    "META": "Meta Platforms, Inc.",
    "MP": "MP Materials Corp.",
    "MPC": "Marathon Petroleum Corporation",
    "MRK": "Merck & Co., Inc.",
    "MRVL": "Marvell Technology, Inc.",
    "MS": "Morgan Stanley",
    "MSFT": "Microsoft Corporation",
    "MU": "Micron Technology, Inc.",
    "NFLX": "Netflix, Inc.",
    "NKE": "NIKE, Inc.",
    "NOW": "ServiceNow, Inc.",
    "NVDA": "NVIDIA Corporation",
    "NVTS": "Navitas Semiconductor Corporation",
    "ORCL": "Oracle Corporation",
    "OXY": "Occidental Petroleum Corporation",
    "PANW": "Palo Alto Networks, Inc.",
    "PEP": "PepsiCo, Inc.",
    "PFE": "Pfizer Inc.",
    "PLTR": "Palantir Technologies Inc.",
    "PSX": "Phillips 66",
    "QCOM": "QUALCOMM Incorporated",
    "RBLX": "Roblox Corporation",
    "SBUX": "Starbucks Corporation",
    "SHOP": "Shopify Inc.",
    "SKHY": "SK hynix Inc.",
    "SLB": "SLB N.V.",
    "SMCI": "Super Micro Computer, Inc.",
    "SNDK": "Sandisk Corporation",
    "SNOW": "Snowflake Inc.",
    "STX": "Seagate Technology Holdings plc",
    "TEAM": "Atlassian Corporation",
    "TSLA": "Tesla, Inc.",
    "TSM": "Taiwan Semiconductor Manufacturing Company Limited",
    "TXN": "Texas Instruments Incorporated",
    "UBER": "Uber Technologies, Inc.",
    "UNH": "UnitedHealth Group Incorporated",
    "V": "Visa Inc.",
    "VLO": "Valero Energy Corporation",
    "VRT": "Vertiv Holdings Co",
    "WDC": "Western Digital Corporation",
    "WFC": "Wells Fargo & Company",
    "WMT": "Walmart Inc.",
    "XOM": "ExxonMobil Holdings Corporation",
    "ZS": "Zscaler, Inc.",
}


TELEGRAM_MAX = 4096


# ── 데이터 수집 유틸 ─────────────────────────────────────────────────────────
# 추세·상대강도를 계산하려면 200일 이동평균과 52주 고저가 필요하다. 달력 520일이면
# 언제 실행해도 거래일 약 350개가 확보되고, 동시에 항상 전년 12월을 포함하므로
# 연초 대비 수익률까지 같은 시계열 한 번의 다운로드에서 나온다.
HISTORY_DAYS = 520
BENCHMARK = "^GSPC"          # 상대강도(RS)의 기준 지수
TRADING_DAYS_52W = 252


def download_history(tickers, start=None, period=None):
    """yfinance 일봉을 {ticker: {"close": Series, "volume": Series|None}} 로 반환."""
    tickers = list(tickers)
    df = yf.download(
        tickers=" ".join(tickers), start=start, period=period,
        interval="1d", auto_adjust=False, progress=False, group_by="ticker",
        threads=True,
    )
    out = {}
    for t in tickers:
        try:
            sub = df[t] if len(tickers) > 1 else df
            close = sub["Close"].dropna()
            if len(close) == 0:
                continue
            volume = sub["Volume"].reindex(close.index) if "Volume" in sub else None
            out[t] = {"close": close, "volume": volume}
        except Exception:
            continue
    return out


def pct(a, b):
    return (a - b) / b * 100.0


def _ret(close, n):
    """n 거래일 전 종가 대비 수익률(%). 이력이 모자라면 None."""
    if len(close) <= n:
        return None
    return round(pct(float(close.iloc[-1]), float(close.iloc[-1 - n])), 2)


def _ret_at(close, n, offset):
    """offset 거래일 전 시점에서 본 n 거래일 수익률(%). 순위의 '변화'를 내기 위한 것."""
    if len(close) <= n + offset:
        return None
    return pct(float(close.iloc[-1 - offset]), float(close.iloc[-1 - offset - n]))


def _vs_ma(close, n):
    """n일 단순이동평균 대비 이격도(%). 이력이 모자라면 None."""
    if len(close) < n:
        return None
    return round(pct(float(close.iloc[-1]), float(close.iloc[-n:].mean())), 2)


def trend_metrics(close, volume=None):
    """추세·상대강도 지표 묶음.

    독자의 전략이 추세추종·섹터 상대강도인데 1일 등락률만 모델에 주면 '오늘 올랐다'
    이상을 쓸 수 없다. 다기간 수익률·이동평균 위치·52주 고점 거리·거래량 배수를 함께
    실어야 오늘의 등락이 추세의 연장인지 반전인지 판단할 근거가 생긴다.
    이력이 모자라 계산되지 않는 항목은 None으로 두고 직렬화 단계에서 제거한다
    (모델에게 'N/A 목록'을 읽히지 않기 위해서).
    """
    m = {
        "chg_pct_d": _ret(close, 1),
        "ret_5d": _ret(close, 5),
        "ret_20d": _ret(close, 20),
        "ret_60d": _ret(close, 60),
        "vs_ma50": _vs_ma(close, 50),
        "vs_ma200": _vs_ma(close, 200),
    }
    if len(close) >= 200:
        m["ma50_over_ma200"] = bool(
            float(close.iloc[-50:].mean()) > float(close.iloc[-200:].mean())
        )
    if len(close) >= 60:
        win = close.iloc[-TRADING_DAYS_52W:]
        px = float(close.iloc[-1])
        m["pct_from_52w_high"] = round(pct(px, float(win.max())), 2)
        m["pct_above_52w_low"] = round(pct(px, float(win.min())), 2)
    if volume is not None:
        v = volume.dropna()
        if len(v) >= 21:
            avg20 = float(v.iloc[-21:-1].mean())
            if avg20 > 0:
                m["vol_x_avg20"] = round(float(v.iloc[-1]) / avg20, 2)
    return m


def strip_none(d):
    return {k: v for k, v in d.items() if v is not None}


def brief_row(m, *extra):
    """watchlist_all에 전문이 이미 실려 있으므로, 편의용 목록은 필요한 필드만 싣는다."""
    keys = ("ticker", "name", "group", "chg_pct_d") + extra
    return {k: m[k] for k in keys if k in m}


def attach_rs_rank(rows, hist, bench_close, lookback=20, ago=5):
    """20일 상대강도 순위와 ago 거래일 전 순위, 그 변화량을 각 행에 붙인다.

    순위의 절대값보다 '변화'가 중요하다. 오늘 1등이어도 20일 RS 순위가 계속 하위권이면
    주도가 아니라 반등이고, 순위가 며칠째 올라오는 섹터가 자금이 실제로 옮겨가는 곳이다.
    """
    def ranked(offset):
        b = _ret_at(bench_close, lookback, offset)
        if b is None:
            return {}
        vals = []
        for r in rows:
            h = hist.get(r["ticker"])
            v = _ret_at(h["close"], lookback, offset) if h else None
            if v is not None:
                vals.append((r["ticker"], v - b))
        vals.sort(key=lambda x: x[1], reverse=True)
        return {t: i + 1 for i, (t, _) in enumerate(vals)}

    now, before = ranked(0), ranked(ago)
    for r in rows:
        cur = now.get(r["ticker"])
        if cur is None:
            continue
        r["rs_rank_20d"] = cur
        prev = before.get(r["ticker"])
        if prev is not None:
            r["rs_rank_20d_5d_ago"] = prev
            r["rs_rank_change_5d"] = prev - cur      # 양수면 순위 상승


def latest_session_info(closes_gspc):
    """S&P500 시계열로 최근 마감 세션(T)과 T-1 날짜를 확정."""
    dates = list(closes_gspc.index)
    t_date = dates[-1].date()
    t1_date = dates[-2].date() if len(dates) >= 2 else None
    return t_date, t1_date


# 지수와 종목은 서로 다른 다운로드로 받으므로 같은 시점의 데이터라는 보장이 없다.
# 실제로 지수는 T 세션, 종목은 T-1 세션인 채로 브리핑이 발송된 적이 있다(2026-09-08):
# 지수 등락률은 09-08, 개별 종목 등락률은 전부 09-04였고, 서사의 대부분이 다른 날
# 이야기였다. 어긋남은 예외를 던지지 않는다 — 각 시계열은 자기 마지막 봉으로 정상
# 계산되고, 결과물은 올바른 브리핑과 똑같은 모습을 한다. 그래서 검사가 필요하다.
ALIGN_MAX_WAIT_MIN = int(os.environ.get("ALIGN_MAX_WAIT_MIN", "35"))
ALIGN_POLL_SEC = int(os.environ.get("ALIGN_POLL_SEC", "120"))
ALIGN_MIN_RATIO = float(os.environ.get("ALIGN_MIN_RATIO", "0.9"))


def _last_date(series):
    return series.index[-1].date()


def fetch_aligned(start, equities):
    """지수와 종목을 같은 세션으로 맞춰 받는다. 맞을 때까지 기다렸다 다시 받는다.

    상장폐지·거래정지 등으로 개별 종목 몇 개가 뒤처지는 것은 정상이므로 비율로 본다.
    대다수가 뒤처져 있으면 데이터가 아직 안 올라온 것이니 기다린다. 끝내 맞지 않으면
    틀린 브리핑을 보내느니 예외를 올린다 — 조용히 섞인 브리핑은 정상과 구분되지 않아
    독자가 잘못된 수치를 사실로 읽게 된다.
    """
    deadline = time.time() + ALIGN_MAX_WAIT_MIN * 60
    attempt = 0
    while True:
        attempt += 1
        idx = download_history(list(INDICES.keys()), start=start)
        if BENCHMARK not in idx or len(idx[BENCHMARK]["close"]) < 2:
            raise RuntimeError("지수 데이터 수집 실패(^GSPC)")
        bench_close = idx[BENCHMARK]["close"]
        t_date = _last_date(bench_close)

        hist = download_history(equities, start=start)
        usable = {t: h for t, h in hist.items() if len(h["close"]) >= 2}
        aligned = {t: h for t, h in usable.items() if _last_date(h["close"]) == t_date}
        stale = sorted(set(usable) - set(aligned))
        ratio = (len(aligned) / len(usable)) if usable else 0.0
        print(f"정합성 점검 {attempt}회차: 기준 세션 {t_date}, "
              f"종목 {len(aligned)}/{len(usable)} 정렬({ratio:.0%})"
              + (f", 뒤처진 종목 {len(stale)}개" if stale else ""), file=sys.stderr)

        if usable and ratio >= ALIGN_MIN_RATIO:
            if stale:
                print(f"  뒤처져 제외: {', '.join(stale[:10])}"
                      + (" …" if len(stale) > 10 else ""), file=sys.stderr)
            return idx, bench_close, t_date, aligned, stale, attempt

        if time.time() >= deadline:
            raise RuntimeError(
                f"지수는 {t_date} 세션인데 종목 시세 {len(usable) - len(aligned)}/{len(usable)}개가 "
                f"이전 세션에 머물러 있습니다({ALIGN_MAX_WAIT_MIN}분 대기 후에도 해소되지 않음). "
                "서로 다른 날짜를 섞은 브리핑을 보내지 않기 위해 중단합니다.")
        time.sleep(ALIGN_POLL_SEC)


def collect_market_data():
    now_et = datetime.now(ET)
    year = now_et.year
    start = (now_et - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")

    # 1) 지수와 종목을 같은 세션으로 정렬해 받는다(위 fetch_aligned 주석 참고).
    equities = sorted(set(MOVER_WATCHLIST) | set(NASDAQ_MEGACAP_CANDIDATES) | set(SECTOR_ETFS))
    idx, bench_close, t_date, hist, stale_tickers, align_attempts = fetch_aligned(start, equities)
    t1_date = latest_session_info(bench_close)[1]
    bench = trend_metrics(bench_close)
    metrics = {
        t: trend_metrics(h["close"], h.get("volume"))
        for t, h in hist.items() if len(h["close"]) >= 2
    }

    def rs(m, field="ret_20d"):
        """벤치마크(S&P500) 대비 초과수익(%p) — 섹터 상대강도의 핵심 축."""
        if m.get(field) is None or bench.get(field) is None:
            return None
        return round(m[field] - bench[field], 2)

    indicators = []
    for ticker, name in INDICES.items():
        h = idx.get(ticker)
        if h is None or len(h["close"]) < 2:
            indicators.append({"ticker": ticker, "name": name, "error": "N/A"})
            continue
        s = h["close"]
        m = trend_metrics(s)
        t_close = float(s.iloc[-1])
        prev_year = s[s.index.year == (year - 1)]
        ye_close = float(prev_year.iloc[-1]) if len(prev_year) else None
        row = {"ticker": ticker, "name": name, "close": round(t_close, 3)}
        if ticker == "^TNX":                      # 금리는 bp 변동으로 읽는다
            row["chg_bp_d"] = round((t_close - float(s.iloc[-2])) * 100, 1)
            if len(s) > 20:
                row["chg_bp_20d"] = round((t_close - float(s.iloc[-21])) * 100, 1)
            if ye_close:
                row["chg_bp_ytd"] = round((t_close - ye_close) * 100, 1)
        else:
            row["chg_pct_d"] = m.get("chg_pct_d")
            row["ret_20d"] = m.get("ret_20d")
            row["ret_60d"] = m.get("ret_60d")
            if ye_close:
                row["chg_pct_ytd"] = round(pct(t_close, ye_close), 2)
        row["vs_ma50"] = m.get("vs_ma50")
        row["vs_ma200"] = m.get("vs_ma200")
        row["pct_from_52w_high"] = m.get("pct_from_52w_high")
        # 지수끼리도 갱신 시점이 다를 수 있다. 기준 세션과 다르면 숨기지 말고 밝힌다.
        if _last_date(s) != t_date:
            row["as_of"] = str(_last_date(s))
        indicators.append(strip_none(row))

    # 2) 섹터 ETF — 1일 등락에 다기간 수익률·RS·이동평균 위치·RS 순위 변화를 더한다.
    sectors = []
    for ticker, name in SECTOR_ETFS.items():
        m = metrics.get(ticker)
        if not m or m.get("chg_pct_d") is None:
            continue
        row = {"ticker": ticker, "name": name}
        row.update(m)
        row["rs_20d_vs_spx"] = rs(m, "ret_20d")
        row["rs_60d_vs_spx"] = rs(m, "ret_60d")
        sectors.append(strip_none(row))
    attach_rs_rank(sectors, hist, bench_close)
    sectors.sort(key=lambda x: x["chg_pct_d"], reverse=True)

    # 3) 나스닥 시총 상위 10 (+ 추세 지표)
    megacaps = []
    for t in NASDAQ_MEGACAP_CANDIDATES:
        m = metrics.get(t)
        if not m or m.get("chg_pct_d") is None:
            continue
        try:
            mcap = yf.Ticker(t).fast_info.get("marketCap")
        except Exception:
            mcap = None
        if not mcap:
            continue
        # 개별 종목의 종가는 브리핑에 쓰지 않으므로 아예 싣지 않는다.
        row = {"ticker": t, "name": TICKER_NAMES.get(t, t), "market_cap": mcap}
        row.update(m)
        row["rs_20d_vs_spx"] = rs(m)
        megacaps.append(strip_none(row))
    megacaps.sort(key=lambda x: x["market_cap"], reverse=True)
    megacaps = megacaps[:10]

    # 4) 워치리스트 급등락 스캔 — 전 종목을 그대로 실어 보낸다.
    #    상위/하위 몇 개만 보내면 "반도체가 오른 날 하락한 SaaS" 같은 로테이션 서사를 쓸
    #    근거가 모델에 도달하지 않는다. 테마 반대편이면 하락폭이 작아도 중요한 종목이다.
    movers = []
    for t in sorted(set(MOVER_WATCHLIST)):
        m = metrics.get(t)
        if not m or m.get("chg_pct_d") is None:
            continue
        row = {"ticker": t, "name": TICKER_NAMES.get(t, t), "group": TICKER_GROUP.get(t, "기타")}
        row.update(m)
        row["rs_20d_vs_spx"] = rs(m)
        # 94행에 실리는 필드는 최소로. 52주 저점 대비는 스핀오프 종목에서 네 자릿수가
        # 나와 오독을 부르고, 정배열 여부는 vs_ma50/vs_ma200 부호로 이미 읽힌다.
        row.pop("pct_above_52w_low", None)
        row.pop("ma50_over_ma200", None)
        movers.append(strip_none(row))
    movers.sort(key=lambda x: x["chg_pct_d"], reverse=True)

    gainers = [brief_row(m, "ret_20d", "vs_ma200") for m in movers[:10]]
    losers = [brief_row(m, "ret_20d", "vs_ma200") for m in reversed(movers[-10:])]

    # 오늘의 등락 순위와 '20일 추세' 순위는 서로 다른 순위다.
    # 둘을 나란히 줘야 모델이 연장/반전을 가를 수 있다.
    by_ret20 = sorted(
        [m for m in movers if m.get("ret_20d") is not None],
        key=lambda x: x["ret_20d"], reverse=True,
    )
    momentum_leaders = [brief_row(m, "ret_20d", "ret_60d", "rs_20d_vs_spx", "vs_ma200")
                        for m in by_ret20[:10]]
    momentum_laggards = [brief_row(m, "ret_20d", "ret_60d", "rs_20d_vs_spx", "vs_ma200")
                         for m in reversed(by_ret20[-10:])]
    volume_surges = [brief_row(m, "vol_x_avg20", "ret_20d") for m in sorted(
        [m for m in movers if m.get("vol_x_avg20")],
        key=lambda x: x["vol_x_avg20"], reverse=True,
    )[:10]]

    # 그룹별 집계 — 그날 자금이 어느 테마에서 어느 테마로 돌았는지, 그리고 그 이동이
    # 하루짜리인지 20일째 이어지는 추세인지를 함께 드러낸다.
    group_perf = []
    for g in MOVER_WATCHLIST_GROUPS:
        members = [m for m in movers if m["group"] == g]
        if not members:
            continue

        def avg(field, _members=members):
            vals = [m[field] for m in _members if m.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        with50 = [m for m in members if m.get("vs_ma50") is not None]
        with200 = [m for m in members if m.get("vs_ma200") is not None]
        group_perf.append(strip_none({
            "group": g,
            "members": len(members),
            "avg_chg_pct_d": avg("chg_pct_d"),
            "avg_ret_5d": avg("ret_5d"),
            "avg_ret_20d": avg("ret_20d"),
            "avg_ret_60d": avg("ret_60d"),
            "avg_rs_20d_vs_spx": avg("rs_20d_vs_spx"),
            "up": sum(1 for m in members if m["chg_pct_d"] > 0),
            "down": sum(1 for m in members if m["chg_pct_d"] < 0),
            "above_ma50": sum(1 for m in with50 if m["vs_ma50"] > 0),
            "above_ma50_of": len(with50) or None,
            "above_ma200": sum(1 for m in with200 if m["vs_ma200"] > 0),
            "above_ma200_of": len(with200) or None,
        }))
    for i, g in enumerate(sorted([g for g in group_perf if g.get("avg_ret_20d") is not None],
                                key=lambda x: x["avg_ret_20d"], reverse=True), 1):
        g["momentum_rank_20d"] = i
    group_perf.sort(key=lambda x: x.get("avg_chg_pct_d", 0), reverse=True)

    # 5) 시장 폭 — 오늘의 등락과 별개로, 워치리스트가 추세 위에 서 있는지를 본다.
    with50 = [m for m in movers if m.get("vs_ma50") is not None]
    with200 = [m for m in movers if m.get("vs_ma200") is not None]
    breadth = {
        "universe": len(movers),
        "advancers": sum(1 for m in movers if m["chg_pct_d"] > 0),
        "decliners": sum(1 for m in movers if m["chg_pct_d"] < 0),
        "above_ma50": sum(1 for m in with50 if m["vs_ma50"] > 0),
        "above_ma50_of": len(with50),
        "above_ma200": sum(1 for m in with200 if m["vs_ma200"] > 0),
        "above_ma200_of": len(with200),
        "within_3pct_of_52w_high": [
            m["ticker"] for m in movers
            if m.get("pct_from_52w_high") is not None and m["pct_from_52w_high"] >= -3
        ],
        # 행에서 뺀 필드라 metrics에서 직접 읽는다.
        "within_10pct_of_52w_low": [
            m["ticker"] for m in movers
            if (metrics[m["ticker"]].get("pct_above_52w_low") or 999) <= 10
        ],
    }

    return {
        "session_date": str(t_date),
        "prev_session_date": str(t1_date),
        "data_alignment": strip_none({
            "aligned_tickers": len(metrics),
            "excluded_stale_tickers": stale_tickers or None,
            "fetch_attempts": align_attempts,
            "note": ("모든 종목 수치는 session_date 세션 기준으로 정렬되어 있다. "
                     "이전 세션에 머물러 있던 종목은 집계에서 제외했다."),
        }),
        "benchmark": strip_none({"ticker": BENCHMARK, "name": "S&P500(상대강도 기준)", **bench}),
        "indicators": indicators,
        "sectors_by_daily_change": sectors,
        "nasdaq_top10_by_mcap": megacaps,
        "market_breadth": breadth,
        "watchlist_group_performance": group_perf,
        "watchlist_top_gainers": gainers,
        "watchlist_top_losers": losers,
        "watchlist_momentum_leaders": momentum_leaders,
        "watchlist_momentum_laggards": momentum_laggards,
        "watchlist_volume_surges": volume_surges,
        "watchlist_all": movers,
        "field_guide": {
            "chg_pct_d": "직전 세션 1일 등락률(%)",
            "ret_5d / ret_20d / ret_60d": "5·20·60 거래일 전 종가 대비 수익률(%). 20일이 중기 추세의 기본 축이다.",
            "vs_ma50 / vs_ma200": "50일·200일 단순이동평균 대비 이격도(%). 양수면 추세 위, 음수면 추세 아래.",
            "ma50_over_ma200": "true면 50일선이 200일선 위(정배열).",
            "pct_from_52w_high": "52주 고점 대비 위치(%). 0에 가까울수록 신고가권.",
            "pct_above_52w_low": "52주 저점 대비 위치(%).",
            "vol_x_avg20": "직전 세션 거래량 ÷ 직전 20일 평균 거래량. 2 이상이면 이벤트가 있었다는 신호.",
            "rs_20d_vs_spx / rs_60d_vs_spx": "해당 기간 수익률에서 S&P500 같은 기간 수익률을 뺀 값(%p). 상대강도.",
            "rs_rank_20d / rs_rank_20d_5d_ago / rs_rank_change_5d": "섹터 ETF의 20일 상대강도 순위, 5거래일 전 순위, 그 변화(양수면 순위 상승).",
            "momentum_rank_20d": "워치리스트 테마 그룹의 20일 평균 수익률 순위.",
            "market_breadth": "워치리스트 전체의 상승/하락 종목 수, 50·200일선 위 종목 수, 52주 고점·저점 근접 종목.",
        },
        "note": ("watchlist_all이 워치리스트 전 종목의 등락률·추세 지표·소속 그룹이다(1일 등락률 내림차순). "
                 "top_gainers/top_losers/momentum_*/volume_surges는 그 중 일부를 뽑아둔 편의용 목록일 뿐이며, "
                 "서사를 쓸 때는 반드시 watchlist_all 전체를 보고 판단할 것. "
                 "오늘의 등락률 순위와 20일 추세 순위는 서로 다르며, 그 차이가 '추세의 연장인가 반전인가'를 "
                 "가르는 근거다. 모두 대형주 워치리스트 기준이라 시장 전체 순위와는 다르다."),
    }


# ── 브리핑 생성 ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 한국 은행 자금부의 시니어 마켓 데스크입니다. 독자는 21년차 뱅커이자
추세추종·섹터 상대강도 전략을 쓰는 개인투자자입니다. 제공된 실측 데이터(JSON)를 바탕으로
직전 미국 정규장 마감 브리핑을 '이해하기 쉬운 서사형'으로 작성하세요.

데이터에는 1일 등락률뿐 아니라 5·20·60일 수익률, 50·200일선 대비 이격도, 52주 고점 대비
위치, 거래량 배수, S&P500 대비 상대강도(rs_20d_vs_spx), 섹터 상대강도 순위와 그 5일 변화가
함께 들어 있습니다(각 필드의 뜻은 field_guide 참고). 독자의 전략이 추세추종·섹터 상대강도이므로
★모든 해석은 "오늘 하루의 움직임이 추세의 연장인가, 반전의 시작인가"에 답해야 합니다.
하루 등락률만 나열하고 추세를 언급하지 않은 브리핑은 실패입니다.

규칙:
- 반드시 한국어. 텔레그램 발송용이므로 마크다운 표 대신 줄단위 텍스트/이모지 사용.
- HTML 태그는 <b>, <i>만 사용 가능(텔레그램 HTML 모드). 다른 태그 금지.
- 개별 종목은 <b>회사명(티커)</b> 뒤에 등락률·추세 수치만 표기한다. 종목의 주가(종가·달러 금액)는
  브리핑 어디에도 쓰지 말 것. 지수·금리·유가·달러인덱스·VIX의 수치는 ②에 그대로 표기한다.
- 수치 표기 약속(줄이 길어지지 않게 이 축약형을 쓸 것):
  1일 등락률은 그냥 +2.1%, 20일 수익률은 20d +8.4%, 200일선 대비는 200일선 +12%,
  상대강도는 RS +5.2%p, 거래량은 거래량 2.3배.
- 구성:
  ①오늘의 서사(4~6문장) — 그날 시장을 움직인 힘을 이야기로: 금리 방향과 그 이유, 지수 간
    breadth의 의미(소형주와 대형주의 엇갈림을 어떻게 읽을지), 유가·달러인덱스·VIX의 흐름과
    함의(달러 강세/약세가 위험자산·원자재에 주는 압력, VIX 수준이 말하는 경계심)를 반드시 해석에
    포함. 여기에 더해 market_breadth의 50일선·200일선 위 종목 비율을 근거로 오늘의 움직임이
    시장 전반의 추세와 같은 방향인지 다른 방향인지 한 문장으로 못박을 것.
  ②핵심 지표 — 10년물(bp)·달러인덱스·WTI·다우·S&P500·나스닥·러셀2000·VIX를
    '전일 / 20일 / 연초' 순으로 나열하고, 지수에는 200일선 대비 위치를 괄호로 덧붙인다.
  ③나스닥 시총 상위 10 등락 — 종목마다 1일 등락률과 20d 수익률을 병기하고,
    수치 뒤에 갈림의 축을 해석 1~2문장.
  ④섹터 3블록 — 브리핑의 심장이며 매일의 핵심. 개별 종목을 먼저 늘어놓지 말고, 반드시
    '섹터 → 그 섹터 안의 종목 → 서사' 순서로 쓴다. 아래 3블록을 이 순서 그대로,
    매일 똑같은 형식으로 반복할 것. 각 블록은 예외 없이 다음 (a)(b)(c) 3단으로 구성한다:
      (a) 섹터 헤더 한 줄 — <b>섹터/테마명</b> 오늘 평균 등락률, 상승·하락 종목 수,
          20d 평균 수익률, RS ±%p, 그리고 대응 섹터 ETF의 RS 순위와 5일 전 대비 변화
          (rs_rank_change_5d가 양수면 ↑n, 음수면 ↓n)
      (b) 그 섹터 안의 종목을 등락률 순으로 최소 4개 —
          <b>회사명(티커)</b> 1일 등락률 · 20d 수익률 · 200일선 대비 형식으로 한 줄씩
      (c) 서사 3~5문장 — 이 섹터가 왜 그렇게 움직였는지. 종목별 촉매(실적, 수주,
          가이던스, 애널리스트 코멘트, 수급, 매크로)를 하나의 흐름으로 엮을 것.
          ★마지막 한 문장은 반드시 추세 판단이다: 오늘의 움직임이 20일 추세의 연장인지,
          되돌림인지, 추세 전환의 초기 신호인지를 20d 수익률·200일선 위치·RS 순위 변화를
          근거로 단정할 것. 종목 나열의 반복으로 끝내지 말 것.

    [블록 1] 오늘 가장 많이 오른 섹터 — 무조건 이 블록으로 ④를 시작한다.
      watchlist_group_performance의 1위 그룹과 sectors_by_daily_change의 1위 섹터를 함께
      보고 그날의 주도 섹터를 정한다. 이 섹터가 그날 시장의 진앙지다.
      ★단, avg_chg_pct_d(오늘)만으로 정하지 말 것. avg_ret_20d·avg_rs_20d_vs_spx·
      rs_rank_change_5d를 함께 보고, 오늘 1등이지만 20일 RS가 여전히 하위권이면 '주도'가
      아니라 '낙폭과대 반등'이라고 명시할 것. (a)(b)(c)를 모두 채우고 촉매를 밝힌다.

    [블록 2] 확산 섹터 — 블록 1의 테마가 밸류체인을 타고 번진 섹터를 동일한 (a)(b)(c)
      구조로 한 번 더 쓴다(예: 반도체 랠리가 전력·냉각·광통신·스토리지·네트워크 장비로
      확산). 확산이 없었던 날이면 "오늘은 확산 없이 주도 섹터에 국한된 랠리였다"고
      한 줄로 명시하고 넘어간다. 없는 확산을 억지로 만들지 말 것.

    [블록 3] 오늘 가장 많이 하락한 반대편 섹터 — 절대 생략하거나 한두 줄로 줄이지 말 것.
      watchlist_group_performance의 최하위 그룹을 기준으로 동일한 (a)(b)(c) 구조로 쓴다.
      ★필수: watchlist_all(워치리스트 전 종목의 등락률·추세 지표·소속 그룹) 전체를 보고 그
      그룹에서 하락한 종목을 최소 4개 고른다. watchlist_top_losers 목록에 없더라도 그 그룹
      소속이면 하락폭이 작아도 반드시 포함한다(하락폭 상위 몇 개만 훑는 것은 이 블록의 실패다).
      (c) 서사에서는 왜 같은 날 같은 방향으로 팔렸는지를 자금 이동으로 설명한다
      (로테이션, 멀티플 압축, 금리, AI가 기존 소프트웨어의 해자를 잠식한다는 침식 서사 등).
      ★그리고 이 하락이 '추세 안에서의 조정'인지 '추세 이탈'인지를 200일선 대비 위치와
      20d 수익률로 구분해 명시할 것. 그 진영 안에서 홀로 오른 예외 종목이 있으면 이유와 함께 짚는다.

  ⑤추세·상대강도 스코어보드 — ④를 모두 채운 뒤, 표가 아닌 6~10줄의 압축 목록으로:
    · 20일 RS 상위 3섹터와 하위 3섹터(각각 순위 변화를 ↑n/↓n로 함께)
    · 5일 사이 RS 순위가 가장 크게 오른 섹터와 가장 크게 내린 섹터 각 1개 + 그 의미 한 줄
    · watchlist_momentum_leaders에서 3종목, watchlist_momentum_laggards에서 3종목
      (회사명(티커) 20d 수익률 · RS)
    · market_breadth 요약 한 줄 — 50일선 위 n/N, 200일선 위 n/N, 52주 고점 3% 이내 n종목
    · watchlist_volume_surges 중 거래량 2배 이상인 종목과 그것이 뜻하는 바 한 줄
      (거래량 없는 상승은 신뢰도가 낮다는 관점을 포함)
  ⑥메가캡의 그늘과 개별 드라마 — 위를 모두 채운 뒤에만, 짧게. 시총 상위 종목 중 크게 움직인
    종목의 개별 스토리(경영진 교체, 신제품 실망, 실적 등)와, 섹터 흐름과 무관하게 자기만의
    이유(규제 이슈, 가이던스 쇼크, M&A, 밈주 수급)로 급등락한 종목 2~3개를
    회사명+티커+등락률+이유로 서술.
  ⑦투자 관점 해석 — 시장 분위기 / 섹터 상대강도(자금이 어느 섹터에서 어느 섹터로 옮겨가는
    중인지 RS 순위 변화를 근거로 명시) / 매크로 / 대중 기대감 / 한국시장 함의 1줄.
    마지막에 추세추종 관점으로 '지금 추세가 살아 있는 진영'과 '추세가 꺾인 진영'을
    각각 한 문장씩 구분해 줄 것.
- 웹검색이 가능하면 급등락 '이유'(실적, 뉴스, 지표)를 확인해 서사에 녹일 것. 확인 안 되는
  이유는 추정하지 말고 수치만 기술.
- ★검색 결과의 영어 원문을 그대로 옮겨 적지 말 것. 반드시 한국어로 소화해 서술한다.
  브리핑 전체에 영어 문장이 한 줄도 있어서는 안 된다(회사명·티커 표기는 예외).
- 제공된 수치를 임의로 바꾸지 말 것. 없는 수치는 만들지 말 것. 데이터에 없는 필드(이력 부족으로
  빠진 항목)는 언급하지 말고 있는 지표로만 판단할 것.
- 마지막에 '투자 권유가 아닌 정보 제공 목적' 1줄.
- ④의 3블록에 지면을 가장 많이 쓸 것. ⑤는 압축적으로. ①②③⑥⑦이 길어져 ④가 밀리면 실패한 브리핑이다.
- 전체 길이는 텔레그램 3~5개 메시지 이내(약 10,000자 이내).
- ★①부터 ⑦까지와 마지막 면책 문구를 반드시 완결할 것. 분량이 부족하다고 느껴지면
  ⑤⑥⑦을 짧게 압축하되, 섹션을 통째로 빠뜨리거나 문장 중간에서 멈추지 말 것.
  중간에서 끊긴 브리핑은 어떤 이유로도 허용되지 않는다."""


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MAX_CONTINUATIONS = 2      # 이어쓰기 재요청 횟수 상한(원 호출 + 최대 2회)
PREAMBLE_MAX = 300         # 검색 직전 안내 멘트로 간주할 text 블록의 최대 길이


def _extract_brief_text(content):
    """응답 블록에서 브리핑 본문만 모은다.

    웹검색을 쓰면 "검색해 보겠습니다" 같은 짧은 안내 멘트가 검색 호출 직전의 text 블록으로
    온다. 이전 구현은 '마지막 검색 이후의 텍스트만' 취해 이를 걸렀는데, 그 규칙은 모델이
    검색을 모두 끝낸 뒤에야 글을 쓴다는 전제에 기대고 있었다. 지금 프롬프트는 섹터마다
    촉매를 확인하라고 요구하므로 검색은 작성 도중에도 일어나고, 그러면 검색 이전에 쓴
    본문이 통째로 버려진다. 그래서 규칙을 뒤집는다 — 검색 호출 '바로 앞'의 '짧은' text
    블록만 안내 멘트로 보고 버리고, 나머지는 모두 본문으로 취급한다.
    thinking 블록은 type이 text가 아니므로 자연히 제외된다.
    """
    parts = []
    for i, block in enumerate(content):
        if block.get("type") != "text":
            continue
        text = block.get("text", "")
        nxt = content[i + 1] if i + 1 < len(content) else None
        if nxt and nxt.get("type") == "server_tool_use" and len(text.strip()) <= PREAMBLE_MAX:
            continue
        if text.strip():
            parts.append(text)
    return "\n".join(parts).strip()


def _call_claude(api_key, body):
    """SSE 스트리밍으로 호출하고 {"content": [...], "stop_reason": ...} 로 되돌린다.

    비스트리밍으로는 큰 max_tokens를 쓸 수 없다. 이 요청은 adaptive thinking + 웹검색 +
    1만 자 본문이 한 응답에 들어가므로 생성에 수 분이 걸리고, 비스트리밍 요청은 그 구간에서
    HTTP 타임아웃에 걸린다. 스트리밍은 그 제약을 없애준다.
    """
    resp = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=dict(body, stream=True),
        timeout=(30, 900),      # (연결, 청크 간 무응답) — 총 소요시간 제한이 아니다
        stream=True,
    )
    resp.raise_for_status()

    content, stop_reason = [], None
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        etype = ev.get("type")
        idx = ev.get("index")
        if etype == "content_block_start" and idx is not None:
            while len(content) <= idx:
                content.append({})
            content[idx] = dict(ev.get("content_block", {}))
        elif etype == "content_block_delta" and idx is not None and idx < len(content):
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                content[idx]["text"] = content[idx].get("text", "") + delta.get("text", "")
        elif etype == "message_delta":
            stop_reason = ev.get("delta", {}).get("stop_reason", stop_reason)
        elif etype == "error":
            raise RuntimeError(f"Claude 스트림 오류: {ev.get('error')}")
    return {"content": content, "stop_reason": stop_reason}


def run_claude_brief(system_prompt, user_text, max_uses=5, default_max_tokens="32000"):
    """system_prompt + user_text로 브리핑 본문을 만들어 돌려준다(실패/키없음이면 None).

    미국 브리핑과 한국 브리핑(kr_main.py)이 같은 호출 경로를 쓰기 위한 공용 진입점이다.
    스트리밍·이어쓰기·검색 안내멘트 제거는 실패를 겪으며 다듬어진 로직이라 브리핑마다
    복제하면 이후 수정이 한쪽에만 반영된다. 프롬프트만 갈아끼우고 경로는 하나로 둔다.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    tools = []
    if os.environ.get("USE_WEB_SEARCH", "1") != "0":
        # 동적 필터링이 들어간 현행 변형. 구형 모델을 쓰게 되면 WEB_SEARCH_TOOL_TYPE로
        # web_search_20250305 으로 되돌릴 수 있다.
        tools = [{
            "type": os.environ.get("WEB_SEARCH_TOOL_TYPE", "web_search_20260209"),
            "name": "web_search",
            "max_uses": max_uses,
        }]

    user_turn = {"role": "user", "content": user_text}
    body = {
        "model": model,
        # max_tokens는 '본문 길이'가 아니라 한 응답이 생성하는 모든 토큰의 상한이다.
        # 여기에는 (1) adaptive thinking — 이 모델은 thinking을 생략해도 기본 동작이다,
        # (2) 웹검색 호출과 그 결과 블록, (3) 실제 브리핑 본문이 모두 들어간다.
        # 게다가 한국어는 글자당 1.5~2토큰이라 '1만 자'는 1.5만~2만 토큰이다.
        # 이 셋을 한 예산에 넣고 1.4만으로 잡았더니 사고와 검색이 먼저 예산을 쓰고
        # 브리핑은 ①만 나온 채 잘렸다. 상한일 뿐 실제 생성량만 과금된다.
        "max_tokens": int(os.environ.get("CLAUDE_MAX_TOKENS", default_max_tokens)),
        "system": system_prompt,
        "messages": [user_turn],
    }
    if tools:
        body["tools"] = tools

    # 예산을 늘려도 절단 가능성은 남는다(검색 결과 분량은 실행마다 다르다). 잘렸으면
    # 지금까지 쓴 본문을 assistant 턴으로 되돌려주고 끊긴 지점부터 이어 쓰게 한다.
    # 무인 실행이라 잘린 브리핑이 그대로 발송되는 것이 가장 나쁜 결과다.
    parts = []
    for attempt in range(MAX_CONTINUATIONS + 1):
        payload = _call_claude(api_key, body)
        chunk = _extract_brief_text(payload["content"])
        if chunk:
            parts.append(chunk)
        if payload["stop_reason"] != "max_tokens":
            break
        so_far = "".join(parts).rstrip()      # assistant 턴이 공백으로 끝나면 API가 거부한다
        if attempt == MAX_CONTINUATIONS or not so_far:
            print(f"경고: 이어쓰기 {attempt}회 후에도 응답이 max_tokens에서 잘렸습니다. "
                  "CLAUDE_MAX_TOKENS를 올리거나 SYSTEM_PROMPT의 분량 지시를 줄이세요.",
                  file=sys.stderr)
            break
        print(f"응답이 max_tokens에서 잘림 → 이어쓰기 요청 {attempt + 1}/{MAX_CONTINUATIONS}",
              file=sys.stderr)
        body["messages"] = [
            user_turn,
            {"role": "assistant", "content": so_far},
            {"role": "user", "content": (
                "위 브리핑이 분량 한도에 걸려 중간에서 끊겼습니다. 끊긴 바로 그 지점부터 "
                "이어서 나머지를 작성해 주세요. 이미 쓴 부분을 다시 쓰거나 요약하지 말고, "
                "머리말·사과·설명 없이 곧바로 이어지는 문장부터 시작하세요. "
                "마지막 면책 문구까지 반드시 완결할 것."
            )},
        ]

    # 이어쓰기 조각은 문장 중간에서 갈라진 것이므로 구분자 없이 그대로 붙인다.
    return "".join(parts).strip() or None


def build_brief_with_claude(data):
    return run_claude_brief(
        SYSTEM_PROMPT,
        f"기준 세션: {data['session_date']} (직전 세션 {data['prev_session_date']}).\n"
        "아래 실측 데이터로 브리핑을 작성해 주세요.\n\n"
        + json.dumps(data, ensure_ascii=False),
    )


def _fmt_trend(m):
    """폴백 브리핑에서 종목 한 줄 뒤에 붙일 추세 꼬리표. 없는 지표는 조용히 생략한다."""
    bits = []
    if m.get("ret_20d") is not None:
        bits.append(f"20d {m['ret_20d']:+.2f}%")
    if m.get("vs_ma200") is not None:
        bits.append(f"200일선 {m['vs_ma200']:+.1f}%")
    if m.get("rs_20d_vs_spx") is not None:
        bits.append(f"RS {m['rs_20d_vs_spx']:+.2f}%p")
    if m.get("vol_x_avg20") is not None:
        bits.append(f"거래량 {m['vol_x_avg20']:.1f}배")
    return f"  <i>{' · '.join(bits)}</i>" if bits else ""


def _rank_arrow(s):
    """RS 순위 변화를 ↑n/↓n로. 변화가 없거나 값이 없으면 빈 문자열."""
    d = s.get("rs_rank_change_5d")
    if not d:
        return ""
    return f" {'↑' if d > 0 else '↓'}{abs(d)}"


def build_fallback_brief(data):
    """API 키가 없거나 실패했을 때의 수치 위주 브리핑."""
    L = [f"📊 <b>미국 시장 브리핑 — {data['session_date']} (현지 마감)</b>", ""]
    L.append("<b>② 핵심 지표</b> <i>(전일 / 20일 / 연초)</i>")
    for r in data["indicators"]:
        if "error" in r:
            L.append(f"· {r['name']}: N/A")
            continue
        if r["ticker"] == "^TNX":
            d20 = f"{r['chg_bp_20d']:+.1f}bp" if r.get("chg_bp_20d") is not None else "N/A"
            ytd = f"{r['chg_bp_ytd']:+.1f}bp" if r.get("chg_bp_ytd") is not None else "N/A"
            L.append(f"· {r['name']}: {r['close']:.3f}%  {r.get('chg_bp_d', 0):+.1f}bp / {d20} / {ytd}")
        else:
            d20 = f"{r['ret_20d']:+.2f}%" if r.get("ret_20d") is not None else "N/A"
            ytd = f"{r['chg_pct_ytd']:+.2f}%" if r.get("chg_pct_ytd") is not None else "N/A"
            ma = f"  <i>200일선 {r['vs_ma200']:+.1f}%</i>" if r.get("vs_ma200") is not None else ""
            L.append(f"· {r['name']}: {r['close']:,.2f}  {r.get('chg_pct_d', 0):+.2f}% / {d20} / {ytd}{ma}")
    L.append("")

    b = data.get("market_breadth")
    if b:
        L.append("<b>시장 폭(워치리스트 기준)</b>")
        L.append(f"· 상승 {b['advancers']} / 하락 {b['decliners']} (전체 {b['universe']})")
        L.append(f"· 50일선 위 {b['above_ma50']}/{b['above_ma50_of']} · "
                 f"200일선 위 {b['above_ma200']}/{b['above_ma200_of']}")
        L.append(f"· 52주 고점 3% 이내 {len(b['within_3pct_of_52w_high'])}종목 · "
                 f"52주 저점 10% 이내 {len(b['within_10pct_of_52w_low'])}종목")
        L.append("")

    L.append("<b>섹터 일일 등락 / 20일 상대강도</b>")
    for s in data["sectors_by_daily_change"]:
        rs = f"RS {s['rs_20d_vs_spx']:+.2f}%p" if s.get("rs_20d_vs_spx") is not None else "RS N/A"
        rank = f", {s['rs_rank_20d']}위{_rank_arrow(s)}" if s.get("rs_rank_20d") else ""
        L.append(f"· {s['name']}: {s['chg_pct_d']:+.2f}%  <i>({rs}{rank})</i>")
    L.append("")

    L.append("<b>③ 나스닥 시총 상위 10</b>")
    for i, m in enumerate(data["nasdaq_top10_by_mcap"], 1):
        cap = f"${m['market_cap']/1e12:.2f}T" if m["market_cap"] >= 1e12 else f"${m['market_cap']/1e9:.0f}B"
        L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): {m['chg_pct_d']:+.2f}%"
                 f"  <i>{cap}</i>{_fmt_trend(m)}")
    L.append("")

    if data.get("watchlist_group_performance"):
        L.append("<b>테마별 오늘 / 20일 평균(워치리스트)</b>")
        for g in data["watchlist_group_performance"]:
            d20 = f"{g['avg_ret_20d']:+.2f}%" if g.get("avg_ret_20d") is not None else "N/A"
            ma = (f", 50일선 위 {g['above_ma50']}/{g['above_ma50_of']}"
                  if g.get("above_ma50_of") else "")
            L.append(f"· {g['group']}: {g['avg_chg_pct_d']:+.2f}% / 20d {d20}"
                     f"  <i>(상승 {g['up']}/하락 {g['down']}{ma})</i>")
        L.append("")

    L.append("<b>④ 워치리스트 급등 Top 10</b> <i>(대형주 워치리스트 기준)</i>")
    for i, m in enumerate(data["watchlist_top_gainers"], 1):
        L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): {m['chg_pct_d']:+.2f}%{_fmt_trend(m)}")
    L.append("")
    L.append("<b>급락 Top 10</b>")
    for i, m in enumerate(data["watchlist_top_losers"], 1):
        L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): {m['chg_pct_d']:+.2f}%{_fmt_trend(m)}")
    L.append("")

    if data.get("watchlist_momentum_leaders"):
        L.append("<b>⑤ 20일 추세 상위</b>")
        for i, m in enumerate(data["watchlist_momentum_leaders"], 1):
            L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): "
                     f"20d {m['ret_20d']:+.2f}%  <i>오늘 {m['chg_pct_d']:+.2f}%</i>")
        L.append("")
        L.append("<b>20일 추세 하위</b>")
        for i, m in enumerate(data["watchlist_momentum_laggards"], 1):
            L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): "
                     f"20d {m['ret_20d']:+.2f}%  <i>오늘 {m['chg_pct_d']:+.2f}%</i>")
        L.append("")

    surges = [m for m in data.get("watchlist_volume_surges", []) if m.get("vol_x_avg20", 0) >= 2]
    if surges:
        L.append("<b>거래량 급증(20일 평균 2배 이상)</b>")
        for m in surges:
            L.append(f"· {m.get('name', m['ticker'])} ({m['ticker']}): "
                     f"{m['vol_x_avg20']:.1f}배  {m['chg_pct_d']:+.2f}%")
        L.append("")

    L.append("<i>본 내용은 투자 권유가 아닌 정보 제공 목적입니다.</i>")
    return "\n".join(L)


# ── 텔레그램 발송 ────────────────────────────────────────────────────────────
# 텔레그램 HTML 모드에서 속성 없이 쓸 수 있는 태그만 허용한다.
ALLOWED_HTML_TAGS = ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre")


def escape_telegram_html(text):
    """허용 태그만 남기고 나머지 <, >, & 를 이스케이프한다.

    Claude가 생성한 본문에 부등호나 & 가 섞이면 텔레그램이 400을 돌려주고
    평문으로 재발송되면서 태그가 그대로 노출된다. 미리 막아 둔다.
    """
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for tag in ALLOWED_HTML_TAGS:
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return out


def split_for_telegram(text, limit=TELEGRAM_MAX - 100):
    """줄 단위로 묶되, 한 줄이 limit을 넘으면 강제로 쪼갠다.

    원본은 첫 줄이 limit보다 길면 빈 문자열을 chunk로 넣어 400(text is empty)으로
    발송 전체가 실패했고, limit을 넘는 줄은 아예 분할되지 않아 4096을 초과했다.
    """
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur = cur + "\n" + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = split_for_telegram(escape_telegram_html(text))

    for chunk in chunks:
        r = requests.post(url, data={
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=60)
        if not r.ok:  # HTML 파싱 오류 등 → 평문 재시도
            r = requests.post(url, data={
                "chat_id": chat_id, "text": chunk,
                "disable_web_page_preview": True,
            }, timeout=60)
        r.raise_for_status()


# ── 메인 ────────────────────────────────────────────────────────────────────
def main():
    now_et = datetime.now(ET)
    now_kst = datetime.now(KST)
    print(f"run at ET={now_et:%Y-%m-%d %H:%M} / KST={now_kst:%Y-%m-%d %H:%M}")

    try:
        data = collect_market_data()
    except Exception as e:
        # 수집 실패를 조용히 넘기지 않는다. 브리핑이 안 오는 것과 잘못된 브리핑이 오는 것
        # 중에서는 전자가 낫지만, 아무 소식도 없는 것이 가장 나쁘다.
        print(f"수집 실패: {e}", file=sys.stderr)
        try:
            send_telegram(f"🇺🇸 <b>미국 브리핑 생성 실패</b>\n<i>{e}</i>")
        except Exception:
            pass
        raise

    session = datetime.strptime(data["session_date"], "%Y-%m-%d").date()

    # 아침 7시(KST) 실행 시, 마감된 세션은 'ET 기준 오늘' 날짜여야 함.
    # 아니라면 그날 미국장은 휴장(공휴일)이었던 것.
    # FORCE_SEND=1이면 휴장 판정을 건너뛰고 직전 마감 세션으로 정상 브리핑을 만든다.
    # 주말·공휴일에도 전체 경로(Claude API·웹검색·발송)를 검증할 수 있게 하는 스위치.
    if session != now_et.date():
        if os.environ.get("FORCE_SEND") == "1":
            print(f"FORCE_SEND=1 → 휴장 판정을 무시하고 직전 마감 세션({session}) 기준으로 발송")
        elif os.environ.get("SKIP_ON_HOLIDAY") == "1":
            print(f"휴장 감지(최근 세션 {session}) → 발송 생략")
            return
        else:
            send_telegram(
                f"🇺🇸 어제({now_et:%m/%d} 현지)는 미국 증시 휴장일이었습니다.\n"
                f"직전 거래일은 {session}이며, 해당 브리핑은 이미 발송되었습니다."
            )
            return

    brief = None
    try:
        brief = build_brief_with_claude(data)
    except Exception as e:
        print(f"Claude API 실패 → 기본 브리핑으로 대체: {e}", file=sys.stderr)
    if not brief:
        brief = build_fallback_brief(data)

    send_telegram(brief)
    print("텔레그램 발송 완료")


if __name__ == "__main__":
    main()
