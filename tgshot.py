#!/usr/bin/env python3
"""Посты Telegram в JPG: снимок поста как он выглядит в Telegram.

Живой пост берётся с t.me (виджет ?embed=1). Если пост удалён, ищем его в веб-архиве
(web.archive.org): страницы канала t.me/s/канал сохраняются там вместе с постами.

Запуск: на вход в stdin JSON {"posts": [{"post": "канал/123", "out": "/путь/без/расширения"}]},
на выход по строке JSON на каждый пост. Нужен Playwright с Chromium.
"""
import json, re, sys, urllib.parse, urllib.request

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
# фон страницы архива зелёный: делаем белым, как в виджете
CLEAN = "html,body,.tgme_background_wrap,.tgme_page_wrap{background:#fff!important}" \
        ".tgme_header,.tgme_channel_info,.tgme_footer{display:none!important}"


def say(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def meta(el):
    """канал и дата поста со страницы."""
    name = el.locator(".tgme_widget_message_owner_name")
    when = el.locator("time[datetime]")
    return (name.first.inner_text().strip() if name.count() else "",
            when.first.get_attribute("datetime") if when.count() else "")


def snapshots(ch, num):
    """архивные копии страниц, где должен быть пост: сам пост и ленты канала вокруг него."""
    def cdx(url):
        q = urllib.parse.urlencode({"url": url, "matchType": "prefix", "output": "json",
                                    "filter": "statuscode:200", "fl": "timestamp,original",
                                    "collapse": "original", "limit": "5000"})
        req = urllib.request.Request(f"https://web.archive.org/cdx/search/cdx?{q}", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                rows = json.load(r)
        except Exception:  # noqa: BLE001
            return []
        return rows[1:]

    out = []
    for ts, orig in cdx(f"t.me/s/{ch}"):
        path = urllib.parse.urlsplit(orig)
        q = urllib.parse.parse_qs(path.query)
        if re.search(rf"/s/{re.escape(ch)}/{num}/?$", path.path, re.I):
            out.append((0, ts, orig))
        elif "before" in q and q["before"][0].isdigit() and num < int(q["before"][0]) <= num + 21:
            out.append((1, ts, orig))
        elif "after" in q and q["after"][0].isdigit() and int(q["after"][0]) < num <= int(q["after"][0]) + 21:
            out.append((1, ts, orig))
    for ts, orig in cdx(f"t.me/{ch}/{num}"):
        if "embed" in orig:
            # виджет с комментариями тоже показывает пост, но тяжелее: в конец очереди
            out.append((2 if "discussion" in orig else 0, ts, orig))
    out.sort(key=lambda x: (x[0], -int(x[1])))
    return out[:8]


def shoot(page, url, ch, num, out, embed):
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    page.add_style_tag(content=CLEAN)
    if embed:
        if page.locator(".tgme_widget_message_error").count():
            return None
        el = page.locator(".tgme_widget_message")
    else:
        el = page.locator(f'.tgme_widget_message[data-post="{ch}/{num}" i]')
    if not el.count():
        return None
    el = el.first
    el.scroll_into_view_if_needed()
    page.wait_for_timeout(600)
    channel, date = meta(el)
    el.screenshot(path=out + ".jpg", type="jpeg", quality=92)
    return channel, date


def main():
    spec = json.load(sys.stdin)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 560, "height": 1000}, device_scale_factor=2, user_agent=UA,
                          locale="ru-RU")
        page.set_default_timeout(30000)
        for item in spec["posts"]:
            ch, num = item["post"].split("/")
            num = int(num)
            try:
                got = shoot(page, f"https://t.me/{ch}/{num}?embed=1&mode=tme", ch, num, item["out"], True)
                if got:
                    say(post=item["post"], ok=True, file=item["out"] + ".jpg", channel=got[0], date=got[1],
                        source="telegram")
                    continue
                say(post=item["post"], stage="в Telegram поста нет, ищу в веб-архиве")
                found = None
                for _, ts, orig in snapshots(ch, num):
                    url = f"https://web.archive.org/web/{ts}if_/{orig}"
                    try:
                        found = shoot(page, url, ch, num, item["out"], "embed" in orig)
                    except Exception:  # noqa: BLE001
                        found = None
                    if found:
                        say(post=item["post"], ok=True, file=item["out"] + ".jpg", channel=found[0],
                            date=found[1], source=f"архив {ts[:4]}-{ts[4:6]}-{ts[6:8]}")
                        break
                if not found:
                    say(post=item["post"], ok=False,
                        error="Пост удалён или закрыт, и в веб-архиве его копии нет")
            except Exception as e:  # noqa: BLE001
                say(post=item["post"], ok=False, error=str(e).splitlines()[0][:300])
        b.close()


if __name__ == "__main__":
    main()
