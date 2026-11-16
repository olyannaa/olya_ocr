"""Сборка итогового корпуса для LLM из обработанных страниц + ручных исправлений.

output/
  chunks.jsonl              — РЕЗУЛЬТАТ: фрагменты ~1200 символов с метаданными -> вход следующего этапа (эмбеддинги/RAG)
  documents/<doc_id>.json   — полный текст документа по страницам (для показа источника пользователю)
  report.xlsx               — отчёт: файлы, ошибки, слова на проверку
"""
import json
import re
import shutil
import time
from pathlib import Path

from . import config as C
from . import db

CLAUSE = re.compile(r"^\s*(\d+(\.\d+)*\.?|[А-ЯA-Z]\.\d+|Приложение|ПРИЛОЖЕНИЕ|Раздел|РАЗДЕЛ|Глава)\s")


def load_pages(file_id) -> list[dict]:
    d = C.WORK_DIR / str(file_id) / "pages"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.exists() else []


def apply_corrections(pages, corrections):
    """corrections: {(page, line, word_idx): text}"""
    for p in pages:
        for li, ln in enumerate(p["lines"]):
            keys = [k for k in corrections if k[0] == p["n"] and k[1] == li]
            if not keys:
                continue
            words = ln["text"].split(" ")
            for k in keys:
                if 0 <= k[2] < len(words):
                    words[k[2]] = corrections[k]
            ln["text"] = " ".join(words)
            ln["corrected"] = True
    return pages


def join_lines(lines) -> str:
    out = ""
    for ln in lines:
        t = ln["text"].replace("\xad", "-")
        if not out:
            out = t
        elif out.endswith("-") and t[:1].islower() and out[-2:-1].isalpha():
            out = out[:-1] + t  # перенос слова
        else:
            out += ("\n\n" if ln.get("par_break") and ln.get("kind") != "tr" else "\n") + t
    return out


def make_chunks(meta, pages):
    """Фрагменты по ~CHUNK_CHARS символов; границы по пунктам документа; у таблиц повторяется шапка."""
    flat = []
    for p in pages:
        for li, ln in enumerate(p["lines"]):
            flat.append({**ln, "page": p["n"], "li": li})
    chunks, cur = [], []
    header_of = {}

    def size(ls):
        return sum(len(l["text"]) + 1 for l in ls)

    def flush():
        if not cur:
            return
        text = join_lines(cur)
        if not text.strip():
            return
        chunks.append({
            "chunk_id": f"{meta['doc_id']}:{len(chunks)}",
            "doc_id": meta["doc_id"],
            "source_path": meta["source_path"],
            "title": meta["title"],
            "pages": sorted({l["page"] for l in cur}),
            "text": text,
            "lines": [[l["page"], l["li"]] for l in (cur[0], cur[-1])],
            "boxes": {str(pg): [l["bbox"] for l in cur if l["page"] == pg and l.get("bbox")]
                      for pg in sorted({l["page"] for l in cur})},
            "has_table": any(l.get("kind") == "tr" for l in cur),
            "ocr": meta["ocr_pages"] > 0,
        })

    for ln in flat:
        if ln.get("kind") == "tr" and ln.get("header"):
            header_of[ln["table"]] = ln
        s = size(cur)
        new_section = CLAUSE.match(ln["text"]) or ln.get("kind") == "h"
        if cur and ((s > C.CHUNK_CHARS * 0.6 and new_section) or s + len(ln["text"]) > C.CHUNK_CHARS * 1.3):
            flush()
            if ln.get("kind") == "tr" and not ln.get("header") and ln.get("table") in header_of:
                cur = [header_of[ln["table"]]]  # повторяем шапку таблицы в новом фрагменте
            elif new_section or not C.CHUNK_OVERLAP_LINES:
                cur = []
            else:
                cur = cur[-C.CHUNK_OVERLAP_LINES:]
        cur.append(ln)
    flush()
    return chunks


def export_all() -> dict:
    t0 = time.time()
    con = db.connect()
    out = C.OUTPUT_DIR
    tmp = out.parent / "output.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "documents").mkdir(parents=True)

    corr = {}
    for r in con.execute("SELECT file_id, page, line, word_idx, word, correction FROM issues "
                         "WHERE severity='word' AND correction IS NOT NULL AND correction<>''"):
        corr.setdefault(r["file_id"], {})[(r["page"], r["line"], r["word_idx"])] = r["correction"]

    files = con.execute("SELECT f.*, b.name AS batch FROM files f LEFT JOIN batches b ON b.id=f.batch_id ORDER BY f.rel_path").fetchall()
    n_docs = n_chunks = 0
    seen, used_ids = set(), set()
    with open(tmp / "chunks.jsonl", "w", encoding="utf-8") as fch:
        for f in files:
            doc_id = (f["sha256"] or str(f["id"]))[:16]
            if f["status"] != "done" or (doc_id, f["rel_path"]) in seen:
                continue
            seen.add((doc_id, f["rel_path"]))
            if doc_id in used_ids:  # одинаковый файл в разных папках — отдельный документ со своим путём
                doc_id = f"{doc_id}-{f['id']}"
            used_ids.add(doc_id)
            pages = apply_corrections(load_pages(f["id"]), corr.get(f["id"], {}))
            meta = {"doc_id": doc_id, "source_path": f["rel_path"], "file_name": Path(f["rel_path"]).name,
                    "title": Path(f["rel_path"]).stem, "format": f["ext"], "size": f["size"], "sha256": f["sha256"],
                    "pages": len(pages), "ocr_pages": f["ocr_pages"], "mean_ocr_conf": f["mean_conf"],
                    "batch": f["batch"], "processed": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f["finished"] or 0))}
            name = doc_id
            (tmp / "documents" / f"{name}.json").write_text(
                json.dumps({**meta, "pages": pages}, ensure_ascii=False), encoding="utf-8")
            for ch in make_chunks(meta, pages):
                fch.write(json.dumps(ch, ensure_ascii=False) + "\n")
                n_chunks += 1
            n_docs += 1

    write_report(con, tmp / "report.xlsx")
    readme = (Path(__file__).parent / "OUTPUT_README.md")
    if readme.exists():
        shutil.copy(readme, tmp / "README.md")
    # заменяем содержимое папки результата (сама папка может быть точкой монтирования — её не трогаем)
    out.mkdir(parents=True, exist_ok=True)
    for p in out.iterdir():
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    for p in tmp.iterdir():
        shutil.move(str(p), str(out / p.name))
    tmp.rmdir()
    info = {"docs": n_docs, "chunks": n_chunks, "seconds": round(time.time() - t0, 1),
            "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    db.kv_set(con, "last_export", info)
    return info


def write_report(con, path: Path):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDE6F3")

    def sheet(ws, header, rows, widths):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = bold, fill
        for r in rows:
            ws.append(list(r))
        for i, wdt in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = wdt
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    st = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM files GROUP BY status")}
    agg = con.execute("SELECT SUM(pages_total) p, SUM(ocr_pages) o, SUM(size) s FROM files WHERE status='done'").fetchone()
    ws = wb.active
    ws.title = "Сводка"
    for row in [("Сформирован", time.strftime("%d.%m.%Y %H:%M")), ("Файлов всего", sum(st.values())),
                ("Обработано", st.get("done", 0)), ("С ошибкой", st.get("error", 0)),
                ("Пропущено (формат не поддерживается)", st.get("skipped", 0)),
                ("В очереди / в работе", st.get("queued", 0) + st.get("processing", 0)),
                ("Страниц обработано", agg["p"] or 0), ("Из них распознано OCR", agg["o"] or 0),
                ("Объём обработанных файлов, МБ", round((agg["s"] or 0) / 1e6, 1))]:
        ws.append(row)
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 22

    sheet(wb.create_sheet("Файлы"), ["Путь", "Статус", "Страниц", "OCR стр.", "Уверенность OCR, %", "Символов", "Ошибка"],
          ((f["rel_path"], f["status"], f["pages_total"], f["ocr_pages"], f["mean_conf"], f["chars"], f["error"])
           for f in con.execute("SELECT * FROM files ORDER BY rel_path")), [70, 12, 10, 10, 18, 12, 80])

    sheet(wb.create_sheet("Ошибки и предупреждения"), ["Файл", "Стр.", "Тип", "Вид", "Сообщение", "Время"],
          ((r["rel_path"], r["page"], {"error": "ошибка", "warning": "предупреждение"}.get(r["severity"], "к сведению"), r["kind"], r["message"],
            time.strftime("%d.%m %H:%M:%S", time.localtime(r["created"])))
           for r in con.execute("SELECT i.*, f.rel_path FROM issues i JOIN files f ON f.id=i.file_id "
                                "WHERE severity IN ('error','warning','info') ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, f.rel_path, i.page")),
          [70, 6, 16, 14, 90, 16])

    ws = wb.create_sheet("Слова на проверку")
    rows = con.execute("SELECT i.*, f.rel_path FROM issues i JOIN files f ON f.id=i.file_id "
                       "WHERE severity='word' ORDER BY f.rel_path, i.page, i.line").fetchall()
    sheet(ws, ["ID", "Файл", "Стр.", "Распознано", "Уверенность", "Исправление", "Контекст (строка)", "Фрагмент скана"],
          ((r["id"], r["rel_path"], r["page"], r["word"], r["conf"], r["correction"] or "", r["context"], "") for r in rows),
          [8, 50, 6, 18, 12, 18, 70, 60])
    # картинки только для первых 300 слов, чтобы файл не разрастался
    for i, r in enumerate(rows[:300], start=2):
        p = C.WORK_DIR / (r["crop"] or "")
        if r["crop"] and p.exists():
            img = XLImage(str(p))
            k = min(1.0, 420 / img.width)
            img.width, img.height = int(img.width * k), int(img.height * k)
            ws.add_image(img, f"H{i}")
            ws.row_dimensions[i].height = max(18, img.height * 0.75)
    for row in ws.iter_rows(min_row=2, max_col=7):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    wb.save(path)
