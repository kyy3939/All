#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_market_data.py
---------------------
stocks.html 안의 <script type="application/json" id="marketData"> 블록을
매일 최신 시세로 갱신하는 스크립트. GitHub Actions에서 스케줄 실행됩니다.

동작 방식:
  1. stocks.html에서 기존 marketData JSON을 읽는다 (실패 시 폴백용 기준값).
  2. yfinance로 지수/환율/금리/유가/보유·관심종목 시세를 새로 받아온다.
  3. Google 뉴스 RSS로 증시 관련 최신 뉴스를 받아온다.
  4. (선택) Google Sheets("주식투자" 폴더)에서 실제 보유 계좌/종목 데이터를 받아온다.
  5. 위 데이터를 marketData 형식으로 합쳐 stocks.html의 JSON 블록만 치환해 저장한다.

원칙: 개별 항목 수집에 실패해도 스크립트 전체가 죽지 않고, 실패한 항목은
직전 값을 그대로 유지한다 (페이지가 빈 값/에러로 깨지는 것을 방지).
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

import yfinance as yf
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------

HTML_PATH = os.environ.get("HTML_PATH", "stocks.html")
KST = timezone(timedelta(hours=9))
HISTORY_PERIOD = "2y"      # 이동평균(120일) 계산을 위해 넉넉히 2년치 받아옴
SERIES_KEEP_ROWS = 260     # 지수/환율 등: 최종적으로 보관할 최근 거래일 수 (1년 뷰 지원)
STOCK_SERIES_KEEP_ROWS = 130  # 관심종목 차트: 종목 수가 많아(최대 수십 개) 페이지 용량을 고려해 절반(약 6개월)만 보관
RETRY = 3
RETRY_SLEEP = 3

# 지수 차트(IDX_SERIES)에 쓰이는 심볼 — 코드 내부 키 : (야후파이낸스 심볼)
IDX_SYMBOLS = {
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
    "SOX": "^SOX",
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW": "^DJI",
}
# 히어로 카드(INDICES)에만 쓰이고 별도 차트는 없는 지수
EXTRA_INDEX_SYMBOLS = {
    "NIKKEI": "^N225",
}
# 환율/금리/유가 (VOL_SERIES)
VOL_SYMBOLS = {
    "KRW": "KRW=X",   # 원/달러
    "TNX": "^TNX",    # 미 10년물 국채금리 (야후는 값의 10배로 표기 -> /10 필요)
    "OIL": "CL=F",    # WTI 유가
}

# INDICES(히어로 카드) 표시 이름 <-> 내부 키 매핑
INDICES_LABELS = [
    ("코스피", "KOSPI", "idx", "KOSPI"),
    ("코스닥", "KOSDAQ", "idx", "KOSDAQ"),
    ("S&P500", "SP500", "idx", "미국"),
    ("나스닥", "NASDAQ", "idx", "미국"),
    ("다우존스", "DOW", "idx", "미국"),
    ("닛케이225", "NIKKEI", "idx", "일본"),
    ("원/달러", "KRW", "vol", "환율 (KRW/USD)"),
    ("미 10년물 금리", "TNX", "vol", "국채금리"),
    ("WTI 유가", "OIL", "vol", "원자재"),
]

RECOMMENDATION_LABEL = {
    "strong_buy": "강력매수(Strong Buy)",
    "strongbuy": "강력매수(Strong Buy)",
    "buy": "매수(Buy)",
    "hold": "중립(Hold)",
    "underperform": "비중축소(Underperform)",
    "sell": "매도(Sell)",
    "strong_sell": "강력매도(Strong Sell)",
    "strongsell": "강력매도(Strong Sell)",
}


def log(msg):
    print(f"[fetch_market_data] {msg}", flush=True)


def retry(fn, what, default=None):
    last_err = None
    for i in range(RETRY):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"WARN: {what} 실패 ({i+1}/{RETRY}): {e}")
            time.sleep(RETRY_SLEEP)
    log(f"ERROR: {what} 최종 실패, 이전 값을 유지합니다: {last_err}")
    return default


# ----------------------------------------------------------------------------
# 1. 기존 marketData 읽기 (폴백용)
# ----------------------------------------------------------------------------

def load_existing(html_path):
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    m = re.search(
        r'<script type="application/json" id="marketData">(.*?)</script>',
        html,
        re.S,
    )
    if not m:
        raise RuntimeError(
            "stocks.html에서 marketData 블록을 찾지 못했습니다. "
            "먼저 marketData JSON 블록 구조로 리팩터링된 stocks.html이어야 합니다."
        )
    data = json.loads(m.group(1))
    return html, data


# ----------------------------------------------------------------------------
# 2. 지수/환율/금리/유가 시계열
# ----------------------------------------------------------------------------

def fetch_history(symbol):
    t = yf.Ticker(symbol)
    h = t.history(period=HISTORY_PERIOD, interval="1d", auto_adjust=False)
    if h is None or h.empty:
        raise RuntimeError(f"{symbol} 히스토리가 비어있음")
    h = h[~h.index.duplicated(keep="last")]
    # Close가 없는 행(휴장일 등)은 그 날 자체가 의미 없으므로 제거한다.
    if "Close" in h.columns:
        h = h.dropna(subset=["Close"])
    if h.empty:
        raise RuntimeError(f"{symbol} 히스토리가 전부 NaN")
    # Open/High/Low는 데이터 공급 지연 등으로 당일 종가만 먼저 들어오고 아직 비어있는 경우가 있다.
    # 그 필드들이 없다고 그 날 전체를 버리면(특히 가장 최신 날짜) "어제 데이터인데 그저께 것만 보임" 문제가
    # 생기므로, 없는 값은 종가로 채워 넣어(캔들이 얇은 선처럼 보일 뿐 그 날 자체는 살아있게) 처리한다.
    for col in ("Open", "High", "Low"):
        if col in h.columns:
            h[col] = h[col].fillna(h["Close"])
    return h


def build_series_for_symbols(symbol_pairs, old_series, what_label, keep_rows=SERIES_KEEP_ROWS):
    """(key, 야후파이낸스 심볼) 쌍 목록을 받아 각각의 OHLC+이동평균(5/20/60/120일) 시계열을
    수집한다. 지수 차트(IDX_SERIES)와 관심종목 차트(STOCK_SERIES)가 완전히 같은 로직을
    쓰므로 공용 헬퍼로 뺐다."""
    result = {}
    for key, symbol in symbol_pairs:
        def _do(symbol=symbol):
            h = fetch_history(symbol)
            closes = h["Close"]
            ma5 = closes.rolling(5).mean()
            ma20 = closes.rolling(20).mean()
            ma60 = closes.rolling(60).mean()
            ma120 = closes.rolling(120).mean()
            rows = []
            for ts in h.index[-keep_rows:]:
                rows.append({
                    "date": ts.strftime("%Y-%m-%d"),
                    "open": round(float(h.loc[ts, "Open"]), 2),
                    "high": round(float(h.loc[ts, "High"]), 2),
                    "low": round(float(h.loc[ts, "Low"]), 2),
                    "close": round(float(h.loc[ts, "Close"]), 2),
                    "ma5": None if ma5.loc[ts] != ma5.loc[ts] else round(float(ma5.loc[ts]), 2),
                    "ma20": None if ma20.loc[ts] != ma20.loc[ts] else round(float(ma20.loc[ts]), 2),
                    "ma60": None if ma60.loc[ts] != ma60.loc[ts] else round(float(ma60.loc[ts]), 2),
                    "ma120": None if ma120.loc[ts] != ma120.loc[ts] else round(float(ma120.loc[ts]), 2),
                })
            return rows

        rows = retry(_do, f"{what_label} 시세 차트 수집 [{key}={symbol}]", default=None)
        result[key] = rows if rows else old_series.get(key, [])
    return result


def build_idx_series(old_series):
    return build_series_for_symbols(list(IDX_SYMBOLS.items()), old_series, "지수")


def build_stock_series(stocks_list, old_series):
    """관심종목 각각의 차트용 시세를 수집한다. STOCKS의 ticker(야후 심볼)를 그대로 키로 쓴다."""
    pairs = [(item["ticker"], item["ticker"]) for item in stocks_list]
    return build_series_for_symbols(pairs, old_series, "종목", keep_rows=STOCK_SERIES_KEEP_ROWS)


def build_vol_series(old_series):
    result = {}
    for key, symbol in VOL_SYMBOLS.items():
        def _do(symbol=symbol, key=key):
            h = fetch_history(symbol)
            scale = 1.0  # 실 수집 결과 확인: yfinance history()의 ^TNX Close는 이미 실제 수익률(%) 값으로 반환됨
            rows = []
            for ts in h.index[-SERIES_KEEP_ROWS:]:
                close = float(h.loc[ts, "Close"]) * scale
                dec = 3 if key == "TNX" else 2
                rows.append({"date": ts.strftime("%Y-%m-%d"), "close": round(close, dec)})
            return rows

        rows = retry(_do, f"환율/금리/유가 수집 [{key}={symbol}]", default=None)
        result[key] = rows if rows else old_series.get(key, [])
    return result


def fetch_nikkei_last2():
    def _do():
        h = fetch_history(EXTRA_INDEX_SYMBOLS["NIKKEI"])
        c = h["Close"]
        return float(c.iloc[-1]), float(c.iloc[-2])
    return retry(_do, "닛케이225 수집", default=None)


# ----------------------------------------------------------------------------
# 3. INDICES(히어로 카드) 조립 — 위에서 받은 시계열의 마지막 두 값을 그대로 사용해
#    차트 카드와 히어로 카드 수치가 항상 일치하도록 한다.
# ----------------------------------------------------------------------------

def fmt_idx_val(key, val):
    if key in ("KOSPI", "KOSDAQ", "SOX", "SP500", "NASDAQ", "DOW", "NIKKEI"):
        return f"{val:,.2f}"
    if key == "KRW":
        return f"{val:,.2f}"
    if key == "TNX":
        return f"{val:.3f}%"
    if key == "OIL":
        return f"${val:,.2f}"
    return str(val)


def build_indices(idx_series, vol_series, nikkei_last2, old_indices):
    old_by_name = {x["name"]: x for x in old_indices}
    out = []
    for name, key, kind, sub in INDICES_LABELS:
        try:
            if key == "NIKKEI":
                if not nikkei_last2:
                    raise RuntimeError("닛케이 데이터 없음")
                last, prev = nikkei_last2
            elif kind == "idx":
                series = idx_series.get(key) or []
                if len(series) < 2:
                    raise RuntimeError("시계열 부족")
                last, prev = series[-1]["close"], series[-2]["close"]
            else:
                series = vol_series.get(key) or []
                if len(series) < 2:
                    raise RuntimeError("시계열 부족")
                last, prev = series[-1]["close"], series[-2]["close"]
            chg = last - prev
            chg_pct = (chg / prev * 100) if prev else 0.0
            out.append({
                "name": name,
                "val": fmt_idx_val(key, last),
                "chg": round(chg, 3),
                "chgPct": round(chg_pct, 2),
                "sub": sub,
            })
        except Exception as e:  # noqa: BLE001
            log(f"WARN: INDICES[{name}] 계산 실패, 이전 값 유지: {e}")
            if name in old_by_name:
                out.append(old_by_name[name])
    return out


# ----------------------------------------------------------------------------
# 4. 보유/관심 종목 (MAJOR_STOCKS, STOCKS)
# ----------------------------------------------------------------------------

def safe_get_info(symbol):
    def _do():
        t = yf.Ticker(symbol)
        info = t.get_info()
        if not info:
            raise RuntimeError("빈 info")
        return info
    return retry(_do, f"종목 정보 수집 [{symbol}]", default=None)


def price_and_chg(info, symbol):
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
    if price is None or prev is None:
        def _do():
            h = fetch_history(symbol)
            return float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2])
        got = retry(_do, f"{symbol} 대체 시세 수집", default=None)
        if got:
            price, prev = got
    if price is None or prev is None:
        return None, None
    chg_pct = (price - prev) / prev * 100 if prev else 0.0
    return price, round(chg_pct, 2)


NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_naver_html(url):
    req = Request(url, headers=NAVER_HEADERS)
    with urlopen(req, timeout=15) as resp:
        raw = resp.read()
    return raw.decode("euc-kr", errors="replace")  # 네이버 금융은 EUC-KR 인코딩


def _parse_num(text):
    text = (text or "").replace(",", "").replace("%", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def _parse_chg_pct(td):
    """등락률 셀은 색상(적=상승/청=하락)으로만 방향을 표시하고 텍스트에 부호가
    없는 경우가 있어, 텍스트의 '-' 기호와 화살표 아이콘의 alt 텍스트(상승/하락/보합)
    를 함께 확인해 방향을 판단한다."""
    text = td.get_text(strip=True)
    val = _parse_num(text)
    if val is None:
        return None
    val = abs(val)
    img = td.find("img")
    alt = img.get("alt") if img else None
    if alt == "보합" or val == 0:
        return 0.0
    is_down = ("-" in text) or (alt == "하락") or ("하락" in text)
    return -val if is_down else val


def parse_naver_table(html, limit=30):
    """네이버 금융 시세 테이블(class="type_2")을 헤더 텍스트 기준으로 파싱한다.
    네이버는 <thead>/<tbody> 없이 <tr> 안에 <th>만으로 헤더 행을 표시하는 옛날식 표
    마크업을 쓰므로, thead 유무에 의존하지 않고 "<th>가 있는 첫 번째 행"을 헤더로
    간주한다. 고정된 열 순서 대신 "종목명"/"현재가"/"등락률"/"시가총액" 헤더 라벨로
    열 위치를 찾으므로, 열이 추가/삭제되는 정도의 구조 변경에는 견딘다."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="type_2")
    if table is None:
        raise RuntimeError("시세 테이블(table.type_2)을 찾지 못함 — 페이지 구조가 바뀌었을 수 있음")

    all_trs = table.find_all("tr")
    if not all_trs:
        raise RuntimeError("테이블에 행이 하나도 없음")

    header_tr = next((tr for tr in all_trs if tr.find("th")), None)
    if header_tr is None:
        raise RuntimeError("헤더 행(<th> 포함)을 찾지 못함")
    headers = [th.get_text(strip=True) for th in header_tr.find_all("th")]

    def col_idx(name):
        for i, h in enumerate(headers):
            if name in h:
                return i
        return None

    idx_name = col_idx("종목명")
    idx_price = col_idx("현재가")
    idx_chg = col_idx("등락률")
    idx_cap = col_idx("시가총액")
    if idx_name is None or idx_price is None or idx_chg is None:
        raise RuntimeError(f"필요한 열을 헤더에서 찾지 못함 (headers={headers})")

    rows = []
    for tr in all_trs:
        if tr is header_tr or tr.find("th"):
            continue
        tds = tr.find_all("td")
        need = max(idx_name, idx_price, idx_chg, idx_cap if idx_cap is not None else 0)
        if len(tds) <= need:
            continue
        link = tds[idx_name].find("a", href=re.compile(r"code=\d{6}"))
        if not link:
            continue
        code_m = re.search(r"code=(\d{6})", link["href"])
        if not code_m:
            continue
        price = _parse_num(tds[idx_price].get_text(strip=True))
        chg_pct = _parse_chg_pct(tds[idx_chg])
        cap = _parse_num(tds[idx_cap].get_text(strip=True)) if idx_cap is not None else None
        if price is None or chg_pct is None:
            continue
        rows.append({
            "name": link.get_text(strip=True),
            "ticker": code_m.group(1),
            "price": int(price),
            "chgPct": round(chg_pct, 2),
            "cap": round((cap or 0) / 10000, 1),  # 네이버는 억원 단위 -> 조원 단위로 환산
        })
        if len(rows) >= limit:
            break

    if len(rows) < 10:
        raise RuntimeError(f"파싱된 행이 너무 적음({len(rows)}개) — 페이지 구조 변경 의심")
    return rows


def fetch_naver_top30(market):
    """market: "KOSPI" 또는 "KOSDAQ" -> (시가총액 상위 30, 등락률 상위 30)"""
    sosok = "0" if market == "KOSPI" else "1"

    cap_html = fetch_naver_html(
        f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page=1"
    )
    cap_rows = parse_naver_table(cap_html, limit=30)

    chg_html = fetch_naver_html(f"https://finance.naver.com/sise/sise_rise.naver?sosok={sosok}")
    chg_rows = parse_naver_table(chg_html, limit=30)

    for r in cap_rows + chg_rows:
        r["market"] = market
    return cap_rows, chg_rows


def naver_ticker(code, market):
    """네이버 금융의 6자리 종목코드를 야후파이낸스 심볼로 변환한다."""
    return f"{code}.KS" if market == "KOSPI" else f"{code}.KQ"


def build_kr_top50_rows():
    """코스피+코스닥 통합 시가총액 상위 50 종목을 네이버 금융 스크리닝으로 매번
    새로 계산한다. 고정 목록이 아니라 실행할 때마다 통째로 다시 산출하므로,
    순위가 바뀌면 목록도 자동으로 갱신되고 크기는 항상 정확히 50개로 유지된다."""
    rows = []
    for market in ("KOSPI", "KOSDAQ"):
        def _do(market=market):
            sosok = "0" if market == "KOSPI" else "1"
            html = fetch_naver_html(
                f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page=1"
            )
            r = parse_naver_table(html, limit=50)
            for x in r:
                x["market"] = market
            return r

        got = retry(_do, f"{market} 시총 상위 50 스크리닝", default=[])
        rows.extend(got or [])

    rows.sort(key=lambda r: r.get("cap") or 0, reverse=True)
    return rows[:50]


def build_kr_top50_stocks(old_stocks_by_ticker):
    """시총 상위 50 스크리닝 결과를 STOCKS 항목 형태로 변환한다. 기존에 이미 추적하던
    종목이면(old_stocks_by_ticker에 존재) 그동안 쌓인 재무값/뉴스 등을 이어받아 이번
    실행의 update_stocks()가 값을 덮어쓸 때까지의 공백을 최소화하고, 이번에 새로
    순위에 든 종목이면 네이버 스크리닝 값만으로 최소 항목을 만든다(상세 재무값은
    이어지는 update_stocks()에서 즉시 채워진다)."""
    rows = build_kr_top50_rows()
    out = []
    for r in rows:
        ticker = naver_ticker(r["ticker"], r["market"])
        cap_str = f"{r['cap']:,.1f}조원" if r.get("cap") is not None else None
        old = old_stocks_by_ticker.get(ticker)
        if old:
            item = dict(old)
        else:
            item = {
                "per": 0.0, "div": 0.0, "roe": 0.0,
                "lo": r["price"], "hi": r["price"], "rec": "-", "target": None, "news": [],
            }
        item["name"] = r["name"]
        item["ticker"] = ticker
        item["market"] = "kr"
        item["exch"] = r["market"]
        item.setdefault("sector", "-")
        item["unit"] = "원"
        item["price"] = r["price"]
        item["chgPct"] = r["chgPct"]
        if cap_str:
            item["cap"] = cap_str
        item["tier"] = "top50"
        out.append(item)
    return out


def build_watchlist_stocks(old_stocks):
    """시총 순위와 무관하게 항상 유지하고 싶은 개별 관심종목(코스피/코스닥 top50 밖이라도
    별도로 요청해서 추가한 국내 종목)만 남긴다. 종목검색은 국내로 한정하기로 했으므로
    해외(market="us") 종목은 더 이상 유지하지 않고, tier가 명시적으로 "watchlist"인
    항목만 관심종목으로 간주한다. 신규 추가는 tier="watchlist"를 붙여 수동으로 넣는다."""
    out = []
    seen = set()
    for s in old_stocks:
        if s.get("tier") != "watchlist" or s.get("market") != "kr" or s["ticker"] in seen:
            continue
        item = dict(s)
        out.append(item)
        seen.add(s["ticker"])
    return out


def build_major_stocks(old_data):
    """코스피/코스닥 x 시가총액상위30/등락률상위30 = 4개 탭.
    고정 종목 리스트가 아니라 네이버 금융 랭킹 페이지를 매일 다시 스크래핑해
    그날그날 실제 순위를 반영한다."""
    empty = {"kospi_cap": [], "kosdaq_cap": [], "kospi_chg": [], "kosdaq_chg": []}
    old_data = old_data if isinstance(old_data, dict) else empty

    result = {}
    for key, market in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        def _do(market=market):
            return fetch_naver_top30(market)

        got = retry(_do, f"{market} 네이버 시총·등락률 상위 30 스크리닝", default=None)
        if got:
            result[f"{key}_cap"], result[f"{key}_chg"] = got
        else:
            result[f"{key}_cap"] = old_data.get(f"{key}_cap", [])
            result[f"{key}_chg"] = old_data.get(f"{key}_chg", [])
    return result


def fmt_cap(market, cap_value):
    if cap_value is None:
        return None
    if market == "kr":
        return f"{cap_value/1e12:,.1f}조원"
    # us
    if cap_value >= 1e12:
        return f"${cap_value/1e12:,.2f}T"
    return f"${cap_value/1e9:,.2f}B"


def build_rec_string(info, old_rec):
    mean = info.get("recommendationMean")
    key = (info.get("recommendationKey") or "").lower()
    label = RECOMMENDATION_LABEL.get(key)
    if mean is None or not label:
        return old_rec  # 야후에 애널리스트 데이터가 없는 경우(특히 국내 종목) 이전 값 유지
    return f"{mean:.1f} · {label}"


def fetch_stock_news(name):
    """종목별 관련 뉴스 최대 3건을 가져온다. 실패하면 예외를 던진다(retry()에서 처리)."""
    items = fetch_news_for_query(f"{name} 주가")
    top = items[:3]
    if not top:
        raise RuntimeError("종목 뉴스 0건 수집")
    return [
        {"t": it["t"], "src": it["src"], "link": it["link"], "date": _format_pub_date_kst(it["pubDate"])}
        for it in top
    ]


# ----------------------------------------------------------------------------
# 4.5 AI 종합 분석 (6개 에이전트 + 투자전문가) — Anthropic API, 선택적 기능
# ----------------------------------------------------------------------------
# ANTHROPIC_API_KEY 환경변수(리포지토리 시크릿)가 없으면 이 단계 전체를 건너뛰고
# 기존 값을 유지한다. 다른 수집 단계와 동일하게 "실패해도 페이지는 안 깨진다" 원칙을 따른다.
# 실시간 웹 검색 없이, 이 스크립트가 이미 수집한 데이터(시세/재무 스냅샷/최근 뉴스)만
# 근거로 삼아 배치로 생성한다 — 실시간성이 필요하면 채팅에서 직접 종목명을 물어보면 된다.

AI_MODEL = "claude-haiku-4-5-20251001"
AI_AGENT_KEYS = ("finance", "chart", "sector", "company", "news", "industry")
AI_AGENT_LABEL = {
    "finance": "재무전문가", "chart": "차트분석가", "sector": "업종분석가",
    "company": "기업분석전문가", "news": "기사분석가", "industry": "산업분석전문가",
}
AI_SYSTEM_PROMPT = (
    "당신은 재무전문가·차트분석가·업종분석가·기업분석전문가·기사분석가·산업분석전문가 "
    "6명과 투자전문가 1명으로 구성된 투자 분석 팀이다. 사용자가 제공하는 종목 스냅샷 데이터"
    "(실시간 웹 검색 불가, 제공된 데이터가 근거의 전부)만 근거로 6개 에이전트 관점의 짧은 "
    "분석과 투자전문가 종합의견을 submit_analysis 도구로 제출하라. 데이터가 부족해 특정 "
    "항목을 판단할 수 없으면 텍스트에 \"확인 필요\"라고 명시하라. 항상 한국어로 작성하고, "
    "전문 금융 용어는 그대로 사용한다."
)
AI_ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "6개 에이전트 분석과 투자전문가 종합의견을 구조화된 형태로 제출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agents": {
                "type": "object",
                "properties": {
                    k: {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 1, "maximum": 10},
                            "text": {"type": "string"},
                        },
                        "required": ["score", "text"],
                    }
                    for k in AI_AGENT_KEYS
                },
                "required": list(AI_AGENT_KEYS),
            },
            "opinion": {
                "type": "string",
                "enum": ["강력매수", "매수", "중립", "매도", "강력매도"],
            },
            "reasons": {
                "type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 3,
            },
            "action": {
                "type": "object",
                "properties": {
                    "entry": {"type": "string"},
                    "buy1": {"type": "string"},
                    "buy2": {"type": "string"},
                    "targetShort": {"type": "string"},
                    "targetMid": {"type": "string"},
                    "stopLoss": {"type": "string"},
                    "horizon": {"type": "string"},
                },
                "required": ["entry", "buy1", "buy2", "targetShort", "targetMid", "stopLoss", "horizon"],
            },
            "risks": {
                "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3,
            },
        },
        "required": ["agents", "opinion", "reasons", "action", "risks"],
    },
}


def _ai_user_prompt(item):
    news = item.get("news") or []
    news_txt = "\n".join(
        f"- ({n.get('date','-')}) {n.get('t','')} [{n.get('src','-')}]" for n in news[:3]
    ) or "- (수집된 관련 뉴스 없음)"
    return f"""종목: {item.get('name')} ({item.get('ticker')}, {item.get('exch','-')}, 섹터: {item.get('sector','-')})
현재가: {item.get('price')}{item.get('unit','원')} (전일 대비 {item.get('chgPct', 0):+.2f}%)
시가총액: {item.get('cap','-')}
PER(forward): {item.get('per','-')}
배당수익률: {item.get('div','-')}%
ROE: {item.get('roe','-')}%
52주 최저/최고: {item.get('lo','-')} / {item.get('hi','-')}
애널리스트 컨센서스: {item.get('rec','-')}
목표주가(평균): {item.get('target','-')}
최근 관련 뉴스:
{news_txt}

위 데이터만 근거로 submit_analysis 도구를 호출하라. 각 에이전트 텍스트는 1~2문장으로 간결하게."""


def call_claude_analysis(client, item):
    resp = client.messages.create(
        model=AI_MODEL,
        max_tokens=1200,
        system=AI_SYSTEM_PROMPT,
        tools=[AI_ANALYSIS_TOOL],
        tool_choice={"type": "tool", "name": "submit_analysis"},
        messages=[{"role": "user", "content": _ai_user_prompt(item)}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_analysis":
            return block.input
    raise RuntimeError("submit_analysis tool_use 블록을 찾지 못함")


def build_ai_analysis(stocks, old_analysis):
    """STOCKS 각 항목에 대해 AI 종합 분석을 생성한다. STOCK_SERIES와 동일하게 이번 실행의
    stocks 목록에 있는 티커만 결과에 남긴다(top50에서 밀려난 종목은 자동으로 정리됨).
    개별 종목 생성 실패 시 그 종목만 이전 값을 유지하고 나머지는 계속 진행한다."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("WARN: ANTHROPIC_API_KEY 미설정 — AI 분석 생성 단계 전체를 건너뜁니다(이전 값 유지).")
        return {s["ticker"]: old_analysis[s["ticker"]] for s in stocks if s["ticker"] in old_analysis}
    try:
        import anthropic
    except ImportError:
        log("WARN: anthropic 패키지가 설치되지 않아 AI 분석 생성을 건너뜁니다.")
        return {s["ticker"]: old_analysis[s["ticker"]] for s in stocks if s["ticker"] in old_analysis}

    client = anthropic.Anthropic(api_key=api_key)
    out = {}
    for item in stocks:
        def _do(item=item):
            return call_claude_analysis(client, item)

        data = retry(_do, f"{item['name']} AI 분석 생성", default=None)
        if data:
            agents = data["agents"]
            total = sum(int(agents[k]["score"]) for k in AI_AGENT_KEYS)
            out[item["ticker"]] = {
                "asOf": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                "agents": agents,
                "total": total,
                "opinion": data["opinion"],
                "reasons": data["reasons"],
                "action": data["action"],
                "risks": data["risks"],
            }
        elif item["ticker"] in old_analysis:
            out[item["ticker"]] = old_analysis[item["ticker"]]
        time.sleep(0.3)
    return out


def update_stocks(old_list):
    out = []
    for item in old_list:
        symbol = item["ticker"]  # 이미 .KS 등 접미사 포함
        info = safe_get_info(symbol)
        new_item = dict(item)
        if not info:
            log(f"WARN: {item['name']}({symbol}) 상세 정보 수집 실패, 이전 값 전체 유지")
        else:
            price, chg_pct = price_and_chg(info, symbol)
            if price is not None:
                new_item["price"] = round(price, 2) if item["unit"] == "$" else int(round(price))
                new_item["chgPct"] = chg_pct

            # top50(국내) 종목은 이미 네이버 스크리닝에서 정확한 시가총액을 받아왔고,
            # 그 값으로 순위(top50 선정)까지 매겨졌다. yfinance의 marketCap은 국내 종목에서
            # 종종 부정확/누락되는 경우가 있어(이미 주요종목 4탭에서 겪은 문제) 그 값으로
            # 덮어쓰면 "표시된 시총"과 "실제 순위를 매긴 시총"이 어긋나 보일 수 있으므로,
            # top50 종목은 네이버 값을 그대로 유지하고 yfinance 값으로 덮어쓰지 않는다.
            if item.get("tier") != "top50":
                cap = info.get("marketCap")
                cap_str = fmt_cap(item["market"], cap) if cap else None
                if cap_str:
                    new_item["cap"] = cap_str

            per = info.get("trailingPE") or info.get("forwardPE")
            if per:
                new_item["per"] = round(per, 2)

            div_rate = info.get("dividendRate")
            if div_rate and price:
                new_item["div"] = round(div_rate / price * 100, 2)
            elif info.get("dividendYield") is not None:
                dy = info.get("dividendYield")
                # yfinance 버전에 따라 0.0058(=0.58%) 또는 0.58(=0.58%)로 오는 경우가 섞여 있어 방어적으로 처리
                new_item["div"] = round(dy * 100, 2) if dy < 1 else round(dy, 2)

            roe = info.get("returnOnEquity")
            if roe is not None:
                new_item["roe"] = round(roe * 100, 2)

            lo = info.get("fiftyTwoWeekLow")
            hi = info.get("fiftyTwoWeekHigh")
            if lo:
                new_item["lo"] = round(lo, 2) if item["unit"] == "$" else int(round(lo))
            if hi:
                new_item["hi"] = round(hi, 2) if item["unit"] == "$" else int(round(hi))

            new_item["rec"] = build_rec_string(info, item.get("rec"))
            target = info.get("targetMeanPrice")
            if target:
                new_item["target"] = round(target, 2) if item["unit"] == "$" else int(round(target))

        # 종목별 관련 뉴스 — yfinance 정보 수집 성공 여부와 무관하게(다른 소스이므로) 항상 시도한다.
        news = retry(lambda name=item["name"]: fetch_stock_news(name), f"{item['name']} 관련 뉴스 수집", default=None)
        new_item["news"] = news if news else item.get("news", [])

        out.append(new_item)
    return out


# ----------------------------------------------------------------------------
# 5. 뉴스 (Google 뉴스 RSS — API 키 불필요)
# ----------------------------------------------------------------------------

NEWS_QUERIES = [
    ("코스피 마감", 2),
    ("코스닥 마감", 2),
    ("코스피 급등주", 2),
    ("코스피 급락주", 2),
    ("업종 테마 강세", 2),
    ("글로벌 증시", 2),
    ("원달러 환율", 2),
    ("국제 유가", 1),
]
NEWS_MAX_TOTAL = 15


def fetch_news_for_query(query):
    import urllib.parse
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=15) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source_el = item.find("source")
        src = source_el.text.strip() if source_el is not None and source_el.text else ""
        pub_date = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        items.append({"t": title, "src": src or "Google News", "link": link, "pubDate": pub_date})
    return items


def _parse_pub_date(s):
    """RSS pubDate 문자열("Thu, 23 Jul 2026 00:42:05 GMT" 등)을 datetime으로 파싱한다.
    실패하면 None. %Z로 매칭된 경우 tzinfo 없는(naive) UTC 벽시계 값이 되고,
    %z로 매칭된 경우 tzinfo가 붙은 aware 값이 된다."""
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:  # noqa: BLE001
            continue
    return None


def _format_pub_date_kst(s):
    """기사 작성일을 한국시간(KST) 기준 "YYYY-MM-DD" 문자열로 변환한다. 실패 시 None."""
    dt_val = _parse_pub_date(s)
    if dt_val is None:
        return None
    if dt_val.tzinfo is not None:
        dt_val = dt_val.astimezone(timezone.utc).replace(tzinfo=None)
    return (dt_val + timedelta(hours=9)).strftime("%Y-%m-%d")


def build_news(old_news):
    """카테고리(코스피/코스닥/강세·약세 종목/섹터/글로벌 증시/환율/유가)별로 검색어를
    나눠서 각각 몇 건씩 가져온 뒤 합친다. 전부 한 검색어로만 모으면 최신순 정렬 특성상
    특정 주제(예: 속보성 이슈)가 결과를 독점해 다른 카테고리가 안 보일 수 있어,
    카테고리별로 건수를 미리 배분해 다양한 주제가 고르게 섞이도록 한다."""
    def _do():
        all_items = []
        seen_titles = set()
        for query, take in NEWS_QUERIES:
            try:
                items = fetch_news_for_query(query)
            except Exception as e:  # noqa: BLE001
                log(f"WARN: 뉴스 검색어 '{query}' 수집 실패, 건너뜀: {e}")
                continue
            count = 0
            for it in items:
                if it["t"] in seen_titles:
                    continue
                seen_titles.add(it["t"])
                all_items.append(it)
                count += 1
                if count >= take:
                    break

        if not all_items:
            raise RuntimeError("뉴스 0건 수집")

        all_items.sort(key=lambda x: _parse_pub_date(x["pubDate"]) or datetime.min, reverse=True)
        top = all_items[:NEWS_MAX_TOTAL]
        return [
            {"t": it["t"], "src": it["src"], "link": it["link"], "date": _format_pub_date_kst(it["pubDate"])}
            for it in top
        ]

    news = retry(_do, "뉴스 수집", default=None)
    return news if news else old_news


# ----------------------------------------------------------------------------
# 6. 내 포트폴리오 (Google Sheets 연동)
# ----------------------------------------------------------------------------
# "개인현황 > 주식투자" 드라이브 폴더의 스프레드시트들이 전부 "링크가 있는 모든 사용자 -
# 뷰어"로 공유되어 있으므로, 인증(서비스계정/API 키) 없이 구글의 공개 gviz CSV export
# 엔드포인트(docs.google.com/spreadsheets/d/{id}/gviz/tq?tqx=out:csv&sheet=...)로
# 그대로 읽어온다. 별도 GitHub Secret이나 GCP 설정이 필요 없다.
#
# 드라이브 폴더 목록 API는 인증 없이 쓸 수 없어서, 대상 스프레드시트 9개(마스터 1개 +
# 계좌 8개, "[백업] 지수관련주" 제외)는 아래에 ID를 고정해 둔다. 계좌를 새로 추가/삭제
# 하면 이 목록을 수동으로 갱신해야 한다 — 대화창에서 "계좌 시트 추가/삭제해줘"라고
# 요청하면 그때 반영한다.
#
# 개인정보 보호: 계좌번호는 절대 수집/저장하지 않는다(마스터 시트의 "계좌번호" 열은
# 의도적으로 읽지 않음). 원금/평가금액/손익 등 금액 정보는 사용자가 명시적으로
# "포함해서 표시"를 선택했으므로 그대로 가져온다.

PORTFOLIO_MASTER_ID = "1zmLvhAmSErtEnH1isRGcYrU4Fl3SFvJEVe98PuUx18o"  # [개인] 주식 전체 계좌 관리
PORTFOLIO_ACCOUNTS = [
    {"id": "1m6UXquZA49Y6ZsYCRg-bJhYpncgC7V_MapoFmvO4lHs", "name": "용연", "extra": None},
    {"id": "1yG_2JV2Fl3lqNpg8u3lPlZhwA3oebphmQG83ty10iEs", "name": "박현미", "extra": None},
    {"id": "1EkNdyjKZSzG_dWLuWFiU38UbEGckjgXKkF7WpVe96TI", "name": "김하준", "extra": None},
    {"id": "1SbhFd8WxdVVFqQvH1KC8nMd7tEfP7cjrdVYohMD0-Sc", "name": "지수관련", "extra": None},
    {"id": "1WNt0nS4lCibBjbEhFaSe2YiuurZmCTdyQ8v3udGlvsc", "name": "생활비", "extra": None},
    {"id": "1CoOS8uRoO5xS7iho-Paa35E47XsFXc7x3y2RKVKH0_4", "name": "연말정산용", "extra": "DB증권"},
    {"id": "1DaM7WrrnrphjQMFyfCKkp54ICGiHk3k34vzxno0Do9g", "name": "우리은행", "extra": "IRP - 연말정산용"},
    {"id": "1FDhP4Netdq3tRvjDHk7ShwVZYdTPEJO29s3ioauEItE", "name": "DB랩", "extra": None},
]


def _pf_num(s):
    """시트 셀 문자열("1,234", "-71.0", "(1,234)", "" 등)을 숫자로 변환한다. 실패/빈값은 None."""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("원", "").strip()
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    v = -v if neg else v
    return int(v) if v == int(v) else round(v, 2)


def _pf_cell(row, i):
    return row[i] if i is not None and i < len(row) else ""


def _pf_find_row(rows, *needles):
    for i, row in enumerate(rows):
        joined = "".join(row)
        if all(n in joined for n in needles):
            return i
    return None


def _pf_col_index(headers, name):
    for i, h in enumerate(headers):
        if name in h:
            return i
    return None


def fetch_sheet_csv_rows(spreadsheet_id, sheet_name):
    """공개(링크 있으면 보기 가능) 구글시트의 특정 탭을 gviz CSV export로 받아
    행(list of list) 형태로 반환한다. 인증이 전혀 필요 없다."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=15) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def parse_overview(rows):
    """마스터 시트의 "전체정리" 탭(계좌별 합산 요약)을 파싱한다. 이 탭은 헤더 텍스트가
    gviz CSV에서도 깨지지 않아 헤더명 매칭으로 안전하게 읽을 수 있다. "계좌번호" 열은
    의도적으로 읽지 않는다(개인정보 제외 원칙)."""
    header_i = _pf_find_row(rows, "항목", "은행명")
    if header_i is None:
        raise RuntimeError("전체정리 헤더 행을 찾지 못함")
    headers = rows[header_i]
    col = {
        name: _pf_col_index(headers, name)
        for name in ["항목", "은행명", "원금", "누적수익", "누적합계", "매입금액", "현금잔액", "평가금액", "평가손익금", "수익률"]
    }
    if col["항목"] is None:
        raise RuntimeError("전체정리 '항목' 열을 찾지 못함")

    def get(row, key):
        return _pf_cell(row, col[key]).strip()

    accounts, total = [], None
    for row in rows[header_i + 1:]:
        name = get(row, "항목")
        if not name:
            continue
        item = {
            "name": name,
            "bank": get(row, "은행명") or None,
            "principal": _pf_num(get(row, "원금")),
            "cumPnl": _pf_num(get(row, "누적수익")),
            "cumTotal": _pf_num(get(row, "누적합계")),
            "buyAmount": _pf_num(get(row, "매입금액")),
            "cashBalance": _pf_num(get(row, "현금잔액")),
            "evalAmount": _pf_num(get(row, "평가금액")),
            "evalPnl": _pf_num(get(row, "평가손익금")),
            "returnPct": _pf_num(get(row, "수익률")),
        }
        if name == "합계":
            total = item
        else:
            accounts.append(item)
    return {"accounts": accounts, "total": total}


# 계좌별 "보유종목" 탭의 고정 열 위치. gviz CSV export가 이 탭 특유의 병합 셀
# (구역 제목 "1. 현재 보유 자산" / "2. 보유종목")을 처리하면서 "현재가"/"단가"/"수량" 등
# 뒤쪽 헤더 텍스트를 비워버리는 현상이 실측(용연/박현미/김하준/우리은행/생활비/DB랩
# 6개 계좌 시트)에서 공통으로 확인됐다. 헤더명 매칭 대신, 모든 계좌 시트에서 동일하게
# 확인된 고정 열 순서로 파싱한다. "종목코드"/"원금"/"평가 총액" 같은 마커 텍스트로
# 구역의 시작 행만 찾고, 그 안의 실제 값은 열 번호로 읽는다.
HOLD_COL = {"code": 1, "name": 2, "price": 3, "avgPrice": 4, "qty": 5, "buyAmount": 6, "evalAmount": 7, "evalPnl": 8, "returnPct": 9}
SUM_COL = {"principal": 1, "buyAmount": 3, "cashBalance": 4, "evalAmount": 5, "evalPnl": 6, "returnPct": 7}


def parse_account_holdings(rows):
    """계좌별 시트의 "보유종목" 탭을 파싱한다. 이 탭 안에 요약(1. 현재 보유 자산)과
    보유종목 상세(2. 보유종목) 두 구역이 함께 들어있어 한 번에 둘 다 얻는다."""
    summary = None
    sum_header_i = _pf_find_row(rows, "원금")
    if sum_header_i is not None and sum_header_i + 1 < len(rows):
        data = rows[sum_header_i + 1]
        summary = {k: _pf_num(_pf_cell(data, i)) for k, i in SUM_COL.items()}

    holdings = []
    hold_header_i = _pf_find_row(rows, "종목코드")
    if hold_header_i is not None:
        for row in rows[hold_header_i + 1:]:
            code = _pf_cell(row, HOLD_COL["code"]).strip()
            name = _pf_cell(row, HOLD_COL["name"]).strip()
            if not code and not name:
                break
            if code in ("평가 총액", "합계") or name in ("평가 총액", "합계"):
                break
            holdings.append({
                "code": code or None,
                "name": name,
                "price": _pf_num(_pf_cell(row, HOLD_COL["price"])),
                "avgPrice": _pf_num(_pf_cell(row, HOLD_COL["avgPrice"])),
                "qty": _pf_num(_pf_cell(row, HOLD_COL["qty"])),
                "buyAmount": _pf_num(_pf_cell(row, HOLD_COL["buyAmount"])),
                "evalAmount": _pf_num(_pf_cell(row, HOLD_COL["evalAmount"])),
                "evalPnl": _pf_num(_pf_cell(row, HOLD_COL["evalPnl"])),
                "returnPct": _pf_num(_pf_cell(row, HOLD_COL["returnPct"])),
            })
    return summary, holdings


def build_portfolio(old_portfolio):
    empty = {"asOf": None, "overview": {"accounts": [], "total": None}, "accounts": []}

    def _do():
        master_rows = fetch_sheet_csv_rows(PORTFOLIO_MASTER_ID, "전체정리")
        overview = parse_overview(master_rows)

        accounts = []
        for acc in PORTFOLIO_ACCOUNTS:
            def _one(acc=acc):
                rows = fetch_sheet_csv_rows(acc["id"], "보유종목")
                summary, holdings = parse_account_holdings(rows)
                return {"name": acc["name"], "extra": acc["extra"], "summary": summary, "holdings": holdings}

            got = retry(_one, f"포트폴리오 계좌 수집 [{acc['name']}]", default=None)
            if got:
                accounts.append(got)
        accounts.sort(key=lambda a: a["name"])

        return {
            "asOf": datetime.now(KST).strftime("%Y-%m-%d"),
            "overview": overview,
            "accounts": accounts,
        }

    result = retry(_do, "포트폴리오(Google Sheets) 전체 수집", default=None)
    return result if result else (old_portfolio if old_portfolio else empty)


# ----------------------------------------------------------------------------
# 7. 메인
# ----------------------------------------------------------------------------

def main():
    html, old = load_existing(HTML_PATH)
    log(f"기존 데이터 기준일: {old.get('asOf')}")

    idx_series = build_idx_series(old.get("IDX_SERIES", {}))
    vol_series = build_vol_series(old.get("VOL_SERIES", {}))
    nikkei_last2 = fetch_nikkei_last2()
    indices = build_indices(idx_series, vol_series, nikkei_last2, old.get("INDICES", []))
    major_stocks = build_major_stocks(old.get("MAJOR_STOCKS", {}))

    old_stocks = old.get("STOCKS", [])
    old_stocks_by_ticker = {s["ticker"]: s for s in old_stocks}
    kr_top50 = build_kr_top50_stocks(old_stocks_by_ticker)
    watchlist = build_watchlist_stocks(old_stocks)
    top50_tickers = {s["ticker"] for s in kr_top50}
    merged_seed = kr_top50 + [s for s in watchlist if s["ticker"] not in top50_tickers]

    stocks = update_stocks(merged_seed)
    stock_series = build_stock_series(stocks, old.get("STOCK_SERIES", {}))
    news = build_news(old.get("NEWS", []))
    ai_analysis = build_ai_analysis(stocks, old.get("AI_ANALYSIS", {}))
    portfolio = build_portfolio(old.get("PORTFOLIO"))

    as_of = None
    if idx_series.get("KOSPI"):
        as_of = idx_series["KOSPI"][-1]["date"]
    else:
        as_of = old.get("asOf")

    new_data = {
        "asOf": as_of,
        "generatedAt": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "INDICES": indices,
        "MAJOR_STOCKS": major_stocks,
        "NEWS": news,
        "STOCKS": stocks,
        "STOCK_SERIES": stock_series,
        "AI_ANALYSIS": ai_analysis,
        "IDX_SERIES": idx_series,
        "VOL_SERIES": vol_series,
        "PORTFOLIO": portfolio,
    }
    try:
        # allow_nan=False: NaN/Infinity가 하나라도 섞여 있으면 여기서 즉시 실패시킨다.
        # (그대로 두면 JSON.parse가 브라우저에서 깨지면서 페이지 전체가 빈 화면이 된다)
        new_json = json.dumps(new_data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError as e:
        log(f"ERROR: 생성된 데이터에 NaN/Infinity가 포함되어 있어 중단합니다 ({e}). "
            f"파일을 변경하지 않았습니다 — 기존 데이터가 그대로 유지됩니다.")
        sys.exit(1)

    def _repl(_m):
        return '<script type="application/json" id="marketData">' + new_json + "</script>"

    new_html, n = re.subn(
        r'<script type="application/json" id="marketData">.*?</script>',
        _repl,
        html,
        count=1,
        flags=re.S,
    )
    if n != 1:
        log("ERROR: marketData 블록 치환 실패 — 파일을 변경하지 않았습니다.")
        sys.exit(1)

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)

    ms_summary = (
        f"코스피시총{len(major_stocks.get('kospi_cap', []))}"
        f"·코스닥시총{len(major_stocks.get('kosdaq_cap', []))}"
        f"·코스피등락{len(major_stocks.get('kospi_chg', []))}"
        f"·코스닥등락{len(major_stocks.get('kosdaq_chg', []))}"
    )
    stocks_with_series = sum(1 for s in stocks if stock_series.get(s["ticker"]))
    stocks_with_news = sum(1 for s in stocks if s.get("news"))
    stocks_with_ai = sum(1 for s in stocks if ai_analysis.get(s["ticker"]))
    pf_accounts = len(portfolio.get("accounts", []))
    pf_holdings = sum(len(a.get("holdings") or []) for a in portfolio.get("accounts", []))
    log(f"완료. asOf={as_of}, INDICES={len(indices)}, MAJOR_STOCKS=({ms_summary}), "
        f"STOCKS={len(stocks)}(차트 {stocks_with_series}·뉴스 {stocks_with_news}·AI분석 {stocks_with_ai}), "
        f"NEWS={len(news)}, PORTFOLIO=(계좌 {pf_accounts}·보유종목 {pf_holdings})")


if __name__ == "__main__":
    main()
