#!/usr/bin/env python3
"""ytads в терминале: ссылки на входе, рекламные фрагменты в mp4 на выходе.

  ytads ССЫЛКА [ССЫЛКА ...]   найти и выдернуть рекламу
  ytads                       то же для ссылок из буфера обмена
  ytads -n ССЫЛКА             только показать найденное, ничего не качать
  ytads -a ССЫЛКА             выдернуть все отрезки, не только рекламу
  ytads -q 720 ССЫЛКА         качество (по умолчанию 1080)
  ytads -k "вкусвилл,тинькофф" ССЫЛКА   свои слова-признаки рекламы
  ytads -t always ССЫЛКА      искать рекламу по субтитрам всегда (auto - только без таймкодов, off - никогда)
  ytads get ССЫЛКА [...]      скачать видео целиком из VK, TikTok, Instagram, Rutube в папку блогера
  ytads get -f "Папка" ССЫЛКА своё название папки
  ytads tg ССЫЛКА [...]       посты Telegram в JPG, удалённые - из веб-архива
  ytads cookies               сохранить куки из буфера обмена
  ytads web                   открыть страницу на http://wibuploads.localhost:8788
"""
import argparse, re, subprocess, sys

import server as S

URL_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+")
DIM, BOLD, AD, OK, ERR, END = "\033[2m", "\033[1m", "\033[38;5;209m", "\033[32m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    DIM = BOLD = AD = OK = ERR = END = ""


def clipboard():
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def say(line):
    """вместо журнала задачи: печатаем то, что полезно видеть."""
    if line.startswith(("[download]", "[info]", "[hlsnative]", "[dashsegments]")):
        return
    print(f"{DIM}  {line[:160]}{END}", flush=True)


S.jlog = lambda job, line: say(line)


def save_clip_cookies():
    try:
        r = S.save_cookies(clipboard())
    except ValueError as e:
        print(f"{ERR}{e}{END}")
        return False
    print(f"{OK}куки сохранены: {r['count']} шт. → {r['file']}{END}")
    return True


def ask_cookies():
    """YouTube отказал: ждём свежий Export и забираем его из буфера."""
    if not sys.stdin.isatty():
        return False
    print(f"\n{AD}YouTube не пускает без свежих кук.{END}\n"
          "  1. открой youtube.com в Arc\n"
          "  2. расширение «Get cookies.txt LOCALLY» → Export\n"
          "  3. вернись сюда и нажми Enter")
    while True:
        try:
            if input(f"{BOLD}Enter, когда скопировал{END} (q — выход) ").strip().lower() == "q":
                return False
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if save_clip_cookies():
            return True


def access():
    req = {"cookies": f"file:{S.ROOT / 'cookies.txt'}", "proxy": S.detect_proxy()}
    if not (S.ROOT / "cookies.txt").exists():
        found = S.detect_cookie_file()
        req["cookies"] = f"file:{found}" if found else "none"
    return req


def scan(urls, keywords, transcript="auto"):
    while True:
        job = {"id": "cli", "log": []}
        S.scan_worker(job, dict(access(), urls=urls, keywords=keywords, sbCats=S.SB_ADS,
                                   transcript=transcript))
        bot = [e for e in job["errors"] if re.search(r"not a bot|sign in to confirm", e["error"], re.I)]
        if bot and ask_cookies():
            continue
        for e in job["errors"]:
            print(f"{ERR}не прочитал {e['url']}{END}\n  {e['error'].splitlines()[0][:200]}")
        return job["videos"]


def show(videos):
    for v in videos:
        ads = sum(1 for c in v["items"] if c["ad"])
        print(f"\n{BOLD}{v['title']}{END}  {DIM}{v['channel']} · {S.hms(v['duration'])}{END}")
        if v.get("subs") in ("нет", "ошибка"):
            print(f"  {DIM}субтитры: {v['subs']}{END}")
        if not v["items"]:
            print(f"  {DIM}ни глав, ни таймкодов в описании, ни разметки SponsorBlock, ни рекламы в субтитрах{END}")
        if v.get("advertisers"):
            print(f"  в описании реклама: {AD}{', '.join(x['name'] for x in v['advertisers'])}{END}")
        if v.get("missing"):
            print(f"  {DIM}в субтитрах не нашёл: {', '.join(v['missing'])}{END}")
        for c in v["items"]:
            mark = f"{AD}реклама{END}" if c["ad"] else f"{DIM}       {END}"
            print(f"  {S.hms(c['start']):>8}–{S.hms(c['end']):<8} {mark}  {c['label'][:70]}"
                  f"  {DIM}{','.join(c['src'])}{END}")
        if v["items"] and not ads:
            print(f"  {DIM}рекламы не видно; взять всё подряд: ytads -a{END}")


def tg_posts(argv):
    p = argparse.ArgumentParser(prog="ytads tg", description="посты Telegram в JPG")
    p.add_argument("urls", nargs="*")
    p.add_argument("-f", "--folder", default="", help="своё название папки")
    a = p.parse_args(argv)
    urls = a.urls or re.findall(r"https?://\S+", clipboard())
    print(f"{DIM}посты: {len(urls)} · папка: {S.TG_DIR}{END}")
    job = {"id": "cli", "log": []}
    S.telegram_worker(job, {"urls": urls, "folder": a.folder})
    for r in job["results"]:
        if r.get("ok"):
            print(f"{OK}✓{END} {r['channel']} · {r['source']}  {r['file']}")
        else:
            print(f"{ERR}✗{END} {r['url']}: {r.get('error')}")


def get_videos(argv):
    p = argparse.ArgumentParser(prog="ytads get", description="видео целиком в папку блогера")
    p.add_argument("urls", nargs="*")
    p.add_argument("-f", "--folder", default="", help="своё название папки")
    p.add_argument("-q", "--quality", type=int, default=1080)
    a = p.parse_args(argv)
    urls = a.urls or re.findall(r"https?://\S+", clipboard())
    if not urls:
        sys.exit(f"{ERR}нет ссылок{END}")
    print(f"{DIM}видео: {len(urls)} · папка: {S.SOCIAL_DIR}{END}")
    job = {"id": "cli", "log": []}
    S.social_worker(job, dict(access(), urls=urls, height=a.quality, folder=a.folder))
    for r in job["results"]:
        if r["ok"]:
            print(f"{OK}✓{END} {r['platform']} · {r['blogger']} · {r['orientation']} · {S.hms(r['seconds'])}  {r['file']}")
        else:
            print(f"{ERR}✗{END} {r['url']}\n  {r['error'].splitlines()[0][:200]}")


def grab(videos, take_all, height):
    items = [dict(c, url=v["url"], id=v["id"], title=v["title"], channel=v["channel"])
             for v in videos for c in v["items"] if take_all or c["ad"]]
    if not items:
        return
    print(f"\n{BOLD}качаю {len(items)} фрагм.{END}")
    while True:
        job = {"id": "cli", "log": []}
        S.grab_worker(job, dict(access(), items=items, height=height))
        res = job.get("results") or []
        failed = [items[i] for i, r in enumerate(res) if not r["ok"]
                  and re.search(r"not a bot|sign in to confirm", r["error"], re.I)]
        for item, r in zip(items, res):
            if r["ok"]:
                print(f"{OK}✓{END} {S.hms(r['seconds'])}  {r['file']}")
            elif item not in failed:
                print(f"{ERR}✗{END} {r['title'][:50]} {S.hms(r['start'])}: {r['error'].splitlines()[0][:160]}")
        if job.get("manifest"):
            print(f"{DIM}манифест: {job['manifest']}{END}")
        if failed and ask_cookies():
            items = failed
            continue
        return


def main():
    if sys.argv[1:2] == ["cookies"]:
        sys.exit(0 if save_clip_cookies() else 1)
    if sys.argv[1:2] == ["tg"]:
        return tg_posts(sys.argv[2:])
    if sys.argv[1:2] == ["get"]:
        return get_videos(sys.argv[2:])
    if sys.argv[1:2] == ["web"]:
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        return S.main()
    p = argparse.ArgumentParser(prog="ytads", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*")
    p.add_argument("-n", "--dry", action="store_true", help="только показать, не качать")
    p.add_argument("-a", "--all", action="store_true", help="выдернуть все отрезки")
    p.add_argument("-q", "--quality", type=int, default=1080)
    p.add_argument("-k", "--keywords", default="")
    p.add_argument("-t", "--transcript", choices=["auto", "always", "off"], default="auto",
                   help="поиск рекламы по субтитрам")
    a = p.parse_args()
    urls = a.urls or URL_RE.findall(clipboard())
    if not urls:
        p.print_help()
        sys.exit(f"\n{ERR}нет ссылок: передай их аргументами или скопируй в буфер{END}")
    print(f"{DIM}ролики: {len(urls)} · куки: {access()['cookies'][5:] or 'нет'}"
          f" · фрагменты: {S.OUT_DIR}{END}")
    videos = scan(urls, a.keywords, a.transcript)
    show(videos)
    if not a.dry:
        grab(videos, a.all, a.quality)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено")
        sys.exit(130)
