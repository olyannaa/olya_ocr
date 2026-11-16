"""Веб-сервис подготовки документов для LLM.  uvicorn app.main:app --host 0.0.0.0 --port 8080"""
import hashlib
import os
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import config as C
from . import db
from .export import export_all
from .worker import Supervisor

SUP = Supervisor()
EXPORT_LOCK = threading.Lock()
STATE = {"exporting": False, "export_error": None}


def auto_export_loop():
    """Когда очередь опустела — автоматически пересобираем результат."""
    con = db.connect()
    while True:
        time.sleep(10)
        try:
            busy = con.execute("SELECT COUNT(*) FROM files WHERE status IN ('queued','processing')").fetchone()[0]
            last_fin = con.execute("SELECT MAX(finished) FROM files").fetchone()[0] or 0
            last = db.kv_get(con, "last_export_ts", 0)
            if not busy and last_fin > last:
                run_export()
        except Exception as e:  # noqa: BLE001
            print("auto export:", e, flush=True)


def run_export():
    if not EXPORT_LOCK.acquire(blocking=False):
        return None
    STATE["exporting"], STATE["export_error"] = True, None
    try:
        info = export_all()
        con = db.connect()
        db.kv_set(con, "last_export_ts", time.time())
        return info
    except Exception as e:  # noqa: BLE001
        STATE["export_error"] = str(e)
        raise
    finally:
        STATE["exporting"] = False
        EXPORT_LOCK.release()


@asynccontextmanager
async def lifespan(_app):
    SUP.start()
    threading.Thread(target=auto_export_loop, daemon=True).start()
    yield
    SUP.shutdown()


app = FastAPI(title="Подготовка документов для LLM", lifespan=lifespan)
WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


def safe_rel(p: str) -> str:
    parts = [x for x in PurePosixPath(p.replace("\\", "/")).parts if x not in ("", "/", ".", "..")]
    if not parts:
        raise HTTPException(400, f"Некорректный путь: {p}")
    return "/".join(parts)


# ------------------------------------------------------------------ загрузка
SERVICE_DIRS = {"__pycache__", "node_modules", "$recycle.bin", "system volume information", "thumbs.db"}


def is_service_path(p: str) -> bool:
    """Скрытые и служебные папки/файлы (.git, .venv, __pycache__, корзина) не обрабатываем и не показываем."""
    return any(x.startswith((".", "~$")) or x.lower() in SERVICE_DIRS for x in PurePosixPath(p.replace("\\", "/")).parts)


def register_skipped(con, bid, rels, now):
    """Неподдерживаемые форматы: файлы попадают в отчёт, а в ленту ошибок — одно сводное сообщение."""
    from collections import Counter
    fresh = [r for r in rels if not con.execute("SELECT 1 FROM files WHERE rel_path=? AND status='skipped'", (r,)).fetchone()]
    first = None
    for rel in fresh:
        ext = Path(rel).suffix.lower() or "(без расширения)"
        fid = con.execute("INSERT INTO files(batch_id, rel_path, stored_path, size, ext, status, error, created, finished)"
                          " VALUES(?,?,?,?,?,?,?,?,?)", (bid, rel, "", 0, ext, "skipped",
                                                         f"Формат {ext} не поддерживается", now, now)).lastrowid
        first = first or fid
    if fresh:
        cnt = Counter(Path(r).suffix.lower() or "без расширения" for r in fresh)
        kinds = ", ".join(f"{e} — {n}" for e, n in cnt.most_common(6)) + (" …" if len(cnt) > 6 else "")
        msg = (f"Пропущен файл неподдерживаемого формата: {fresh[0]}" if len(fresh) == 1 else
               f"Пропущено {len(fresh)} файлов неподдерживаемых форматов ({kinds}). Список — в отчёте Excel")
        db.add_issue(con, first, "warning", "unsupported", msg)

class FileItem(BaseModel):
    path: str
    size: int


class NewBatch(BaseModel):
    name: str = ""
    files: list[FileItem]
    skipped: list[str] = []


@app.post("/api/batches")
def new_batch(req: NewBatch):
    con = db.connect()
    now = time.time()
    with db.tx(con):
        bid = con.execute("INSERT INTO batches(name, source, created) VALUES(?,?,?)",
                          (req.name or time.strftime("Загрузка %d.%m %H:%M"), "upload", now)).lastrowid
        uploads, already = [], []
        for f in req.files:
            if is_service_path(f.path):
                continue
            rel = safe_rel(f.path)
            ex = con.execute("SELECT id, status FROM files WHERE rel_path=? AND size=? AND status NOT IN ('uploading')"
                             " ORDER BY id DESC LIMIT 1", (rel, f.size)).fetchone()
            if ex:
                already.append(rel)  # уже загружен ранее — повторно не грузим (докачка после обрыва)
                continue
            # незавершённая прошлая загрузка — переиспользуем запись
            old = con.execute("SELECT id FROM files WHERE rel_path=? AND size=? AND status='uploading'", (rel, f.size)).fetchone()
            if old:
                uploads.append({"path": f.path, "file_id": old["id"]})
                continue
            stored = C.UPLOAD_DIR / str(bid) / rel
            fid = con.execute("INSERT INTO files(batch_id, rel_path, stored_path, size, ext, status, created)"
                              " VALUES(?,?,?,?,?,?,?)",
                              (bid, rel, str(stored), f.size, Path(rel).suffix.lower(), "uploading", now)).lastrowid
            uploads.append({"path": f.path, "file_id": fid})
        register_skipped(con, bid, [safe_rel(p) for p in req.skipped if not is_service_path(p)], now)
    return {"batch_id": bid, "uploads": uploads, "already": already}


@app.put("/api/upload/{file_id}")
async def upload(file_id: int, request: Request):
    con = db.connect()
    row = con.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row or row["status"] != "uploading":
        raise HTTPException(409, "Файл не ожидает загрузки")
    dst = Path(row["stored_path"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    h, n = hashlib.sha256(), 0
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            h.update(chunk)
            n += len(chunk)
    if n != row["size"]:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"Загружено {n} байт из {row['size']} — повторите")
    os.replace(tmp, dst)
    con.execute("UPDATE files SET status='queued', sha256=?, created=? WHERE id=?", (h.hexdigest(), time.time(), file_id))
    return {"ok": True}


@app.post("/api/scan-input")
def scan_input():
    """Поставить в очередь файлы из смонтированной папки /input (сетевой диск) — без загрузки через браузер."""
    if not C.INPUT_DIR.exists() or not any(C.INPUT_DIR.iterdir()):
        raise HTTPException(400, f"Папка {C.INPUT_DIR} пуста или не подключена (см. docker-compose.yml)")
    con = db.connect()
    added = known = 0
    skipped_list = []
    now = time.time()
    with db.tx(con):
        bid = con.execute("INSERT INTO batches(name, source, created) VALUES(?,?,?)",
                          (time.strftime("Папка input %d.%m %H:%M"), "input", now)).lastrowid
        for p in sorted(C.INPUT_DIR.rglob("*")):
            if not p.is_file() or p.name.startswith((".", "~$")):
                continue
            rel, size = p.relative_to(C.INPUT_DIR).as_posix(), p.stat().st_size
            if con.execute("SELECT 1 FROM files WHERE rel_path=? AND size=? AND status<>'uploading'", (rel, size)).fetchone():
                known += 1
                continue
            ext = p.suffix.lower()
            if is_service_path(rel):
                continue
            if ext not in C.SUPPORTED:
                skipped_list.append(rel)
                continue
            con.execute("INSERT INTO files(batch_id, rel_path, stored_path, size, ext, status, sha256, created)"
                        " VALUES(?,?,?,?,?,?,?,?)", (bid, rel, str(p), size, ext, "queued", _sha(p), now))
            added += 1
        register_skipped(con, bid, skipped_list, now)
    return {"added": added, "skipped": len(skipped_list), "already": known}


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------ мониторинг
@app.get("/api/status")
def status():
    con = db.connect()
    st = {r["status"]: {"n": r["n"], "size": r["s"] or 0}
          for r in con.execute("SELECT status, COUNT(*) n, SUM(size) s FROM files GROUP BY status")}
    pages = con.execute("SELECT COALESCE(SUM(pages_done),0) d FROM files WHERE status IN ('done','processing','error')").fetchone()["d"]
    # скорость: страниц за последние 10 минут
    now = time.time()
    since = now - 600
    r = con.execute("SELECT COALESCE(SUM(CASE WHEN status='done' THEN pages_total ELSE pages_done END),0) p, MIN(started) s "
                    "FROM files WHERE (status='done' AND finished>?) OR status='processing'", (since,)).fetchone()
    minutes = max(2.0, (now - max(r["s"] or now, since)) / 60)  # первые минуты не завышаем скорость
    recent_rate = r["p"] / minutes
    sev = {r["severity"]: r["n"] for r in con.execute("SELECT severity, COUNT(*) n FROM issues GROUP BY severity")}
    words_open = con.execute("SELECT COUNT(*) FROM issues WHERE severity='word' AND (correction IS NULL OR correction='')").fetchone()[0]
    active = [dict(r) for r in con.execute(
        "SELECT id, rel_path, pages_done, pages_total, started FROM files WHERE status='processing' ORDER BY started")]
    queued_size = st.get("queued", {}).get("size", 0)
    return {"files": st, "pages_done": pages, "pages_per_min": round(recent_rate, 1),
            "issues": sev, "words_open": words_open, "active": active, "queued_bytes": queued_size,
            "paused": db.kv_get(con, "paused", False), "workers": C.WORKERS,
            "export": db.kv_get(con, "last_export"), "exporting": STATE["exporting"], "export_error": STATE["export_error"],
            "input_dir": str(C.INPUT_DIR), "input_available": C.INPUT_DIR.exists() and any(C.INPUT_DIR.iterdir()),
            "output_dir": str(C.OUTPUT_DIR)}


@app.get("/api/issues")
def issues(after: int = 0, severity: str = "error,warning", limit: int = 200, file_id: int | None = None):
    con = db.connect()
    sevs = [s for s in severity.split(",") if s]
    q = ("SELECT i.id, i.file_id, i.page, i.severity, i.kind, i.message, i.created, f.rel_path FROM issues i "
         "JOIN files f ON f.id=i.file_id WHERE i.id>? AND i.severity IN (%s)" % ",".join("?" * len(sevs)))
    args = [after, *sevs]
    if file_id:
        q += " AND i.file_id=?"
        args.append(file_id)
    q += " ORDER BY i.id DESC LIMIT ?"
    args.append(min(limit, 1000))
    return [dict(r) for r in con.execute(q, args)]


@app.get("/api/files")
def files(status: str = "", q: str = "", offset: int = 0, limit: int = 100):
    con = db.connect()
    sql, args = "SELECT id, rel_path, status, size, pages_total, pages_done, ocr_pages, mean_conf, error FROM files WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if q:
        sql += " AND rel_path LIKE ?"
        args.append(f"%{q}%")
    total = con.execute(sql.replace("SELECT id, rel_path, status, size, pages_total, pages_done, ocr_pages, mean_conf, error",
                                    "SELECT COUNT(*)"), args).fetchone()[0]
    rows = [dict(r) for r in con.execute(sql + " ORDER BY rel_path LIMIT ? OFFSET ?", [*args, limit, offset])]
    return {"total": total, "rows": rows}


# ------------------------------------------------------------------ слова на проверку
@app.get("/api/words")
def words(only_open: bool = True, offset: int = 0, limit: int = 60):
    con = db.connect()
    cond = "severity='word'" + (" AND (correction IS NULL OR correction='')" if only_open else "")
    total = con.execute(f"SELECT COUNT(*) FROM issues WHERE {cond}").fetchone()[0]
    rows = [dict(r) for r in con.execute(
        f"SELECT i.id, i.page, i.word, i.conf, i.context, i.crop, i.correction, f.rel_path FROM issues i "
        f"JOIN files f ON f.id=i.file_id WHERE {cond} ORDER BY i.conf, i.id LIMIT ? OFFSET ?", (limit, offset))]
    return {"total": total, "rows": rows}


class Correction(BaseModel):
    correction: str
    apply_all: bool = False


@app.post("/api/words/{issue_id}")
def correct(issue_id: int, c: Correction):
    con = db.connect()
    r = con.execute("SELECT word FROM issues WHERE id=? AND severity='word'", (issue_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    val = c.correction.strip()
    if c.apply_all:
        n = con.execute("UPDATE issues SET correction=? WHERE severity='word' AND word=? AND (correction IS NULL OR correction='')",
                        (val, r["word"])).rowcount
        con.execute("UPDATE issues SET correction=? WHERE id=?", (val, issue_id))
    else:
        n = con.execute("UPDATE issues SET correction=? WHERE id=?", (val, issue_id)).rowcount
    db.kv_set(con, "last_export_ts", 0)  # исправления попадут в результат при следующей сборке
    return {"updated": n}


@app.get("/api/crop/{path:path}")
def crop(path: str):
    p = (C.WORK_DIR / safe_rel(path)).resolve()
    if not str(p).startswith(str(C.WORK_DIR.resolve())) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


# ------------------------------------------------------------------ управление
@app.post("/api/pause")
def pause(paused: bool = True):
    con = db.connect()
    db.kv_set(con, "paused", paused)
    return {"paused": paused}


@app.post("/api/retry")
def retry():
    """Повторить файлы с ошибками и страницы, которые не удалось обработать."""
    con = db.connect()
    n_files = 0
    with db.tx(con):
        for r in con.execute("SELECT id FROM files WHERE status='error'").fetchall():
            con.execute("DELETE FROM issues WHERE file_id=? AND severity='error'", (r["id"],))
            con.execute("UPDATE files SET status='queued', attempts=0, error=NULL WHERE id=?", (r["id"],))
            n_files += 1
        for r in con.execute("SELECT DISTINCT file_id FROM issues WHERE kind IN ('page_error','timeout')").fetchall():
            for pf in (C.WORK_DIR / str(r["file_id"]) / "pages").glob("*.json"):
                if '"method": "error"' in pf.read_text(encoding="utf-8"):
                    pf.unlink()
            con.execute("DELETE FROM issues WHERE file_id=? AND kind IN ('page_error','timeout')", (r["file_id"],))
            con.execute("UPDATE files SET status='queued', attempts=0 WHERE id=? AND status='done'", (r["file_id"],))
            n_files += 1
    return {"requeued": n_files}


@app.post("/api/export")
def export_now():
    info = run_export()
    if info is None:
        raise HTTPException(409, "Сборка уже идёт")
    return info


@app.get("/api/download/report.xlsx")
def dl_report():
    p = C.OUTPUT_DIR / "report.xlsx"
    if not p.exists():
        raise HTTPException(404, "Отчёт ещё не сформирован")
    return FileResponse(p, filename="report.xlsx")


@app.get("/api/download/output.zip")
def dl_zip():
    if not (C.OUTPUT_DIR / "chunks.jsonl").exists():
        raise HTTPException(404, "Результат ещё не сформирован")
    z = C.DATA_DIR / "output.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in C.OUTPUT_DIR.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(C.OUTPUT_DIR))
    return FileResponse(z, filename="llm_corpus.zip")
