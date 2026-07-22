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
  4. 위 데이터를 marketData 형식으로 합쳐 stocks.html의 JSON 블록만 치환해 저장한다.

원칙: 개별 항목 수집에 실패해도 스크립트 전체가 죽지 않고, 실패한 항목은
직전 값을 그대로 유지한다 (페이지가 빈 값/에러로 깨지는 것을 방지).
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

import yfinance as yf

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------

HTML_PATH = os.environ.get("HTML_PATH", "stocks.html")
KST = timezone(timedelta(hours=9))
HISTORY_PERIOD = "2y"      # 이동평균(120일) 계산을 위해 넉넉히 2년치 받아옴
SERIES_KEEP_ROWS = 260     # 최종적으로 보관할 최근 거래일 수 (1년 뷰 지원)
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


def build_idx_series(old_series):
    result = {}
    for key, symbol in IDX_SYMBOLS.items():
        def _do(symbol=symbol):
            h = fetch_history(symbol)
            closes = h["Close"]
            ma5 = closes.rolling(5).mean()
            ma20 = closes.rolling(20).mean()
            ma60 = closes.rolling(60).mean()
            ma120 = closes.rolling(120).mean()
            rows = []
            for ts in h.index[-SERIES_KEEP_ROWS:]:
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

        rows = retry(_do, f"지수 시세 수집 [{key}={symbol}]", default=None)
        result[key] = rows if rows else old_series.get(key, [])
    return result


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

def kr_symbol(ticker, market):
    suffix = ".KQ" if market == "KOSDAQ" else ".KS"
    return ticker if ticker.endswith((".KS", ".KQ")) else f"{ticker}{suffix}"


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


def update_major_stocks(old_list):
    out = []
    for item in old_list:
        symbol = kr_symbol(item["ticker"], item["market"])
        info = safe_get_info(symbol)
        new_item = dict(item)
        if info:
            price, chg_pct = price_and_chg(info, symbol)
            if price is not None:
                new_item["price"] = int(round(price))
                new_item["chgPct"] = chg_pct
            cap = info.get("marketCap")
            if cap:
                new_item["cap"] = round(cap / 1e12, 1)  # 조원 단위
        else:
            log(f"WARN: {item['name']}({symbol}) 정보 수집 실패, 이전 값 유지")
        out.append(new_item)
    return out


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


def update_stocks(old_list):
    out = []
    for item in old_list:
        symbol = item["ticker"]  # 이미 .KS 등 접미사 포함
        info = safe_get_info(symbol)
        new_item = dict(item)
        if not info:
            log(f"WARN: {item['name']}({symbol}) 상세 정보 수집 실패, 이전 값 전체 유지")
            out.append(new_item)
            continue

        price, chg_pct = price_and_chg(info, symbol)
        if price is not None:
            new_item["price"] = round(price, 2) if item["unit"] == "$" else int(round(price))
            new_item["chgPct"] = chg_pct

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

        out.append(new_item)
    return out


# ----------------------------------------------------------------------------
# 5. 뉴스 (Google 뉴스 RSS — API 키 불필요)
# ----------------------------------------------------------------------------

NEWS_QUERIES = ["코스피 마감", "글로벌 증시"]


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


def build_news(old_news):
    def _do():
        all_items = []
        seen_titles = set()
        for q in NEWS_QUERIES:
            for it in fetch_news_for_query(q):
                if it["t"] in seen_titles:
                    continue
                seen_titles.add(it["t"])
                all_items.append(it)

        def parse_date(s):
            for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:  # noqa: BLE001
                    continue
            return datetime.min

        all_items.sort(key=lambda x: parse_date(x["pubDate"]), reverse=True)
        top5 = all_items[:5]
        if not top5:
            raise RuntimeError("뉴스 0건 수집")
        return [{"t": it["t"], "src": it["src"], "link": it["link"]} for it in top5]

    news = retry(_do, "뉴스 수집", default=None)
    return news if news else old_news


# ----------------------------------------------------------------------------
# 6. 메인
# ----------------------------------------------------------------------------

def main():
    html, old = load_existing(HTML_PATH)
    log(f"기존 데이터 기준일: {old.get('asOf')}")

    idx_series = build_idx_series(old.get("IDX_SERIES", {}))
    vol_series = build_vol_series(old.get("VOL_SERIES", {}))
    nikkei_last2 = fetch_nikkei_last2()
    indices = build_indices(idx_series, vol_series, nikkei_last2, old.get("INDICES", []))
    major_stocks = update_major_stocks(old.get("MAJOR_STOCKS", []))
    stocks = update_stocks(old.get("STOCKS", []))
    news = build_news(old.get("NEWS", []))

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
        "IDX_SERIES": idx_series,
        "VOL_SERIES": vol_series,
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

    log(f"완료. asOf={as_of}, INDICES={len(indices)}, MAJOR_STOCKS={len(major_stocks)}, "
        f"STOCKS={len(stocks)}, NEWS={len(news)}")


if __name__ == "__main__":
    main()
