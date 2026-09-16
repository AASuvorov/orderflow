#!/usr/bin/env bash
# Установка публикации сайта на GitHub Pages.
#
# Запускать на СЕРВЕРЕ от root, ПОСЛЕ install-tg.sh:
#   bash install-site.sh
#
# Зачем сайт живёт на сервере, а не собирается с ноутбука: он собирается из архива
# публикаций, а публикует сервер. Собирать на ноутбуке значило бы, что сайт
# обновляется только когда ноутбук включён, то есть перестаёт быть автоматическим.
#
# Права на запись даёт deploy key — SSH-ключ, привязанный к одному репозиторию.
# Личный токен здесь был бы хуже: он открывает доступ ко всем репозиториям аккаунта,
# а сервер должен уметь ровно одно — толкать ветку gh-pages.

set -euo pipefail

APP_DIR=/opt/orderflow
DATA_DIR=/var/lib/orderflow
SITE_DIR=$DATA_DIR/site
KEY=/root/.ssh/orderflow_pages
REPO=git@github.com:AASuvorov/orderflow.git

if [ ! -f "$KEY" ]; then
	echo "ОШИБКА: нет ключа $KEY" >&2
	echo "Создать:  ssh-keygen -t ed25519 -N '' -f $KEY" >&2
	echo "Затем публичную часть добавить в Deploy keys репозитория с правом записи." >&2
	exit 1
fi

command -v git >/dev/null || { echo ">>> git"; apt-get install -y -qq git; }

echo ">>> Каталог сайта"
mkdir -p "$SITE_DIR"
cd "$SITE_DIR"
if [ ! -d .git ]; then
	git init -q -b gh-pages
fi
git remote remove origin 2>/dev/null || true
git remote add origin "$REPO"

# Ключ прописывается в конфиг репозитория, а не в ~/.ssh/config: так он действует
# только здесь и не влияет на прочие подключения сервера по SSH.
git config core.sshCommand "ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
git config user.name "orderflow"
git config user.email "orderflow@localhost"

echo ">>> Проверка доступа"
GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
	git ls-remote --heads origin >/dev/null
echo "доступ есть"

echo ">>> systemd"
cat >/etc/systemd/system/orderflow-site.service <<EOF
[Unit]
Description=Сборка и публикация сайта на GitHub Pages
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=ORDERFLOW_DATA=$DATA_DIR
Environment=ORDERFLOW_SITE=$SITE_DIR
Environment=MPLCONFIGDIR=$DATA_DIR/mpl
EnvironmentFile=/etc/orderflow/telegram.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/site_build.py --publish
TimeoutStartSec=300
EOF

cat >/etc/systemd/system/orderflow-site.timer <<'EOF'
[Unit]
Description=Публикация сайта, дважды в сутки

[Timer]
# Дважды: после утреннего поста и после вечерней проверки событий. Чаще смысла нет
# — сайт меняется только когда выходит публикация, а сборка перезаписывает файлы
# целиком, из-за чего каждый лишний запуск создаёт пустой коммит.
OnCalendar=*-*-* 07,18:30:00
Persistent=true
AccuracySec=5m

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now orderflow-site.timer

echo
echo "=== Готово ==="
echo "Сайт:        https://aasuvorov.github.io/orderflow/"
echo "Расписание:  systemctl list-timers orderflow-site"
echo "Логи:        journalctl -u orderflow-site -n 30 --no-pager"
echo "Пробный:     systemctl start orderflow-site.service"
