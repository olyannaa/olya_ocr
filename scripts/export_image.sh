#!/bin/sh
# Собрать комплект для переноса на компьютер без интернета (флешка):
#   ./scripts/export_image.sh   ->  dist/olya_ocr/  (образ x86_64, install.sh, Docker для офлайн-установки, инструкция)
set -e
cd "$(dirname "$0")/.."
OUT=dist/olya_ocr
rm -rf "$OUT" && mkdir -p "$OUT/docker_offline"
docker buildx build --platform linux/amd64 -t olya-ocr:1.0 --load .
docker save olya-ocr:1.0 | gzip > "$OUT/olya_ocr_image.tar.gz"
cp deploy/install.sh deploy/ИНСТРУКЦИЯ_Astra_Linux.md "$OUT/"
cp deploy/install_docker_offline.sh "$OUT/docker_offline/"
curl -fsSL -o "$OUT/docker_offline/docker-27.5.1.tgz" https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz
curl -fsSL -o "$OUT/docker_offline/docker-compose-linux-x86_64" https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64
(cd "$OUT" && sha256sum olya_ocr_image.tar.gz install.sh docker_offline/* > SHA256SUMS.txt 2>/dev/null || shasum -a 256 olya_ocr_image.tar.gz install.sh docker_offline/* > SHA256SUMS.txt)
echo "Готово: $OUT — скопируйте папку целиком на флешку"
