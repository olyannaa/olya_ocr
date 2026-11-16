"""Очередь обработки: N процессов-обработчиков + надзиратель.

* каждый файл обрабатывается в отдельном процессе — падение библиотеки на «плохом» файле
  не роняет сервис: надзиратель перезапускает процесс, файл возвращается в очередь
  (не более MAX_ATTEMPTS раз, дальше — ошибка «файл пропущен»);
* ошибка одного файла/страницы записывается и обработка идёт дальше;
* после перезапуска контейнера незавершённые файлы продолжаются с той страницы, где остановились.
"""
import multiprocessing as mp
import os
import signal
import threading
import time
import traceback

from . import config as C
from . import db

_ctx = mp.get_context("spawn")


def worker_loop(slot: int):
    os.environ["OMP_THREAD_LIMIT"] = "1"  # Tesseract: 1 поток на процесс, параллельность — процессами
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from .extract import FileError, process_file  # импорт внутри процесса
    con = db.connect()
    pid = os.getpid()
    while True:
        if db.kv_get(con, "paused", False):
            time.sleep(2)
            continue
        row = db.claim_next(con, pid)
        if row is None:
            time.sleep(2)
            continue
        try:
            res = process_file(con, row)
            con.execute("UPDATE files SET status='done', pages_total=?, ocr_pages=?, chars=?, mean_conf=?, finished=?, "
                        "error=NULL WHERE id=?",
                        (res["pages_total"], res["ocr_pages"], res["chars"], res["mean_conf"], time.time(), row["id"]))
        except FileError as e:
            con.execute("UPDATE files SET status='error', error=?, finished=? WHERE id=?",
                        (e.message, time.time(), row["id"]))
            db.add_issue(con, row["id"], "error", e.kind, e.message)
        except Exception as e:  # noqa: BLE001
            msg = f"Непредвиденная ошибка: {type(e).__name__}: {str(e)[:400]}"
            con.execute("UPDATE files SET status='error', error=?, finished=? WHERE id=?",
                        (msg, time.time(), row["id"]))
            db.add_issue(con, row["id"], "error", "internal", msg)
            traceback.print_exc()


class Supervisor:
    def __init__(self):
        self.procs: dict[int, mp.Process] = {}
        self.stop = False

    def start(self):
        con = db.init()
        # плановый перезапуск не считаем «падением» файла
        con.execute("UPDATE files SET attempts=MAX(attempts-1,0) WHERE status='processing'")
        n = db.requeue_interrupted(con, reason="обработка прервалась (перезапуск сервиса)")
        if n:
            print(f"Продолжаю обработку: {n} незавершённых файлов возвращены в очередь", flush=True)
        for s in range(C.WORKERS):
            self._spawn(s)
        threading.Thread(target=self._watch, daemon=True).start()

    def _spawn(self, slot):
        p = _ctx.Process(target=worker_loop, args=(slot,), daemon=True)
        p.start()
        self.procs[slot] = p

    def _watch(self):
        con = db.connect()
        while not self.stop:
            time.sleep(3)
            for slot, p in list(self.procs.items()):
                if not p.is_alive():
                    # процесс упал (segfault, нехватка памяти) — файл обратно в очередь
                    rows = con.execute("SELECT id FROM files WHERE status='processing' AND worker=?", (p.pid,)).fetchall()
                    for r in rows:
                        db.requeue_interrupted(con, r["id"], reason=f"процесс обработки аварийно завершился (код {p.exitcode})")
                    self._spawn(slot)
            # зависание: нет прогресса дольше FILE_STALL_SECONDS
            stalled = con.execute("SELECT id, worker FROM files WHERE status='processing' AND heartbeat < ?",
                                  (time.time() - C.FILE_STALL_SECONDS,)).fetchall()
            for r in stalled:
                for slot, p in self.procs.items():
                    if p.pid == r["worker"] and p.is_alive():
                        p.kill()  # следующая итерация перезапустит процесс и вернёт файл в очередь

    def workers_info(self):
        return [{"slot": s, "pid": p.pid, "alive": p.is_alive()} for s, p in self.procs.items()]

    def shutdown(self):
        self.stop = True
        for p in self.procs.values():
            p.kill()
