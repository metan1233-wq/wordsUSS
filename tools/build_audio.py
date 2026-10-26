"""Готовит озвучку для words.txt.

Для каждого слова:
  * живая запись диктора из открытого словаря (dictionaryapi.dev, записи Wiktionary), если она есть;
  * нейроголос Microsoft (edge-tts) — для слова и для примера;
  * русский нейроголос — для перевода (тренировка «Перевод на слух»).
Файлы складываются в audio/, а audio/index.json говорит приложению, что где лежит.
Уже готовые файлы повторно не создаются, лишние удаляются.

Запуск вручную:  pip install edge-tts  &&  python tools/build_audio.py
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

VOICE = "en-US-AvaMultilingualNeural"   # другой голос: en-US-AndrewMultilingualNeural, en-US-EmmaMultilingualNeural, en-GB-SoniaNeural
ACCENT = "us"                            # какую живую запись брать первой: us / uk
RATE = "-5%"                             # чуть медленнее обычного темпа
VOICE_RU = "ru-RU-SvetlanaNeural"        # голос для переводов: ru-RU-DmitryNeural — мужской

ROOT = Path(__file__).resolve().parent.parent
WORDS = ROOT / "words.txt"
AUDIO = ROOT / "audio"
INDEX = AUDIO / "index.json"
UA = {"User-Agent": "ty-geniy-words/1.1 (personal vocabulary app; https://github.com/metan1233-wq/wordsuss)"}
LOG = []


def log(msg):
    print(msg)
    LOG.append(msg)


def parse_words(text):
    """Те же правила, что в приложении: слово ; перевод ; пример, # тема, // комментарий."""
    out = []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        if ";" in line:
            parts = line.split(";")
        elif "\t" in line:
            parts = line.split("\t")
        else:
            parts = re.split(r"\s+[-–—]\s+", line)
        parts = [p.strip() for p in parts]
        en = parts[0] if parts else ""
        rest = [p for p in parts[1:] if p]
        ex = ""
        if len(rest) >= 2 and re.search(r"[A-Za-z]", rest[-1]) and not re.search(r"[А-Яа-яЁё]", rest[-1]):
            ex = rest.pop()  # пример всегда последний, значения перевода могут идти через «;»
        ru = "; ".join(rest)
        if en and ru:
            out.append({"key": re.sub(r"\s+", " ", en.lower()), "en": en, "ru": ru, "ex": ex})
    return out


def slug(text, salt=""):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "w"
    return f"{s}-{hashlib.sha1((text + salt).encode('utf-8')).hexdigest()[:6]}"


def dict_term(en):
    t = re.sub(r"^to\s+", "", en.split("/")[0].strip(), flags=re.I)
    return t if re.fullmatch(r"[A-Za-z][A-Za-z'-]*", t) else None


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def fetch_bytes(url):
    if url.startswith("//"):
        url = "https:" + url
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == 2:
                raise
            time.sleep(3 + attempt * 5)


def to_mp3(data, dest):
    """Любой формат (ogg, wav, mp3) -> mp3: моно, обрезанная тишина, выровненная громкость."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
    try:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", tmp.name,
                        "-af", "silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.15,areverse,"
                               "silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.2,areverse,"
                               "adelay=120:all=1,apad=pad_dur=0.25,loudnorm=I=-16:TP=-1.5",
                        "-ac", "1", "-ar", "44100", "-b:a", "96k", str(dest)], check=True)
    finally:
        os.unlink(tmp.name)
    if dest.stat().st_size < 1500:
        dest.unlink()
        raise ValueError("пустая запись")


def wiktionary_audio(term):
    """Файлы произношения из Викисловаря: сначала американские, потом остальные."""
    url = ("https://en.wiktionary.org/w/api.php?action=parse&prop=wikitext&format=json&formatversion=2&page="
           + urllib.parse.quote(term))
    data = json.loads(fetch_bytes(url))
    text = data.get("parse", {}).get("wikitext", "")
    m = re.search(r"==English==(.*?)(\n==[^=]|$)", text, re.S)
    text = m.group(1) if m else ""
    files = re.findall(r"\{\{audio\|en\|([^|}]+)", text)
    us = [f for f in files if re.search(r"(^|[-_ ])(us|en-us|ga)([-_ .]|$)", f, re.I) or f.lower().startswith("en-us")]
    uk = [f for f in files if f.lower().startswith(("en-uk", "en-gb")) or re.search(r"(^|[-_ ])(uk|rp)([-_ .]|$)", f, re.I)]
    order = (us + uk) if ACCENT == "us" else (uk + us)
    order += [f for f in files if f not in order]
    ipa = ""
    for line in text.splitlines():
        if "{{IPA|en|" in line and (ACCENT == "us" and re.search(r"GA|US", line) or not ipa):
            mm = re.search(r"\{\{IPA\|en\|([^|}]+)", line)
            if mm:
                ipa = mm.group(1).strip()
    return [f.strip() for f in order], ipa


def human_audio(term, dest, api_url):
    files, ipa = [], ""
    try:
        files, ipa = wiktionary_audio(term)
    except Exception as e:
        log(f"  ! Викисловарь «{term}»: {e}")
        return None, "", False
    for f in files[:3]:
        try:
            to_mp3(fetch_bytes("https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(f.replace(" ", "_"))), dest)
            return "wiktionary:" + f, ipa, True
        except Exception as e:
            log(f"  ! запись {f}: {e}")
    if api_url:
        try:
            to_mp3(fetch_bytes(api_url), dest)
            return "dictionaryapi", ipa, True
        except Exception as e:
            log(f"  ! запись dictionaryapi «{term}»: {e}")
    return None, ipa, True


def dictionary_info(term):
    """Транскрипция и ссылка на живую запись (сначала нужный акцент)."""
    try:
        data = fetch_json("https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(term.lower()))
    except Exception as e:
        log(f"  ! dictionaryapi «{term}»: {e}")
        data = []
    ipa, audio = {}, {}
    for entry in data if isinstance(data, list) else []:
        for ph in entry.get("phonetics", []):
            a = ph.get("audio") or ""
            acc = "uk" if "-uk." in a else "us" if "-us." in a else "au" if "-au." in a else ""
            if a and acc and acc not in audio:
                audio[acc] = a
            if a and "any" not in audio:
                audio["any"] = a
            if ph.get("text"):
                if acc and acc not in ipa:
                    ipa[acc] = ph["text"]
                ipa.setdefault("any", ph["text"])
        if entry.get("phonetic"):
            ipa.setdefault("any", entry["phonetic"])
    other = "uk" if ACCENT == "us" else "us"
    return (ipa.get(ACCENT) or ipa.get("any") or ipa.get(other) or "",
            audio.get(ACCENT) or audio.get(other) or audio.get("any") or "")


def ru_speech(ru):
    """Перевод для озвучки: без пояснений в скобках и помет."""
    t = re.sub(r"\([^)]*\)", " ", ru)
    t = re.sub(r"\b(разг|сленг|устар)\.", " ", t)
    t = t.replace(";", ",").replace("/", ",")
    t = re.sub(r"\s*,\s*(,\s*)*", ", ", t)
    return re.sub(r"\s+", " ", t).strip(" ,.—-")


async def tts(text, dest, sem, voice=VOICE):
    import edge_tts
    async with sem:
        for attempt in range(6):
            try:
                await edge_tts.Communicate(text, voice, rate=RATE if voice == VOICE else "+0%").save(str(dest))
                if dest.stat().st_size > 800:
                    return True
            except Exception as e:  # сеть или сервис — пробуем ещё
                log(f"  ! нейроголос «{text[:40]}»: {e}")
            await asyncio.sleep(3 + attempt * 4)  # сервис иногда отвечает пустым звуком, ждём и пробуем снова
        dest.unlink(missing_ok=True)
        return False


async def main():
    words = parse_words(WORDS.read_text(encoding="utf-8"))
    for sub in ("w", "h", "e", "r"):
        (AUDIO / sub).mkdir(parents=True, exist_ok=True)
    old = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    old_words = old.get("words", {}) if old.get("voice") == VOICE else {}
    index = {"voice": VOICE, "words": {}}
    sem = asyncio.Semaphore(5)
    jobs = []
    made = {"w": 0, "h": 0, "e": 0, "r": 0}
    lookups = []

    for w in words:
        key, prev = w["key"], old_words.get(w["key"], {})
        rec = {}
        # нейроголос для слова
        wf = f"w/{slug(w['en'], VOICE)}.mp3"
        rec["w"] = wf
        if not (AUDIO / wf).exists():
            jobs.append((w["en"], AUDIO / wf, "w", VOICE))
        # живая запись и транскрипция — спрашиваем словарь один раз
        if prev.get("dict") == 4:
            rec["ipa"], rec["dict"] = prev.get("ipa", ""), prev["dict"]
            if prev.get("h") and (AUDIO / prev["h"]).exists():
                rec["h"] = prev["h"]
        else:
            rec["ipa"], rec["dict"] = "", 4
            term = dict_term(w["en"])
            if term:
                lookups.append((term, rec))
        # нейроголос для примера
        if w["ex"]:
            ef = f"e/{slug(w['ex'], VOICE)}.mp3"
            rec["e"] = ef
            if not (AUDIO / ef).exists():
                jobs.append((w["ex"], AUDIO / ef, "e", VOICE))
        # русский нейроголос для перевода
        ru_text = ru_speech(w["ru"])
        if ru_text:
            rf = f"r/{slug(ru_text, VOICE_RU)}.mp3"
            rec["r"] = rf
            if not (AUDIO / rf).exists():
                jobs.append((ru_text, AUDIO / rf, "r", VOICE_RU))
        index["words"][key] = rec

    # живые записи и транскрипция: несколько запросов параллельно, чтобы большой список не шёл часами
    def lookup(item):
        term, rec = item
        ipa, url = dictionary_info(term)
        hf = f"h/{slug(term.lower())}.mp3"
        src, wipa, checked = human_audio(term, AUDIO / hf, url)
        rec["ipa"] = wipa if wipa.startswith("/") else (ipa or wipa)
        if not checked:
            rec.pop("dict", None)
        if src:
            rec["h"], rec["src"] = hf, src
        else:
            log(f"  - живой записи нет: {term}")
        time.sleep(0.3)
        return bool(src)
    if lookups:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            made["h"] += sum(pool.map(lookup, lookups))

    results = await asyncio.gather(*(tts(t, d, sem, v) for t, d, _, v in jobs))
    for (_, dest, kind, _v), ok in zip(jobs, results):
        if ok:
            made[kind] += 1
        else:  # не получилось — приложение озвучит голосом устройства
            for rec in index["words"].values():
                if rec.get(kind) == str(dest.relative_to(AUDIO)).replace("\\", "/"):
                    rec.pop(kind, None)

    used = {f for rec in index["words"].values() for k, f in rec.items() if k in ("w", "h", "e", "r")}
    removed = 0
    for sub in ("w", "h", "e", "r"):
        for f in (AUDIO / sub).glob("*.mp3"):
            if f"{sub}/{f.name}" not in used:
                f.unlink()
                removed += 1

    INDEX.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    log(f"Слов: {len(words)}. Новых файлов: нейроголос слов {made['w']}, примеров {made['e']}, "
          f"переводов {made['r']}, живых записей {made['h']}. Удалено лишних: {removed}.")

    (AUDIO / "build-log.txt").write_text("\n".join(LOG) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
