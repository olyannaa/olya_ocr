"""Загрузка папки через API сервиса (то же, что делает веб-форма). python scripts/upload_folder.py <папка> [url]"""
import sys
from pathlib import Path

import httpx

SUP = {'.pdf','.tif','.tiff','.jpg','.jpeg','.png','.bmp','.docx','.doc','.rtf','.odt','.xlsx','.xlsm','.xls','.ods','.csv','.txt'}
root = Path(sys.argv[1]); base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
files = [p for p in sorted(root.rglob("*")) if p.is_file()]
rel = lambda p: (Path(root.name) / p.relative_to(root)).as_posix()
ok = [p for p in files if p.suffix.lower() in SUP]
c = httpx.Client(base_url=base, timeout=600)
b = c.post("/api/batches", json={"files": [{"path": rel(p), "size": p.stat().st_size} for p in ok],
                                 "skipped": [rel(p) for p in files if p.suffix.lower() not in SUP]}).json()
by = {rel(p): p for p in ok}
for u in b["uploads"]:
    r = c.put(f"/api/upload/{u['file_id']}", content=by[u["path"]].read_bytes())
    print(r.status_code, u["path"])
print("уже были:", len(b["already"]))
