#!/usr/bin/env python3
"""ytads - собирает рекламные фрагменты из роликов YouTube в отдельные mp4.

Границы берутся из таймкодов: главы ролика, список таймкодов в описании,
база SponsorBlock. Найденное показывается на странице, человек отмечает нужное,
инструмент выдёргивает отмеченные куски и пишет манифест в csv.
"""
import csv, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("YTADS_OUT") or Path.home() / "Desktop" / "Рекламные фрагменты")
SRC_DIR = OUT_DIR / ".source"
MANIFEST = OUT_DIR / "ads.csv"
# видео целиком из VK, TikTok, Instagram, Rutube: папка на каждого блогера
SOCIAL_DIR = Path(os.environ.get("YTADS_SOCIAL_OUT") or Path.home() / "Desktop" / "Видео из соцсетей")
SOCIAL_MANIFEST = SOCIAL_DIR / "videos.csv"
# посты Telegram снимками JPG, папка на каждый канал
TG_DIR = Path(os.environ.get("YTADS_TG_OUT") or Path.home() / "Desktop" / "Посты Telegram")
TG_MANIFEST = TG_DIR / "posts.csv"
TG_RE = re.compile(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})/(\d+)", re.I)
PLATFORMS = [("VK", ("vk.com", "vk.ru", "vkvideo.ru")), ("TikTok", ("tiktok.com",)),
             ("Instagram", ("instagram.com",)), ("Rutube", ("rutube.ru",)),
             ("YouTube", ("youtube.com", "youtu.be"))]
# российские площадки быстрее напрямую, TikTok и Instagram - через прокси, если он есть
DIRECT = {"VK", "Rutube"}
POT_SCRIPT = Path.home() / ".local/share/bgutil-pot/server/build/generate_once.js"
PORT = int(os.environ.get("YTADS_PORT", "8788"))
# страница живёт только локально: http://wibuploads.localhost:8788
WEB_ORIGINS = set()
LOCAL_HOSTS = {"wibuploads.localhost", "ytads.localhost", "localhost", "127.0.0.1"}
PROXY_PORTS = [10808, 10809, 1080, 1086, 2080, 7890, 8889, 9150]


def tool(name):
    """путь к программе. Если её нет, страница подскажет, как поставить: сервер всё равно запускается."""
    return shutil.which(name) or next((str(Path(d) / name) for d in ("/opt/homebrew/bin", "/usr/local/bin")
                                      if (Path(d) / name).exists()), None)


YTDLP, FFMPEG, FFPROBE = tool("yt-dlp"), tool("ffmpeg"), tool("ffprobe")
MISSING = [n for n, v in (("yt-dlp", YTDLP), ("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)) if not v]
YTDLP, FFMPEG, FFPROBE = YTDLP or "yt-dlp", FFMPEG or "ffmpeg", FFPROBE or "ffprobe"
WIB_HOME = Path(os.environ.get("WIB_HOME") or Path.home() / ".wibuploads")

JOBS = {}
LOCK = threading.Lock()

VID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")
# таймкод в начале строки или после разделителя: 4:12, 04:12, 1:02:03
TC_RE = re.compile(r"(?:^|[\s(\[|·—–-])((?:\d{1,2}:)?\d{1,2}:\d{2})(?=[\s)\]|.,:;—–-]|$)")

AD_WORDS = ["реклам", "спонсор", "промокод", "промо", "партнер", "партнёр", "интеграц",
            "о спонсоре", "наш друг", "скидк", "sponsor", "promo", "advert", "ad break",
            "paid", "brought to you"]
NEG_WORDS = ["не реклама", "без реклам", "не спонсор", "не промо", "никакой реклам"]
SB_ADS = ["sponsor", "selfpromo"]
SB_LABELS = {"sponsor": "реклама (SponsorBlock)", "selfpromo": "самопромо (SponsorBlock)",
             "interaction": "призыв подписаться", "intro": "интро", "outro": "концовка",
             "preview": "анонс", "filler": "оффтоп", "music_offtopic": "немузыкальное"}
BOT_HINT = ("\n\nYouTube не пускает это подключение. Добавь свежие куки во вкладке «Куки». "
            "Если YouTube открывается только через VPN, проверь прокси в настройках.")


def video_id(url):
    url = (url or "").strip()
    m = VID_RE.search(url)
    if m:
        return m.group(1)
    return url if re.fullmatch(r"[A-Za-z0-9_-]{11}", url) else None


def tag(t):
    """метка времени для имени файла: однозначная и сортируемая."""
    t = max(0, int(round(t or 0)))
    return f"{t // 3600}-{t % 3600 // 60:02d}-{t % 60:02d}"


def hms(t):
    t = max(0, int(round(t or 0)))
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_tc(s):
    p = [int(x) for x in s.split(":")]
    return sum(v * 60 ** i for i, v in enumerate(reversed(p)))


# ---------- внешние источники ----------

# не держать человека в ожидании, если YouTube молчит
NET_ARGS = ["--socket-timeout", "15", "--retries", "2", "--extractor-retries", "1"]


def auth_args(req):
    """куки, прокси, тайм-ауты и провайдер PO-токена."""
    args = list(NET_ARGS)
    ck = ((req or {}).get("cookies") or "").strip()
    if ck.startswith("file:"):
        path = Path(ck[5:]).expanduser()
        if path.exists():
            # yt-dlp дописывает куки обратно в файл и портит экспорт, поэтому отдаём копию
            fd, tmp = tempfile.mkstemp(prefix="ytads-cookies-", suffix=".txt")
            os.close(fd)
            shutil.copyfile(path, tmp)
            args += ["--cookies", tmp]
    elif ck.startswith("path:"):
        args += ["--cookies-from-browser", f"chrome:{Path(ck[5:]).expanduser()}"]
    elif ck and ck != "none":
        args += ["--cookies-from-browser", ck]
    px = ((req or {}).get("proxy") or "").strip()
    if px:
        args += ["--proxy", px]
    if POT_SCRIPT.exists():
        args += ["--extractor-args", f"youtubepot-bgutilscript:script_path={POT_SCRIPT}"]
    return args


def friendly(msg):
    low = msg.lower()
    if "no such file or directory" in low and re.search(r"yt-dlp|ffmpeg|ffprobe", low):
        return ("Не нашёл yt-dlp или ffmpeg. Открой вкладку «Старт» и сделай, что там написано, "
                "или дважды кликни файл «Установить.command» в папке проекта.")
    return msg + BOT_HINT if "not a bot" in low or "sign in to confirm" in low else msg


def ytdlp_info(url, extra=()):
    r = subprocess.run([YTDLP, "-J", "--no-playlist", "--no-warnings", *extra, url],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(friendly((r.stderr or "").strip()[-400:]) or "yt-dlp не прочитал ссылку")
    return json.loads(r.stdout)


def sponsorblock(vid, cats):
    cats = [c for c in cats if c in SB_LABELS] or SB_ADS
    q = urllib.parse.urlencode({"videoID": vid, "categories": json.dumps(cats)})
    req = urllib.request.Request(f"https://sponsor.ajay.app/api/skipSegments?{q}",
                                 headers={"User-Agent": "ytads/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return []
        raise RuntimeError(f"SponsorBlock ответил {e.code}")
    except Exception:
        return []
    segs = [{"start": round(s["segment"][0], 2), "end": round(s["segment"][1], 2),
             "category": s.get("category", "sponsor")}
            for s in data if s["segment"][1] - s["segment"][0] > 0.5]
    segs.sort(key=lambda x: x["start"])
    return segs


# ---------- поиск фрагментов по таймкодам ----------

def desc_timecodes(text, duration):
    """таймкоды из описания: подпись - остаток строки, конец - следующий таймкод."""
    found = []
    for line in (text or "").splitlines():
        m = TC_RE.search(line)
        if not m:
            continue
        t = parse_tc(m.group(1))
        if duration and t >= duration:
            continue
        label = (line[:m.start(1)] + " " + line[m.end(1):]).strip()
        # после выреза таймкода остаются пустые скобки: "[ 00:00 ] - Текст" -> "[ ] - Текст"
        label = re.sub(r"[\[\(]\s*[\]\)]", " ", label)
        label = re.sub(r"^[\s\-–—:.·|)\]}>([{<]+|[\s\-–—:.·|)\]}>([{<]+$", "", label)
        label = re.sub(r"\s+:\s+", ": ", label)
        label = re.sub(r"\s{2,}", " ", label)
        found.append((t, label))
    found.sort(key=lambda x: x[0])
    uniq = []
    for t, label in found:
        if uniq and abs(uniq[-1][0] - t) < 1:
            if len(label) > len(uniq[-1][1]):
                uniq[-1] = (t, label)
            continue
        uniq.append((t, label))
    out = []
    for i, (t, label) in enumerate(uniq):
        end = uniq[i + 1][0] if i + 1 < len(uniq) else (duration or t + 60)
        if end - t > 1:
            out.append((t, end, label))
    return out


def is_ad(label, extra_words=()):
    low = " " + re.sub(r"[^\w\s]+", " ", (label or "").lower()) + " "
    if any(n in low for n in NEG_WORDS):
        return False
    words = list(AD_WORDS) + [w.strip().lower() for w in extra_words if w.strip()]
    return any(w in low for w in words)


def candidates(info, sb, extra_words=()):
    dur = info.get("duration") or 0
    raw = []
    for ch in info.get("chapters") or []:
        raw.append({"start": float(ch.get("start_time") or 0),
                    "end": float(ch.get("end_time") or dur),
                    "label": (ch.get("title") or "").strip(), "src": ["глава"], "forced": False})
    for a, b, label in desc_timecodes(info.get("description"), dur):
        raw.append({"start": float(a), "end": float(b), "label": label,
                    "src": ["описание"], "forced": False})
    for s in sb:
        raw.append({"start": s["start"], "end": s["end"],
                    "label": SB_LABELS.get(s["category"], s["category"]),
                    "src": ["sponsorblock"], "forced": s["category"] in SB_ADS})

    raw.sort(key=lambda c: (c["start"], c["end"]))
    merged = []
    for c in raw:
        hit = None
        for m in merged:
            inter = min(m["end"], c["end"]) - max(m["start"], c["start"])
            longest = max(m["end"] - m["start"], c["end"] - c["start"])
            if longest > 0 and inter > 0.6 * longest:
                hit = m
                break
        if not hit:
            merged.append(dict(c, src=list(c["src"])))
            continue
        for s in c["src"]:
            if s not in hit["src"]:
                hit["src"].append(s)
        hit["forced"] = hit["forced"] or c["forced"]
        # границы SponsorBlock точнее, подпись из главы или описания информативнее
        if "sponsorblock" in c["src"]:
            hit["start"], hit["end"] = c["start"], c["end"]
        # глава «Про ипотеку», совпавшая с рекламой SponsorBlock, рекламу не переименовывает
        if (len(c["label"]) > len(hit["label"]) and "sponsorblock" not in c["src"]
                and not (hit["forced"] and not is_ad(c["label"], extra_words))):
            hit["label"] = c["label"]

    for m in merged:
        if dur:
            m["end"] = min(m["end"], dur)
        m["ad"] = bool(m["forced"] or is_ad(m["label"], extra_words))
        m["start"], m["end"] = round(m["start"], 2), round(m["end"], 2)
    return [m for m in merged if m["end"] - m["start"] > 1]


# ---------- реклама по субтитрам ----------
# когда таймкодов нет, читаем автосубтитры и ищем, где кучно звучат бренд и призыв

TRIGGERS = [(re.compile(p), w) for p, w in [
    (r"реклам", 2), (r"спонсор", 3), (r"партнер\w* (выпуска|видео|этого|сегодняшн)", 4),
    (r"при поддержке", 3), (r"промокод", 4), (r"ссылк\w* в описани", 4), (r"в описании", 2),
    (r"ссылк\w* оставл|оставл\w* ссылк|ид\w* в описание|ссылк\w* ниже|закрепленн\w* коммент", 4),
    (r"\bqr\b|кьюар", 4), (r"по ссылке", 2), (r"скидк", 2), (r"ке[шэ]б[эе]к|кэшб[эе]к", 2),
    (r"ненадолго прерв|прервемся|небольш\w* пауз", 4),
    (r"возвращаемся к|вернемся к|продолжаем выпуск", 3),
    (r"переходите|скачивайте|оформляйте|обновляйте|обновите|регистрируйтесь|заказывайте"
     r"|успейте|заходите в|покупайте|покупай ", 3),
    (r"рекомендую", 2), (r"первый месяц", 2), (r"рассрочк|гаранти", 1), (r"бесплатн", 1),
    (r"акци[яию]", 1), (r"\d+ ?%|процент", 1), (r"приложени", 1), (r"подписк", 1),
    (r"доставк", 1), (r"курс\w* ", 1),
]]
AD_START = re.compile(r"друзья|ненадолго|прерв|хочу поделиться|новост|кстати|расскажу|реклам|спонсор|партнер")
AD_END = re.compile(r"возвращаемся|вернемся|продолжаем|а теперь|итак")
# как рекламодатель записан в маркировке -> как его называть и как его произносят
BRAND_ALIAS = {
    "тбанк": ("Т-Банк", ["т-банк", "т банк", "тбанк", "тинькофф", "тиньков"]),
    "тинькофф": ("Т-Банк", ["т-банк", "т банк", "тинькофф", "тиньков"]),
    "tbank": ("Т-Банк", ["т-банк", "т банк", "тбанк", "тинькофф"]),
    "tinkoff": ("Т-Банк", ["т-банк", "т банк", "тинькофф"]), "tcpa": ("Т-Банк", ["т-банк", "тинькофф"]),
    "сбербанк": ("Сбер", ["сбер"]), "сбер": ("Сбер", ["сбер"]),
    "рвб": ("Wildberries", ["wildberries", "вайлдберриз", "вб"]),
    "wildberries": ("Wildberries", ["wildberries", "вайлдберриз"]),
    "wb": ("Wildberries", ["wildberries", "вайлдберриз", "вб"]),
    "яндекс": ("Яндекс", ["яндекс"]), "yandex": ("Яндекс", ["яндекс"]),
    "aviasales": ("Авиасейлс", ["авиасейлс", "aviasales"]),
    "skillbox": ("Skillbox", ["скилбокс", "скиллбокс", "skillbox"]), "skbx": ("Skillbox", ["скилбокс", "skillbox"]),
    "скилбокс": ("Skillbox", ["скилбокс", "скиллбокс", "skillbox"]),
    "образовательныетехнологии": ("Skillbox", ["скилбокс", "skillbox"]),
    "ozon": ("Ozon", ["озон", "ozon"]), "интернетрешения": ("Ozon", ["озон", "ozon"]), "озон": ("Ozon", ["озон", "ozon"]),
    "авито": ("Авито", ["авито"]), "avito": ("Авито", ["авито"]), "кехекоммерц": ("Авито", ["авито"]),
    "skyeng": ("Skyeng", ["скайенг", "skyeng"]), "эдюсон": ("Eduson", ["эдюсон", "eduson"]),
    "альфа": ("Альфа-Банк", ["альфа"]), "втб": ("ВТБ", ["втб"]), "мтс": ("МТС", ["мтс"]),
    "сибирь": ("S7", ["s7", "эс севен"]), "s7": ("S7", ["s7", "эс севен"]),
}
LEGAL_FORM = re.compile(r"\b(ооо|ао|пао|зао|нао|ип|ноу|дпо|оао|ано|мкао)\b", re.I)
# сокращатели и служебные адреса: по ним бренд не узнать
LINK_HOSTS = {"bit", "clck", "linkwb", "max", "2w", "tb", "youtube", "youtu", "vk", "t", "l", "goo",
              "trk", "ppdu", "band", "taplink", "linktr", "instagram", "tiktok", "telegram", "ticketscloud",
              "boosty", "patreon", "donationalerts", "google", "apple", "onelink", "adj", "app", "go", "redirect"}
MARK_RE = re.compile(r"\berid\b|реклама[.:]|рекламодатель|\bинн\b", re.I)
PROMO_RE = re.compile(r"промокод|promo ?code|скидк|кешб[эе]к|кэшб[эе]к", re.I)
URL_RE = re.compile(r"https?://([^\s/]+)(\S*)")


def flat(s):
    return re.sub(r"[\s\-–—.«»\"']+", "", (s or "").lower().replace("ё", "е"))


def _canon(name):
    """«ООО "Медиа-дом «Красный квадрат»"» -> «Красный квадрат», плюс как его произносят."""
    n = LEGAL_FORM.sub(" ", name or "")
    n = re.sub(r"^\s*(медиа-дом|авиакомпания|московская школа управления|школа|компания)\s+", "", n, flags=re.I)
    n = re.sub(r"\s{2,}", " ", n).strip(" .,-«»\"“”'")
    f = flat(n)
    for key, (display, spoken) in BRAND_ALIAS.items():
        if key in f:
            return display, list(spoken)
    return n, [n.lower().replace("ё", "е")] if n else []


def _hosts(text):
    """домены из ссылок без сокращателей: l.tbank.ru -> tbank."""
    out = []
    for host, _ in URL_RE.findall(text):
        parts = [p for p in host.lower().split(".") if p not in ("www", "l", "t", "m", "go")]
        if len(parts) >= 2 and parts[-2] not in LINK_HOSTS and len(parts[-2]) >= 3:
            out.append(parts[-2])
    for d in re.findall(r"(?<![/\w.])([a-z0-9-]{3,})\.(?:ru|com|io|pro|me|app|net|org)\b", text, re.I):
        if d.lower() not in LINK_HOSTS:
            out.append(d.lower())
    return out


def _product(lines):
    """первое имя собственное в строке над маркировкой: «WB Ресейл», «Revyline»."""
    for line in lines:
        if MARK_RE.search(line):
            continue
        text = URL_RE.sub(" ", line)
        for m in re.finditer(r"[A-ZА-ЯЁ][\w-]+|[A-Za-z][\w-]{2,}", text):
            w = m.group(0)
            if m.start() == 0 and re.match(r"[А-ЯЁ]", w) and not re.match(r"[А-ЯЁ]{2,}$", w):
                continue  # обычное начало предложения: «Скачать», «Покупайте»
            if w.lower() not in CAP_STOP and flat(w) not in LINK_HOSTS:
                return [w]
    return []


def _marker_ads(desc):
    """рекламодатели по маркировке «Реклама. … erid», ИНН и по промокоду со ссылкой;
    текст - сама маркировка и строки прямо над ней."""
    lines = (desc or "").splitlines()
    found = {}

    def add(name, words, marks, erid=None, extra=(), text=""):
        display, spoken = _canon(name)
        if not display or len(display) < 2 or flat(display) in LINK_HOSTS:
            return
        key = flat(display)
        ad = found.setdefault(key, {"name": display, "words": [], "marks": [], "erid": [], "text": ""})
        ad["text"] += " " + text
        for w in list(spoken) + list(words) + list(extra):
            w = (w or "").lower().strip()
            if len(w) >= 2 and w not in ad["words"]:
                ad["words"].append(w)
        for m in marks:
            if m not in ad["marks"]:
                ad["marks"].append(m)
        if erid and erid not in ad["erid"]:
            ad["erid"].append(erid)

    for i, line in enumerate(lines):
        marked = MARK_RE.search(line)
        promo = PROMO_RE.search(line)
        if not marked and not promo:
            continue
        # блок рекламы: строка с маркировкой и пара строк над ней до пустой
        top = i
        while top > 0 and i - top < 3 and lines[top - 1].strip():
            top -= 1
        chunk = "\n".join(lines[top:i + 1])
        erid = re.search(r"erid[:\s=]*([\w-]{6,})", chunk, re.I)
        marks = (["erid"] if erid else []) + (["маркировка"] if re.search(r"реклама[.:]", line, re.I) else []) \
            + (["промокод"] if re.search(r"промокод|promo ?code", chunk, re.I) else [])
        hosts = _hosts(chunk)
        names = []
        in_line = [display for key, (display, _) in BRAND_ALIAS.items() if key in flat(line)]
        known = [display for key, (display, _) in BRAND_ALIAS.items() if key in flat(chunk)]
        line_hosts = _hosts(line)
        if marked:
            names += re.findall(r"[«\"“]([^»\"”]{2,40})[»\"”]", line)
            m = re.search(r"рекламодатель[:\s]+(.+?)(?:[.,]|\bинн\b|$)", line, re.I)
            if m:
                names.append(m.group(1))
            m = re.search(r"\b(?:ооо|ао|пао|ип)\s+([^«\"“,.]{3,40}?)\s*(?:,|\.|\bинн\b)", line, re.I)
            if m:
                names.append(m.group(1))
            # «Скачать приложение Т-Банка: ссылка / Erid: …» - юрлица нет, бренд в тексте над ней
            if not names:
                names += (in_line[:1] or [h.capitalize() for h in line_hosts[:1]] or known[:1]
                          or [h.capitalize() for h in hosts[:1]] or _product(lines[top:i + 1]))
        elif hosts:
            # промокод без маркировки считается, только если рядом ссылка на бренд
            names = hosts[:1]
        for n in names[:1]:
            # рекламный текст блока: по его словам реклама ищется в субтитрах
            add(n, hosts, marks or ["ссылка"], erid.group(1) if erid else None, text=chunk)
    return list(found.values())


SOCIAL_HOSTS = {"t.me", "telegram.me", "vk.com", "vk.ru", "instagram.com", "youtube.com", "youtu.be",
                "tiktok.com", "boosty.to", "twitch.tv", "dzen.ru", "x.com", "twitter.com", "facebook.com",
                "patreon.com", "donationalerts.com", "music.yandex.ru", "band.link", "taplink.cc",
                "telegram.dog", "vkvideo.ru", "rutube.ru"}
SHORTENERS = {"clck.ru", "bit.ly", "goo.su", "2w.ru", "t.tb.ru", "tinyurl.com", "cutt.ly", "vk.cc",
              "s.click.aliexpress.com", "fas.st", "kas.pr", "l.skbx.pro", "ya.cc", "linkwb.ru", "ozon.ru/t"}
SELL_RE = re.compile(r"промокод|promo ?code|скидк|бесплатн|без комисси|кешб[эе]к|кэшб[эе]к|\d ?%|₽|\bруб|"
                     r"откройте|открой|оформ|подключ|закаж|заказ|купи|покуп|получи|переход|регистр|скача|"
                     r"установ|попробуй|попробуйте|акци|тариф|подарок|в подарок|выгод", re.I)


# свои ссылки автора: источники, подписка, телеграм
NOT_AD_RE = re.compile(r"источник|подпис\w* на (мой |наш )?канал|подпишись|подписывайтесь|"
                       r"(мой|наш|у меня в|в моем|в нашем) (тг|телеграм)|telegram|телеграм-канал|"
                       r"соцсет|мерч|донат|поддержать канал|boosty|сотрудничеств|по вопросам рекламы|"
                       r"размещение рекламы", re.I)


def _ad_paragraphs(desc):
    """абзацы описания, которые выглядят как реклама: ссылка не на соцсети автора
    и продающие слова или короткая ссылка. Маркировку в конце описания они не содержат."""
    out = []
    for para in re.split(r"\n\s*\n|\n[\s_—–=*~•.-]{3,}\n", desc or ""):
        # строки со своими ссылками автора (телеграм, источники) в рекламный текст не берём
        lines = [l for l in para.splitlines() if l.strip() and not MARK_RE.search(l)
                 and not NOT_AD_RE.search(l)
                 and not (URL_RE.search(l) and all(h.lower().removeprefix("www.") in SOCIAL_HOSTS
                                                   for h, _ in URL_RE.findall(l)))]
        if not lines or sum(1 for l in lines if TC_RE.match(l.strip())) >= 2:
            continue
        # в одном абзаце бывает несколько реклам подряд: каждая кончается своей ссылкой
        units, cur = [], []
        for l in lines:
            cur.append(l)
            if URL_RE.search(l):
                units.append(cur)
                cur = []
        if cur and units:
            units[-1] += cur
        elif cur:
            units.append(cur)
        for u in units:
            text = "\n".join(u)
            if _is_ad_text(text):
                out.append(text)
    return out


def _is_ad_text(text):
    hosts = [h.lower().removeprefix("www.") for h, _ in URL_RE.findall(text)]
    shop = [h for h in hosts if h not in SOCIAL_HOSTS]
    return bool(shop and (SELL_RE.search(text) or any(h in SHORTENERS for h in shop)))


def _block_name(text):
    """как назвать рекламу по её абзацу: «Плати по всему миру», Т-Банк, ggsel, ЧТИВО."""
    q = re.findall(r"[«\"“]([^»\"”]{3,40})[»\"”]", text)
    if q:
        return q[0]
    known = [display for key, (display, _) in BRAND_ALIAS.items() if key in flat(text)]
    if known:
        return known[0]
    words = re.findall(r"[A-Za-zА-ЯЁа-яё][\w-]{2,}", URL_RE.sub(" ", text))
    hosts = _hosts(text)
    for h in hosts:
        # домен, который написан и словами: ggsel.net и «ggsel», chtv.ru и «ЧТИВО»
        for w in words:
            if flat(w) == h or (len(skeleton(h)) >= 3 and skeleton(w) == skeleton(h)):
                return w
    names = _product(text.splitlines())
    return hosts[0].capitalize() if hosts else (names[0] if names else "")


def advertisers(desc):
    """кто рекламируется в описании. Рекламный абзац («Откройте счёт в Точка Банке… ссылка»)
    и маркировка «Реклама. АО «Точка» … Erid» часто стоят в разных концах описания:
    связываем их по имени, а оставшиеся - по порядку."""
    ads = _marker_ads(desc)
    blocks = [b for b in _ad_paragraphs(desc)
              if not any(flat(b)[:60] in flat(a["text"]) for a in ads)]
    # маркировка без рекламного текста и без ссылки: текст где-то выше в описании
    bare = [a for a in ads if not URL_RE.search(a["text"]) and not re.sub(
        r"(?im)^.*(erid|реклама[.:]|рекламодатель|\bинн\b).*$", "", a["text"]).strip()]
    left = []
    for b in blocks:
        fb = flat(b)
        hit = next((a for a in ads if any(len(pre(flat(w))) >= 4 and pre(flat(w)) in fb
                                          for w in [a["name"]] + a["words"])), None)
        if hit:
            hit["text"] += " " + b
            if hit in bare:
                bare.remove(hit)
        else:
            left.append(b)
    # маркировки без текста и абзацы без маркировки: обычно перечислены в одном порядке
    known = {d for d, _ in BRAND_ALIAS.values()}
    for a, b in zip(list(bare), list(left)):
        name = _block_name(b)
        if a["name"] in known and name in known and name != a["name"]:
            continue  # «Авито» в тексте и «Ozon» в маркировке - разные рекламодатели
        if name and flat(a["name"]) not in flat(b):
            a["words"].append(a["name"].lower())
            a["legal"], a["name"] = a["name"], name
        a["text"] += " " + b
        left.remove(b)
    for b in left:
        name = _block_name(b)
        if not name:
            continue
        same = next((a for a in ads if flat(a["name"]) == flat(name)), None)
        if same:
            same["text"] += " " + b
        else:
            ads.append({"name": name, "words": [name.lower()] + _hosts(b), "marks": ["ссылка"],
                        "erid": [], "text": b})
    return ads


def ad_brands(desc):
    """слова, по которым рекламодателей из описания искать в субтитрах."""
    return sorted({w for ad in advertisers(desc) for w in ad["words"]})


def brand_in(text, ads, bursty=()):
    """какой рекламодатель звучит в куске субтитров: имя бренда или слова его блока в описании."""
    toks = re.findall(r"[а-яёa-z0-9-]+", text.lower().replace("ё", "е"))
    toks = [translit(t) if re.match(r"[a-z]", t) else t for t in toks]
    best, top = None, 0
    for ad in ads:
        stems, names = ad_vocab(ad)
        skels = {skeleton(n) for n in names if len(skeleton(n)) >= 3}
        score = 3 * sum(1 for t in toks if flat(t) in names or (len(t) >= 4 and (
            any(len(n) >= 4 and n in flat(t) for n in names) or skeleton(t) in skels)))
        score += len({pre(t) for t in toks if len(t) >= 4 and pre(t) in stems})
        if score > top:
            best, top = ad["name"], score
    if best and top >= 2:
        return best
    if ads:
        return ""
    names = {}
    for tok in WORD.findall(text):
        st = stem(tok.lower().replace("ё", "е"))
        if st in bursty and (tok[0].isupper() or re.match(r"[A-Za-z]", tok)):
            names[tok] = names.get(tok, 0) + 1
    return max(names, key=names.get) if names else ""


CAP_STOP = set("""это этот эта эти тоже когда потом сейчас просто вообще конечно спасибо друзья
ребята привет давайте смотрите короче кстати значит поэтому хорошо итак тогда здесь там теперь
также если пока ладно окей блин слушай слушайте""".split())
WORD = re.compile(r"[A-Za-zА-ЯЁа-яё][\w-]{2,}")


def norm_text(s):
    s = re.sub(r"[^\w\s%-]+", " ", s.lower().replace("ё", "е"))
    return " " + re.sub(r"\s+", " ", s).strip() + " "


def stem(w):
    return w[:max(4, len(w) - 2)]


def bursty_names(segs):
    """слова с большой буквы, которые звучат кучно и больше нигде в ролике: похоже на бренд."""
    seen, caps = {}, set()
    for a, _, t in segs:
        for m in WORD.finditer(t):
            w = m.group(0)
            st = stem(w.lower().replace("ё", "е"))
            seen.setdefault(st, []).append(a)
            before = t[:m.start()].rstrip()
            if re.match(r"[A-Za-z]", w) or (w[0].isupper() and before and before[-1] not in ".!?>\""):
                caps.add(st)
    return {st for st in caps if st not in CAP_STOP and len(st) >= 3
            and 2 <= len(seen[st]) <= 15 and seen[st][-1] - seen[st][0] <= 150}


def subtitle_segments(url, args, info):
    """автосубтитры на языке ролика: [[начало, конец, текст], ...] или None."""
    lang = (info.get("language") or "ru").split("-")[0]
    manual, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    pick = None
    for key in (lang, "ru", "en"):
        if key in manual:
            pick = ("--write-subs", key)
            break
        for k in (f"{key}-orig", key):
            if k in auto:
                pick = ("--write-auto-subs", k)
                break
        if pick:
            break
    if not pick:
        return None, None
    with tempfile.TemporaryDirectory(prefix="ytads-subs-") as d:
        r = subprocess.run([YTDLP, "--skip-download", pick[0], "--sub-langs", pick[1],
                            "--sub-format", "json3", "--no-warnings", "-q",
                            "-o", f"{d}/s.%(ext)s", *args, url],
                           capture_output=True, text=True, errors="replace")
        files = list(Path(d).glob("*.json3"))
        if not files:
            raise RuntimeError(friendly((r.stderr or "").strip()[-300:]) or "субтитры не скачались")
        events = json.loads(files[0].read_text()).get("events") or []
    segs = []
    for e in events:
        t = "".join(s.get("utf8", "") for s in e.get("segs") or []).replace("\n", " ").strip()
        if t:
            a = e.get("tStartMs", 0) / 1000
            segs.append([a, a + e.get("dDurationMs", 0) / 1000, t])
    # строки автосубтитров наезжают друг на друга, конец строки = начало следующей
    for i in range(len(segs) - 1):
        segs[i][1] = min(segs[i][1], segs[i + 1][0])
    return segs, pick[1]


def transcript_ads(segs, desc="", extra_words=()):
    """кучки, где рядом звучат бренд и призыв: «ссылка в описании», промокод, QR."""
    brands = {stem(norm_text(b).strip()) for b in ad_brands(desc)}
    brands |= {norm_text(w).strip() for w in extra_words if w.strip()}
    brands = {b for b in brands if len(b) >= 3}
    bursty = bursty_names(segs)
    scored = []
    for a, b, t in segs:
        n, score, call, named, marked, why = norm_text(t), 0, 0, 0, 0, []
        for rx, w in TRIGGERS:
            m = rx.search(n)
            if m:
                score, call = score + w, max(call, w)
                why.append(m.group(0).strip())
        for x in brands:
            if x in n:
                score, named, marked = score + 4, named + 1, marked + 1
                why.append(x)
        for tok in set(WORD.findall(t)):
            if stem(tok.lower().replace("ё", "е")) in bursty and tok.lower() not in why:
                score, named = score + 3, named + 1
                why.append(tok)
        scored.append({"a": a, "b": b, "t": n, "score": score, "call": call, "named": named,
                       "marked": marked, "why": why})

    groups = []
    for i, s in enumerate(scored):
        if not s["score"]:
            continue
        if groups and s["a"] - scored[groups[-1][-1]]["b"] <= 75:
            groups[-1].append(i)
        else:
            groups.append([i])
    out = []
    for g in groups:
        hits = [scored[i] for i in g]
        if (sum(h["score"] for h in hits) < 10 or not any(h["call"] >= 3 for h in hits)
                or not any(h["named"] for h in hits)):
            continue
        # края держатся за бренд из маркировки или призыв; имя, которое просто звучит
        # кучно, годится только если ничего надёжнее нет
        core = [i for i in g if scored[i]["marked"] or scored[i]["call"] >= 3]
        if not any(scored[i]["marked"] for i in g):
            core = [i for i in g if scored[i]["named"] or scored[i]["call"] >= 3]
        i0, i1 = core[0], core[-1]
        a, b = scored[i0]["a"], scored[i1]["b"]
        for j in range(i0 - 1, -1, -1):
            if a - scored[j]["a"] > 15:
                break
            if AD_START.search(scored[j]["t"]):
                a = scored[j]["a"]
        b0 = b
        for j in range(i1 + 1, len(scored) if not AD_END.search(scored[i1]["t"]) else 0):
            if scored[j]["a"] - b0 > 12:
                break
            b = scored[j]["b"]
            if AD_END.search(scored[j]["t"]):
                break
        why = []
        for i in range(i0, i1 + 1):
            why += [w for w in scored[i]["why"] if w not in why]
        out.append({"start": max(0.0, a - 2), "end": b + 2, "why": why[:5]})
    return out


TRANSLIT = [("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("ts", "ц"), ("kh", "х"), ("ph", "ф"),
            ("th", "т"), ("ya", "я"), ("yu", "ю"), ("oo", "у"), ("ee", "и"), ("ck", "к"), ("x", "кс"),
            ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"),
            ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"),
            ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("y", "и"),
            ("z", "з")]
# слова рекламного текста, которые ничего не говорят о бренде
AD_STOP = set("""скачать скачайте скачивайте переходите ссылке ссылка ссылки описании подробнее
условия узнать реклама рекламодатель лицензия erid utm source medium campaign content https http
www html youtube выпуск видео канал этого через только также можно более всего ваших ваши своих
который которые будет этом этот этой""".split())


def translit(w):
    for a, b in TRANSLIT:
        w = w.replace(a, b)
    return w


def skeleton(w):
    """согласные без повторов: «Revyline» и «Ревилайн» -> «рвлн»."""
    w = translit(w.lower())
    w = re.sub(r"[аеёиоуыэюяйьъ]", "", w)
    return re.sub(r"(.)\1+", r"\1", w)


def pre(w):
    """одинаковое начало у разных форм: «продавайте» и «продавай» -> «прода»."""
    return w[:5]


def ad_vocab(ad):
    """основы слов из рекламного блока описания и из ссылки, латиница - ещё и кириллицей."""
    text = re.sub(r"erid[:\s=]*\S+", " ", ad.get("text", ""), flags=re.I)
    text += " " + " ".join(re.split(r"[/_.?=&#-]+", " ".join(p for _, p in URL_RE.findall(text))))
    stems = set()
    for w in re.findall(r"[A-Za-zА-ЯЁа-яё]{4,}", text):
        w = w.lower().replace("ё", "е")
        if w in AD_STOP or re.search(r"\d", w):
            continue
        stems.add(pre(translit(w) if re.match(r"[a-z]", w) else w))
    names = {flat(ad["name"])} | {flat(w) for w in ad["words"]}
    names = {translit(n) if re.match(r"[a-z]", n) else n for n in names if len(n) >= 2}
    return stems, names


def locate_advertisers(segs, ads):
    """каждого рекламодателя из описания ищем в субтитрах по словам его же рекламного
    текста: бренд в автосубтитрах часто искажён («VBALE» вместо WB), а слоган совпадает."""
    words = [[w for w in re.findall(r"[а-яёa-z0-9-]+", s[2].lower().replace("ё", "е"))] for s in segs]
    total = {}
    for ws in words:
        for w in set(pre(translit(x) if re.match(r"[a-z]", x) else x) for x in ws if len(x) >= 4):
            total[w] = total.get(w, 0) + 1
    out = []
    for ad in ads:
        stems, names = ad_vocab(ad)
        skels = {skeleton(n) for n in names if len(skeleton(n)) >= 3}
        hits = []
        for i, ws in enumerate(words):
            got, brand = set(), False
            for x in ws:
                xc = translit(x) if re.match(r"[a-z]", x) else x
                if len(xc) >= 4 and pre(xc) in stems:
                    got.add(pre(xc))
                if flat(xc) in names or any(len(n) >= 4 and n in flat(xc) for n in names) \
                        or (len(xc) >= 4 and skeleton(xc) in skels):
                    brand = True
            if got or brand:
                hits.append((i, got, brand))
        # вес слова: редкое в ролике весит 1, частое («банк», «карта») - меньше
        weight = lambda w: min(1.0, 2.0 / max(1, total.get(w, 1)))
        # окна по минуте: где кучно звучат слова рекламного блока
        marked = []
        for k, (i, got, brand) in enumerate(hits):
            t0 = segs[i][0]
            near = [h for h in hits[k:] if segs[h[0]][0] - t0 <= 60]
            ws = set().union(*(h[1] for h in near))
            score = sum(weight(w) for w in ws) + (1.5 if any(h[2] for h in near) else 0)
            if score >= 2 and len(ws) >= 2:
                marked.append((near[0][0], near[-1][0]))
        regions = []
        for i0, i1 in marked:
            if regions and segs[i0][0] - segs[regions[-1][1]][1] <= 20:
                regions[-1][1] = max(regions[-1][1], i1)
            else:
                regions.append([i0, i1])
        best = []
        for i0, i1 in regions:
            inside = [h for h in hits if i0 <= h[0] <= i1]
            ws = set().union(*(h[1] for h in inside))
            score = sum(weight(w) for w in ws) + (1.5 if any(h[2] for h in inside) else 0)
            # края - по самым весомым попаданиям, а не по случайному «банк»
            core = [h[0] for h in inside if h[2] or any(weight(w) >= 0.5 for w in h[1])]
            if core:
                best.append((score, core[0], core[-1], sorted(ws, key=weight, reverse=True)))
        for score, i0, i1, strong in sorted(best, reverse=True)[:2]:
            a, b = ad_bounds(segs, i0, i1)
            out.append({"start": a, "end": b, "brand": ad["name"], "why": strong[:5], "score": round(score, 1)})
    return out


def ad_bounds(segs, i0, i1):
    """края вставки: ближайшая пауза в субтитрах или [музыка] до первого и после
    последнего попадания. Обычно реклама так и отбита от выпуска."""
    def pause(j):
        gap = segs[j][0] - segs[j - 1][1] if j > 0 else 99
        # [музыка] посреди фразы бывает и внутри рекламы, границей считаем только отдельную строку
        return gap >= 2.5 or re.fullmatch(r"(>>\s*)?\[(музыка|music)\]", segs[j][2].strip().lower())
    a = segs[i0][0]
    for j in range(i0, -1, -1):
        if segs[i0][0] - segs[j][0] > 25:
            break
        if pause(j):
            a = segs[j][1] if re.search(r"\[(музыка|music)\]", segs[j][2].lower()) else segs[j][0]
            break
    b = segs[i1][1]
    for j in range(i1 + 1, len(segs)):
        if segs[j][0] - segs[i1][1] > 15:
            break
        if pause(j):
            b = segs[j - 1][1]
            break
        b = segs[j][1]
    return round(max(0.0, a - 0.5), 2), round(b + 0.5, 2)


def add_transcript(cands, found, dur):
    for f in found:
        same = [c for c in cands if c["ad"] and min(c["end"], f["end"]) > max(c["start"], f["start"])]
        if same:
            # реклама уже известна, субтитры только подтверждают
            for c in same:
                if "субтитры" not in c["src"]:
                    c["src"].append("субтитры")
            continue
        cands.append({"start": round(f["start"], 2), "end": round(min(f["end"], dur or f["end"]), 2),
                      "label": f"{f['brand']} (по описанию и субтитрам)" if f.get("brand")
                      else "по субтитрам: " + ", ".join(f["why"]),
                      "src": ["описание", "субтитры"] if f.get("brand") else ["субтитры"],
                      "forced": False, "ad": True, **({"brand": f["brand"]} if f.get("brand") else {})})
    cands.sort(key=lambda c: c["start"])
    return cands


# ---------- разметка рекламы через Claude ----------
# подбор слов ошибается: бренд в субтитрах искажён, «точка» бывает просто словом.
# Claude читает описание и субтитры целиком и ставит границы почти до секунды.

def find_claude():
    for p in (shutil.which("claude"), Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude",
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if p and Path(p).exists():
            return str(p)
    return None


CLAUDE = find_claude()
AI_MODEL = os.environ.get("YTADS_MODEL", "sonnet")
AI_NAMES = {"claude": "Claude", "openai": "ChatGPT"}
OPENAI_BASE = os.environ.get("YTADS_OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_PREF = ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini", "gpt-5", "gpt-4.1", "gpt-4o"]
_openai_model = {}
AI_PROMPT = """Ниже описание YouTube-ролика и его автосубтитры с метками времени [м:сс]. Найди в ролике \
все рекламные интеграции: места, где ведущий или вставка рекламирует товар, сервис или бренд. Опирайся \
на описание: рекламные абзацы со ссылками и промокодами и маркировка «Реклама … erid» показывают, кто \
рекламодатели. Бренд в автосубтитрах может быть искажён. Самопромо канала (подписка, свой телеграм) не \
считай рекламой. Обычное обсуждение компании в выпуске тоже не реклама.

Границы ставь точно: начало — первая фраза рекламы (часто заход вроде «кстати», смена темы или \
подводка-сценка), конец — последняя фраза рекламы до возвращения к выпуску.

Ответь только JSON-массивом без пояснений:
[{"start":"м:сс","end":"м:сс","brand":"бренд","evidence":"короткая цитата"}]"""


def curl_json(url, headers=None, body=None, proxy="", timeout=60):
    """запрос через curl: идёт и через SOCKS-прокси, а ключ не виден в списке процессов."""
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-w", "\n%{http_code}"]
    if proxy:
        cmd += ["-x", proxy.replace("socks5://", "socks5h://")]
    hfile = None
    if headers:
        fd, hfile = tempfile.mkstemp(prefix="ytads-h-")
        os.close(fd)
        os.chmod(hfile, 0o600)
        Path(hfile).write_text("\n".join(f"{k}: {v}" for k, v in headers.items()))
        cmd += ["-H", f"@{hfile}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True,
                           text=True, errors="replace", timeout=timeout + 10)
    finally:
        if hfile:
            os.unlink(hfile)
    if r.returncode != 0:
        raise RuntimeError(f"нет связи с {urllib.parse.urlsplit(url).hostname}")
    text, _, code = r.stdout.rpartition("\n")
    try:
        data = json.loads(text)
    except ValueError:
        data = {"raw": text[:300]}
    return int(code or 0), data


def read_secret(name):
    f = ROOT / ".secrets" / name
    return f.read_text().strip() if f.exists() else ""


def write_secret(name, value):
    d = ROOT / ".secrets"
    d.mkdir(exist_ok=True)
    os.chmod(d, 0o700)
    f = d / name
    f.write_text(value.strip() + "\n")
    os.chmod(f, 0o600)


def openai_key():
    return os.environ.get("OPENAI_API_KEY", "").strip() or read_secret("openai_api_key.txt")


def ai_provider(pref=None):
    """кто размечает рекламу: claude, openai или None. pref: auto, claude, openai, off."""
    pref = pref or os.environ.get("YTADS_AI") or "auto"
    if pref == "off":
        return None
    if pref in ("auto", "claude") and CLAUDE:
        return "claude"
    if pref in ("auto", "openai") and openai_key():
        return "openai"
    return None


def pick_openai_model(proxy=""):
    if os.environ.get("YTADS_OPENAI_MODEL"):
        return os.environ["YTADS_OPENAI_MODEL"]
    if "m" not in _openai_model:
        code, d = curl_json(OPENAI_BASE + "/models", {"Authorization": "Bearer " + openai_key()},
                            proxy=proxy, timeout=30)
        ids = {x.get("id") for x in d.get("data", [])} if code == 200 and isinstance(d, dict) else set()
        _openai_model["m"] = next((m for m in OPENAI_PREF if m in ids), "gpt-4o-mini")
    return _openai_model["m"]


def _ask_openai(text, proxy=""):
    code, d = curl_json(OPENAI_BASE + "/chat/completions", {"Authorization": "Bearer " + openai_key()},
                        {"model": pick_openai_model(proxy), "messages": [
                            {"role": "system", "content": AI_PROMPT}, {"role": "user", "content": text}]},
                        proxy=proxy, timeout=600)
    err = d.get("error", {}) if isinstance(d, dict) else {}
    if code in (401, 403):
        raise RuntimeError("OpenAI не принял ключ")
    if code == 429 or err.get("code") == "insufficient_quota":
        raise RuntimeError("На счёте OpenAI закончились деньги или превышен лимит запросов")
    if code != 200:
        raise RuntimeError(f"ChatGPT не ответил: {str(err.get('message') or d)[:200]}")
    return d["choices"][0]["message"]["content"] or ""


def _ask_claude(text):
    r = subprocess.run([CLAUDE, "-p", AI_PROMPT, "--model", AI_MODEL], input=text,
                       capture_output=True, text=True, errors="replace", timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"Claude не ответил: {(r.stderr or r.stdout or '').strip()[-200:]}")
    return r.stdout or ""


def ai_ads(segs, desc, title="", provider=None, proxy=""):
    """реклама по разметке нейросети: [{start, end, brand, why}]. None, если нейросети нет."""
    provider = provider or ai_provider()
    if not provider:
        return None
    lines = [f"[{hms(a)}] {t}" for a, _, t in segs]
    text = f"НАЗВАНИЕ: {title}\n\nОПИСАНИЕ:\n{desc or ''}\n\nСУБТИТРЫ:\n" + "\n".join(lines)
    answer = _ask_openai(text, proxy) if provider == "openai" else _ask_claude(text)
    m = re.search(r"\[.*\]", answer, re.S)
    if not m:
        raise RuntimeError(f"{AI_NAMES[provider]} ответил не в том виде: {answer.strip()[:160]}")
    out = []
    for f in json.loads(m.group(0)):
        try:
            a, b = parse_tc(str(f["start"])), parse_tc(str(f["end"]))
        except (KeyError, ValueError):
            continue
        if b > a:
            out.append({"start": float(a), "end": float(b), "brand": (f.get("brand") or "").strip(),
                        "why": [(f.get("evidence") or "").strip()[:160]]})
    return out


def add_ai(cands, found, dur, src="claude"):
    """реклама от Claude. Где она совпала с SponsorBlock или главой, границы оставляем их,
    а бренд и источник дописываем."""
    for f in found:
        same = [c for c in cands if min(c["end"], f["end"]) - max(c["start"], f["start"])
                > 0.5 * min(c["end"] - c["start"], f["end"] - f["start"])]
        if same:
            for c in same:
                if c["ad"] or "sponsorblock" in c["src"] or len(same) == 1:
                    if not c["ad"] and "глава" in c["src"]:
                        # глава целиком про рекламу: берём точные края от Claude
                        c["start"], c["end"] = f["start"], f["end"]
                    c["ad"] = True
                    c["brand"] = f["brand"]
                    if flat(f["brand"]) not in flat(c["label"]):
                        c["label"] = f"{f['brand']} · {c['label']}" if c["label"] else f["brand"]
                    if src not in c["src"]:
                        c["src"].append(src)
            if any(c["ad"] for c in same):
                continue
        cands.append({"start": round(f["start"], 2), "end": round(min(f["end"], dur or f["end"]), 2),
                      "label": f["brand"] or "реклама", "src": [src], "forced": False, "ad": True,
                      "brand": f["brand"], "evidence": f["why"][0] if f["why"] else ""})
    cands.sort(key=lambda c: c["start"])
    return cands


# ---------- задачи ----------

def jset(job, **kw):
    with LOCK:
        job.update(kw)


def jlog(job, line):
    with LOCK:
        job["log"].append(line)
        del job["log"][:-300]


DL_RE = re.compile(r"\[download\]\s+([\d.]+)%.*?at\s+(\S+)(?:.*?ETA\s+(\S+))?")
FF_RE = re.compile(r"size=\s*(\d+)\s*(KiB|kB|MiB|B)?.*?time=(-?\d+):(\d+):([\d.]+)")
UNIT = {"B": 1, "kB": 1000, "KiB": 1024, "MiB": 1024 ** 2, None: 1024}


def rate_text(bps):
    return f"{bps / 1024 ** 2:.1f} МБ/с" if bps >= 1024 ** 2 else f"{bps / 1024:.0f} КБ/с"


def progress(job, frac, speed="", eta=""):
    """доля текущего файла плюс общий прогресс по очереди."""
    i, n = job.get("slot") or (0, 1)
    jset(job, pct=(i + min(max(frac, 0), 1)) / n * 100,
         sub={"pct": round(min(max(frac, 0), 1) * 100), "speed": speed, "eta": eta})


def run_stream(cmd, job, span=None):
    """span - длина отрезка в секундах, по ней считается прогресс ffmpeg."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, errors="replace")
    jset(job, proc=proc)
    tail = []
    t0, size0 = time.time(), 0
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-6]
        m = DL_RE.search(line)
        if m:
            speed = m.group(2).replace("KiB/s", " КБ/с").replace("MiB/s", " МБ/с")
            eta = m.group(3) if m.group(3) not in (None, "Unknown") else ""
            progress(job, float(m.group(1)) / 100, "" if "Unknown" in speed else speed, eta)
            continue
        m = FF_RE.search(line)
        if m:
            done = int(m.group(3)) * 3600 + int(m.group(4)) * 60 + float(m.group(5))
            size = int(m.group(1)) * UNIT[m.group(2)]
            dt = time.time() - t0
            bps = size / dt if dt > 1 else 0
            frac = done / span if span else 0
            eta = ""
            if 0 < frac < 1 and dt > 3:
                left = dt / frac * (1 - frac)
                eta = f"{int(left // 60)}:{int(left % 60):02d}"
            progress(job, frac, rate_text(bps) if bps else "", eta)
            continue
        if "[download]" in line and "%" in line:
            continue
        jlog(job, line)
    proc.wait()
    jset(job, proc=None)
    if proc.returncode != 0:
        if job.get("cancelled"):
            raise RuntimeError("отменено")
        raise RuntimeError(" / ".join(tail[-3:]) or f"код {proc.returncode}")


def label_brands(cands, segs, ads):
    """каждой рекламе - имя бренда по тому, что звучит в субтитрах на её отрезке."""
    bursty = bursty_names(segs)
    for c in cands:
        if not c["ad"] or c.get("brand"):
            continue
        text = " ".join(t for a, b, t in segs if b > c["start"] and a < c["end"])
        brand = brand_in(text, ads, bursty)
        if not brand:
            continue
        c["brand"] = brand
        if c["src"] == ["субтитры"]:
            c["label"] = f"{brand} (по субтитрам)"
        elif flat(brand) not in flat(c["label"]):
            c["label"] = f"{brand} · {c['label']}"


def scan_one(url, req, args, extra, extra_sb, job, note=""):
    info = ytdlp_info(url, args)
    vid = info.get("id") or video_id(url)
    sb = sponsorblock(vid, extra_sb) if vid else []
    cands = candidates(info, sb, extra)
    ads = advertisers(info.get("description"))
    subs = None
    mode = req.get("transcript") or "auto"
    # субтитры нужны, если таймкодов на рекламу нет или в описании есть рекламодатели:
    # тогда по ним видно, какой бренд где, и находятся те, кого нет в таймкодах
    prov = ai_provider((req or {}).get("ai"))
    if mode == "always" or (mode == "auto" and (prov or ads or not any(c["ad"] for c in cands))):
        jset(job, stage=f"читаю субтитры {note}")
        try:
            segs, subs = subtitle_segments(url, args, info)
            ai = None
            if segs:
                if prov:
                    jset(job, stage=f"{AI_NAMES[prov]} размечает рекламу {note}")
                try:
                    ai = ai_ads(segs, info.get("description"), info.get("title"), prov,
                                (req or {}).get("proxy") or "")
                except Exception as e:  # noqa: BLE001
                    jlog(job, f"{str(e)[:200]}; ищу рекламу по словам описания")
            if ai is not None:
                cands = add_ai(cands, ai, info.get("duration"), AI_NAMES[prov].lower())
                subs = f"{subs}, разметил {AI_NAMES[prov]}"
            elif segs:
                if ads:
                    # реклама из описания: ищем каждого рекламодателя по словам его блока
                    found = locate_advertisers(segs, ads)
                    found += [f for f in transcript_ads(segs, "", extra) if extra
                              and not any(min(f["end"], g["end"]) > max(f["start"], g["start"]) for g in found)]
                else:
                    found = transcript_ads(segs, info.get("description"), extra)
                cands = add_transcript(cands, found, info.get("duration"))
                label_brands(cands, segs, ads)
            else:
                subs = "нет"
        except Exception as e:  # noqa: BLE001
            subs = "ошибка"
            jlog(job, f"субтитры не прочитал: {str(e).splitlines()[0][:200]}")
    seen = {f(c.get("brand")) for c in cands if c["ad"] and c.get("brand")
            for f in (flat, lambda b: flat(_canon(b)[0]))}
    shown = lambda a: any(x and (x in flat(a["name"]) or flat(a["name"]) in x
                                 or flat(a.get("legal")) and flat(a.get("legal")) in x) for x in seen)
    return {
        "id": vid, "url": info.get("webpage_url") or url,
        "title": info.get("title") or vid,
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": info.get("duration") or 0,
        "hasChapters": bool(info.get("chapters")),
        "sbCount": len(sb),
        "subs": subs,
        "advertisers": [{"name": a["name"], "marks": a["marks"], "erid": len(a["erid"])} for a in ads],
        # в описании есть, а в ролике место не нашлось
        "missing": [a["name"] for a in ads if not shown(a)] if subs not in (None, "нет", "ошибка") else [],
        "items": cands,
    }


def scan_worker(job, req):
    urls = [u.strip() for u in (req.get("urls") or []) if u.strip()]
    extra = [w for w in re.split(r"[,\n;]+", req.get("keywords") or "") if w.strip()]
    extra_sb = req.get("sbCats") or SB_ADS
    args = auth_args(req)
    videos, errors = [], []
    for i, url in enumerate(urls):
        if job.get("cancelled"):
            break
        jset(job, pct=i / max(1, len(urls)) * 100, stage=f"читаю {i + 1} из {len(urls)}")
        try:
            v = scan_one(url, req, args, extra, extra_sb, job, f"{i + 1} из {len(urls)}")
            videos.append(v)
            jlog(job, f"{v['title'][:60]}: найдено {len(v['items'])}, "
                      f"похоже на рекламу {sum(1 for c in v['items'] if c['ad'])}"
                      + (f", в описании: {', '.join(a['name'] for a in v['advertisers'])}" if v["advertisers"] else ""))
        except Exception as e:  # noqa: BLE001
            errors.append({"url": url, "error": str(e)})
            jlog(job, f"ошибка на {url}: {e}")
    jset(job, state="done", pct=100.0, stage="готово", videos=videos, errors=errors)


def grab_one(item, args, height, job, work):
    """качает только нужный отрезок; если не вышло - целиком и режет ffmpeg."""
    a, b = float(item["start"]), float(item["end"])
    name = safe_name(f"{tag(a)} - {item.get('title','')} - {item.get('label') or 'фрагмент'}")
    folder = OUT_DIR / safe_name(item.get("channel") or "без канала")
    folder.mkdir(parents=True, exist_ok=True)
    out = unique(folder / f"{name}.mp4")
    fmt = f"bv*[height<=?{height}][ext=mp4]+ba[ext=m4a]/bv*[height<=?{height}]+ba/b[height<=?{height}]/b"
    try:
        run_stream([YTDLP, "--no-playlist", "--newline", "--no-warnings", *args, "-f", fmt,
                    "--download-sections", f"*{a:.2f}-{b:.2f}", "--force-keyframes-at-cuts",
                    "--merge-output-format", "mp4", "-o", str(out.with_suffix(".%(ext)s")),
                    item["url"]], job, span=b - a)
        got = next((p for p in folder.glob(out.stem + ".*")
                    if p.suffix.lower() in (".mp4", ".mkv", ".webm")), None)
        if got:
            return got
        raise RuntimeError("отрезок не сохранился")
    except Exception as e:  # noqa: BLE001
        jlog(job, f"отрезком не вышло ({e}), качаю ролик целиком")
        src = full_source(item["url"], item["id"], height, job, args)
        run_stream([FFMPEG, "-y", "-hide_banner", "-ss", f"{a:.3f}", "-i", str(src),
                    "-t", f"{b - a:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(out)], job, span=b - a)
        return out


def full_source(url, vid, h, job, args):
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted(SRC_DIR.glob(f"{vid}_{h}.*")):
        if p.suffix.lower() in (".mp4", ".mkv", ".webm"):
            return p
    fmt = f"bv*[height<=?{h}][ext=mp4]+ba[ext=m4a]/bv*[height<=?{h}]+ba/b[height<=?{h}]/b"
    run_stream([YTDLP, "--no-playlist", "--newline", "--no-warnings", *args, "-f", fmt,
                "--merge-output-format", "mp4",
                "-o", str(SRC_DIR / f"{vid}_{h}.%(ext)s"), url], job)
    for p in sorted(SRC_DIR.glob(f"{vid}_{h}.*")):
        if p.suffix.lower() in (".mp4", ".mkv", ".webm"):
            return p
    raise RuntimeError("исходник не скачался")


def probe_duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


MANIFEST_HEAD = ["дата", "канал", "ролик", "ссылка", "id", "начало", "конец",
                 "длина_с", "подпись", "источники", "файл", "бренд"]


def write_manifest(rows):
    new = not MANIFEST.exists()
    if not new:
        # старый манифест без колонки «бренд»: дописываем её в заголовок
        text = MANIFEST.read_text(encoding="utf-8-sig")
        head, _, rest = text.partition("\n")
        if head.rstrip("\r").split(";") == MANIFEST_HEAD[:-1]:
            MANIFEST.write_text(";".join(MANIFEST_HEAD) + "\n" + rest, encoding="utf-8-sig")
    with MANIFEST.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(MANIFEST_HEAD)
        w.writerows(rows)


def safe_name(s):
    s = re.sub(r"[/\\:\x00-\x1f]+", " ", s or "фрагмент")
    s = re.sub(r"\s{2,}", " ", s).strip(" .")
    return (s[:110] or "фрагмент")


def unique(path):
    if not path.exists():
        return path
    for i in range(2, 999):
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
    return path


def grab_worker(job, req):
    items = req.get("items") or []
    args = auth_args(req)
    height = int(req.get("height") or 1080)
    done, rows = [], []
    work = OUT_DIR / ".work" / job["id"]
    try:
        for i, item in enumerate(items):
            jset(job, pct=i / max(1, len(items)) * 100, slot=(i, len(items)), sub=None,
                 stage=f"фрагмент {i + 1} из {len(items)}")
            if job.get("cancelled"):
                break
            try:
                path = grab_one(item, args, height, job, work)
                got = probe_duration(path)
                done.append({"file": str(path), "ok": True, "seconds": round(got, 2),
                             "label": item.get("label"), "title": item.get("title"),
                             "start": item["start"], "end": item["end"]})
                rows.append([datetime.now().strftime("%Y-%m-%d %H:%M"),
                             item.get("channel", ""), item.get("title", ""), item["url"],
                             item.get("id", ""), hms(item["start"]), hms(item["end"]),
                             round(float(item["end"]) - float(item["start"]), 1),
                             item.get("label", ""), ",".join(item.get("src") or []),
                             str(path.relative_to(OUT_DIR)) if OUT_DIR in path.parents else str(path),
                             item.get("brand", "")])
            except Exception as e:  # noqa: BLE001
                done.append({"ok": False, "error": friendly(str(e)), "label": item.get("label"),
                             "title": item.get("title"), "start": item["start"], "end": item["end"]})
                jlog(job, f"не вышло: {e}")
        if rows:
            write_manifest(rows)
        jset(job, state="done", pct=100.0, stage="готово", results=done,
             manifest=str(MANIFEST) if rows else "")
    except Exception as e:  # noqa: BLE001
        jset(job, state="error", stage="ошибка", error=friendly(str(e)), results=done)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def platform(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return next((name for name, hosts in PLATFORMS if any(host == h or host.endswith("." + h) for h in hosts)), "")


def orientation(w, h):
    if not w or not h:
        return ""
    return "вертикальное" if h > w * 1.1 else "горизонтальное" if w > h * 1.1 else "квадратное"


def probe_size(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
        return w, h
    except ValueError:
        return 0, 0


def social_one(url, req, height, job, folder=""):
    """одно видео целиком в папку блогера."""
    plat = platform(url)
    args = auth_args(dict(req, proxy="") if plat in DIRECT else req)
    info = ytdlp_info(url, args)
    # имя, которое видно на странице: в TikTok channel - ник «GEO», uploader - логин
    blogger = (folder or info.get("channel") or info.get("uploader") or info.get("uploader_id")
               or plat or "без автора").strip()
    title = (info.get("title") or info.get("description") or info.get("id") or "видео").strip()
    title = re.sub(r"\s+", " ", title)[:90]
    day = info.get("upload_date") or ""
    day = f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else datetime.now().strftime("%Y-%m-%d")
    out = SOCIAL_DIR / safe_name(blogger)
    out.mkdir(parents=True, exist_ok=True)
    path = unique(out / f"{safe_name(f'{day} - {title}')}.mp4")
    with tempfile.NamedTemporaryFile("w", suffix=".info.json", delete=False) as f:
        json.dump(info, f)
        info_path = f.name
    try:
        # info уже есть: качаем по нему, без второго запроса к площадке
        run_stream([YTDLP, "--load-info-json", info_path, "--newline", "--no-warnings", *args,
                    "-S", f"res:{height},vcodec:h264,ext:mp4:m4a", "--merge-output-format", "mp4",
                    "-o", str(path)], job)
    finally:
        os.unlink(info_path)
    if not path.exists():
        cand = sorted(out.glob(path.stem + ".*"), key=lambda p: p.stat().st_mtime)
        if not cand:
            raise RuntimeError("файл не появился")
        path = cand[-1]
    w, h = probe_size(path)
    return {"ok": True, "url": url, "platform": plat, "blogger": blogger, "title": title,
            "file": str(path), "seconds": round(probe_duration(path), 1), "width": w, "height": h,
            "orientation": orientation(w, h)}


def social_worker(job, req):
    urls = [u.strip() for u in (req.get("urls") or []) if u.strip()]
    height = int(req.get("height") or 1080)
    folder = (req.get("folder") or "").strip()
    done, rows = [], []
    for i, url in enumerate(urls):
        if job.get("cancelled"):
            break
        jset(job, pct=i / max(1, len(urls)) * 100, slot=(i, len(urls)), sub=None,
             stage=f"видео {i + 1} из {len(urls)}")
        try:
            r = social_one(url, req, height, job, folder)
            done.append(r)
            rows.append([datetime.now().strftime("%Y-%m-%d %H:%M"), r["platform"], r["blogger"], r["title"],
                         url, r["seconds"], r["orientation"], f"{r['width']}x{r['height']}",
                         str(Path(r["file"]).relative_to(SOCIAL_DIR))])
            jlog(job, f"{r['platform']} · {r['blogger']} · {r['orientation']} → {r['file']}")
        except Exception as e:  # noqa: BLE001
            msg = friendly(str(e))
            if re.search(r"login|log in|cookies|sign in|private", msg, re.I) and platform(url) != "YouTube":
                msg += f"\n\n{platform(url) or 'Площадка'} пускает только залогиненных: вставь куки этого сайта в «Куки»."
            done.append({"ok": False, "url": url, "platform": platform(url), "error": msg})
            jlog(job, f"не вышло {url}: {str(e).splitlines()[0][:200]}")
    if rows:
        new = not SOCIAL_MANIFEST.exists()
        SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
        with SOCIAL_MANIFEST.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["дата", "площадка", "блогер", "название", "ссылка", "длина_с",
                            "ориентация", "разрешение", "файл"])
            w.writerows(rows)
    jset(job, state="done", pct=100.0, stage="готово", results=done,
         manifest=str(SOCIAL_MANIFEST) if rows else "", folder=str(SOCIAL_DIR))


def playwright_python():
    """питон, у которого есть Playwright: снимки постов делает Chromium."""
    cands = [str(WIB_HOME / "venv/bin/python3"), sys.executable, shutil.which("python3")]
    cands += sorted(str(p) for p in Path("/Library/Frameworks/Python.framework/Versions").glob("*/bin/python3"))
    cands += [str(p) for p in Path("/opt/homebrew/bin").glob("python3.*")]
    for c in dict.fromkeys(filter(None, cands)):
        if not Path(c).exists():
            continue
        r = subprocess.run([c, "-c", "import playwright"], capture_output=True)
        if r.returncode == 0:
            return c
    return None


def telegram_worker(job, req):
    posts = []
    for u in req.get("urls") or []:
        m = TG_RE.search(u.strip())
        if m:
            posts.append(f"{m.group(1)}/{m.group(2)}")
        elif u.strip():
            jlog(job, f"не похоже на пост Telegram: {u.strip()}")
    posts = list(dict.fromkeys(posts))
    if not posts:
        raise RuntimeError("Нужны ссылки на посты вида https://t.me/канал/123")
    py = playwright_python()
    if not py:
        raise RuntimeError("Для снимков постов нужен Playwright: pip3 install playwright && playwright install chromium")
    folder = (req.get("folder") or "").strip()
    work = TG_DIR / ".work"
    work.mkdir(parents=True, exist_ok=True)
    spec = {"posts": [{"post": p, "out": str(work / p.replace("/", "_"))} for p in posts]}
    proc = subprocess.Popen([py, str(ROOT / "tgshot.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    jset(job, proc=proc, stage=f"пост 1 из {len(posts)}")
    proc.stdin.write(json.dumps(spec))
    proc.stdin.close()
    done, rows = [], []
    for line in proc.stdout:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if "stage" in r:
            jset(job, stage=f"{r['post']}: {r['stage']}")
            jlog(job, f"{r['post']}: {r['stage']}")
            continue
        if r.get("ok"):
            ch_name = folder or r.get("channel") or r["post"].split("/")[0]
            day = (r.get("date") or "")[:10]
            out = TG_DIR / safe_name(ch_name)
            out.mkdir(parents=True, exist_ok=True)
            stem_ = f"{day} - {r['post'].replace('/', '_')}" if day else r["post"].replace("/", "_")
            dest = unique(out / f"{safe_name(stem_)}.jpg")
            shutil.move(r["file"], dest)
            r.update(file=str(dest), channel=ch_name, url=f"https://t.me/{r['post']}")
            rows.append([datetime.now().strftime("%Y-%m-%d %H:%M"), ch_name, r["url"], day, r["source"],
                         str(dest.relative_to(TG_DIR))])
            jlog(job, f"{r['post']} → {dest} ({r['source']})")
        else:
            r["url"] = f"https://t.me/{r['post']}"
            jlog(job, f"{r['post']}: {r.get('error')}")
        done.append(r)
        jset(job, pct=len(done) / len(posts) * 100, stage=f"пост {min(len(done) + 1, len(posts))} из {len(posts)}")
    proc.wait()
    jset(job, proc=None)
    if proc.returncode and not done:
        raise RuntimeError((proc.stderr.read() or "").strip()[-300:] or "снимки не получились")
    shutil.rmtree(work, ignore_errors=True)
    if rows:
        new = not TG_MANIFEST.exists()
        with TG_MANIFEST.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["дата", "канал", "ссылка", "дата_поста", "откуда", "файл"])
            w.writerows(rows)
    jset(job, state="done", pct=100.0, stage="готово", results=done,
         manifest=str(TG_MANIFEST) if rows else "")


# ---------- поиск выпусков с интеграциями по описаниям (YouTube Data API) ----------

KEY_FILE = ROOT / ".secrets" / "youtube_api_key.txt"  # ключ можно положить вручную или сохранить на странице
FIND_DIR = Path(os.environ.get("YTADS_FIND_OUT") or Path.home() / "Desktop" / "Поиск интеграций")
API = "https://www.googleapis.com/youtube/v3/"
SEP = r"[\s\-‐‑–—.]*"


def api_key():
    k = os.environ.get("YTADS_YT_KEY", "").strip()
    if not k and KEY_FILE.exists():
        k = KEY_FILE.read_text().strip()
    return k


_tl = threading.local()


def yt_api(method, **params):
    """запрос к YouTube Data API; ключ в заголовке, чтобы не светился в адресах."""
    key = api_key()
    if not key:
        raise RuntimeError("Нет ключа YouTube API. Добавь его во вкладке «Старт»")
    url = API + method + "?" + urllib.parse.urlencode(params)
    code, d = curl_json(url, {"X-goog-api-key": key}, proxy=getattr(_tl, "proxy", ""), timeout=30)
    if code == 200:
        return d
    err = d.get("error", {}) if isinstance(d, dict) else {}
    reason = ((err.get("errors") or [{}])[0]).get("reason", "")
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        raise RuntimeError("Дневной лимит YouTube API исчерпан, он обновится в 10:00 по Москве")
    if code in (400, 403) and "key" in (err.get("message") or "").lower():
        raise RuntimeError("YouTube API не принял ключ. Проверь его во вкладке «Старт»")
    raise RuntimeError(f"YouTube API: {err.get('message') or code}")


def resolve_channel(q):
    """ссылка, @handle, ссылка на ролик или просто имя -> (id, название, uploads, другие варианты)."""
    q = q.strip()
    others = []
    m = re.search(r"/channel/(UC[\w-]{22})", q)
    vid = video_id(q) if re.search(r"youtu", q) else None
    if m:
        d = yt_api("channels", part="snippet,contentDetails", id=m.group(1))
    elif vid:
        v = yt_api("videos", part="snippet", id=vid).get("items") or []
        if not v:
            raise RuntimeError("ролик не найден")
        d = yt_api("channels", part="snippet,contentDetails", id=v[0]["snippet"]["channelId"])
    elif re.match(r"@[\w.\-]+$", q) or re.search(r"youtube\.com/@", q):
        handle = "@" + re.search(r"@([\w.\-]+)", q).group(1)
        d = yt_api("channels", part="snippet,contentDetails", forHandle=handle)
    elif re.search(r"youtube\.com/user/([\w.\-]+)", q):
        d = yt_api("channels", part="snippet,contentDetails",
                   forUsername=re.search(r"/user/([\w.\-]+)", q).group(1))
    else:
        name = re.sub(r"^https?://(www\.)?youtube\.com/c/", "", q)
        s = yt_api("search", part="snippet", type="channel", q=name, maxResults=5).get("items") or []
        if not s:
            raise RuntimeError("канал не найден")
        ids = [i["snippet"]["channelId"] for i in s]
        others = [i["snippet"]["title"] for i in s[1:]]
        d = yt_api("channels", part="snippet,contentDetails", id=ids[0])
    items = d.get("items") or []
    if not items:
        raise RuntimeError("канал не найден")
    c = items[0]
    return c["id"], c["snippet"]["title"], c["contentDetails"]["relatedPlaylists"]["uploads"], others


def _word_rx(w):
    """слово с любым окончанием: «хоккейный» -> «хоккейн\\w*»."""
    w = re.escape(w)
    if len(w) >= 6 and re.match(r"[а-яё]", w):
        w = w[:len(w) - 2]
    elif len(w) >= 5 and re.match(r"[а-яё]", w):
        w = w[:len(w) - 1]
    return w + r"\w*"


def keyword_rx(kw):
    """ключевое слово и его формы: «Т-Банк» -> т-банк, тбанк, т банка, тинькофф, tbank.ru."""
    kw = kw.strip().lower().replace("ё", "е")
    forms = {kw}
    display, spoken = _canon(kw)
    if display != kw.strip():
        forms.update(spoken)
        forms.update(k for k, (d, _) in BRAND_ALIAS.items() if d == display)
    parts = []
    for f in forms:
        words = re.findall(r"\w+", f)
        if not words:
            continue
        rx = SEP.join(_word_rx(w) for w in words)
        parts.append(rf"(?<!\w){rx}" if len(f) > 3 else rf"(?<!\w){rx}(?!\w)")
    return re.compile("|".join(sorted(parts, key=len, reverse=True)), re.I)


def match_video(title, desc, kws):
    """что нашлось в ролике и почему это похоже на интеграцию."""
    text = (desc or "").replace("ё", "е").replace("Ё", "Е")
    lines = text.splitlines()
    found, near, snippet, in_title = [], set(), "", False
    for kw, rx in kws:
        hit_title = bool(rx.search(title or ""))
        hit_lines = [i for i, l in enumerate(lines) if rx.search(l)]
        if not hit_title and not hit_lines:
            continue
        found.append(kw)
        in_title |= hit_title
        for i in hit_lines:
            near.update(range(max(0, i - 1), min(len(lines), i + 4)))
            snippet = snippet or lines[i].strip()
    if not found:
        return None
    why = list(found)
    # рекламный блок целиком: абзац с текстом плюс его маркировка, даже если она в конце описания
    try:
        ads = [a for a in advertisers(desc) if any(rx.search(a["text"].replace("ё", "е")) or rx.search(a["name"])
                                                   for _, rx in kws)]
    except Exception:  # noqa: BLE001
        ads = []
    zone = "\n".join(a["text"] for a in ads) or "\n".join(lines[i] for i in sorted(near))
    if any(a.get("erid") for a in ads):
        zone += "\nerid"
    links = [h for h, p in URL_RE.findall(zone) if h.lower().removeprefix("www.") not in SOCIAL_HOSTS]
    if links:
        why.append("ссылка на продукт")
    if PROMO_RE.search(zone):
        why.append("промокод" if re.search(r"промокод|promo ?code", zone, re.I) else "скидка или кешбэк")
    if re.search(r"\berid\b", zone, re.I):
        why.append("erid")
    elif re.search(r"реклама[.:]|рекламодатель|\bинн\b", zone, re.I):
        why.append("маркировка «Реклама»")
    elif re.search(r"\berid\b", text, re.I):
        why.append("erid в описании")
    if in_title:
        why.append("в названии")
    ad = bool(links) or any(w in why for w in ("erid", "промокод", "скидка или кешбэк", "маркировка «Реклама»"))
    if not ad:
        why.append("только упоминание")
    score = len(found) * 10 + 4 * ("erid" in why) + 2 * bool(links) + ("промокод" in why)
    return {"why": why, "score": score, "snippet": snippet[:220], "all": len(found) == len(kws), "ad": ad}


def find_worker(job, req):
    bloggers = [b.strip() for b in (req.get("bloggers") or []) if b.strip()]
    kws_raw = [k.strip() for k in re.split(r"[,;\n]", req.get("keywords") or "") if k.strip()]
    if not bloggers:
        raise RuntimeError("Напиши блогеров, каждого с новой строки: имя, @handle или ссылку на канал")
    if not kws_raw:
        raise RuntimeError("Напиши, что искать в описании, через запятую. Например: Т-Банк, Хоккейный вклад")
    _tl.proxy = (req.get("proxy") or "").strip()
    kws = [(k, keyword_rx(k)) for k in kws_raw]
    since = (req.get("since") or "").strip()[:10]
    limit = int(req.get("limit") or 3000)
    found, errors, channels = [], [], []
    for bi, b in enumerate(bloggers):
        if job.get("cancelled"):
            break
        jset(job, pct=bi / len(bloggers) * 100, stage=f"{b}: ищу канал")
        try:
            cid, title, uploads, others = resolve_channel(b)
        except Exception as e:  # noqa: BLE001
            errors.append({"blogger": b, "error": str(e)})
            jlog(job, f"{b}: {e}")
            continue
        jlog(job, f"{b} → канал «{title}»" + (f" (другие похожие: {', '.join(others[:3])})" if others else ""))
        seen, hits, token = 0, 0, None
        while seen < limit and not job.get("cancelled"):
            page = yt_api("playlistItems", part="snippet,contentDetails", playlistId=uploads, maxResults=50,
                          **({"pageToken": token} if token else {}))
            stop = False
            for it in page.get("items", []):
                s, cd = it["snippet"], it.get("contentDetails", {})
                day = (cd.get("videoPublishedAt") or s.get("publishedAt") or "")[:10]
                if since and day and day < since:
                    stop = True
                    break
                seen += 1
                if s.get("title") in ("Private video", "Deleted video"):
                    continue
                m = match_video(s.get("title", ""), s.get("description", ""), kws)
                if m:
                    hits += 1
                    vid = cd.get("videoId") or s["resourceId"]["videoId"]
                    found.append({"blogger": title, "query": b, "title": s.get("title", ""), "date": day,
                                  "url": f"https://www.youtube.com/watch?v={vid}", **m})
            jset(job, stage=f"{title}: проверено {seen} роликов, найдено {hits}",
                 pct=(bi + min(seen / max(limit, 1), 0.95)) / len(bloggers) * 100)
            token = page.get("nextPageToken")
            if stop or not token:
                break
        channels.append({"query": b, "title": title, "id": cid, "seen": seen, "hits": hits, "others": others[:3]})
        jlog(job, f"«{title}»: проверено {seen}, подходят {hits}")
    # сначала ролики, где нашлись все слова; внутри блогера свежие сверху
    found.sort(key=lambda r: r["date"], reverse=True)
    found.sort(key=lambda r: (not r["ad"], not r["all"], bloggers.index(r["query"])))
    manifest = ""
    if found:
        FIND_DIR.mkdir(parents=True, exist_ok=True)
        name = safe_name(f"{datetime.now():%Y-%m-%d %H-%M} - {', '.join(kws_raw)}")[:90]
        manifest = str(unique(FIND_DIR / f"{name}.csv"))
        with open(manifest, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["блогер | ролик", "ссылка", "идентификация", "дата"])
            for r in found:
                w.writerow([f"{r['blogger']} | {r['title']}", r["url"], ", ".join(r["why"]), r["date"]])
    jset(job, state="done", pct=100.0, stage="готово", results=found, errors=errors, channels=channels,
         keywords=kws_raw, manifest=manifest)


def doctor():
    """что готово и чего не хватает; тексты и подсказки собирает страница."""
    ver = ""
    if not any(m == "yt-dlp" for m in MISSING):
        r = subprocess.run([YTDLP, "--version"], capture_output=True, text=True)
        ver = (r.stdout or "").strip()
    return {"missing": MISSING, "ytdlp": ver, "ai": ai_provider() or "", "claude": bool(CLAUDE),
            "openai": bool(openai_key()), "cookies": bool(detect_cookie_file()),
            "proxy": detect_proxy(), "ytKey": bool(api_key()),
            "telegram": bool(playwright_python()), "out": str(OUT_DIR)}


def net_probe(proxy=""):
    """открывается ли YouTube: напрямую и, если задан, через прокси."""
    def one(px):
        cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8"]
        if px:
            cmd += ["-x", px.replace("socks5://", "socks5h://")]
        t = time.time()
        r = subprocess.run(cmd + ["https://www.youtube.com/generate_204"], capture_output=True, text=True)
        return {"ok": (r.stdout or "").strip() in ("204", "200"), "ms": int((time.time() - t) * 1000)}
    return {"direct": one(""), "proxy": one(proxy) if proxy else None}


def save_secret(name, key, proxy=""):
    """проверяем ключ у сервиса и только потом кладём в .secrets (папка 700, файл 600)."""
    key = (key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{20,300}", key):
        raise ValueError("Это не похоже на ключ. Скопируй его целиком, без пробелов")
    try:
        if name == "openai":
            code, _ = curl_json(OPENAI_BASE + "/models", {"Authorization": "Bearer " + key}, proxy=proxy, timeout=30)
            if code in (401, 403):
                raise ValueError("OpenAI не принял этот ключ. Проверь, что скопировал его целиком")
            if code != 200:
                raise ValueError(f"OpenAI ответил ошибкой {code}. Попробуй ещё раз позже")
            fname = "openai_api_key.txt"
        elif name == "youtube":
            code, _ = curl_json(API + "channels?part=id&forHandle=@YouTube", {"X-goog-api-key": key},
                                proxy=proxy, timeout=30)
            if code != 200:
                raise ValueError("YouTube не принял ключ. Проверь, что включил «YouTube Data API v3» "
                                 "и скопировал ключ целиком")
            fname = "youtube_api_key.txt"
        else:
            raise ValueError("Неизвестный ключ")
    except RuntimeError as e:
        raise ValueError(f"Не достучался до сервиса ({e}). Если он закрыт, включи VPN для всего компьютера "
                         "или впиши прокси в «Настройках»") from None
    write_secret(fname, key)
    _openai_model.clear()
    return {"ok": True}


def test_download(req):
    """один известный ролик: проверяем, что yt-dlp, сеть и куки работают вместе."""
    info = ytdlp_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", auth_args(req))
    return {"ok": True, "title": info.get("title"), "seconds": info.get("duration")}


def detect_cookie_file():
    """свежий экспорт cookies.txt в обычных местах."""
    pats = ["*cookie*.txt", "*cookies*.txt"]
    found = []
    for d in (Path.home() / "Downloads", Path.home() / "Desktop", ROOT):
        if not d.exists():
            continue
        for pat in pats:
            found += [f for f in d.glob(pat) if f.is_file() and f.stat().st_size > 50]
    if not found:
        return ""
    best = max(set(found), key=lambda f: f.stat().st_mtime)
    return str(best)


LOGIN_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "SAPISID"}


COOKIE_SITES = {"youtube.com": ("YouTube", LOGIN_COOKIES), "instagram.com": ("Instagram", {"sessionid"}),
                "tiktok.com": ("TikTok", {"sessionid", "sid_tt"}), "vk.com": ("VK", {"remixsid", "remixnsid"}),
                "vkvideo.ru": ("VK", set()), "rutube.ru": ("Rutube", set())}


def save_cookies(text):
    """куки, вставленные на странице, дописываются в cookies.txt рядом с сервером.
    Один файл на все сайты: новые строки сайта заменяют его старые, чужие остаются."""
    rows = [l for l in text.replace("\r", "").split("\n")
            if l.strip() and (not l.startswith("#") or l.startswith("#HttpOnly_"))]
    cells = [r.split("\t") for r in rows]
    site = lambda c: next((d for d in COOKIE_SITES if len(c) >= 7 and c[0].removeprefix("#HttpOnly_").lstrip(".").endswith(d)), None)
    got = {}
    for c in cells:
        d = site(c)
        if d:
            got.setdefault(d, []).append(c)
    if not got:
        raise ValueError("Это не похоже на куки. Нужен текст, который расширение «Get cookies.txt LOCALLY» "
                         "копирует на вкладке youtube.com, instagram.com, tiktok.com или vk.com")
    for d, cs in got.items():
        name, login = COOKIE_SITES[d]
        if login and not any(c[5] in login for c in cs):
            raise ValueError(f"В куках {name} нет входа в аккаунт. Войди на {d} и выгрузи их заново")
    path = ROOT / "cookies.txt"
    keep = []
    if path.exists():
        for l in path.read_text(errors="replace").splitlines():
            c = l.split("\t")
            if l.strip() and not l.startswith("# ") and len(c) >= 7 and site(c) not in got:
                keep.append(l)
    new = ["\t".join(c) for cs in got.values() for c in cs]
    body = "# Netscape HTTP Cookie File\n" + "\n".join(keep + new) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    return {"ok": True, "file": str(path), "count": len(new),
            "sites": sorted({COOKIE_SITES[d][0] for d in got})}


def detect_proxy():
    for p in PROXY_PORTS:
        s = socket.socket()
        s.settimeout(0.25)
        try:
            s.connect(("127.0.0.1", p))
            return f"socks5://127.0.0.1:{p}"
        except OSError:
            continue
        finally:
            s.close()
    return ""


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "ytads"

    def log_message(self, *a):
        pass

    def _origin_ok(self):
        """свой адрес или запрос без Origin (curl, адресная строка)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in WEB_ORIGINS or urllib.parse.urlsplit(origin).hostname in LOCAL_HOSTS

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin in WEB_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def do_OPTIONS(self):
        self.send_response(204 if self.headers.get("Origin") in WEB_ORIGINS else 403)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {"error": "чужой сайт"})
        path = self.path.partition("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/env":
            arc = Path.home() / "Library/Application Support/Arc/User Data/Default"
            return self._send(200, {"pot": POT_SCRIPT.exists(), "claude": bool(CLAUDE), "ai": ai_provider() or "", "out": str(OUT_DIR),
                                    "socialOut": str(SOCIAL_DIR), "ytKey": bool(api_key()),
                                    "proxy": detect_proxy(), "arc": arc.exists(),
                                    "cookieFile": detect_cookie_file(),
                                    "sbLabels": SB_LABELS})
        if re.fullmatch(r"/icons/[a-z]+\.png", path) and (ROOT / path.lstrip("/")).exists():
            return self._send(200, (ROOT / path.lstrip("/")).read_bytes(), "image/png")
        if path == "/api/image":
            # превью снимков постов: только jpg из папки Telegram
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            f = Path((q.get("path") or [""])[0]).resolve()
            if f.suffix.lower() == ".jpg" and TG_DIR.resolve() in f.parents and f.exists():
                return self._send(200, f.read_bytes(), "image/jpeg")
            return self._send(404, {"error": "нет файла"})
        if path == "/api/doctor":
            return self._send(200, doctor())
        if path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._send(404, {"error": "нет такой задачи"})
            with LOCK:
                return self._send(200, {k: v for k, v in job.items() if k != "proc"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        # иначе любой открытый сайт мог бы запускать загрузки простым POST
        if not self._origin_ok():
            return self._send(403, {"error": "чужой сайт"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "плохой JSON"})
        path = self.path.partition("?")[0]
        if path in ("/api/scan", "/api/grab", "/api/social", "/api/telegram", "/api/find"):
            kind = path.rsplit("/", 1)[-1]
            with LOCK:
                busy = next((j for j in JOBS.values()
                             if j.get("kind") == kind and j.get("state") == "running"), None)
            if busy:
                return self._send(200, {"job": busy["id"], "attached": True})
            job = {"id": uuid.uuid4().hex[:12], "kind": kind, "state": "running",
                   "stage": "начинаю", "pct": 0.0, "log": [], "proc": None}
            JOBS[job["id"]] = job
            fn = {"scan": scan_worker, "grab": grab_worker, "social": social_worker,
                  "telegram": telegram_worker, "find": find_worker}[kind]
            threading.Thread(target=_guard(fn), args=(job, req), daemon=True).start()
            return self._send(200, {"job": job["id"]})
        if path == "/api/netcheck":
            return self._send(200, net_probe((req.get("proxy") or "").strip()))
        if path == "/api/test":
            try:
                return self._send(200, test_download(req))
            except Exception as e:  # noqa: BLE001
                msg = re.sub(r"^ERROR:\s*(\[\w+\]\s*\S+:\s*)?", "", str(e).strip().splitlines()[-1])[:300]
                return self._send(400, {"error": friendly(msg)})
        if path == "/api/secret":
            try:
                return self._send(200, save_secret(req.get("name"), req.get("key"), (req.get("proxy") or "").strip()))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if path == "/api/cancel":
            job = JOBS.get(req.get("job"))
            if job:
                jset(job, cancelled=True)
                if job.get("proc"):
                    job["proc"].terminate()
            return self._send(200, {"ok": True})
        if path == "/api/cookies":
            try:
                return self._send(200, save_cookies(req.get("text") or ""))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if path == "/api/reveal":
            p = Path(req.get("path") or "")
            if p.exists():
                subprocess.run(["open", "-R", str(p)], check=False)
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "файл не найден"})
        return self._send(404, {"error": "not found"})


def _guard(fn):
    def inner(job, req):
        try:
            fn(job, req)
        except Exception as e:  # noqa: BLE001
            jset(job, state="error", stage="ошибка", error=friendly(str(e)))
    return inner


def selftest():
    info = {
        "id": "abcdefghijk", "title": "Выпуск про всё", "channel": "Канал",
        "duration": 3600,
        "chapters": [
            {"start_time": 0, "end_time": 120, "title": "Вступление"},
            {"start_time": 120, "end_time": 300, "title": "Реклама"},
            {"start_time": 300, "end_time": 1800, "title": "Основная часть"},
            {"start_time": 1800, "end_time": 1950, "title": "Промокод от партнёра"},
            {"start_time": 1950, "end_time": 3600, "title": "Это не реклама, а мнение"},
        ],
        "description": "Таймкоды:\n0:00 Вступление\n2:00 - Реклама\n"
                       "5:00 Основная часть\n30:00 — Промокод от партнёра\n"
                       "32:30 Это не реклама, а мнение\n55:00 Интеграция со спонсором\n",
    }
    sb = [{"start": 118.0, "end": 305.0, "category": "sponsor"}]
    res = candidates(info, sb)
    print(f"{'старт':>8} {'конец':>8} {'реклама':>8}  источники / подпись")
    for c in res:
        print(f"{hms(c['start']):>8} {hms(c['end']):>8} {'да' if c['ad'] else 'нет':>8}  "
              f"{','.join(c['src']):<24} {c['label']}")
    ads = [c for c in res if c["ad"]]
    assert len(res) == 6, f"ожидал 6 фрагментов, получил {len(res)}"
    assert len(ads) == 3, f"ожидал 3 рекламных, получил {len(ads)}"
    assert ads[0]["start"] == 118.0 and ads[0]["end"] == 305.0, "границы SponsorBlock не применились"
    assert "глава" in ads[0]["src"] and "sponsorblock" in ads[0]["src"], "источники не слились"
    assert not any("не реклама" in c["label"].lower() and c["ad"] for c in res), "отрицание не учтено"
    assert ads[2]["start"] == 3300, "реклама внутри длинной главы потерялась"
    lines = ["Сегодня поговорим про горы", "и про то, как туда добраться",
             "Друзья, ненадолго прервёмся.", "Хорошие новости от Т-Банка:",
             "обновилось приложение, кэшбэк до 30%", "переводы стали быстрее",
             "Обновляйте приложение Т-Банка по ссылке", "в описании или по QR-коду.",
             "А мы возвращаемся к выпуску.", "Итак, горы"]
    segs = [[200 + i * 5, 205 + i * 5, t] for i, t in enumerate(lines)]
    lines2 = ["что-то про горы"] * 40
    segs += [[300 + i * 5, 305 + i * 5, t] for i, t in enumerate(lines2)]
    found = transcript_ads(segs, "Реклама. АО «ТБанк», лицензия №2673. erid: 2Vtzqx")
    print(f"\nпо субтитрам: {[(hms(f['start']), hms(f['end']), f['why']) for f in found]}")
    assert len(found) == 1, "реклама в субтитрах не нашлась"
    assert 205 <= found[0]["start"] <= 212 and 240 <= found[0]["end"] <= 248, "границы по субтитрам съехали"
    desc = "WB Ресейл. Продавайте без комиссии. Покупайте безопасно! https://linkwb.ru/x\nErid: 2Vtzqv6gMGh"
    talk = ["про отношения"] * 30 + ["[музыка]", "Так не выбрасывай, продай.", "На VBALE. Это сервис",
                                     "продаёшь его без комиссии", "покупать там тоже можно?",
                                     "главное, всё безопасно.", "Вбри. Продавай без комиссии,",
                                     "покупай безопасно.", "[музыка]"] + ["снова про отношения"] * 30
    segs = [[i * 3, i * 3 + 3, t] for i, t in enumerate(talk)]
    found = locate_advertisers(segs, advertisers(desc))
    print(f"по описанию: {[(hms(f['start']), hms(f['end']), f['brand'], f['why']) for f in found]}")
    assert len(found) == 1 and found[0]["brand"] == "Wildberries", "реклама из описания не нашлась"
    assert 90 <= found[0]["start"] <= 94 and 112 <= found[0]["end"] <= 117, "края рекламы из описания съехали"
    print("\nselftest пройден")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    port = PORT
    for _ in range(20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("не нашёл свободный порт")
    url = f"http://wibuploads.localhost:{port}/"
    print(f"ytads работает: {url}\n       резервный адрес: http://127.0.0.1:{port}/"
          f"\nфрагменты: {OUT_DIR}\nCtrl+C — остановить")
    if MISSING:
        print(f"Не нашёл: {', '.join(MISSING)}. Дважды кликни «Установить.command» или выполни: brew install yt-dlp ffmpeg")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: __import__("webbrowser").open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")


if __name__ == "__main__":
    main()
