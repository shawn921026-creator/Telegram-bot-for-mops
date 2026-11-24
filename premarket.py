#!/usr/bin/env python3
"""
盤前市場整理 → Telegram（每個交易日 08:30）

資料來源
- 美股 / 指數 / 日韓：yfinance（前一交易日收盤）
- 注意自結：前一交易日「注意股票」名單（證交所 notice + 櫃買 openapi）
            → 公開資訊觀測站重大訊息「達公布注意交易資訊標準…財務業務」→ 解析單月 EPS / YoY
            上市另以證交所 opendata t187ap04_L（每日重大訊息）補
- 處置：證交所 announcement/punish（可查區間）、櫃買 openapi tpex_disposal_information（只列進行中，
        所以把看過的記錄存在 premarket_state.json，才算得出「出處置」）
    今日進處置   = 處置起日 == 今天
    今日出處置   = 處置迄日 == 前一交易日
    下交易日出處置 = 處置迄日 == 今天
- 除權息：證交所 exRight/TWT48U（除權除息預告表）、櫃買 openapi tpex_exright_prepost
- 今日法說：公開資訊觀測站 法人說明會一覽表 t100sb02_1（上市→上櫃→興櫃）
- 交易日：證交所休市日 + 週末
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from fetch import (BOT_TOKEN, CHAT_ID, fetch_material_detail,  # noqa: F401
                   fetch_material_list, log, tg_send_long)

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "premarket_state.json"
SEND_AT = (8, 30)

# 美股重點（顯示名稱, ticker），依漲跌幅由大到小排
US_STOCKS = [
    ("AMD", "AMD"), ("Google", "GOOGL"), ("Tesla", "TSLA"), ("Amazon", "AMZN"),
    ("Microsoft", "MSFT"), ("Apple", "AAPL"), ("NVIDIA", "NVDA"), ("TSMC ADR", "TSM"),
    ("Bloom Energy", "BE"), ("SuperMicro", "SMCI"),
]
US_INDICES = [("DJI", "^DJI"), ("SPX", "^GSPC"), ("NDX", "^NDX"), ("SOX", "^SOX")]
ASIA = [("日本", "^N225"), ("韓國", "^KS11")]

NEWS_PLACEHOLDER = "（新聞待編輯）"

INCLUDE_ETF = False  # 除權息是否列 ETF（代號 0 開頭）


# ─── HTTP ─────────────────────────────────────────────────────────────────────

def get_json(url, retries=4):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:
            if i == retries - 1:
                log(f"  ⚠ GET {url}: {e}")
                return None
            time.sleep(3 * (i + 1))


def roc_to_date(s):
    """'115/11/19'、'1151119'、'115年11月19日'、'115.11.19' → date"""
    nums = re.findall(r"\d+", s or "")
    if len(nums) == 1 and len(nums[0]) == 7:
        n = nums[0]
        nums = [n[:3], n[3:5], n[5:]]
    if len(nums) < 3:
        return None
    try:
        return date(int(nums[0]) + 1911, int(nums[1]), int(nums[2]))
    except ValueError:
        return None


def is_stock(code):
    return bool(re.fullmatch(r"[1-9]\d{3}", code or ""))


# ─── 交易日 ───────────────────────────────────────────────────────────────────

_closed = None
HOLIDAY_FILE = BASE / "holidays.json"
# 證交所 API 只回傳「最新公布」那一年，換年前拿不到今年的 → 手動補已知休市日，並把抓過的存檔累積
KNOWN_CLOSED = {"2026-12-25"}  # 115 年行憲紀念日（週五）休市


def closed_days():
    global _closed
    if _closed is None:
        try:
            saved = set(json.loads(HOLIDAY_FILE.read_text(encoding="utf-8")))
        except Exception:
            saved = set()
        _closed = {date.fromisoformat(x) for x in saved | KNOWN_CLOSED}
        for _ in (0,):
            d = get_json("https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
                         "?response=json") or {}
            for row in d.get("data") or []:
                try:
                    day = date.fromisoformat(row[0])
                except Exception:
                    day = roc_to_date(row[0])
                name = row[1] or ""
                # 「開始交易日」「最後交易日」仍有交易；其餘（含「市場無交易」）休市
                if day and ("無交易" in name or "交易" not in name):
                    _closed.add(day)
        try:
            HOLIDAY_FILE.write_text(json.dumps(sorted(x.isoformat() for x in _closed)), encoding="utf-8")
        except Exception:
            pass
    return _closed


def is_trading_day(d):
    return d.weekday() < 5 and d not in closed_days()


def shift_trading_day(d, step):
    d += timedelta(days=step)
    while not is_trading_day(d):
        d += timedelta(days=step)
    return d


# ─── 美股 / 指數 ─────────────────────────────────────────────────────────────

def fetch_pct(tickers, before, include_today=()):
    """before 當天（台北）以前的最後兩個收盤 → 漲跌幅；回測也適用。"""
    import yfinance as yf
    out = {}
    for t in tickers:
        try:
            df = yf.download(t, start=before - timedelta(days=12), end=before + timedelta(days=1),
                             interval="1d", progress=False, auto_adjust=False)
            closes = df[("Close", t)].dropna()
            # 美股取台北今天以前的收盤；日韓 08:00 已開盤，取今天盤中（回測時就是當天收盤）
            closes = closes[closes.index.date <= before] if t in include_today \
                else closes[closes.index.date < before]
            if len(closes) >= 2:
                out[t] = (float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1) * 100
        except Exception as e:
            log(f"  ⚠ {t}: {e}")
    return out


def fmt_pct(p):
    return "N/A" if p is None else f"{p:+.2f}%"


def section_markets(today):
    tickers = [t for _, t in US_INDICES + ASIA + US_STOCKS]
    q = fetch_pct(tickers, today, include_today={t for _, t in ASIA})
    log(f"  quotes {len(q)}/{len(tickers)}")
    idx = "、".join(f"{n} {fmt_pct(q.get(t))}" for n, t in US_INDICES)
    asia = "，".join(f"{n} {fmt_pct(q.get(t))}" for n, t in ASIA)
    stocks = sorted(US_STOCKS, key=lambda x: q.get(x[1], -1e9), reverse=True)
    us = "、".join(f"{n} {fmt_pct(q.get(t))}" for n, t in stocks)
    return f"{idx}\n{asia}\n\n美股重點\n{us}\n\n{NEWS_PLACEHOLDER}"


# ─── 注意自結 ─────────────────────────────────────────────────────────────────

NOTICE_KEY = "注意交易資訊"
NUM = r"-?\(?[\d,]+(?:\.\d+)?\)?"


def _num(tok):
    tok = tok.replace(",", "")
    neg = tok.startswith("(") or tok.startswith("-")
    v = float(tok.strip("()-"))
    return -v if neg else v


def parse_self_settle(text):
    """回傳 (單月EPS, YoY%)；抓第一個「每股盈餘」那列的數字。"""
    text = text.replace("\u3000", " ").replace("\r", "")
    text = re.sub(r"([-－])\s+(?=\d)", "-", text)  # 「- 0.58」「- 2.74%」
    line = None
    for m in re.finditer("每股盈餘", text):
        rest = re.sub(r"^\s*[（(]元[)）]", "", text[m.end():])
        if re.match(rf"[ \t]*{NUM}", rest):   # 跳過說明文字裡的「每股盈餘、每股淨值」
            line = rest.split("\n", 1)[0]
            break
    if line is None:
        return None
    toks = re.findall(rf"{NUM}%?|不適用|N/?A", line)
    nums = [t for t in toks if re.fullmatch(NUM, t)]
    if not nums:
        return None
    eps = _num(nums[0])
    pct_tok = next((t for t in toks if t.endswith("%")), None)
    if pct_tok:
        yoy = _num(pct_tok[:-1])
    elif len(nums) >= 2 and _num(nums[1]) != 0:   # 格式：本月 / 去年同月 / 增減% 但% 欄寫不適用
        yoy = (eps - _num(nums[1])) / abs(_num(nums[1])) * 100
    else:
        yoy = None
    return eps, yoy


NOTICE_LOOKBACK_DAYS = 14  # 「近期多次達注意標準」的公告，當天不一定在注意名單上


def notice_codes(d, state, include_twse=True):
    """近 NOTICE_LOOKBACK_DAYS 天（到 d 為止）曾列注意股票的代號 → 名稱（上市 + 上櫃）"""
    codes = {}
    start = d - timedelta(days=NOTICE_LOOKBACK_DAYS)
    if not include_twse:
        tw = {}
    else:
        tw = get_json("https://www.twse.com.tw/rwd/zh/announcement/notice?response=json&querytype=1"
                      f"&startDate={start:%Y%m%d}&endDate={d:%Y%m%d}") or {}
    for r in tw.get("data") or []:
        if is_stock(r[1]):
            codes[r[1]] = r[2].strip()
    seen = state.setdefault("tpex_notice", {})  # code -> [name, 最後注意日 iso]
    for r in get_json("https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information") or []:
        c, nd = r.get("SecuritiesCompanyCode", ""), roc_to_date(r.get("Date"))
        if is_stock(c) and nd:
            if c not in seen or seen[c][1] < nd.isoformat():
                seen[c] = [r.get("CompanyName", "").strip(), nd.isoformat()]
    for c, (name, nd) in list(seen.items()):
        if nd < (d - timedelta(days=60)).isoformat():
            del seen[c]
        elif start.isoformat() <= nd <= d.isoformat():
            codes.setdefault(c, name)
    return codes


def section_self_settle(prev_day, today, state):
    found = {}  # code -> (name, eps, yoy)

    # 1) 上市每日重大訊息 opendata（不用逐家查）
    for r in get_json("https://openapi.twse.com.tw/v1/opendata/t187ap04_L") or []:
        subj = r.get("主旨 ") or r.get("主旨") or ""
        if NOTICE_KEY in subj and prev_day <= (roc_to_date(r.get("發言日期")) or date.min) < today:
            res = parse_self_settle(r.get("說明") or "")
            if res:
                found[r["公司代號"]] = (r["公司名稱"].strip(), *res)

    # 2) 注意股票名單逐家查 MOPS（主要補上櫃；opendata 上櫃版會晚一天）
    # 即時跑：上市已由 opendata t187ap04_L 涵蓋，只需逐家查上櫃；回測才逐家查上市
    codes = notice_codes(prev_day, state, include_twse=(today != date.today()))
    log(f"  notice stocks {prev_day}: {len(codes)}")
    for code, name in codes.items():
        if code in found:
            continue
        for row in fetch_material_list(code):
            try:
                d = roc_to_date(row[2])
                subj = row[4] or ""
            except Exception:
                continue
            if not d or not (prev_day <= d < today) or NOTICE_KEY not in subj:
                continue
            params = (row[5] or {}).get("parameters") if isinstance(row[5], dict) else None
            detail = fetch_material_detail(params) if params else None
            text = (detail[-1] or "") if detail else ""
            res = parse_self_settle(text)
            if res:
                found[code] = (name, *res)
                break
        time.sleep(0.3)

    if not found:
        return "注意自結：\n無"
    lines = []
    for code, (name, eps, yoy) in found.items():
        y = f" (YoY {yoy:+.2f}%)" if yoy is not None else ""
        lines.append(f"{code} {name}：{eps:.2f}{y}")
    return "注意自結：\n" + "\n".join(lines)


# ─── 處置 ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"disposal": {}}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_disposals(today, state):
    """回傳 list of (code, name, start, end)；順便把櫃買記錄存進 state。"""
    recs = {}
    start = (today - timedelta(days=45)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    tw = get_json("https://www.twse.com.tw/rwd/zh/announcement/punish"
                  f"?response=json&startDate={start}&endDate={end}") or {}
    for r in tw.get("data") or []:
        code, name, period = r[2], r[3].strip(), r[6]
        parts = re.split(r"[～~]", period)
        if len(parts) == 2:
            recs[f"{code}|{parts[0]}"] = [code, name, parts[0].strip(), parts[1].strip()]

    for r in get_json("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information") or []:
        parts = (r.get("DispositionPeriod") or "").split("~")
        if len(parts) == 2:
            code = r.get("SecuritiesCompanyCode", "")
            state["disposal"][f"{code}|{parts[0]}"] = [code, r.get("CompanyName", "").strip(),
                                                       parts[0].strip(), parts[1].strip()]
    # 清掉 60 天前結束的
    cutoff = today - timedelta(days=60)
    state["disposal"] = {k: v for k, v in state["disposal"].items()
                         if (roc_to_date(v[3]) or today) >= cutoff}
    recs.update(state["disposal"])

    out = []
    for code, name, s, e in recs.values():
        sd, ed = roc_to_date(s), roc_to_date(e)
        if is_stock(code) and sd and ed:
            out.append((code, name, sd, ed))
    return out


def section_disposal(today, prev_day, state):
    recs = fetch_disposals(today, state)

    def fmt(items):
        seen, names = set(), []
        for code, name, *_ in sorted(items):
            if code not in seen:
                seen.add(code)
                names.append(f"{name}({code})")
        return "、".join(names) or "無"

    next_day = shift_trading_day(today, 1)
    starts_today = {r[0] for r in recs if r[2] == today}
    starts_next = {r[0] for r in recs if r[2] == next_day}
    enter = [r for r in recs if r[2] == today]
    # 迄日到了但緊接著又被處置（續處置）就不算出處置
    exit_today = [r for r in recs if r[3] == prev_day and r[0] not in starts_today]
    exit_next = [r for r in recs if r[3] == today and r[0] not in starts_next]
    return (f"今日進處置：{fmt(enter)}\n"
            f"今日出處置：{fmt(exit_today)}\n"
            f"下交易日出處置：{fmt(exit_next)}")


# ─── 除權息 ───────────────────────────────────────────────────────────────────

def _money(v):
    try:
        f = float(str(v).replace(",", ""))
    except ValueError:
        return None
    return f"{f:.4f}".rstrip("0").rstrip(".") if f else None


def fetch_exrights(past_days=()):
    """回傳 list of (date, code, name, cash, stock_ratio, sub_ratio, sub_price)"""
    out = []
    tw = get_json("https://www.twse.com.tw/rwd/zh/exRight/TWT48U?response=json") or {}
    for r in tw.get("data") or []:
        cash = re.sub(r"<[^>]+>", "", r[7] or "").strip()
        out.append((roc_to_date(r[0]), r[1], r[2].strip(), cash, r[4], r[5], r[6]))
    for d in past_days:
        t49 = get_json("https://www.twse.com.tw/rwd/zh/exRight/TWT49U?response=json"
                       f"&startDate={d:%Y%m%d}&endDate={d:%Y%m%d}") or {}
        for r in t49.get("data") or []:   # 權值+息值 拆不開，只有息就當現金股利
            v = _money(r[5])
            label = r[5] if r[6] == "息" else f"{'除權息' if r[6] == '權息' else '除權'}（權值+息值{v}元）"
            out.append((roc_to_date(r[0]), r[1], r[2].strip(), label, "", "", ""))
    for r in get_json("https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost") or []:
        out.append((roc_to_date(r.get("ExRrightsExDividendDate")), r.get("SecuritiesCompanyCode", ""),
                    r.get("CompanyName", "").strip(), r.get("CashDividend", ""),
                    r.get("StockDividendRatio", ""), r.get("SubscriptionRatioToNewSharesIssued", ""),
                    r.get("SubscriptionPricePerShare", "")))
    return out


def section_exrights(today, next_day):
    recs = fetch_exrights([d for d in (today, next_day) if d < date.today()])

    def fmt(d):
        lines, seen = [], set()
        for day, code, name, cash, ratio, sub_ratio, sub_price in sorted(recs, key=lambda x: x[1]):
            if day != d or code in seen:
                continue
            if not (is_stock(code) or (INCLUDE_ETF and code.startswith("0"))):
                continue
            seen.add(code)
            cash_s = _money(cash) if re.fullmatch(r"[\d.,]+", cash or "") else (cash or None)
            stock_s = _money(float(ratio) * 10) if _money(ratio) else None  # 配股率 → 每股配股（元）
            vals = []
            if cash_s:
                vals.append(f"{cash_s}元" if re.match(r"[\d.]", cash_s) else cash_s)
            if stock_s:
                vals.append(f"股票{stock_s}元")
            if _money(sub_ratio):
                vals.append(f"現增每股認{float(sub_ratio):.4f}股@{_money(sub_price)}元")
            if cash_s and len(vals) > 1:
                vals[0] = "現金" + vals[0]
            val = "、".join(vals) or "—"
            lines.append(f"{code} {name}：{val}")
        return "\n".join(lines) or "無"

    return f"今日除權息個股：\n{fmt(today)}\n\n下交易日除權息個股：\n{fmt(next_day)}"


# ─── 今日法說 ─────────────────────────────────────────────────────────────────
# 公開資訊觀測站「法人說明會一覽表」(t100sb02_1)：新版 MOPS 頁面也是 POST 到這支舊版 ajax

MOPS_CONF_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1"
CONF_MARKETS = ("sii", "otc", "rotc")  # 上市、上櫃、興櫃（順序即顯示順序）


def fetch_conferences(d, market):
    """回傳 [(code, name, date, time)]，該月份該市場全部法說會。"""
    import html as htmllib
    form = urllib.parse.urlencode({
        "encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1,
        "TYPEK": market, "year": d.year - 1911, "month": f"{d.month:02d}", "co_id": "",
    }).encode()
    for i in range(4):
        try:
            req = urllib.request.Request(MOPS_CONF_URL, data=form, method="POST",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                page = r.read().decode("utf-8", errors="replace")
            break
        except Exception as e:
            if i == 3:
                raise
            time.sleep(5 * (i + 1))
    if "查詢過於頻繁" in page or "Overrun" in page:
        raise RuntimeError("MOPS 回應查詢過於頻繁")
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [htmllib.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 4 and re.fullmatch(r"\d{4}", cells[0]):
            if "至" in cells[2]:
                continue  # 多日海外論壇（例 115/11/18 至 115/11/19）不列，同手動整理習慣
            out.append((cells[0], cells[1], roc_to_date(cells[2]), cells[3]))
    return out


def section_conferences(today):
    names = []
    for i, m in enumerate(CONF_MARKETS):
        if i:
            time.sleep(3)  # MOPS 連續查詢太快會擋
        rows = fetch_conferences(today, m)
        log(f"  法說 {m}: {len(rows)} rows this month")
        for code, name, d, _ in rows:
            if d == today and name not in names:
                names.append(name)
    return "今日法說：" + ("、".join(names) or "無")


# ─── Main ─────────────────────────────────────────────────────────────────────

def build(today):
    prev_day = shift_trading_day(today, -1)
    next_day = shift_trading_day(today, 1)
    state = load_state()
    parts = [f"盤前市場整理_{today:%Y_%m%d}"]
    for name, fn in [
        ("markets", lambda: section_markets(today)),
        ("self_settle", lambda: section_self_settle(prev_day, today, state)),
        ("disposal", lambda: section_disposal(today, prev_day, state)),
        ("exrights", lambda: section_exrights(today, next_day)),
        ("conferences", lambda: section_conferences(today)),
    ]:
        try:
            parts.append(fn())
        except Exception as e:
            import traceback
            log(f"  ⚠ section {name}: {e}\n{traceback.format_exc()}")
            parts.append(f"（{name} 抓取失敗：{e}）")
    save_state(state)
    return "\n\n".join(parts)


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    force = "--force" in args
    day_arg = next((a for a in args if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a)), None)
    today = date.fromisoformat(day_arg) if day_arg else date.today()

    log("=== premarket start ===")
    if not force and not is_trading_day(today):
        log(f"  {today} 非交易日，略過")
        return

    if not dry and not day_arg:
        # GitHub cron 會提早觸發 / 延遲；提早就等到 08:30
        target = datetime.combine(today, datetime.min.time()).replace(hour=SEND_AT[0], minute=SEND_AT[1])
        wait = (target - datetime.now()).total_seconds() - 900  # 先預留 15 分鐘抓資料（逐家查 MOPS 約 3–5 分）
        if 0 < wait < 3600:
            log(f"  sleep {wait:.0f}s")
            time.sleep(wait)

    msg = build(today)
    if dry:
        print(msg)
    else:
        if not day_arg:
            wait = (datetime.combine(today, datetime.min.time())
                    .replace(hour=SEND_AT[0], minute=SEND_AT[1]) - datetime.now()).total_seconds()
            if 0 < wait < 600:
                time.sleep(wait)
        tg_send_long(msg)
    log("=== premarket done ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        try:
            tg_send_long(f"⚠️ 盤前市場整理 bot 異常: {e}")
        except Exception:
            pass
        sys.exit(1)
