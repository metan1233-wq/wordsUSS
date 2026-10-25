"""Готовит озвучку для words.txt.

Для каждого слова:
  * живая запись диктора из открытого словаря (dictionaryapi.dev, записи Wiktionary), если она есть;
  * нейроголос Microsoft (edge-tts) — для слова и для примера.
Файлы складываются в audio/, а audio/index.json говорит приложению, что где лежит.
Уже готовые файлы повторно не создаются, лишние удаляются.

Запуск вручную:  pip install edge-tts  &&  python tools/build_audio.py
"""
import asyncio
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

VOICE = "en-US-AvaMultilingualNeural"   # другой голос: en-US-AndrewMultilingualNeural, en-US-EmmaMultilingualNeural, en-GB-SoniaNeural
ACCENT = "us"                            # какую живую запись брать первой: us / uk
RATE = "-5%"                             # чуть медленнее обычного темпа

ROOT = Path(__file__).resolve().parent.parent
WORDS = ROOT / "words.txt"
AUDIO = ROOT / "audio"
INDEX = AUDIO / "index.json"
UA = {"User-Agent": "ty-geniy-words/1.0"}


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
        ru = parts[1] if len(parts) > 1 else ""
        ex = "; ".join(p for p in parts[2:] if p)
        if en and ru:
            out.append({"key": re.sub(r"\s+", " ", en.lower()), "en": en, "ex": ex})
    return out


def slug(text, salt=""):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "w"
    return f"{s}-{hashlib.sha1((text + salt).encode('utf-8')).hexdigest()[:6]}"


def dict_term(en):
    t = re.sub(r"^to\s+", "", en.split("/")[0].strip().lower())
    return t if re.fullmatch(r"[a-z][a-z'-]*", t) else None


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def download(url, dest):
    if url.startswith("//"):
        url = "https:" + url
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    if len(data) < 800:
        raise ValueError("слишком короткий файл")
    dest.write_bytes(data)


def dictionary_info(term):
    """Транскрипция и ссылка на живую запись (сначала нужный акцент)."""
    data = fetch_json("https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(term))
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


async def tts(text, dest, sem):
    import edge_tts
    async with sem:
        for attempt in range(3):
            try:
                await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(dest))
                if dest.stat().st_size > 800:
                    return True
            except Exception as e:  # сеть или сервис — пробуем ещё
                print(f"  ! нейроголос «{text[:40]}»: {e}", file=sys.stderr)
            await asyncio.sleep(2 + attempt * 3)
        dest.unlink(missing_ok=True)
        return False


async def main():
    words = parse_words(WORDS.read_text(encoding="utf-8"))
    for sub in ("w", "h", "e"):
        (AUDIO / sub).mkdir(parents=True, exist_ok=True)
    old = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    old_words = old.get("words", {}) if old.get("voice") == VOICE else {}
    index = {"voice": VOICE, "words": {}}
    sem = asyncio.Semaphore(4)
    jobs = []
    made = {"w": 0, "h": 0, "e": 0}

    for w in words:
        key, prev = w["key"], old_words.get(w["key"], {})
        rec = {}
        # нейроголос для слова
        wf = f"w/{slug(w['en'], VOICE)}.mp3"
        rec["w"] = wf
        if not (AUDIO / wf).exists():
            jobs.append((w["en"], AUDIO / wf, "w"))
        # живая запись и транскрипция — спрашиваем словарь один раз
        if "dict" in prev:
            rec["ipa"], rec["dict"] = prev.get("ipa", ""), prev["dict"]
            if prev.get("h") and (AUDIO / prev["h"]).exists():
                rec["h"] = prev["h"]
        else:
            term = dict_term(w["en"])
            rec["ipa"], rec["dict"] = "", True
            if term:
                try:
                    ipa, url = dictionary_info(term)
                    rec["ipa"] = ipa
                    if url:
                        hf = f"h/{slug(term)}.mp3"
                        download(url, AUDIO / hf)
                        rec["h"] = hf
                        made["h"] += 1
                except Exception as e:
                    print(f"  ! словарь «{term}»: {e}", file=sys.stderr)
                    rec.pop("dict")  # попробуем в следующий раз
        # нейроголос для примера
        if w["ex"]:
            ef = f"e/{slug(w['ex'], VOICE)}.mp3"
            rec["e"] = ef
            if not (AUDIO / ef).exists():
                jobs.append((w["ex"], AUDIO / ef, "e"))
        index["words"][key] = rec

    results = await asyncio.gather(*(tts(t, d, sem) for t, d, _ in jobs))
    for (_, dest, kind), ok in zip(jobs, results):
        if ok:
            made[kind] += 1
        else:  # не получилось — приложение озвучит голосом устройства
            for rec in index["words"].values():
                if rec.get(kind) == str(dest.relative_to(AUDIO)).replace("\\", "/"):
                    rec.pop(kind, None)

    used = {f for rec in index["words"].values() for k, f in rec.items() if k in ("w", "h", "e")}
    removed = 0
    for sub in ("w", "h", "e"):
        for f in (AUDIO / sub).glob("*.mp3"):
            if f"{sub}/{f.name}" not in used:
                f.unlink()
                removed += 1

    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"Слов: {len(words)}. Новых файлов: нейроголос слов {made['w']}, примеров {made['e']}, "
          f"живых записей {made['h']}. Удалено лишних: {removed}.")


if __name__ == "__main__":
    asyncio.run(main())
