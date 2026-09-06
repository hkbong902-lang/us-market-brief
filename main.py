# -*- coding: utf-8 -*-
"""
US Market Daily Brief -> Telegram
- 직전 미국 정규장 마감 기준 데이터를 수집하고,
- Claude API로 서사형 한국어 브리핑을 작성한 뒤,
- 텔레그램 봇으로 발송한다.

필수 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수: ANTHROPIC_API_KEY (없으면 수치 위주 기본 브리핑으로 발송)
              CLAUDE_MODEL (기본: claude-sonnet-5)
              USE_WEB_SEARCH ("0"이면 뉴스 웹검색 비활성, 기본 활성)
              SKIP_ON_HOLIDAY ("1"이면 휴장일에 아무것도 안 보냄, 기본은 휴장 안내 발송)
              FORCE_SEND ("1"이면 휴장 판정을 무시하고 직전 마감 세션 브리핑을 발송. 테스트용)
"""

import json
import os
import sys
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
def download_closes(tickers, start=None, period=None):
    """yfinance 일봉 종가 DataFrame (index=date, columns=ticker)."""
    df = yf.download(
        tickers=" ".join(tickers), start=start, period=period,
        interval="1d", auto_adjust=False, progress=False, group_by="ticker",
        threads=True,
    )
    closes = {}
    for t in tickers:
        try:
            s = df[t]["Close"].dropna() if len(tickers) > 1 else df["Close"].dropna()
            if len(s) > 0:
                closes[t] = s
        except Exception:
            continue
    return closes


def pct(a, b):
    return (a - b) / b * 100.0


def latest_session_info(closes_gspc):
    """S&P500 시계열로 최근 마감 세션(T)과 T-1 날짜를 확정."""
    dates = list(closes_gspc.index)
    t_date = dates[-1].date()
    t1_date = dates[-2].date() if len(dates) >= 2 else None
    return t_date, t1_date


def collect_market_data():
    year = datetime.now(ET).year
    prev_year_end_start = f"{year - 1}-12-15"

    # 1) 지수/금리/유가: 전년 말부터 받아 T, T-1, 전년말 종가를 한 번에 확보
    idx = download_closes(list(INDICES.keys()), start=prev_year_end_start)
    if "^GSPC" not in idx or len(idx["^GSPC"]) < 2:
        raise RuntimeError("지수 데이터 수집 실패(^GSPC)")

    t_date, t1_date = latest_session_info(idx["^GSPC"])

    indicators = []
    for ticker, name in INDICES.items():
        s = idx.get(ticker)
        if s is None or len(s) < 2:
            indicators.append({"ticker": ticker, "name": name, "error": "N/A"})
            continue
        t_close = float(s.iloc[-1])
        t1_close = float(s.iloc[-2])
        prev_year = s[s.index.year == (year - 1)]
        ye_close = float(prev_year.iloc[-1]) if len(prev_year) else None
        row = {"ticker": ticker, "name": name, "close": round(t_close, 3)}
        if ticker == "^TNX":  # 금리는 bp 변동
            row["chg_bp_d"] = round((t_close - t1_close) * 100, 1)
            if ye_close:
                row["chg_bp_ytd"] = round((t_close - ye_close) * 100, 1)
        else:
            row["chg_pct_d"] = round(pct(t_close, t1_close), 2)
            if ye_close:
                row["chg_pct_ytd"] = round(pct(t_close, ye_close), 2)
        indicators.append(row)

    # 2) 섹터 ETF: 일일 등락률
    sec = download_closes(list(SECTOR_ETFS.keys()), period="10d")
    sectors = []
    for ticker, name in SECTOR_ETFS.items():
        s = sec.get(ticker)
        if s is None or len(s) < 2:
            continue
        sectors.append({
            "ticker": ticker, "name": name,
            "chg_pct_d": round(pct(float(s.iloc[-1]), float(s.iloc[-2])), 2),
        })
    sectors.sort(key=lambda x: x["chg_pct_d"], reverse=True)

    # 3) 나스닥 시총 상위 10 (+ 일일 등락률)
    mega_prices = download_closes(NASDAQ_MEGACAP_CANDIDATES, period="10d")
    megacaps = []
    for t in NASDAQ_MEGACAP_CANDIDATES:
        s = mega_prices.get(t)
        if s is None or len(s) < 2:
            continue
        try:
            mcap = yf.Ticker(t).fast_info.get("marketCap")
        except Exception:
            mcap = None
        # 개별 종목의 종가는 브리핑에 쓰지 않으므로 아예 싣지 않는다.
        megacaps.append({
            "ticker": t,
            "name": TICKER_NAMES.get(t, t),
            "chg_pct_d": round(pct(float(s.iloc[-1]), float(s.iloc[-2])), 2),
            "market_cap": mcap,
        })
    megacaps = [m for m in megacaps if m["market_cap"]]
    megacaps.sort(key=lambda x: x["market_cap"], reverse=True)
    megacaps = megacaps[:10]

    # 4) 워치리스트 급등락 스캔 — 전 종목을 그대로 실어 보낸다.
    #    상위/하위 몇 개만 보내면 "반도체가 오른 날 하락한 SaaS" 같은 로테이션 서사를 쓸
    #    근거가 모델에 도달하지 않는다. 테마 반대편이면 하락폭이 작아도 중요한 종목이다.
    wl = download_closes(sorted(set(MOVER_WATCHLIST)), period="10d")
    movers = []
    for t, s in wl.items():
        if len(s) < 2:
            continue
        movers.append({
            "ticker": t,
            "name": TICKER_NAMES.get(t, t),
            "group": TICKER_GROUP.get(t, "기타"),
            "chg_pct_d": round(pct(float(s.iloc[-1]), float(s.iloc[-2])), 2),
        })
    movers.sort(key=lambda x: x["chg_pct_d"], reverse=True)
    gainers = movers[:10]
    losers = list(reversed(movers[-10:]))

    # 그룹별 평균 등락률 — 그날 자금이 어느 테마에서 어느 테마로 돌았는지 드러낸다.
    group_perf = []
    for g in MOVER_WATCHLIST_GROUPS:
        vals = [m["chg_pct_d"] for m in movers if m["group"] == g]
        if vals:
            group_perf.append({
                "group": g,
                "avg_chg_pct_d": round(sum(vals) / len(vals), 2),
                "up": sum(1 for v in vals if v > 0),
                "down": sum(1 for v in vals if v < 0),
            })
    group_perf.sort(key=lambda x: x["avg_chg_pct_d"], reverse=True)

    return {
        "session_date": str(t_date),
        "prev_session_date": str(t1_date),
        "indicators": indicators,
        "sectors_by_daily_change": sectors,
        "nasdaq_top10_by_mcap": megacaps,
        "watchlist_group_performance": group_perf,
        "watchlist_top_gainers": gainers,
        "watchlist_top_losers": losers,
        "watchlist_all": movers,
        "note": ("watchlist_all이 워치리스트 전 종목의 등락률·소속 그룹이다(등락률 내림차순). "
                 "top_gainers/top_losers는 그 중 상·하위 10개를 뽑아둔 편의용 목록일 뿐이며, "
                 "서사를 쓸 때는 반드시 watchlist_all 전체를 보고 판단할 것. "
                 "모두 대형주 워치리스트 기준이라 시장 전체 순위와는 다르다."),
    }


# ── 브리핑 생성 ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 한국 은행 자금부의 시니어 마켓 데스크입니다. 독자는 21년차 뱅커이자
추세추종·섹터 상대강도 전략을 쓰는 개인투자자입니다. 제공된 실측 데이터(JSON)를 바탕으로
직전 미국 정규장 마감 브리핑을 '이해하기 쉬운 서사형'으로 작성하세요.

규칙:
- 반드시 한국어. 텔레그램 발송용이므로 마크다운 표 대신 줄단위 텍스트/이모지 사용.
- HTML 태그는 <b>, <i>만 사용 가능(텔레그램 HTML 모드). 다른 태그 금지.
- 개별 종목은 <b>회사명(티커)</b> 등락률 만 표기한다. 종목의 주가(종가·달러 금액)는
  브리핑 어디에도 쓰지 말 것. 지수·금리·유가·달러인덱스·VIX의 수치는 ②에 그대로 표기한다.
- 구성: ①오늘의 서사(그날 시장을 움직인 힘을 4~6문장의 이야기로: 금리 방향과 그 이유,
  지수 간 breadth의 의미 — 예: 소형주와 대형주의 엇갈림을 어떻게 읽어야 하는지, 유가·달러인덱스·
  VIX 등 핵심 변수의 흐름과 함의(달러 강세/약세가 위험자산과 원자재에 주는 압력, VIX 수준이
  말하는 시장의 경계심을 반드시 해석에 포함). 단순 요약 한 줄이 아니라 해석이 담긴 서사 문단으로 작성)
  ②핵심 지표(10년물 bp표기·달러인덱스·WTI·다우·S&P500·나스닥·러셀2000·VIX, 전일대비/연초대비
    수치 나열)
  ③나스닥 시총 상위 10 등락(수치 뒤에 갈림의 축을 해석 1~2문장)
  ④섹터 3블록 — 브리핑의 심장이며 매일의 핵심. 개별 종목을 먼저 늘어놓지 말고, 반드시
    '섹터 → 그 섹터 안의 종목 → 서사' 순서로 쓴다. 아래 3블록을 이 순서 그대로,
    매일 똑같은 형식으로 반복할 것. 각 블록은 예외 없이 다음 (a)(b)(c) 3단으로 구성한다:
      (a) 섹터 헤더 한 줄 — <b>섹터/테마명</b> 평균 등락률, 상승·하락 종목 수
      (b) 그 섹터 안의 종목을 등락률 순으로 최소 4개 —
          <b>회사명(티커)</b> 등락률 형식으로 한 줄씩
      (c) 서사 3~5문장 — 이 섹터가 왜 그렇게 움직였는지. 종목별 촉매(실적, 수주,
          가이던스, 애널리스트 코멘트, 수급, 매크로)를 하나의 흐름으로 엮을 것.
          종목 나열의 반복으로 끝내지 말 것.

    [블록 1] 오늘 가장 많이 오른 섹터 — 무조건 이 블록으로 ④를 시작한다.
      watchlist_group_performance의 1위 그룹과 sectors_by_daily_change의 1위 섹터를 함께
      보고 그날의 주도 섹터를 정한다. 이 섹터가 그날 시장의 진앙지다.
      (a)(b)(c)를 모두 채우고, 랠리에 불을 붙인 촉매를 반드시 밝힌다.

    [블록 2] 확산 섹터 — 블록 1의 테마가 밸류체인을 타고 번진 섹터를 동일한 (a)(b)(c)
      구조로 한 번 더 쓴다(예: 반도체 랠리가 전력·냉각·광통신·스토리지·네트워크 장비로
      확산). 확산이 없었던 날이면 "오늘은 확산 없이 주도 섹터에 국한된 랠리였다"고
      한 줄로 명시하고 넘어간다. 없는 확산을 억지로 만들지 말 것.

    [블록 3] 오늘 가장 많이 하락한 반대편 섹터 — 절대 생략하거나 한두 줄로 줄이지 말 것.
      watchlist_group_performance의 최하위 그룹을 기준으로 동일한 (a)(b)(c) 구조로 쓴다.
      ★필수: watchlist_all(워치리스트 전 종목의 등락률·소속 그룹) 전체를 보고 그 그룹에서
      하락한 종목을 최소 4개 고른다. watchlist_top_losers 목록에 없더라도 그 그룹 소속이면
      하락폭이 작아도 반드시 포함한다(하락폭 상위 몇 개만 훑는 것은 이 블록의 실패다).
      (c) 서사에서는 왜 같은 날 같은 방향으로 팔렸는지를 자금 이동으로 설명한다
      (로테이션, 멀티플 압축, 금리, AI가 기존 소프트웨어의 해자를 잠식한다는 침식 서사 등).
      그 진영 안에서 홀로 오른 예외 종목이 있으면 이유와 함께 짚는다.

  ⑤메가캡의 그늘과 개별 드라마 — 위 3블록을 모두 채운 뒤에만, 짧게. 시총 상위 종목 중
    크게 움직인 종목의 개별 스토리(경영진 교체, 신제품 실망, 실적 등)와, 섹터 흐름과
    무관하게 자기만의 이유(규제 이슈, 가이던스 쇼크, M&A, 밈주 수급)로 급등락한 종목
    2~3개를 회사명+티커+등락률+이유로 서술.
  ⑥투자 관점 해석(시장 분위기/섹터 상대강도/매크로/대중 기대감/한국시장 함의 1줄).
- 웹검색이 가능하면 급등락 '이유'(실적, 뉴스, 지표)를 확인해 서사에 녹일 것. 확인 안 되는
  이유는 추정하지 말고 수치만 기술.
- 제공된 수치를 임의로 바꾸지 말 것. 없는 수치는 만들지 말 것.
- 마지막에 '투자 권유가 아닌 정보 제공 목적' 1줄.
- ④의 3블록에 지면을 가장 많이 쓸 것. ①②③⑤⑥이 길어져 ④가 밀리면 실패한 브리핑이다.
- 전체 길이는 텔레그램 2~4개 메시지 이내(약 8,000자 이내)."""


def build_brief_with_claude(data):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    tools = []
    if os.environ.get("USE_WEB_SEARCH", "1") != "0":
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    body = {
        "model": model,
        "max_tokens": int(os.environ.get("CLAUDE_MAX_TOKENS", "12000")),
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": (
                f"기준 세션: {data['session_date']} (직전 세션 {data['prev_session_date']}).\n"
                "아래 실측 데이터로 브리핑을 작성해 주세요.\n\n"
                + json.dumps(data, ensure_ascii=False)
            ),
        }],
    }
    if tools:
        body["tools"] = tools
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=300,
    )
    resp.raise_for_status()
    payload = resp.json()
    content = payload.get("content", [])

    # 웹검색을 쓰면 "검색해 보겠습니다" 같은 중간 멘트도 text 블록으로 온다.
    # 마지막 검색 결과 이후의 텍스트만 브리핑 본문으로 사용한다.
    last_search = -1
    for i, block in enumerate(content):
        if block.get("type") in ("server_tool_use", "web_search_tool_result"):
            last_search = i
    tail = content[last_search + 1:] if last_search >= 0 else content

    def join_text(blocks):
        return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    text = join_text(tail) or join_text(content)

    if payload.get("stop_reason") == "max_tokens":
        print("경고: Claude 응답이 max_tokens에서 잘렸습니다. CLAUDE_MAX_TOKENS를 올리세요.",
              file=sys.stderr)
    return text or None


def build_fallback_brief(data):
    """API 키가 없거나 실패했을 때의 수치 위주 브리핑."""
    L = [f"📊 <b>미국 시장 브리핑 — {data['session_date']} (현지 마감)</b>", ""]
    L.append("<b>② 핵심 지표</b>")
    for r in data["indicators"]:
        if "error" in r:
            L.append(f"· {r['name']}: N/A")
        elif r["ticker"] == "^TNX":
            ytd = f"{r['chg_bp_ytd']:+.1f}bp" if r.get("chg_bp_ytd") is not None else "N/A"
            L.append(f"· {r['name']}: {r['close']:.3f}%  전일 {r.get('chg_bp_d', 0):+.1f}bp / 연초 {ytd}")
        else:
            ytd = f"{r['chg_pct_ytd']:+.2f}%" if r.get("chg_pct_ytd") is not None else "N/A"
            L.append(f"· {r['name']}: {r['close']:,.2f}  전일 {r.get('chg_pct_d', 0):+.2f}% / 연초 {ytd}")
    L.append("")
    L.append("<b>섹터 일일 등락(상위→하위)</b>")
    for s in data["sectors_by_daily_change"]:
        L.append(f"· {s['name']}: {s['chg_pct_d']:+.2f}%")
    L.append("")
    L.append("<b>③ 나스닥 시총 상위 10</b>")
    for i, m in enumerate(data["nasdaq_top10_by_mcap"], 1):
        cap = f"${m['market_cap']/1e12:.2f}T" if m["market_cap"] >= 1e12 else f"${m['market_cap']/1e9:.0f}B"
        L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): {m['chg_pct_d']:+.2f}%  <i>{cap}</i>")
    L.append("")
    if data.get("watchlist_group_performance"):
        L.append("<b>테마별 평균 등락(워치리스트)</b>")
        for g in data["watchlist_group_performance"]:
            L.append(f"· {g['group']}: {g['avg_chg_pct_d']:+.2f}% (상승 {g['up']} / 하락 {g['down']})")
        L.append("")
    L.append("<b>④ 워치리스트 급등 Top 10</b> <i>(대형주 워치리스트 기준)</i>")
    for i, m in enumerate(data["watchlist_top_gainers"], 1):
        L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): {m['chg_pct_d']:+.2f}%")
    L.append("")
    L.append("<b>급락 Top 10</b>")
    for i, m in enumerate(data["watchlist_top_losers"], 1):
        L.append(f"{i}. {m.get('name', m['ticker'])} ({m['ticker']}): {m['chg_pct_d']:+.2f}%")
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

    data = collect_market_data()
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
