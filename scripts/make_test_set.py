"""Тестовый набор: нормальные документы + «плохие» случаи (битые, пустые, с паролем, повёрнутый скан и т.д.).
python scripts/make_test_set.py <папка_с_исходными_docs> <куда>
"""
import io
import random
import shutil
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFilter

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
shutil.rmtree(dst, ignore_errors=True)
dst.mkdir(parents=True)

# 1. реальные документы со структурой папок (скан, текстовый PDF, docx, txt)
for p in src.rglob("*"):
    if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".txt"}:
        q = dst / "Нормативка" / p.relative_to(src)
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, q)

bad = dst / "Проблемные"
bad.mkdir()
# 2. битый PDF (обрезан наполовину)
gost = next(src.rglob("*2.105-68*"))
data = gost.read_bytes()
(bad / "обрезанный.pdf").write_bytes(data[: len(data) // 3])
# 3. не PDF, но с расширением .pdf
(bad / "на самом деле текст.pdf").write_text("это не pdf", encoding="utf-8")
# 4. пустой файл
(bad / "пустой.docx").write_bytes(b"")
# 5. PDF с паролем
d = pymupdf.open(next(src.rglob("*ПЗИ-2026*")))
d.save(bad / "с паролем.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="123", owner_pw="456")
# 6. неподдерживаемые форматы
(bad / "чертёж.dwg").write_bytes(b"AC1032" + bytes(100))
(bad / "архив.zip").write_bytes(b"PK\x05\x06" + bytes(18))
# 7. скан, повёрнутый на 90°, плюс страница плохого качества
scan = pymupdf.open(next(src.rglob("*И-ОТК-03*")))
pix = scan[0].get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
out = pymupdf.open()
rot = img.rotate(90, expand=True)
buf = io.BytesIO(); rot.save(buf, "JPEG", quality=70)
p = out.new_page(width=842, height=595); p.insert_image(p.rect, stream=buf.getvalue())
noisy = img.resize((img.width // 2, img.height // 2)).filter(ImageFilter.GaussianBlur(1.2))
px = noisy.load(); rnd = random.Random(1)
for _ in range(noisy.width * noisy.height // 60):
    px[rnd.randrange(noisy.width), rnd.randrange(noisy.height)] = rnd.choice((0, 255, 120))
buf = io.BytesIO(); noisy.save(buf, "JPEG", quality=35)
p = out.new_page(width=595, height=842); p.insert_image(p.rect, stream=buf.getvalue())
out.save(bad / "повёрнутый и плохой скан.pdf")
# 8. многостраничный TIFF
frames = [img, img.rotate(0)]
frames[0].save(dst / "Нормативка" / "скан_многостраничный.tif", save_all=True, append_images=frames[1:], compression="tiff_lzw")
# 9. Excel и CSV
from openpyxl import Workbook
wb = Workbook(); ws = wb.active; ws.title = "Поверка СИ"
ws.append(["Наименование СИ", "Зав. №", "Периодичность, мес.", "Дата поверки", "Ответственный"])
for i in range(1, 80):
    ws.append([f"Штангенциркуль ШЦ-I-{125 + i}", f"{100200 + i}", 12, f"{(i % 28) + 1:02d}.03.2026", "ОТК"])
ws2 = wb.create_sheet("Пустой лист")
wb.save(dst / "Нормативка" / "График поверки СИ.xlsx")
(dst / "Нормативка" / "Перечень.csv").write_text("Код;Наименование;Отдел\nОГК-01;Перечень КД;ОГК\nОТК-05;Регламент поверки;ОТК\n", encoding="cp1251")
# 10. Word со вставленным сканом
from docx import Document
from docx.shared import Cm
doc = Document(); doc.add_heading("Служебная записка", 1)
doc.add_paragraph("Прошу принять к сведению копию инструкции входного контроля (скан ниже).")
t = doc.add_table(rows=2, cols=2); t.cell(0, 0).text = "Отдел"; t.cell(0, 1).text = "Подпись"; t.cell(1, 0).text = "ОТК"; t.cell(1, 1).text = "Иванов"
buf = io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)
doc.add_picture(buf, width=Cm(16))
doc.save(dst / "Нормативка" / "Служебная записка со сканом.docx")
print("Готово:", sum(1 for _ in dst.rglob("*") if _.is_file()), "файлов в", dst)
