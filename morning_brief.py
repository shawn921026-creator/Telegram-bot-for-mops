#!/usr/bin/env python3
"""
每日 08:20 早報 → Telegram
1. 光通美股: AVGO, LITE, COHR, VIAV
   PCB 美股: TTMI (TTM Technologies)
2. 日韓指數 + 代表性個股
5. 原物料: 銅, Brent 原油, 鋁
6. 航運代理: BDRY, ZIM, MATX
7. 貴金屬: 黃金, 白銀, 鈀, 鉑
"""

import json
import sys
import time
import urllib.parse
import urllib.request
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
from pathlib import Path

from fetch import BOT_TOKEN, CHAT_ID  # 環境變數或 secrets.json

TG_BASE   = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOG_FILE  = Path(__file__).resolve().parent / "run.log"


# ─── logging ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── Telegram ─────────────────────────────────────────────────────────────────

def tg_send(text, parse_mode="HTML"):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(f"{TG_BASE}/sendMessage", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if not res.get("ok"):
                log(f"  ⚠ TG: {res}")
    except Exception as e:
        log(f"  ⚠ TG error: {e}")


# ─── Data fetching ─────────────────────────────────────────────────────────────

def fetch_quotes(tickers: list[str]) -> dict:
    """
    回傳 {ticker: (close, chg, pct)} 使用前一完整交易日收盤。
    """
    import yfinance as yf

    result = {}
    for t in tickers:
        try:
            data = yf.download(t, period="5d", interval="1d", progress=False)
            if data.empty:
                log(f"  ⚠ {t}: no data")
                continue
            closes = data[("Close", t)].dropna()
            if len(closes) < 2:
                log(f"  ⚠ {t}: < 2 rows")
                continue
            close = float(closes.iloc[-1])
            prev  = float(closes.iloc[-2])
            chg   = close - prev
            pct   = chg / prev * 100
            result[t] = (close, chg, pct)
        except Exception as e:
            log(f"  ⚠ {t}: {e}")
    return result


# ─── Formatting helpers ────────────────────────────────────────────────────────

def _dot(pct):
    if pct is None:
        return "⚪"
    return "🟢" if pct >= 0 else "🔴"

def _pct(pct):
    if pct is None:
        return "—"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"

def _p(price, dec=2):
    if price is None:
        return "—"
    return f"{price:,.{dec}f}"

def _row(dot, label, ticker, price, pct, unit="$", dec=2, note=""):
    n = f" <i>{note}</i>" if note else ""
    return f"{dot} {label} ({ticker})  {unit}{_p(price, dec)}  {_pct(pct)}{n}"

def _idx_row(dot, label, price, pct, dec=0):
    return f"{dot} {label}  {_p(price, dec)}  {_pct(pct)}"


# ─── Sections ─────────────────────────────────────────────────────────────────

OPTICAL_PCB = [
    # (ticker, display_name, group, unit, dec)
    ("AVGO",  "Broadcom",       "光通", "$", 2),
    ("LITE",  "Lumentum",       "光通", "$", 2),
    ("COHR",  "Coherent",       "光通", "$", 2),
    ("VIAV",  "Viavi Solutions","光通", "$", 2),
    ("TTMI",  "TTM Tech",       "PCB",  "$", 2),
]

JAPAN = [
    ("^N225",   "Nikkei 225",       "idx", 0),
    ("1306.T",  "TOPIX ETF",        "idx", 0),
    ("8035.T",  "Tokyo Electron",   "stk", 0),
    ("4062.T",  "Ibiden",           "stk", 0),
]

KOREA = [
    ("^KS11",    "KOSPI",                    "idx", 0),
    ("^KQ11",    "KOSDAQ",                   "idx", 2),
    ("000660.KS","SK Hynix",                 "stk", 0),
    ("009150.KS","Samsung E-Mec",            "stk", 0),
]

COMMODITIES = [
    ("HG=F",  "銅",       "$/lb",  4),
    ("BZ=F",  "Brent 原油","$/bbl", 2),
    ("ALI=F", "鋁",       "$/MT",  0),
]

SHIPPING = [
    ("BDRY", "Breakwave Dry ETF", "$",  2),
    ("ZIM",  "ZIM Shipping",      "$",  2),
    ("MATX", "Matson",            "$",  2),
]

METALS = [
    ("GC=F", "黃金", "$/oz",  2),
    ("SI=F", "白銀", "$/oz",  3),
    ("PA=F", "鈀金", "$/oz",  0),
    ("PL=F", "鉑金", "$/oz",  0),
]


def build_optical_pcb(q):
    lines = ["<b>🔆 光通 / PCB 美股（前日收盤）</b>"]
    cur_group = None
    for ticker, name, group, unit, dec in OPTICAL_PCB:
        if group != cur_group:
            cur_group = group
            lines.append(f"\n<b>── {group} ──</b>")
        data = q.get(ticker)
        if data:
            price, chg, pct = data
            lines.append(f"  {_dot(pct)} {name} ({ticker})  {unit}{_p(price, dec)}  {_pct(pct)}")
        else:
            lines.append(f"  ⚪ {name} ({ticker})  N/A")
    return "\n".join(lines)


def build_japan_korea(q):
    lines = ["<b>🌏 日韓（前日收盤）</b>"]

    lines.append("\n<b>🇯🇵 日本</b>")
    for ticker, name, kind, dec in JAPAN:
        data = q.get(ticker)
        if data:
            price, chg, pct = data
            lines.append(f"  {_dot(pct)} {name}  {_p(price, dec)}  {_pct(pct)}")
        else:
            lines.append(f"  ⚪ {name}  N/A")

    lines.append("\n<b>🇰🇷 韓國</b>")
    for ticker, name, kind, dec in KOREA:
        data = q.get(ticker)
        if data:
            price, chg, pct = data
            lines.append(f"  {_dot(pct)} {name}  {_p(price, dec)}  {_pct(pct)}")
        else:
            lines.append(f"  ⚪ {name}  N/A")

    return "\n".join(lines)


def build_commodities(q):
    lines = ["<b>⛏️ 上游原物料（前日）</b>"]
    for ticker, name, unit, dec in COMMODITIES:
        data = q.get(ticker)
        if data:
            price, chg, pct = data
            lines.append(f"  {_dot(pct)} {name}  {_p(price, dec)} {unit}  {_pct(pct)}")
        else:
            lines.append(f"  ⚪ {name}  N/A")
    return "\n".join(lines)


def build_shipping(q):
    lines = ["<b>🚢 貨櫃航運（前日，股價代理）</b>"]
    for ticker, name, unit, dec in SHIPPING:
        data = q.get(ticker)
        if data:
            price, chg, pct = data
            lines.append(f"  {_dot(pct)} {name} ({ticker})  {unit}{_p(price, dec)}  {_pct(pct)}")
        else:
            lines.append(f"  ⚪ {name}  N/A")
    return "\n".join(lines)


def build_metals(q):
    lines = ["<b>🥇 貴金屬（前日）</b>"]
    for ticker, name, unit, dec in METALS:
        data = q.get(ticker)
        if data:
            price, chg, pct = data
            lines.append(f"  {_dot(pct)} {name}  {_p(price, dec)} {unit}  {_pct(pct)}")
        else:
            lines.append(f"  ⚪ {name}  N/A")
    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== morning brief start ===")

    all_tickers = (
        [t for t, *_ in OPTICAL_PCB] +
        [t for t, *_ in JAPAN] +
        [t for t, *_ in KOREA] +
        [t for t, *_ in COMMODITIES] +
        [t for t, *_ in SHIPPING] +
        [t for t, *_ in METALS]
    )

    q = fetch_quotes(all_tickers)
    log(f"  fetched {len(q)}/{len(all_tickers)} tickers")

    now = datetime.now()
    header = f"📋 <b>早報 {now:%Y-%m-%d %H:%M}</b>"

    msg = "\n\n".join([
        header,
        build_optical_pcb(q),
        build_japan_korea(q),
        build_commodities(q),
        build_shipping(q),
        build_metals(q),
    ])

    tg_send(msg)
    log("=== morning brief done ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        try:
            tg_send(f"⚠️ 早報 bot 異常: {e}", parse_mode=None)
        except Exception:
            pass
        sys.exit(1)
