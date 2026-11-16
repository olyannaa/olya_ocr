"""Настройки сервиса. Всё задаётся переменными окружения (см. docker-compose.yml)."""
import os
from pathlib import Path


def _int(name, default):
    return int(os.environ.get(name, default))


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/input"))  # смонтированная сетевая папка (необязательно)

UPLOAD_DIR = DATA_DIR / "uploads"   # загруженные через форму файлы (структура папок сохраняется)
WORK_DIR = DATA_DIR / "work"        # промежуточные результаты по страницам (для продолжения после сбоя)
OUTPUT_DIR = DATA_DIR / "output"    # итоговый корпус для LLM
DB_PATH = DATA_DIR / "state.db"

# Процессы обработки. По умолчанию: ядра - 1 (на 6 ядрах = 5)
WORKERS = _int("WORKERS", max(1, (os.cpu_count() or 2) - 1))

# OCR
OCR_LANG = os.environ.get("OCR_LANG", "rus+eng")
OCR_DPI = _int("OCR_DPI", 300)
OCR_MAX_SIDE = _int("OCR_MAX_SIDE", 7000)          # ограничение размера картинки (чертежи А0 и т.п.)
TESSDATA_FAST = os.environ.get("TESSDATA_FAST", "")  # пусто = системные модели Tesseract
TESSDATA_BEST = os.environ.get("TESSDATA_BEST", "/usr/share/tessdata_best")
OCR_MODE = os.environ.get("OCR_MODE", "auto")     # fast | auto (быстрая, точная — для плохих страниц) | best (всё точной, ~5-8x медленнее)
LINE_DROP_CONF = _int("LINE_DROP_CONF", 30)        # строки с уверенностью ниже — мусор (печати, рисунки), в текст не идут
LINE_WORDS_CONF = _int("LINE_WORDS_CONF", 45)      # в строках ниже этого слова на проверку не выносим (вся строка ненадёжна)
PAGE_CONF_RETRY = _int("PAGE_CONF_RETRY", 75)      # средняя уверенность ниже -> повтор (поворот / точная модель)
PAGE_CONF_WARN = _int("PAGE_CONF_WARN", 60)        # ниже -> предупреждение «плохое качество страницы»
WORD_CONF = _int("WORD_CONF", 55)                  # слово с уверенностью ниже -> «на проверку»
MAX_WORDS_PER_PAGE = _int("MAX_WORDS_PER_PAGE", 25)
OCR_TIMEOUT = _int("OCR_TIMEOUT", 300)             # секунд на одну страницу

# Текстовый слой PDF
MIN_TEXT_CHARS = _int("MIN_TEXT_CHARS", 40)

# Надёжность
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 3)             # сколько раз пробовать файл, если процесс падает
CONVERT_TIMEOUT = _int("CONVERT_TIMEOUT", 300)     # конвертация .doc/.rtf/.xls через LibreOffice
FILE_STALL_SECONDS = _int("FILE_STALL_SECONDS", 900)

# Фрагменты для LLM
CHUNK_CHARS = _int("CHUNK_CHARS", 1200)
CHUNK_OVERLAP_LINES = _int("CHUNK_OVERLAP_LINES", 2)

PDF_EXT = {".pdf"}
IMAGE_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}
WORD_EXT = {".docx", ".doc", ".rtf", ".odt"}
EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".ods", ".csv"}
TEXT_EXT = {".txt"}
SUPPORTED = PDF_EXT | IMAGE_EXT | WORD_EXT | EXCEL_EXT | TEXT_EXT

for d in (UPLOAD_DIR, WORK_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)
