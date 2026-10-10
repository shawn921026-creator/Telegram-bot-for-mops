#!/usr/bin/env python3
"""
MOPS 重大訊息 + 月營收 → Telegram bot
- 重大訊息：分類後推；財報附圖卡、董事會/澄清補完整內文
- 月營收：每天檢查，新公布就推 YoY/MoM
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_secret(key):
    """雲端用環境變數，本機讀 secrets.json（不進版控）。"""
    val = os.environ.get(key)
    if val:
        return val
    try:
        return json.loads((BASE / "secrets.json").read_text(encoding="utf-8"))[key]
    except Exception:
        sys.exit(f"missing {key}: set env var or secrets.json")


BOT_TOKEN = _load_secret("BOT_TOKEN")
CHAT_ID = _load_secret("CHAT_ID")

COMPANIES = {
    "2383": "台光電",
    "6274": "台燿",
    "6213": "聯茂",
    "3037": "欣興",
    "2368": "金像電",
    "5439": "高技",
    "4979": "華星光",
    "6451": "訊芯-KY",
    "3189": "景碩",
    "8046": "南電",
    "4958": "臻鼎-KY",
    "1802": "台玻",
    "1815": "富喬",
    "5475": "德宏",
    "8021": "尖點",
    "2454": "聯發科",
    "8358": "金居",
    "4722": "國精化",
    "4764": "雙鍵",
    "1303": "南亞",
    "8155": "博智",
    "3715": "定穎投控",
    "5340": "建榮",
    "8210": "勤誠",
    "5284": "Jpp-KY",
    "1582": "信錦",
}

LOOKBACK_DAYS = 7
MOPS_BASE = "https://mops.twse.com.tw/mops/api"
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# 在這些時間點推送一則「巡查完成」摘要 (HH:MM；到點後 45 分內的第一次執行就發)
HEARTBEAT_HHMM = ("00:00", "08:30", "13:30", "18:30")
HEARTBEAT_GRACE_MIN = 45
# 每天這個小時之後的第一次執行，推當日行事曆（法說會 / 財報董事會）
CALENDAR_HOUR = 8
CALENDAR_AHEAD_DAYS = 30
EVENT_BACKFILL_DAYS = 60

SEEN_FILE = BASE / "seen.json"
SEEN_REV_FILE = BASE / "seen_revenue.json"
EVENTS_FILE = BASE / "events.json"
STATE_FILE = BASE / "state.json"
IMG_DIR = BASE / "images"
LOG_FILE = BASE / "run.log"

CJK_FONT = next((p for p in (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
) if os.path.exists(p)), None)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------- HTTP helpers ----------

def wait_for_network(max_wait_sec=120):
    """Wake-from-sleep DNS 還沒就緒時等網路。每 5 秒探一次 mops。"""
    import socket
    waited = 0
    while waited < max_wait_sec:
        try:
            socket.gethostbyname("mops.twse.com.tw")
            if waited > 0:
                log(f"  network ready after {waited}s wait")
            return True
        except Exception:
            time.sleep(5)
            waited += 5
    log(f"  ⚠ network not ready after {max_wait_sec}s, proceeding anyway")
    return False


def post_json(url, payload, timeout=25, retries=6):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://mops.twse.com.tw",
        "Referer": "https://mops.twse.com.tw/mops/",
    }
    delays = [3, 6, 10, 15, 20, 30]
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(delays[attempt])


# ---------- MOPS APIs ----------

def fetch_material_list(company_id, roc_year=None):
    """roc_year 有給就只抓那一年整年，否則抓今年（跨年頭幾天補抓去年 12 月）。"""
    today = datetime.now()
    whole_year = roc_year is not None
    roc_year = roc_year or today.year - 1911
    payloads = [{"companyId": company_id, "year": str(roc_year),
                 "month": "all", "firstDay": "", "lastDay": ""}]
    if not whole_year and today.month == 1 and today.day <= LOOKBACK_DAYS:
        payloads.append({"companyId": company_id, "year": str(roc_year - 1),
                         "month": "12", "firstDay": "", "lastDay": ""})
    rows = []
    for p in payloads:
        try:
            resp = post_json(f"{MOPS_BASE}/t05st01", p)
        except Exception as e:
            log(f"  ⚠ {company_id} list fetch error: {e}")
            continue
        if resp.get("code") != 200:
            log(f"  ⚠ {company_id} list resp: {resp.get('code')} {resp.get('message')}")
            continue
        rows.extend((resp.get("result") or {}).get("data") or [])
    return rows


def fetch_material_detail(detail_params):
    try:
        resp = post_json(f"{MOPS_BASE}/t05st01_detail", detail_params)
    except Exception as e:
        log(f"  ⚠ detail fetch error: {e}")
        return None
    if resp.get("code") != 200:
        return None
    data = (resp.get("result") or {}).get("data") or []
    return data[0] if data else None


def fetch_company_summary(company_id):
    try:
        resp = post_json(f"{MOPS_BASE}/t146sb05", {"companyId": company_id})
    except Exception as e:
        log(f"  ⚠ {company_id} summary error: {e}")
        return None
    if resp.get("code") != 200:
        return None
    return resp.get("result") or {}


# ---------- Parsing ----------

def parse_roc_datetime(date_str, time_str):
    p = date_str.split("/")
    dt = datetime(int(p[0]) + 1911, int(p[1]), int(p[2]))
    try:
        h, m, s = [int(x) for x in time_str.split(":")]
        dt = dt.replace(hour=h, minute=m, second=s)
    except Exception:
        pass
    return dt


def material_key(row):
    detail = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
    p = detail.get("parameters") or {}
    return f"{p.get('companyId')}|{p.get('enterDate')}|{p.get('serialNumber')}"


def categorize(subject):
    s = subject or ""
    # 自結（單月 / 單季 / 年度都算，例：公告本公司2026年第2季自結合併損益）
    if "自結" in s:
        return "self_settle"
    # 要排在財務報告之前：主旨同時含「財務報告」
    if "董事會" in s and "預計召開" in s:
        return "board_date"
    if "法人說明會" in s or "法說" in s:
        return "investor_conf"
    if any(k in s for k in ("財務報告", "合併財務", "個體財務")):
        return "financial_report"
    if any(k in s for k in ("澄清", "重大訊息說明", "不實")):
        return "clarification"
    if "董事會" in s:
        return "board_meeting"
    if any(k in s for k in ("營業收入", "月份營收", "月營收")):
        return "monthly_revenue_announce"  # skip; revenue module handles
    return "general"


FINANCIAL_PATTERNS = {
    "period":           r"報導期間\s*起訖日期[^:：]*[:：]\s*([0-9./~\- 年月日]+)",
    "cum_revenue":      r"累計至本期止營業收入\s*\(仟元\)[:：]\s*([0-9,\-()]+)",
    "cum_gross_profit": r"累計至本期止營業毛利[^(]*\(仟元\)[:：]\s*([0-9,\-()]+)",
    "cum_op_income":    r"累計至本期止營業利益[^(]*\(仟元\)[:：]\s*([0-9,\-()]+)",
    "cum_pretax":       r"累計至本期止稅前淨利[^(]*\(仟元\)[:：]\s*([0-9,\-()]+)",
    "cum_net_income":   r"累計至本期止本期淨利[^(]*\(仟元\)[:：]\s*([0-9,\-()]+)",
    "cum_eps":          r"累計至本期止基本每股盈餘[^(]*\(元\)[:：]\s*([0-9,\.\-]+)",
    "total_assets":     r"期末總資產\s*\(仟元\)[:：]\s*([0-9,\-]+)",
    "total_liab":       r"期末總負債\s*\(仟元\)[:：]\s*([0-9,\-]+)",
}


def parse_financial(text):
    out = {}
    for key, pat in FINANCIAL_PATTERNS.items():
        m = re.search(pat, text or "")
        if m:
            out[key] = m.group(1).strip()
    return out


# ---------- 內文欄位解析 ----------

def split_items(text):
    """把「1.標題:內容」式的重訊內文拆成 [(標題, 內容)]。
    只認連號的項次，避免把內容裡的「1.」「2.」小標誤當成新項目。"""
    items, cur, expect = [], None, 1
    for line in (text or "").replace("\r", "").split("\n"):
        m = re.match(r"\s*(\d{1,2})[.、]\s*(.*)", line)
        if m and int(m.group(1)) == expect:
            cur = [m.group(2)]
            items.append(cur)
            expect += 1
        elif cur is not None:
            cur.append(line)
    out = []
    for lines in items:
        block = "\n".join(lines)
        # 標題結束在第一個括號外的冒號（標題本身常折行、括號內有說明文字）
        depth, cut = 0, None
        for i, ch in enumerate(block):
            if ch in "(（":
                depth += 1
            elif ch in ")）":
                depth = max(0, depth - 1)
            elif ch in ":：" and depth == 0:
                cut = i
                break
        if cut is None:
            out.append((block.replace("\n", "").strip(), ""))
        else:
            out.append((block[:cut].replace("\n", "").strip(), block[cut + 1:].strip()))
    return out


def find_item(items, keys):
    for title, value in items:
        if any(k in title for k in keys):
            return value
    return ""


def one_line(value):
    return " / ".join(l.strip() for l in value.split("\n") if l.strip())


def add_yi(value):
    """金額後補億元換算：2,101,075仟元 → 2,101,075仟元(≈21.01億)。"""
    def repl(m):
        n = float(m.group(1).replace(",", ""))
        return f"{m.group(0)}(≈{n / 1e5:,.2f}億)" if n >= 1e4 else m.group(0)
    value = re.sub(r"([\d,]*\d(?:\.\d+)?)\s*[仟千]元", repl, value)
    # 直接以元表示的大額：10,088,880,000元 → (≈100.89億)
    return re.sub(r"(?<![\d,.])(\d[\d,]{8,})元",
                  lambda m: f"{m.group(0)}(≈{float(m.group(1).replace(',', '')) / 1e8:,.2f}億)", value)


def parse_any_date(s):
    """115/10/28、民國115年10月28日、2026/10/28 → date；抓不到回 None。"""
    m = re.search(r"(\d{2,4})\s*[/年.\-]\s*(\d{1,2})\s*[/月.\-]\s*(\d{1,2})", s or "")
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y + 1911 if y < 1911 else y, mo, d)
    except ValueError:
        return None


# 取得/處分資產公告（第20款等）要摘出來的欄位：(顯示名稱, 標題關鍵字)
ASSET_FIELDS = [
    ("標的", ("標的物之名稱", "證券名稱", "標的名稱")),
    ("事實發生日", ("事實發生日", "交易日期")),
    ("數量/金額", ("交易總金額", "交易金額")),
    ("交易相對人", ("交易相對人及其與公司之關係",)),
    ("關係人交易", ("本次交易為關係人交易",)),
    ("用途", ("具體目的或用途",)),
    ("處分損益", ("處分利益",)),
    ("估價", ("估價金額",)),
    ("付款條件", ("付款條件",)),
    ("董事會通過", ("董事會通過日期",)),
]
EMPTY_VALUES = ("", "不適用", "無", "否", "否或不適用", "NA", "N/A")


def is_asset_text(text):
    return "交易總金額" in text or "交易相對人及其與公司之關係" in text


def compose_asset(name, code, dt, subject, text):
    """摘出花多少錢、買什麼、跟誰買；欄位抓不到兩個以上就回 None 改送全文。"""
    items = split_items(text)
    fields = []
    for label, keys in ASSET_FIELDS:
        value = one_line(find_item(items, keys))
        if value.rstrip("。 ") not in EMPTY_VALUES:
            fields.append(f"▪️{label}：{add_yi(value)}")
    if len(fields) < 2:
        return None
    return (
        f"🏭 {name} ({code}) 取得/處分資產\n"
        f"📅 {dt:%Y-%m-%d %H:%M:%S}\n"
        f"📝 {subject.strip()}\n\n" + "\n".join(fields)
    )


EVENT_KINDS = {"investor_conf": "法說會", "board_date": "財報董事會"}


def extract_event(kind, name, code, subject, text):
    """從法說會 / 董事會預計召開日期公告抓出事件日期。"""
    items = split_items(text)
    if kind == "investor_conf":
        d = parse_any_date(find_item(items, ("法人說明會之日期", "日期")))
        t = re.search(r"(\d{1,2})\s*[時:：]\s*(\d{1,2})", find_item(items, ("時間",)))
        ev = {
            "time": f"{int(t.group(1)):02d}:{int(t.group(2)):02d}" if t else "",
            "place": one_line(find_item(items, ("地點",))),
            "desc": one_line(find_item(items, ("擇要訊息",))) or one_line(subject),
        }
    else:
        d = (parse_any_date(find_item(items, ("預計召開日期",)))
             or parse_any_date(subject.split("預計召開")[-1]))
        ev = {"time": "", "place": "",
              "desc": one_line(find_item(items, ("年季", "財務報告"))) or one_line(subject)}
    if not d:
        return None
    ev.update({"type": EVENT_KINDS[kind], "code": code, "name": name, "date": d.isoformat()})
    return ev


# ---------- 行事曆 ----------

WEEKDAYS = "一二三四五六日"


def report_window(today):
    """目前所處的財報公告期 → (標籤, 起日, 法定期限)；不在公告期回 None。"""
    y = today.year
    for label, start, end in (
        (f"{y - 1} 年報", date(y, 1, 1), date(y, 3, 31)),
        (f"{y}Q1", date(y, 4, 1), date(y, 5, 15)),
        (f"{y}Q2", date(y, 7, 1), date(y, 8, 14)),
        (f"{y}Q3", date(y, 10, 1), date(y, 11, 14)),
    ):
        if start <= today <= end:
            return label, start, end
    return None


def last_year_report_dates(state, win):
    """去年同一期財報的公告日，給還沒預告的公司當參考；每期只抓一次存在 state。"""
    label, start, end = win
    cache = state.get("last_year_report") or {}
    if cache.get("label") == label:
        return cache["dates"]
    lo, hi = start.replace(year=start.year - 1), end.replace(year=end.year - 1)
    dates = {}
    for code in COMPANIES:
        for row in fetch_material_list(code, roc_year=lo.year - 1911):
            d = parse_any_date(row[2])
            subject = row[4] or ""
            if (d and lo <= d <= hi and "更正" not in subject
                    and categorize(subject) == "financial_report"):
                dates[code] = min(dates.get(code, "9"), d.isoformat())
    if dates:
        state["last_year_report"] = {"label": label, "dates": dates}
    return dates


def compose_calendar(events, seen, today, last_year=None):
    """未來的法說會 / 財報董事會，加上本季財報公告進度。沒東西可報回 None。"""
    out = []
    upcoming = sorted(
        (e for e in events.values()
         if today <= date.fromisoformat(e["date"]) <= today + timedelta(days=CALENDAR_AHEAD_DAYS)),
        key=lambda e: (e["date"], e["time"], e["code"]))
    if upcoming:
        out.append(f"📅 未來 {CALENDAR_AHEAD_DAYS} 天行事曆")
        for e in upcoming:
            d = date.fromisoformat(e["date"])
            when = "今天" if d == today else f"{d:%m/%d}({WEEKDAYS[d.weekday()]})"
            icon = "🎤" if e["type"] == "法說會" else "🗓"
            extra = " ".join(x for x in (e["time"], e["place"]) if x)
            out.append(f"{when} {icon} {e['name']} {e['type']}"
                       + (f" {extra}" if extra else "") + f"\n      {e['desc'][:60]}")

    win = report_window(today)
    if win:
        label, start, end = win
        announced, scheduled = {}, {}
        for v in seen.values():
            d = parse_any_date(v.get("date"))
            if (d and start <= d <= end and "更正" not in v["subject"]
                    and categorize(v["subject"]) == "financial_report"):
                announced[v["code"]] = d
        for e in events.values():
            d = date.fromisoformat(e["date"])
            if e["type"] == "財報董事會" and start <= d and e["code"] not in announced:
                scheduled[e["code"]] = d
        last_year = last_year or {}
        pending = [n + (f"(去年{date.fromisoformat(last_year[c]):%m/%d})" if c in last_year else "")
                   for c, n in sorted(COMPANIES.items(), key=lambda x: last_year.get(x[0], "9"))
                   if c not in announced and c not in scheduled]

        def names(m):
            return "、".join(f"{COMPANIES.get(c, c)} {d:%m/%d}"
                            for c, d in sorted(m.items(), key=lambda x: x[1])) or "—"
        out.append(f"{chr(10) if out else ''}📊 {label} 財報進度（法定期限 {end:%m/%d}）")
        out.append(f"✅ 已公告：{names(announced)}")
        out.append(f"🗓 已預告董事會：{names(scheduled)}")
        out.append(f"⏳ 尚未預告（{len(pending)}）：{'、'.join(pending) or '—'}")
    return "\n".join(out) if out else None


# ---------- Image rendering ----------

def _wrap_cjk(text, max_chars):
    """Naive CJK-friendly wrap by character count."""
    out = []
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if not line:
            out.append("")
            continue
        while len(line) > max_chars:
            out.append(line[:max_chars])
            line = line[max_chars:]
        out.append(line)
    return out


def render_detail_card(company, code, dt, kind_label, subject, content_text, out_path):
    """Render the FULL 內文 as a clean image — no parsing, nothing missing."""
    from PIL import Image, ImageDraw, ImageFont
    f_title = ImageFont.truetype(CJK_FONT, 30)
    f_sub = ImageFont.truetype(CJK_FONT, 19)
    f_body = ImageFont.truetype(CJK_FONT, 21)
    f_foot = ImageFont.truetype(CJK_FONT, 14)

    W = 920
    margin_x = 40
    body_max_chars = 42
    body_line_h = 30

    subject_lines = _wrap_cjk(subject or "", 50)
    content_lines = _wrap_cjk(content_text or "(無詳細內容)", body_max_chars)

    header_h = 70 + 28 * len(subject_lines) + 10
    body_h = body_line_h * len(content_lines) + 24
    H = header_h + body_h + 50

    img = Image.new("RGB", (W, H), (250, 251, 254))
    d = ImageDraw.Draw(img)

    d.rectangle((0, 0, W, header_h), fill=(28, 41, 89))
    d.text((margin_x, 18), f"{kind_label} · {company} ({code})",
           fill=(255, 255, 255), font=f_title)
    d.text((margin_x, 56), f"{dt:%Y-%m-%d %H:%M:%S}",
           fill=(190, 200, 230), font=f_sub)
    sy = 84
    for s in subject_lines:
        d.text((margin_x, sy), s, fill=(220, 225, 245), font=f_sub)
        sy += 26

    by = header_h + 12
    for line in content_lines:
        stripped = line.lstrip()
        if stripped[:3].rstrip(".").isdigit() or (len(stripped) >= 2 and stripped[1] == "." and stripped[0].isdigit()):
            color = (28, 41, 89)
        else:
            color = (60, 65, 90)
        d.text((margin_x + 10, by), line, fill=color, font=f_body)
        by += body_line_h

    d.text((margin_x, H - 28),
           f"資料來源: 公開資訊觀測站 t05st01_detail · 產生於 {datetime.now():%Y-%m-%d %H:%M}",
           fill=(140, 140, 150), font=f_foot)
    img.save(out_path, "PNG", optimize=True)
    return out_path


# ---------- Telegram ----------

def tg_send_message(text, parse_mode=None):
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(f"{TG_BASE}/sendMessage", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if not res.get("ok"):
                log(f"  ⚠ TG sendMessage: {res}")
            return res.get("ok", False)
    except Exception as e:
        log(f"  ⚠ TG sendMessage error: {e}")
        return False


def tg_send_photo(photo_path, caption=""):
    boundary = "----mopsboundary" + os.urandom(8).hex()
    parts = []

    def add_field(name, value):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8") if isinstance(value, str) else value)
        parts.append(b"\r\n")

    add_field("chat_id", CHAT_ID)
    add_field("caption", caption[:1024])
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="photo"; filename="{os.path.basename(photo_path)}"\r\n'
        f'Content-Type: image/png\r\n\r\n'.encode())
    with open(photo_path, "rb") as f:
        parts.append(f.read())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{TG_BASE}/sendPhoto", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if not res.get("ok"):
                log(f"  ⚠ TG sendPhoto: {res}")
            return res.get("ok", False)
    except Exception as e:
        log(f"  ⚠ TG sendPhoto error: {e}")
        return False


# ---------- State ----------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return None


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Composition ----------

def tg_send_long(text, limit=3800):
    """超過 Telegram 單則上限就依行切成多則，內文不截斷。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            chunks.append(cur + line[:limit - len(cur)])
            line, cur = line[limit - len(cur):], ""
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    ok = True
    for c in chunks:
        if c.strip():
            ok = tg_send_message(c.rstrip("\n")) and ok
            time.sleep(0.4)
    return ok


def compose_general(name, code, dt, subject):
    return (
        f"📢 {name} ({code})\n"
        f"📅 {dt:%Y-%m-%d %H:%M:%S}\n"
        f"📝 {subject.strip()}"
    )


def compose_with_detail(icon, name, code, dt, subject, detail_row):
    body = compose_general(name, code, dt, subject).replace("📢", icon, 1)
    if detail_row and len(detail_row) >= 9:
        spokesperson = (detail_row[3] or "").strip()
        title = (detail_row[4] or "").strip()
        content = (detail_row[-1] or "").strip()
        body += f"\n\n👤 發言人: {spokesperson} ({title})"
        body += f"\n\n📄 詳細內容:\n{content}"
    return body


def compose_revenue(name, code, info):
    rev_blocks = info.get("revenueInformation") or []
    if not rev_blocks:
        return None
    ytd = rev_blocks[0]["data"] if len(rev_blocks) >= 1 else None
    latest = rev_blocks[1] if len(rev_blocks) >= 2 else None
    if not latest:
        return None
    period = latest["titles"][0]["main"]
    cur, prev_y, yoy = latest["data"]
    msg = [f"💰 {name} ({code}) 月營收公布"]
    msg.append(f"📅 {period}")
    msg.append(f"  本月: {cur} 仟元")
    msg.append(f"  去年同期: {prev_y} 仟元")
    msg.append(f"  YoY: {yoy}")
    if len(rev_blocks) >= 3:
        prev_m = rev_blocks[2]
        prev_period = prev_m["titles"][0]["main"]
        prev_cur = prev_m["data"][0]
        msg.append(f"  上月 ({prev_period}): {prev_cur} 仟元")
        try:
            cur_n = float(cur.replace(",", ""))
            pm_n = float(prev_cur.replace(",", ""))
            if pm_n > 0:
                mom = (cur_n - pm_n) / pm_n * 100
                msg.append(f"  MoM: {mom:+.2f}%")
        except Exception:
            pass
    if ytd and len(ytd) >= 3:
        msg.append(f"\n📊 YTD 累計: {ytd[0]} 仟元 (YoY {ytd[2]})")
    return "\n".join(msg)


def revenue_key(code, info):
    blocks = info.get("revenueInformation") or []
    if len(blocks) < 2:
        return None
    period = blocks[1]["titles"][0]["main"]
    return f"{code}|{period}"


# ---------- Main ----------

# 本次執行抓到資料的公司數 / 總筆數；全部抓不到代表 MOPS 被擋或網路斷
RUN_STATS = {"companies_ok": 0, "rows": 0}


def process_material_info(seen, first_run, backfill_events=False):
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    backfill_cutoff = datetime.now() - timedelta(days=EVENT_BACKFILL_DAYS)
    queue = []  # (datetime, kind, payload)
    backfill = []  # 行事曆第一次建立時，回頭補抓近期的法說會 / 董事會日期公告

    for code, name in COMPANIES.items():
        rows = fetch_material_list(code)
        RUN_STATS["companies_ok"] += bool(rows)
        RUN_STATS["rows"] += len(rows)
        n_recent = 0
        for row in rows:
            try:
                dt = parse_roc_datetime(row[2], row[3])
            except Exception:
                continue
            key = material_key(row)
            subject = (row[4] or "").strip()
            if backfill_events and dt >= backfill_cutoff and categorize(subject) in EVENT_KINDS:
                backfill.append({"code": code, "name": name, "key": key, "subject": subject,
                                 "kind": categorize(subject),
                                 "detail_params": (row[5] or {}).get("parameters")})
            if dt < cutoff:
                continue
            n_recent += 1
            if key in seen:
                continue
            seen[key] = {"company": name, "code": code,
                         "date": row[2], "time": row[3], "subject": subject}
            kind = categorize(subject)
            if kind == "monthly_revenue_announce":
                continue  # 由月營收模組處理
            detail_params = (row[5] or {}).get("parameters")
            queue.append((dt, kind, {
                "code": code, "name": name, "dt": dt, "key": key,
                "subject": subject, "detail_params": detail_params,
            }))
        log(f"  {code} {name}: {len(rows)} rows, {n_recent} in last {LOOKBACK_DAYS}d")
    queue.sort(key=lambda x: x[0])
    return queue, backfill


def process_revenue(seen_rev, first_run):
    pushed = []
    for code, name in COMPANIES.items():
        info = fetch_company_summary(code)
        if not info:
            continue
        rev = info.get("revenue_information") or {}
        key = revenue_key(code, rev)
        if not key:
            continue
        if key in seen_rev:
            continue
        seen_rev[key] = {"company": name, "fetched_at": datetime.now().isoformat()}
        msg = compose_revenue(name, code, rev)
        if msg:
            pushed.append((code, name, msg))
        log(f"  {code} {name}: revenue {key.split('|')[1]} new")
    return pushed


KIND_META = {
    "financial_report":  ("📊", "財務報告"),
    "self_settle":       ("🧾", "單月自結"),
    "board_meeting":     ("🏛️", "董事會決議"),
    "clarification":     ("🔍", "澄清/說明"),
    "asset":             ("🏭", "取得/處分資產"),
    "investor_conf":     ("🎤", "法說會"),
    "board_date":        ("🗓", "財報董事會日期"),
    "general":           ("📢", "一般公告"),
}

# 哪些類型額外送一張詳細內文圖卡（截圖式，避免漏抓）
# 自結內文常很長，圖卡會被 Telegram 縮到看不清楚，改送全文文字
KINDS_WITH_IMAGE = {"financial_report", "board_meeting"}


def push_material(item, kind, events):
    code = item["code"]; name = item["name"]; dt = item["dt"]
    subject = item["subject"]; params = item["detail_params"]

    detail_row = fetch_material_detail(params) if params else None
    full_text = ""
    if detail_row and len(detail_row) >= 9:
        full_text = (detail_row[-1] or "").strip()

    # 取得/處分資產看內文格式判斷（主旨寫法各家不同，也可能掛在董事會決議底下）
    if kind in ("general", "board_meeting") and is_asset_text(full_text):
        kind = "asset"
    icon, label = KIND_META.get(kind, KIND_META["general"])

    if kind in EVENT_KINDS and full_text:
        ev = extract_event(kind, name, code, subject, full_text)
        if ev:
            events[item["key"]] = ev

    if kind == "asset":
        msg = compose_asset(name, code, dt, subject, full_text)
        if msg:
            return tg_send_long(msg)

    if kind == "general" or not detail_row:
        return tg_send_message(compose_general(name, code, dt, subject))

    if kind in KINDS_WITH_IMAGE and full_text:
        IMG_DIR.mkdir(exist_ok=True)
        img_path = IMG_DIR / f"{code}_{params.get('enterDate')}_{params.get('serialNumber')}.png"
        try:
            render_detail_card(name, code, dt, label, subject, full_text, str(img_path))
            caption = f"{icon} {name} ({code}) {label}\n{dt:%Y-%m-%d %H:%M}"
            tg_send_photo(str(img_path), caption=caption[:1024])
            return True
        except Exception as e:
            log(f"  ⚠ render/upload image fail: {e}")

    # 其他（澄清等）或圖卡失敗 → 純文字含完整 內文
    body = compose_with_detail(icon, name, code, dt, subject, detail_row)
    return tg_send_long(body)


def due_heartbeat(state, now):
    """回傳到點但今天還沒發過的 heartbeat 時段；排程延遲也不會漏。"""
    sent = state.setdefault("heartbeat", {})
    today = now.date().isoformat()
    for hhmm in HEARTBEAT_HHMM:
        h, m = [int(x) for x in hhmm.split(":")]
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if (timedelta(minutes=-1) <= now - slot < timedelta(minutes=HEARTBEAT_GRACE_MIN)
                and sent.get(hhmm) != today):
            sent[hhmm] = today
            return hhmm
    return None


def backfill_events(candidates, events):
    today = date.today()
    for c in candidates:
        if c["key"] in events or not c["detail_params"]:
            continue
        row = fetch_material_detail(c["detail_params"])
        text = (row[-1] or "").strip() if row and len(row) >= 9 else ""
        ev = extract_event(c["kind"], c["name"], c["code"], c["subject"], text)
        if ev and date.fromisoformat(ev["date"]) >= today:
            events[c["key"]] = ev
        time.sleep(0.3)
    log(f"  events backfill: {len(candidates)} candidates, {len(events)} upcoming")


def main():
    log(f"=== run start, {len(COMPANIES)} companies ===")
    wait_for_network(max_wait_sec=120)

    seen = load_json(SEEN_FILE, {})
    first_run = seen is None
    if first_run:
        seen = {}
        log("First run: will init without spamming.")

    seen_rev = load_json(SEEN_REV_FILE, {})
    first_run_rev = seen_rev is None
    if first_run_rev:
        seen_rev = {}

    events = load_json(EVENTS_FILE, {})
    first_run_events = events is None
    if first_run_events:
        events = {}
    state = load_json(STATE_FILE, {}) or {}

    queue, backfill = process_material_info(seen, first_run, backfill_events=first_run_events)
    rev_pushed = process_revenue(seen_rev, first_run_rev)
    if first_run_events:
        backfill_events(backfill, events)

    if first_run:
        tg_send_message(
            f"✅ MOPS 重訊通知初始化完成\n"
            f"監控 {len(COMPANIES)} 家，已記錄近 {LOOKBACK_DAYS} 天 {len(queue)} 則訊息。\n"
            f"後續 08:50 / 15:00 / 00:00 推新增訊息與月營收。"
        )
    else:
        for dt, kind, payload in queue:
            try:
                push_material(payload, kind, events)
            except Exception as e:
                log(f"  ⚠ push material fail: {e}")
            time.sleep(0.6)

    if first_run_rev:
        log(f"  revenue init: {len(rev_pushed)} entries cached, no spam")
    else:
        for code, name, msg in rev_pushed:
            tg_send_message(msg)
            time.sleep(0.6)

    # 過期一週以上的事件清掉
    stale = (date.today() - timedelta(days=7)).isoformat()
    events = {k: v for k, v in events.items() if v["date"] >= stale}

    save_json(SEEN_FILE, seen)
    save_json(SEEN_REV_FILE, seen_rev)
    save_json(EVENTS_FILE, events)
    pushed_n = 0 if first_run else len(queue)
    rev_n = 0 if first_run_rev else len(rev_pushed)
    log(f"=== end: pushed {pushed_n} material + {rev_n} revenue, "
        f"seen {len(seen)} / rev {len(seen_rev)} ===\n")

    now = datetime.now()
    state["last_run"] = dict(RUN_STATS, at=now.isoformat(timespec="seconds"),
                             host="cloud" if os.environ.get("GITHUB_ACTIONS") else "local")
    if RUN_STATS["companies_ok"] == 0 and state.get("blocked_alert") != now.date().isoformat():
        if tg_send_message("⚠️ MOPS bot 這次抓不到任何公司的資料（可能被 MOPS 擋或網路異常）"):
            state["blocked_alert"] = now.date().isoformat()
    hb = due_heartbeat(state, now)
    if hb and not first_run and not first_run_rev:
        tg_send_message(
            f"✅ {hb} 巡查完成\n"
            f"📌 監控 {len(COMPANIES)} 家\n"
            f"📨 新增重訊: {pushed_n}\n"
            f"💰 新增月營收: {rev_n}"
        )

    # 每天早上第一次執行推行事曆
    today = now.date()
    if now.hour >= CALENDAR_HOUR and state.get("calendar") != today.isoformat() and not first_run:
        win = report_window(today)
        cal = compose_calendar(events, seen, today,
                               last_year_report_dates(state, win) if win else None)
        if cal:
            tg_send_long(cal)
        state["calendar"] = today.isoformat()
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        try:
            tg_send_message(f"⚠️ MOPS bot 異常: {e}")
        except Exception:
            pass
        sys.exit(1)
