#!/bin/bash
# ЗАПАСНОЙ вариант: установка Docker без интернета из файлов на флешке
# (если Docker нельзя поставить из репозитория Astra Linux:  sudo apt install docker.io).
# Запуск:  sudo bash docker_offline/install_docker_offline.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
die() { echo -e "\033[1;31mОШИБКА: $*\033[0m"; exit 1; }
[ "$(id -u)" = 0 ] || die "запустите через sudo"
command -v docker >/dev/null && { echo "Docker уже установлен: $(docker --version)"; exit 0; }

echo "==> Распаковываю Docker в /usr/local/bin"
tar xzf "$DIR"/docker-*.tgz -C /tmp
cp /tmp/docker/* /usr/local/bin/
rm -rf /tmp/docker
mkdir -p /usr/local/lib/docker/cli-plugins
cp "$DIR/docker-compose-linux-x86_64" /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/bin/* /usr/local/lib/docker/cli-plugins/docker-compose

getent group docker >/dev/null || groupadd docker

echo "==> Создаю системную службу docker"
cat > /etc/systemd/system/docker.service <<'EOF'
[Unit]
Description=Docker (установлен с флешки)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd --group docker
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=3
LimitNOFILE=infinity
LimitNPROC=infinity
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now docker
sleep 3
docker info >/dev/null 2>&1 || die "служба не запустилась. Подробности: sudo journalctl -u docker --no-pager | tail -40"
echo -e "\033[1;32mDocker установлен: $(docker --version)\033[0m"
