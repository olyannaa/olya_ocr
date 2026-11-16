"""OCR картинки страницы через Tesseract с оценкой уверенности по каждому слову.

Стратегия (баланс скорость/качество для 6 ядер):
1. быстрая модель Tesseract;
2. если средняя уверенность низкая — определяем поворот страницы (OSD) и повторяем;
3. если всё ещё низкая — точная модель (tessdata_best), берём лучший результат.
"""
import re
from pathlib import Path

import pytesseract
from PIL import Image, ImageFilter, ImageOps

from . import config as C

_letters = re.compile(r"[A-Za-zА-Яа-яЁё]")
_morph = None


def morph():
    global _morph
    if _morph is None:
        try:
            import pymorphy3
            _morph = pymorphy3.MorphAnalyzer()
        except Exception:  # noqa: BLE001
            _morph = False
    return _morph


def is_known_word(w: str) -> bool:
    """Слово есть в словаре русского языка (или это не слово: число, код, латиница-аббревиатура)."""
    core = w.strip(".,;:!?()[]«»\"'—–-…/\\|*")
    if len(core) < 3 or not _letters.search(core):
        return True
    if re.search(r"\d", core):          # коды, обозначения: 09Г2С, ГОСТ-2.105, А1
        return True
    if core.isupper() and len(core) <= 6:  # аббревиатуры: ОТК, ГОСТ, ЕСКД
        return True
    m = morph()
    if not m:
        return True
    if re.fullmatch(r"[А-Яа-яЁё\-]+", core):
        return all(m.word_is_known(p.lower()) for p in core.split("-") if p)
    return True  # латиница — словаря нет, не помечаем


def preprocess(img: Image.Image) -> Image.Image:
    img = img.convert("L")
    if max(img.size) > C.OCR_MAX_SIDE:
        k = C.OCR_MAX_SIDE / max(img.size)
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    img = img.filter(ImageFilter.MedianFilter(3))
    return ImageOps.autocontrast(img, cutoff=1)


def _run(img, tessdata: str | None):
    cfg = "--oem 1 --psm 3"
    if tessdata:
        cfg += f' --tessdata-dir "{tessdata}"'
    d = pytesseract.image_to_data(img, lang=C.OCR_LANG, config=cfg, output_type=pytesseract.Output.DICT,
                                  timeout=C.OCR_TIMEOUT)
    words = []
    for i in range(len(d["text"])):
        t = (d["text"][i] or "").strip()
        conf = float(d["conf"][i])
        if not t or conf < 0:
            continue
        words.append({"t": t, "conf": conf, "x": d["left"][i], "y": d["top"][i], "w": d["width"][i],
                      "h": d["height"][i], "key": (d["block_num"][i], d["par_num"][i], d["line_num"][i])})
    return words


_cyr_lower = re.compile(r"^[а-яё]")


def fix_common(words):
    """Типовые ошибки Tesseract: «1» распознаётся как «|» перед «%» или словом со строчной буквы («| %», «| раз»)."""
    for i, w in enumerate(words):
        if w["t"] in ("|", "l") and i + 1 < len(words) and words[i + 1]["key"] == w["key"]:
            nxt = words[i + 1]["t"]
            if nxt.startswith("%") or _cyr_lower.match(nxt):
                w["t"] = "1"
        elif w["t"].startswith("|") and w["t"][1:2] == "%":
            w["t"] = "1" + w["t"][1:]
    return words


def mean_conf(words) -> float:
    total = sum(len(w["t"]) for w in words)
    return sum(w["conf"] * len(w["t"]) for w in words) / total if total else 0.0


def _rotation(img) -> int:
    try:
        osd = pytesseract.image_to_osd(img, config="--psm 0 -c min_characters_to_try=10", timeout=60,
                                       output_type=pytesseract.Output.DICT)
        return int(osd.get("rotate", 0))
    except Exception:  # noqa: BLE001
        return 0


def ocr_image(img: Image.Image) -> dict:
    """Возвращает {lines, conf, rotated, model, size}. Строки: text, bbox (в пикселях img), conf, words."""
    img = preprocess(img)
    first = C.TESSDATA_BEST if C.OCR_MODE == "best" and Path(C.TESSDATA_BEST, "rus.traineddata").exists() else (C.TESSDATA_FAST or None)
    words = _run(img, first)
    conf, rotated, model = mean_conf(words), 0, "best" if first == C.TESSDATA_BEST else "fast"

    if conf < C.PAGE_CONF_RETRY:
        rot = _rotation(img)
        if rot:
            img2 = img.rotate(-rot, expand=True, fillcolor=255)
            w2 = _run(img2, C.TESSDATA_FAST or None)
            if mean_conf(w2) > conf:
                img, words, conf, rotated = img2, w2, mean_conf(w2), rot
    if C.OCR_MODE == "auto" and conf < C.PAGE_CONF_RETRY and Path(C.TESSDATA_BEST, "rus.traineddata").exists():
        w3 = _run(img, C.TESSDATA_BEST)
        if mean_conf(w3) >= conf:
            words, conf, model = w3, mean_conf(w3), "best"

    fix_common(words)
    # слова -> строки
    lines, order = {}, []
    for w in words:
        if w["key"] not in lines:
            lines[w["key"]] = []
            order.append(w["key"])
        lines[w["key"]].append(w)
    out, last_par, dropped = [], None, 0
    for key in order:
        ws = lines[key]
        lc = mean_conf(ws)
        if lc < C.LINE_DROP_CONF:  # мусор: печати, подписи, линии, рисунки
            dropped += 1
            continue
        x0 = min(w["x"] for w in ws)
        y0 = min(w["y"] for w in ws)
        x1 = max(w["x"] + w["w"] for w in ws)
        y1 = max(w["y"] + w["h"] for w in ws)
        out.append({"text": " ".join(w["t"] for w in ws), "bbox": [x0, y0, x1, y1],
                    "conf": round(mean_conf(ws), 1), "par_break": last_par is not None and key[:2] != last_par,
                    "words": [{"t": w["t"], "conf": w["conf"], "box": [w["x"], w["y"], w["x"] + w["w"], w["y"] + w["h"]]}
                              for w in ws]})
        last_par = key[:2]
    return {"lines": out, "conf": round(conf, 1), "rotated": rotated, "model": model, "image": img, "dropped": dropped}


def suspicious_words(lines) -> list[dict]:
    """Слова с низкой уверенностью, которых нет в словаре -> на ручную проверку."""
    res = []
    for li, ln in enumerate(lines):
        if ln["conf"] < C.LINE_WORDS_CONF:
            continue  # строка целиком ненадёжна — о ней предупреждение по странице, а не по словам
        for wi, w in enumerate(ln["words"]):
            t = w["t"]
            if t.count(".") >= 3 or sum(not ch.isalnum() for ch in t) > len(t) * 0.3:
                continue  # отточия оглавлений, «мусор» из знаков
            # перенос слова между строками: проверяем склеенное слово («запя-» + «тых» -> «запятых»)
            if wi == len(ln["words"]) - 1 and t.endswith("-") and li + 1 < len(lines) and lines[li + 1]["words"]:
                if is_known_word(t[:-1] + lines[li + 1]["words"][0]["t"]):
                    continue
            if wi == 0 and li > 0 and lines[li - 1]["words"] and lines[li - 1]["words"][-1]["t"].endswith("-"):
                if is_known_word(lines[li - 1]["words"][-1]["t"][:-1] + t):
                    continue
            if w["conf"] < C.WORD_CONF and not is_known_word(t):
                res.append({"line": li, "word_idx": wi, "word": w["t"], "conf": round(w["conf"], 1),
                            "box": w["box"], "context": ln["text"]})
    return res
