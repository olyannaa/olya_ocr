#!/bin/bash
# Установка / обновление сервиса распознавания документов olya_ocr.
# Запуск из папки с флешки:   sudo bash install.sh
# Повторный запуск = обновление: распознанные данные и очередь сохраняются.
set -e

PORT="${PORT:-8080}"                 # порт веб-формы; другой: sudo PORT=8081 bash install.sh
WORKERS="${WORKERS:-5}"              # процессов распознавания (ядер минус одно)
MEMORY="${MEMORY:-12g}"              # предел памяти контейнера (на 16 ГБ — 12g)
OCR_MODE="${OCR_MODE:-auto}"         # fast | auto | best
HOME_DIR="${OCR_HOME:-/opt/olya_ocr}"  # здесь будут папки output (результат) и input
IMAGE=olya-ocr:1.0
NAME=olya_ocr
SRC="$(cd "$(dirname "$0")" && pwd)"

say() { echo -e "\n\033[1;34m==> $*\033[0m"; }
die() { echo -e "\n\033[1;31mОШИБКА: $*\033[0m"; exit 1; }

[ "$(id -u)" = 0 ] || die "запустите через sudo:  sudo bash install.sh"
[ "$(uname -m)" = x86_64 ] || die "нужен компьютер x86_64 (сейчас $(uname -m))"
command -v docker >/dev/null || die "Docker не установлен. См. инструкцию, шаг 2 (или: sudo bash docker_offline/install_docker_offline.sh)"
docker info >/dev/null 2>&1 || { systemctl start docker 2>/dev/null || true; sleep 3; docker info >/dev/null 2>&1 || die "служба Docker не запущена: sudo systemctl start docker"; }

FREE_GB=$(df -BG --output=avail /var/lib/docker 2>/dev/null | tail -1 | tr -dc 0-9)
[ -n "$FREE_GB" ] && [ "$FREE_GB" -lt 10 ] && die "мало места на диске: ${FREE_GB} ГБ (нужно от 10 ГБ + объём документов)"

say "1/4 Загружаю образ в Docker (1–3 минуты)…"
[ -f "$SRC/olya_ocr_image.tar.gz" ] || die "нет файла olya_ocr_image.tar.gz рядом со скриптом"
docker load -i "$SRC/olya_ocr_image.tar.gz"

say "2/4 Готовлю папки в $HOME_DIR"
mkdir -p "$HOME_DIR/output" "$HOME_DIR/input"
cp -f "$SRC/install.sh" "$SRC"/ИНСТРУКЦИЯ* "$HOME_DIR/" 2>/dev/null || true
chmod 755 "$HOME_DIR" "$HOME_DIR/output" "$HOME_DIR/input"

say "3/4 Запускаю сервис"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --restart unless-stopped \
  -p "$PORT:8080" \
  --memory "$MEMORY" \
  -e WORKERS="$WORKERS" -e OCR_MODE="$OCR_MODE" \
  -v olya_ocr_data:/data \
  -v "$HOME_DIR/output:/data/output" \
  -v "$HOME_DIR/input:/input:ro" \
  "$IMAGE" >/dev/null

say "4/4 Проверяю, что сервис отвечает…"
for i in $(seq 1 30); do
  if docker exec "$NAME" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/status',timeout=3)" >/dev/null 2>&1; then
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo -e "\n\033[1;32mГОТОВО. Сервис работает.\033[0m"
    echo "  Откройте в браузере:        http://localhost:$PORT"
    [ -n "$IP" ] && echo "  С других компьютеров сети:  http://$IP:$PORT"
    echo "  Результат распознавания:    $HOME_DIR/output/chunks.jsonl"
    echo "  Сервис сам запускается после перезагрузки компьютера."
    exit 0
  fi
  sleep 2
done
docker logs --tail 30 "$NAME"
die "сервис не ответил за 60 секунд — пришлите вывод команды: sudo docker logs $NAME"
