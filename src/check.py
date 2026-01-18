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
    "tolerance_minutes": 240,   # 4h Toleranz
    "retries": 2,
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
    # EN
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Accept")',
    'button:has-text("I Agree")',
    'button:has-text("Agree")',
    'button:has-text("Allow all")',
    # DE
    'button:has-text("Alle akzeptieren")',
    'button:has-text("Alles akzeptieren")',
    'button:has-text("Akzeptieren")',
    'button:has-text("Zustimmen")',
    'button:has-text("Einverstanden")',
    # generic
    '[role="button"]:has-text("Accept")',
    '[role="button"]:has-text("Alle akzeptieren")',
]

DEBUG_DIR = "debug"  # wird im Runner als Artefakt gespeichert (optional im Workflow)


def expected_window(now_berlin: datetime):
    day = now_berlin.date()
    expected = TZ.localize(datetime(day.year, day.month, day.day, CONFIG["expected_hour"], CONFIG["expected_minute"]))
    tol = timedelta(minutes=CONFIG["tolerance_minutes"])
    return expected - tol, expected + tol, expected


def looks_blocked_text(html: str):
    # Erkennung, ob wir eine Login/Consent/Block-Seite bekommen
    return bool(re.search(
        r'captcha|verify|unusual traffic|robot|consent|cookie|cookies|'
        r'login|log in|sign in|anmelden|einloggen|'
        r'access denied|forbidden|temporarily blocked|'
        r'please enable javascript',
        html,
        re.IGNORECASE
    ))


def try_click_consent(page):
    for sel in CONSENT_SELECTORS:
        try:
            page.click(sel, timeout=1500)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            pass
    return False


def goto(page, url: str, timeout_ms: int = 45000):
    # domcontentloaded + networkidle hilft oft bei dynamischen Seiten
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1500)
    try_click_consent(page)
    # nach Consent nochmal warten
    page.wait_for_timeout(1500)


def soft_scroll(page, steps=3):
    # leichtes scrollen, damit Feeds Inhalte nachladen
    for _ in range(steps):
        try:
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1200)
        except Exception:
            break


def safe_write_debug(page, source_id: str, name: str):
    # Debug-Screenshot + HTML dump (hilft 100% beim Nachschärfen)
    try:
        import os
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=f"{DEBUG_DIR}/{source_id}_{name}.png", full_page=True)
        with open(f"{DEBUG_DIR}/{source_id}_{name}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


# ---------- Plattform-spezifische "latest link" + time extraction (DOM-basiert) ----------

def latest_x(page):
    # X: warte auf Tweets oder erkenne Login
    html = page.content()
    if "log in" in html.lower() or "login" in html.lower() or "/i/flow/login" in html.lower():
        return None, "blocked/consent/login suspected"

    # Versuch: erstes Tweet-Article
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=8000)
    except Exception:
        pass

    # Scroll, damit Tweets sicher geladen sind
    soft_scroll(page, steps=2)

    # Tweet Link (status/ID) aus dem ersten Article
    try:
        article = page.locator('article[data-testid="tweet"]').first
        link = article.locator('a[href*="/status/"]').first.get_attribute("href")
        if link:
            return "https://x.com" + link.split("?")[0], "latest tweet link found"
    except Exception:
        pass

    # Fallback regex
    html = page.content()
    m = re.search(r'href="(/[^"/]+/status/\d+)"', html, re.IGNORECASE)
    if m:
        return "https://x.com" + m.group(1), "latest tweet link (regex)"
    return None, "no latest link found"


def published_time_from_time_tag(page):
    # Sehr robust: <time datetime="...">
    try:
        t = page.locator("time").first.get_attribute("datetime")
        if t:
            dt = dtparser.parse(t)
            if dt.tzinfo is None:
                dt = TZ.localize(dt)
            else:
                dt = dt.astimezone(TZ)
            return dt
    except Exception:
        pass
    return None


def latest_instagram(page):
    html = page.content().lower()
    # Wenn IG Login/Consent präsentiert, lieber WARN statt falsches ❌
    if "login" in html or "anmelden" in html or looks_blocked_text(page.content()):
        return None, "blocked/consent/login suspected"

    # IG: Links /p/ oder /reel/ in anchor tags
    try:
        # kurz warten
        page.wait_for_timeout(2000)
        soft_scroll(page, steps=2)
        # versuche DOM links
        link = page.locator('a[href*="/p/"], a[href*="/reel/"]').first.get_attribute("href")
        if link:
            if link.startswith("/"):
                return "https://www.instagram.com" + link.split("?")[0], "latest ig post link found"
            return link.split("?")[0], "latest ig post link found"
    except Exception:
        pass

    # Fallback regex
    html2 = page.content()
    m = re.search(r'href="(/(p|reel)/[^"/]+/)"', html2, re.IGNORECASE)
    if m:
        return "https://www.instagram.com" + m.group(1), "latest ig post link (regex)"
    return None, "no latest link found"


def latest_tiktok(page):
    html = page.content().lower()
    if looks_blocked_text(page.content()) or "verify" in html or "captcha" in html:
        return None, "blocked/consent/login suspected"

    try:
        page.wait_for_timeout(2000)
        soft_scroll(page, steps=3)
        link = page.locator('a[href*="/video/"]').first.get_attribute("href")
        if link:
            if link.startswith("/"):
                return "https://www.tiktok.com" + link.split("?")[0], "latest tiktok link found"
            return link.split("?")[0], "latest tiktok link found"
    except Exception:
        pass

    html2 = page.content()
    m = re.search(r'href="(/@[^"]+/video/\d+)"', html2, re.IGNORECASE)
    if m:
        return "https://www.tiktok.com" + m.group(1), "latest tiktok link (regex)"
    return None, "no latest link found"


def latest_linkedin(page):
    html = page.content().lower()
    if looks_blocked_text(page.content()) or "sign in" in html or "anmelden" in html:
        return None, "blocked/consent/login suspected"

    # LinkedIn Company Posts sind oft dynamisch und blocken; wir versuchen Post-Links
    try:
        page.wait_for_timeout(2500)
        soft_scroll(page, steps=3)
        # Versuch: feed/update links
        link = page.locator('a[href*="urn:li:activity"], a[href*="/feed/update/"]').first.get_attribute("href")
        if link:
            if link.startswith("/"):
                return "https://www.linkedin.com" + link.split("?")[0], "latest linkedin link found"
            return link.split("?")[0], "latest linkedin link found"
    except Exception:
        pass

    html2 = page.content()
    m = re.search(r'(https://www\.linkedin\.com/feed/update/urn:li:activity:\d+[^"\']*)', html2, re.IGNORECASE)
    if m:
        return m.group(1), "latest linkedin link (regex)"
    return None, "no latest link found"


def latest_youtube_shorts(page):
    # YouTube: /shorts/ID
    try:
        page.wait_for_timeout(1500)
        soft_scroll(page, steps=2)
        link = page.locator('a[href^="/shorts/"]').first.get_attribute("href")
        if link:
            return "https://www.youtube.com" + link.split("?")[0], "latest shorts link found"
    except Exception:
        pass

    html = page.content()
    m = re.search(r'href="(/shorts/[^"?]+)"', html, re.IGNORECASE)
    if m:
        return "https://www.youtube.com" + m.group(1), "latest shorts link (regex)"
    return None, "no latest link found"


def shorts_to_watch(url: str):
    if "/shorts/" in url:
        vid = url.split("/shorts/")[1].split("?")[0].split("&")[0]
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    return None


def latest_blog(page):
    # Blogs: first article link
    html = page.content()
    # DOM zuerst
    try:
        link = page.locator("article a[href]").first.get_attribute("href")
        if link:
            return link.split("?")[0], "latest blog link found"
    except Exception:
        pass

    # regex fallback
    m = re.search(r'<article[\s\S]*?<a[^>]+href="([^"]+)"', html, re.IGNORECASE)
    if m:
        return m.group(1).split("?")[0], "latest blog link (regex)"
    m2 = re.search(r'href="([^"]+/20\d{2}/[^"]+)"', html, re.IGNORECASE)
    if m2:
        return m2.group(1).split("?")[0], "latest blog link (regex2)"
    return None, "no latest link found"


def get_latest(page, stype: str):
    if stype == "x":
        return latest_x(page)
    if stype == "instagram":
        return latest_instagram(page)
    if stype == "tiktok":
        return latest_tiktok(page)
    if stype == "linkedin":
        return latest_linkedin(page)
    if stype == "youtube_shorts":
        return latest_youtube_shorts(page)
    if stype == "blog":
        return latest_blog(page)
    return None, "unknown type"


def check_one(page, source):
    url = source["url"]
    stype = source["type"]

    last_note = ""
    for attempt in range(CONFIG["retries"] + 1):
        try:
            goto(page, url)
            # bei dynamischen Seiten: kurz network idle simulieren
            page.wait_for_timeout(1500)

            latest_url, note = get_latest(page, stype)
            if not latest_url:
                # debug dump für Socials hilft enorm
                safe_write_debug(page, source["id"], "profile")
                # wenn note block-like -> warn
                if "blocked" in note:
                    last_note = note
                    continue
                return {"status": "missing", "latest_url": None, "published_at": None, "note": note}

            # Jetzt Post öffnen + Zeit extrahieren (DOM time tag, sehr robust)
            published_at = None

            # YouTube Shorts: zusätzlich watch?v=… öffnen, um date/time zuverlässiger zu bekommen
            if stype == "youtube_shorts":
                # zuerst shorts
                goto(page, latest_url)
                published_at = published_time_from_time_tag(page)

                if not published_at:
                    watch = shorts_to_watch(latest_url)
                    if watch:
                        goto(page, watch)
                        published_at = published_time_from_time_tag(page)
                        if published_at:
                            latest_url = watch

            else:
                goto(page, latest_url)
                published_at = published_time_from_time_tag(page)

            # Wenn Social blockt auf Post-Seite: debug speichern
            if looks_blocked_text(page.content()):
                safe_write_debug(page, source["id"], "post")
                last_note = "blocked/consent/login suspected"
                continue

            now_b = datetime.now(TZ)
            win_start, win_end, expected = expected_window(now_b)

            if published_at:
                ok = (published_at.date() == now_b.date()) and (published_at >= win_start)
                return {
                    "status": "ok" if ok else "missing",
                    "latest_url": latest_url,
                    "published_at": published_at.isoformat(),
                    "note": "published time found"
                }

            # Kein Timestamp → warn (nicht rot)
            return {
                "status": "warn",
                "latest_url": latest_url,
                "published_at": None,
                "note": "latest link found, no timestamp"
            }

        except PwTimeout:
            last_note = "timeout"
            safe_write_debug(page, source["id"], f"timeout_{attempt}")
            continue
        except Exception as e:
            last_note = f"error: {type(e).__name__}"
            safe_write_debug(page, source["id"], f"error_{attempt}")
            continue

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
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1365, "height": 900},
            locale="de-DE",
            extra_http_headers={
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"
            }
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

    with open("docs/status.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
