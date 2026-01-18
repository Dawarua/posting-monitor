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
    "tolerance_minutes": 240,  # 4h Toleranz
    "retries": 2,              # zusätzliche Versuche bei block/timeout
    "sources": [
        {"id": "x",  "name": "X @utradetoken",              "type": "x",             "url": "https://x.com/utradetoken"},
        {"id": "ig", "name": "Instagram @utradetoken",      "type": "instagram",     "url": "https://www.instagram.com/utradetoken"},
        {"id": "tt", "name": "TikTok @utradetoken",         "type": "tiktok",        "url": "https://www.tiktok.com/@utradetoken"},
        {"id": "li", "name": "LinkedIn utradetoken",        "type": "linkedin",      "url": "https://www.linkedin.com/company/utradetoken/"},
        {"id": "yt", "name": "YouTube Shorts @utradetoken", "type": "youtube_shorts","url": "https://m.youtube.com/@utradetoken/shorts"},
        {"id": "b1", "name": "Blog utrade.vip",             "type": "blog",          "url": "https://utrade.vip/blog/"},
        {"id": "b2", "name": "Blog umerch.store",           "type": "blog",          "url": "https://umerch.store/blog/"},
        {"id": "b3", "name": "Blog utap.click",             "type": "blog",          "url": "https://utap.click/blog-utrade/"}
    ]
}

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

CONSENT_SELECTORS = [
    # English
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Accept")',
    'button:has-text("I Agree")',
    'button:has-text("Agree")',
    'button:has-text("Allow all")',
    # German
    'button:has-text("Alle akzeptieren")',
    'button:has-text("Alles akzeptieren")',
    'button:has-text("Akzeptieren")',
    'button:has-text("Zustimmen")',
    'button:has-text("Einverstanden")',
]


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
    """
    Heuristiken: Wir versuchen jeweils den "neuesten Beitrag"-Link zu finden.
    Das ist ohne API/Scraping-Resistenz nie 100%, aber hier möglichst robust.
    """
    if stype == "x":
        m = first_match(html, [r'href="(/[^"/]+/status/\d+)"'])
        return f"https://x.com{m}" if m else None

    if stype == "instagram":
        # /p/ oder /reel/ Links
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
    """
    Versucht aus HTML published time zu ziehen:
    - <time datetime="...">
    - meta property="article:published_time"
    - itemprop="datePublished"
    - JSON-LD "datePublished"
    """
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
    for c in candidates[:40]:
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
    """
    Erweitert: erkennt Captcha/Consent/Login/Robots Hinweise.
    Dadurch werden Fälle eher ⚠️ statt ❌, wenn die Seite blockt.
    """
    return bool(re.search(
        r'captcha|verify|unusual traffic|robot|consent|cookie|cookies|login|log in|sign in|anmelden|einloggen|'
        r'please enable javascript|access denied|forbidden|restricted|temporarily blocked|'
        r'instagram|tiktok|linkedin',
        html,
        re.IGNORECASE
    ))


def try_click_consent(page):
    """
    Versucht Cookie/Consent Banner zu akzeptieren.
    Fehler werden ignoriert.
    """
    for sel in CONSENT_SELECTORS:
        try:
            page.click(sel, timeout=1500)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            continue
    return False


def goto_with_soft_handling(page, url: str, timeout_ms: int = 30000):
    """
    Seite laden + kurz warten + Consent versuchen.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3500)
    try_click_consent(page)
    page.wait_for_timeout(1500)


def youtube_shorts_to_watch(latest_url: str):
    """
    Shorts URL -> watch?v=...
    """
    try:
        if "/shorts/" in latest_url:
            vid = latest_url.split("/shorts/")[1].split("?")[0].split("&")[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
    except Exception:
        pass
    return None


def check_one(page, source):
    url = source["url"]
    stype = source["type"]

    last_note = ""
    for attempt in range(CONFIG["retries"] + 1):
        try:
            goto_with_soft_handling(page, url)
            html = page.content()

            latest_url = extract_latest_url(stype, html)

            if not latest_url:
                if looks_blocked(html):
                    last_note = "blocked/consent/login suspected"
                    # retry bei block
                    continue
                return {"status": "missing", "latest_url": None, "published_at": None, "note": "no latest link found"}

            # Neuesten Post öffnen und Published Time suchen
            published_at = None

            # YouTube: Erst /shorts/ öffnen, dann fallback auf watch?v=
            if stype == "youtube_shorts":
                try:
                    goto_with_soft_handling(page, latest_url)
                    post_html = page.content()
                    published_at = extract_published_datetime(post_html)
                except Exception:
                    published_at = None

                if not published_at:
                    watch_url = youtube_shorts_to_watch(latest_url)
                    if watch_url:
                        try:
                            goto_with_soft_handling(page, watch_url)
                            post_html = page.content()
                            published_at = extract_published_datetime(post_html)
                            # wenn watch_url brauchbarer ist, auch Link umstellen:
                            if published_at:
                                latest_url = watch_url
                        except Exception:
                            published_at = None
            else:
                try:
                    goto_with_soft_handling(page, latest_url)
                    post_html = page.content()
                    published_at = extract_published_datetime(post_html)
                except Exception:
                    published_at = None

            now_b = datetime.now(TZ)
            win_start, win_end, expected = expected_window(now_b)

            if published_at:
                # "heute" und innerhalb toleranzfenster (>= win_start)
                ok = (published_at.date() == now_b.date()) and (published_at >= win_start)
                return {
                    "status": "ok" if ok else "missing",
                    "latest_url": latest_url,
                    "published_at": published_at.isoformat(),
                    "note": "published time found"
                }

            # Kein Timestamp -> nicht rot, sondern warn (Link gefunden, aber nicht sicher)
            return {
                "status": "warn",
                "latest_url": latest_url,
                "published_at": None,
                "note": "latest link found, no timestamp"
            }

        except PwTimeout:
            last_note = "timeout"
            continue
        except Exception as e:
            last_note = f"error: {type(e).__name__}"
            continue

    # Wenn alle Versuche scheitern:
    return {"status": "warn", "latest_url": None, "published_at": None, "note": last_note or "warn"}


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
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1365, "height": 900},
            locale="de-DE",
        )

        page = context.new_page()

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

    # docs/status.json aktualisieren
    with open("docs/status.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
