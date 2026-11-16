"""Извлечение текста из файлов разных форматов в единую структуру «документ -> страницы -> строки».

Каждая страница сохраняется в work/<file_id>/pages/NNNN.json сразу после обработки,
поэтому при сбое обработка продолжается со следующей страницы, а не с начала файла.
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageSequence

from . import config as C
from . import db
from .ocr import ocr_image, suspicious_words


class FileError(Exception):
    """Ошибка, из-за которой файл целиком не может быть обработан."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


_word_ok = re.compile(r"^[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9.,;:()«»\"'%/\-–—№°]*$")


def text_quality(tokens) -> float:
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if len(t) >= 2 and _word_ok.match(t)) / len(tokens)


# ---------------------------------------------------------------- контекст обработки файла
class Job:
    def __init__(self, con, row):
        self.con = con
        self.row = row
        self.id = row["id"]
        self.path = Path(row["stored_path"])
        self.dir = C.WORK_DIR / str(self.id)
        (self.dir / "pages").mkdir(parents=True, exist_ok=True)
        (self.dir / "crops").mkdir(exist_ok=True)
        self.total = 0

    def issue(self, severity, kind, message, page=None, **kw):
        db.add_issue(self.con, self.id, severity, kind, message, page=page, **kw)

    def set_total(self, n):
        self.total = n
        self.con.execute("UPDATE files SET pages_total=?, heartbeat=? WHERE id=?", (n, time.time(), self.id))

    def page_file(self, n):
        return self.dir / "pages" / f"{n:05d}.json"

    def pages(self, n_pages, fn):
        """Обработка страниц с кэшем; ошибка на странице не останавливает файл."""
        self.set_total(n_pages)
        for n in range(1, n_pages + 1):
            pf = self.page_file(n)
            if pf.exists():
                continue
            try:
                page = fn(n)
            except FileError:
                raise
            except Exception as e:  # noqa: BLE001
                kind = "timeout" if "timeout" in str(e).lower() else "page_error"
                msg = f"Стр. {n}: не удалось обработать ({type(e).__name__}: {str(e)[:300]})"
                self.issue("error", kind, msg, page=n)
                page = {"n": n, "method": "error", "error": msg, "lines": []}
            tmp = pf.with_suffix(".tmp")
            tmp.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
            tmp.replace(pf)  # атомарно: недописанная страница не попадёт в кэш
            self.con.execute("UPDATE files SET pages_done=?, heartbeat=? WHERE id=?", (n, time.time(), self.id))

    # ---- OCR одной картинки с регистрацией подозрительных слов
    def ocr_page(self, n, img: Image.Image, to_points: float | None = None, label="") -> dict:
        r = ocr_image(img)
        lines = r["lines"]
        scale = to_points if (to_points and not r["rotated"]) else None
        if r["conf"] < C.PAGE_CONF_WARN and lines:
            self.issue("warning", "low_quality",
                       f"Стр. {n}{label}: низкое качество распознавания (уверенность {r['conf']:.0f}%) — проверьте скан",
                       page=n, conf=r["conf"])
        if r.get("dropped"):
            self.issue("info", "dropped_lines",
                       f"Стр. {n}{label}: {r['dropped']} нечитаемых строк исключено из текста (печати, подписи, рисунки, сильные дефекты скана)",
                       page=n)
        if r["rotated"]:
            self.issue("info", "rotated", f"Стр. {n}{label}: страница была повёрнута на {r['rotated']}°, распознана с поворотом", page=n)
        sus = suspicious_words(lines)
        if len(sus) > C.MAX_WORDS_PER_PAGE:
            self.issue("info", "many_words",
                       f"Стр. {n}{label}: {len(sus)} сомнительных слов, на проверку вынесены {C.MAX_WORDS_PER_PAGE} самых неуверенных",
                       page=n)
            sus = sorted(sus, key=lambda s: s["conf"])[: C.MAX_WORDS_PER_PAGE]
        for k, s in enumerate(sus):
            crop = self._crop(r["image"], lines[s["line"]]["bbox"], s["box"], f"p{n:05d}_{k}.png")
            self.issue("word", "word", f"Стр. {n}{label}: не уверен в слове «{s['word']}»", page=n, word=s["word"],
                       conf=s["conf"], line=s["line"], word_idx=s["word_idx"], context=s["context"], crop=crop)
        out_lines = []
        for ln in lines:
            bbox = [round(v * scale, 1) for v in ln["bbox"]] if scale else None
            out_lines.append({"text": ln["text"], "bbox": bbox, "conf": ln["conf"], "kind": "p",
                              "par_break": ln["par_break"]})
        return {"n": n, "method": "ocr", "conf": r["conf"], "model": r["model"], "rotated": r["rotated"],
                "units": "pt" if scale else None, "lines": out_lines}

    def _crop(self, img, line_box, word_box, name):
        """Картинка строки с рамкой вокруг сомнительного слова — для ручной проверки."""
        pad = 12
        x0, y0 = max(0, line_box[0] - pad), max(0, line_box[1] - pad)
        x1, y1 = min(img.width, line_box[2] + pad), min(img.height, line_box[3] + pad)
        # ограничим ширину контекста вокруг слова
        cx = (word_box[0] + word_box[2]) // 2
        if x1 - x0 > 1400:
            x0, x1 = max(0, cx - 700), min(img.width, cx + 700)
        c = img.crop((x0, y0, x1, y1)).convert("RGB")
        d = ImageDraw.Draw(c)
        d.rectangle([word_box[0] - x0 - 3, word_box[1] - y0 - 3, word_box[2] - x0 + 3, word_box[3] - y0 + 3],
                    outline=(220, 30, 30), width=3)
        if c.width > 900:
            c = c.resize((900, int(c.height * 900 / c.width)))
        p = self.dir / "crops" / name
        c.save(p, "PNG", optimize=True)
        return str(p.relative_to(C.WORK_DIR))


# ---------------------------------------------------------------- PDF
def _pdf_text_lines(page):
    words = page.get_text("words", sort=True)
    lines, order = {}, []
    for x0, y0, x1, y1, w, b, l, _ in words:
        k = (b, l)
        if k not in lines:
            lines[k] = {"w": [], "box": [x0, y0, x1, y1], "b": b}
            order.append(k)
        ln = lines[k]
        ln["w"].append(w)
        bx = ln["box"]
        ln["box"] = [min(bx[0], x0), min(bx[1], y0), max(bx[2], x1), max(bx[3], y1)]
    out, last_b = [], None
    for k in order:
        ln = lines[k]
        out.append({"text": " ".join(ln["w"]), "bbox": [round(v, 1) for v in ln["box"]], "kind": "p",
                    "par_break": last_b is not None and ln["b"] != last_b})
        last_b = ln["b"]
    return out, [w[4] for w in words]


def _image_coverage(page) -> float:
    area = page.rect.width * page.rect.height or 1
    cov = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        cov += r.width * r.height
    return min(1.0, cov / area)


def process_pdf(job: Job, path: Path = None, page_offset=0):
    path = path or job.path
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001
        raise FileError("corrupt", f"Файл повреждён или не является PDF: {e}")
    if doc.needs_pass and not doc.authenticate(""):
        raise FileError("encrypted", "PDF защищён паролем — обработка невозможна")
    if doc.page_count == 0:
        raise FileError("empty", "В PDF нет страниц")
    if getattr(doc, "is_repaired", False):
        job.issue("warning", "repaired", "PDF был повреждён и восстановлен автоматически — часть страниц или текста может отсутствовать")

    def do_page(n):
        page = doc[n - 1]
        lines, tokens = _pdf_text_lines(page)
        chars = sum(len(t) for t in tokens)
        good = chars >= C.MIN_TEXT_CHARS and text_quality(tokens) >= 0.6
        cov = _image_coverage(page)
        if good and not (cov > 0.6 and chars < 300):
            return {"n": n, "method": "text", "conf": None, "units": "pt", "lines": lines}
        if cov < 0.05 and chars == 0 and not page.get_drawings():
            return {"n": n, "method": "empty", "lines": []}
        dpi = C.OCR_DPI
        longest = max(page.rect.width, page.rect.height) / 72 * dpi
        if longest > C.OCR_MAX_SIDE:  # чертежи большого формата
            dpi = int(dpi * C.OCR_MAX_SIDE / longest)
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        res = job.ocr_page(n, img, to_points=72 / dpi)
        # смешанная страница: если текстовый слой богаче — оставляем его
        if good and chars > sum(len(l["text"]) for l in res["lines"]):
            return {"n": n, "method": "text", "conf": None, "units": "pt", "lines": lines}
        return res

    job.pages(doc.page_count, do_page)


# ---------------------------------------------------------------- картинки (TIFF многостраничный, JPG, PNG)
def process_image(job: Job):
    try:
        im = Image.open(job.path)
        frames = [f.copy() for f in ImageSequence.Iterator(im)]
    except Exception as e:  # noqa: BLE001
        raise FileError("corrupt", f"Изображение повреждено или формат не распознан: {e}")
    job.pages(len(frames), lambda n: job.ocr_page(n, frames[n - 1]))


# ---------------------------------------------------------------- конвертация старых форматов через LibreOffice
def convert_with_libreoffice(src: Path, target_ext: str) -> Path:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise FileError("unsupported", f"Для формата {src.suffix} нужен LibreOffice (есть в Docker-образе)")
    out = Path(tempfile.mkdtemp(prefix="conv_"))
    profile = Path(tempfile.gettempdir()) / f"lo_profile_{os.getpid()}"
    try:
        r = subprocess.run([soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
                            "--convert-to", target_ext.strip("."), "--outdir", str(out), str(src)],
                           capture_output=True, timeout=C.CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise FileError("timeout", f"Конвертация {src.suffix} не завершилась за {C.CONVERT_TIMEOUT} с")
    res = list(out.glob(f"*{target_ext}"))
    if not res:
        raise FileError("corrupt", f"Не удалось открыть файл {src.suffix} (повреждён?): "
                                   f"{(r.stderr or r.stdout).decode(errors='ignore')[:200]}")
    return res[0]


# ---------------------------------------------------------------- Word
def _table_lines(rows, tid):
    out = []
    for i, cells in enumerate(rows):
        # объединённые ячейки python-docx повторяет — убираем подряд идущие дубли
        dedup = [c for j, c in enumerate(cells) if j == 0 or c != cells[j - 1]]
        if not any(dedup):
            continue
        txt = "| " + " | ".join(c.replace("\n", " ").replace("|", "/") for c in dedup) + " |"
        out.append({"text": txt, "bbox": None, "kind": "tr", "table": tid, "header": i == 0})
    return out


def process_word(job: Job):
    path = job.path
    if path.suffix.lower() != ".docx":
        path = convert_with_libreoffice(path, ".docx")
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        d = Document(str(path))
    except Exception as e:  # noqa: BLE001
        raise FileError("corrupt", f"Документ Word повреждён или не открывается: {e}")

    def text_page(_n):
        lines, tid = [], 0
        for el in d.element.body.iterchildren():
            tag = el.tag.split("}")[-1]
            if tag == "p":
                p = Paragraph(el, d)
                t = p.text.strip()
                if t:
                    style = (p.style.name or "").lower() if p.style is not None else ""
                    kind = "h" if style.startswith(("heading", "заголовок", "title")) else "p"
                    lines.append({"text": t, "bbox": None, "kind": kind, "par_break": True})
            elif tag == "tbl":
                tid += 1
                t = Table(el, d)
                rows = [[c.text.strip() for c in r.cells] for r in t.rows]
                lines.extend(_table_lines(rows, tid))
        return {"n": 1, "method": "docx", "lines": lines}

    # картинки внутри документа (часто туда вставляют сканы) — распознаём отдельными «страницами»
    images = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in sorted(z.namelist()):
                if name.startswith("word/media/") and name.lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")):
                    try:
                        im = Image.open(io.BytesIO(z.read(name)))
                        if im.width >= 400 and im.height >= 300:
                            images.append((name.split("/")[-1], im.copy()))
                    except Exception:  # noqa: BLE001
                        job.issue("warning", "image", f"Не удалось прочитать изображение {name} внутри документа")
    except zipfile.BadZipFile:
        pass

    def do_page(n):
        if n == 1:
            return text_page(n)
        name, im = images[n - 2]
        res = job.ocr_page(n, im, label=f" (изображение {name})")
        res["source_image"] = name
        if res["lines"]:
            res["lines"].insert(0, {"text": f"[Текст с изображения {name}]", "bbox": None, "kind": "h", "par_break": True})
        return res

    job.pages(1 + len(images), do_page)


# ---------------------------------------------------------------- Excel / CSV
def _cell(v):
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%d.%m.%Y") if getattr(v, "hour", 0) == 0 and getattr(v, "minute", 0) == 0 else v.strftime("%d.%m.%Y %H:%M")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _sheet_page(n, title, rows):
    rows = [[_cell(v) for v in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if rows:
        width = max(max((i + 1 for i, v in enumerate(r) if v), default=0) for r in rows)
        rows = [r[:width] + [""] * (width - len(r[:width])) for r in rows]
    lines = [{"text": f"Лист: {title}", "bbox": None, "kind": "h", "par_break": True}]
    lines += _table_lines(rows, n)
    return {"n": n, "method": "table", "sheet": title, "lines": lines}


def process_excel(job: Job):
    path, ext = job.path, job.path.suffix.lower()
    if ext == ".csv":
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        job.pages(1, lambda n: _sheet_page(1, path.name, rows))
        return
    if ext in (".xls", ".ods"):
        path = convert_with_libreoffice(path, ".xlsx")
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise FileError("corrupt", f"Файл Excel повреждён или не открывается: {e}")
    sheets = wb.sheetnames
    job.pages(len(sheets), lambda n: _sheet_page(n, sheets[n - 1], wb[sheets[n - 1]].iter_rows(values_only=True)))


# ---------------------------------------------------------------- TXT
def process_txt(job: Job):
    raw = job.path.read_bytes()
    for enc in ("utf-8-sig", "cp1251", "koi8-r"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = [{"text": t.strip(), "bbox": None, "kind": "p", "par_break": True} for t in text.splitlines() if t.strip()]
    job.pages(1, lambda n: {"n": 1, "method": "text", "lines": lines})


# ---------------------------------------------------------------- точка входа
MAGIC = {".pdf": [b"%PDF"], ".docx": [b"PK"], ".xlsx": [b"PK"], ".xlsm": [b"PK"], ".odt": [b"PK"], ".ods": [b"PK"],
         ".doc": [b"\xd0\xcf\x11\xe0", b"{\\rtf"], ".xls": [b"\xd0\xcf\x11\xe0", b"<"], ".rtf": [b"{\\rtf"]}


def process_file(con, row) -> dict:
    job = Job(con, row)
    if not job.path.exists():
        raise FileError("missing", "Файл не найден на диске (удалён или не дозагрузился)")
    if job.path.stat().st_size == 0:
        raise FileError("empty", "Файл пустой (0 байт)")
    ext = job.path.suffix.lower()
    head = job.path.open("rb").read(8)
    if ext in MAGIC and not any(head.startswith(m) for m in MAGIC[ext]):
        if ext == ".pdf" and b"%PDF" in job.path.open("rb").read(1024):
            pass  # мусор перед заголовком — MuPDF справится
        else:
            raise FileError("corrupt", f"Содержимое не соответствует формату {ext} (файл повреждён или переименован)")

    if ext in C.PDF_EXT:
        process_pdf(job)
    elif ext in C.IMAGE_EXT:
        process_image(job)
    elif ext in C.WORD_EXT:
        process_word(job)
    elif ext in C.EXCEL_EXT:
        process_excel(job)
    elif ext in C.TEXT_EXT:
        process_txt(job)
    else:
        raise FileError("unsupported", f"Формат {ext} не поддерживается")

    # итог по файлу
    pages = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((job.dir / "pages").glob("*.json"))]
    chars = sum(len(l["text"]) for p in pages for l in p["lines"])
    ocr = [p for p in pages if p.get("method") == "ocr"]
    confs = [p["conf"] for p in ocr if p.get("conf")]
    if chars == 0:
        job.issue("warning", "no_text", "В файле не найден текст (пустой документ, чертёж или нечитаемый скан)")
    return {"pages_total": len(pages), "ocr_pages": len(ocr), "chars": chars,
            "mean_conf": round(sum(confs) / len(confs), 1) if confs else None}
