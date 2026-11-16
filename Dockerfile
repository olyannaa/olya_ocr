# Сервис подготовки документов для LLM.
# Всё нужное (Tesseract + русский язык, точные модели OCR, LibreOffice, Python-библиотеки)
# скачивается ОДИН раз при сборке образа. Дальше работа полностью без интернета.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_THREAD_LIMIT=1 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng tesseract-ocr-osd \
        libreoffice-writer-nogui libreoffice-calc-nogui \
        fonts-dejavu-core fonts-liberation \
        curl ca-certificates \
    && mkdir -p /usr/share/tessdata_best \
    && for l in rus eng osd; do \
         curl -fsSL --retry 5 -o /usr/share/tessdata_best/$l.traineddata \
           https://github.com/tesseract-ocr/tessdata_best/raw/main/$l.traineddata || exit 1; \
       done \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web

ENV DATA_DIR=/data INPUT_DIR=/input TESSDATA_BEST=/usr/share/tessdata_best
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/status',timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "120"]
