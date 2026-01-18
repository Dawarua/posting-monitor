import json
import re
from datetime import datetime, timedelta
from dateutil import parser as dtparser
import pytz
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

TZ = pytz.timezone("Europe/Berlin")

CONFIG = {
  "expected_hour": 18,
  "expected_minute": 0,
  "tolerance_minutes": 240,  # 4h, damit "leicht verspätet" nicht gleich rot ist
  "sources": [
    {"id":"x",  "name":"X @utradetoken",              "type":"x",             "url":"https://x.com/utradetoken"},
    {"id":"ig", "name":"Instagram @utradetoken",      "type":"instagram",     "url":"https://www.instagram.com/utradetoken"},
    {"id":"tt", "name":"TikTok @utradetoken",         "type":"tiktok",        "url":"https://www.tiktok.com/@utradetoken"},
    {"id":"li", "name":"LinkedIn utradetoken",        "type":"linkedin",      "url":"https://www.linkedin.com/company/utradetoken/"},
    {"id":"yt", "name":"YouTube Shorts @utradetoken", "type":"youtube_shorts","url":"https://m.youtube.com/@utradetoken/shorts"},
    {"id":"b1", "name":"Blog utrade.vip",             "type":"blog",          "url":"https://utrade.vip/blog/"},
    {"id":"b2", "name":"Blog umerch.store",           "type":"blog",          "url":"https://umerch.store/blog/"},
    {"id":"b3", "name":"Blog utap.click",             "type":"blog",          "url":"https://utap.click/blog-utrade/"}
  ]
}

def expected_window(now_berlin: datetime):
    day = now_berlin.date()
    expected = TZ.localize(datetime(day.year, day.month, day.day, CONFIG["expected_hour"], CONFIG["expected_minute"]))
    tol = timedelta(minutes=CONFIG["tolerance_minutes"])
    return expected - tol, expected + tol, expected

def first_match(html: str, patterns):
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def extract_latest_url(stype: str, html: str):
    if stype == "x":
        m = first_match(html, [r'href="(/[^"/]+/status/\d+)"'])
        return f"https://x.com{m}" if m else None
    if stype == "instagram":
        m = first_match(html, [r'href="(/(p|reel)/[^"/]+/)"'])
        return f"https://www.instagram.com{m}" if m else None
    if stype == "tiktok":
        m = first_match(html, [r'href="(/@[^"]+/video/\d+)"'])
        return f"https://www.tiktok.com{m}" if m else None
    if stype == "linkedin":
        m = first_match(html, [
            r'(https://www\.linkedin\.com/feed/update/urn:li:activity:\d+[^"\']*)',
            r'(https://www\.linkedin\.com/company/[^/]+/posts/[^"\']+)'
        ])
        return m
    if stype == "youtube_shorts":
        m = first_match(html, [r'href="(/shorts/[^"?]+)"'])
        return f"https://www.youtube.com{m}" if m else None
    if stype == "blog":
        m = first_match(html, [
            r'<article[\s\S]*?<a[^>]+href="([^"]+)"',
            r'href="([^"]+/20\d{2}/[^"]+)"'
        ])
        return m
    return None

def extract_published_datetime(html: str):
    candidates = []
    for m in re.finditer(r'datetime="([^"]+)"', html, re.IGNORECASE):
        candidates.append(m.group(1))
    for m in re.finditer(r'property="article:published_time"\s+content="([^"]+)"', html, re.IGNORECASE):
        candidates.append(m.group(1))
    for m in re.finditer(r'itemprop="datePublished"\s+content="([^"]+)"', html, re.IGNORECASE):
        candidates.append(m.group(1))
    for m in re.finditer(r'"datePublished"\s*:\s*"([^"]+)"', html, re.IGNORECASE):
        candidates.append(m.group(1))

    parsed = []
    for c in candidates[:30]:
        try:
            dt = dtparser.parse(c)
            if dt.tzinfo is None:
                dt = TZ.localize(dt)
            else:
                dt = dt.astimezone(TZ)
            parsed.append(dt)
        except Exception:
            pass
    return max(parsed) if parsed else None

def looks_blocked(html: str):
    return bool(re.search(r'captcha|verify|unusual traffic|robot|consent|cookie|login|sign in', html, re.IGNORECASE))

def check_one(page, source):
    url = source["url"]
    stype = source["type"]

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(1500)
        html = page.content()

        latest_url = extract_latest_url(stype, html)
        if not latest_url:
            if looks_blocked(html):
                return {"status":"warn","latest_url":None,"published_at":None,"note":"blocked/consent/login suspected"}
            return {"status":"missing","latest_url":None,"published_at":None,"note":"no latest link found"}

        published_at = None
        try:
            page.goto(latest_url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(1200)
            post_html = page.content()
            published_at = extract_published_datetime(post_html)
        except Exception:
            published_at = None

        now_b = datetime.now(TZ)
        win_start, win_end, expected = expected_window(now_b)

        if published_at:
            ok = (published_at.date() == now_b.date()) and (published_at >= win_start)
            return {"status":"ok" if ok else "missing", "latest_url":latest_url,
                    "published_at": published_at.isoformat(), "note":"published time found"}

        return {"status":"warn","latest_url":latest_url,"published_at":None,"note":"latest link found, no timestamp"}

    except PwTimeout:
        return {"status":"warn","latest_url":None,"published_at":None,"note":"timeout"}
    except Exception as e:
        return {"status":"warn","latest_url":None,"published_at":None,"note":f"error: {type(e).__name__}"}

def main():
    now_b = datetime.now(TZ)
    win_start, win_end, expected = expected_window(now_b)

    out = {
        "day_berlin": now_b.date().isoformat(),
        "checked_at_berlin": now_b.strftime("%Y-%m-%d %H:%M:%S"),
        "expected_time_berlin": expected.strftime("%Y-%m-%d %H:%M"),
        "sources": []
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )

        for s in CONFIG["sources"]:
            r = check_one(page, s)
            published_at_berlin = None
            if r["published_at"]:
                try:
                    dt = dtparser.parse(r["published_at"]).astimezone(TZ)
                    published_at_berlin = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            out["sources"].append({
                "id": s["id"],
                "name": s["name"],
                "type": s["type"],
                "url": s["url"],
                "status": r["status"],
                "latest_url": r["latest_url"],
                "published_at_berlin": published_at_berlin,
                "note": r["note"]
            })

        browser.close()

    with open("docs/status.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
