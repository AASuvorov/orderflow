#!/usr/bin/env bash
# Установка ежедневной публикации замеров в Telegram.
#
# Запускать на СЕРВЕРЕ от root, ПОСЛЕ install.sh:
#   bash install-tg.sh
#
# Почему отдельным скриптом, а не внутри install.sh: сбор тиков и публикация —
# разные по критичности вещи. Сбор нельзя прерывать, пропущенная сессия не
# восстанавливается; пропущенный пост восстанавливается сам на следующий день.
# Разделение позволяет переустанавливать публикацию, не трогая сборщик.
#
# Публикация живёт на сервере, а не на ноутбуке, потому что отчёт о сессии МОЕХ
# читает те же тики, что собираются здесь: на ноутбуке они появляются только
# после sync.sh pull, то есть с задержкой в дни.

set -euo pipefail

APP_DIR=/opt/orderflow
DATA_DIR=/var/lib/orderflow
ENV_FILE=/etc/orderflow/telegram.env

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
	echo "ОШИБКА: сначала install.sh — нет окружения в $APP_DIR/.venv" >&2
	exit 1
fi

for m in tg_post.py funding.py mm_screen.py moex_feasibility.py moex.py; do
	if [ ! -f "$APP_DIR/$m" ]; then
		echo "ОШИБКА: нет $APP_DIR/$m" >&2
		echo "С ноутбука: bash deploy/sync.sh push root@IP" >&2
		exit 1
	fi
done

# Часовой пояс фиксируем в UTC, чтобы расписание читалось однозначно. У МСК нет
# перехода на летнее время, поэтому 06:00 UTC — это 09:00 МСК круглый год.
CURRENT_TZ="$(timedatectl show --property=Timezone --value 2>/dev/null || echo unknown)"
if [ "$CURRENT_TZ" != "UTC" ] && [ "$CURRENT_TZ" != "Etc/UTC" ]; then
	echo ">>> Часовой пояс: $CURRENT_TZ -> UTC"
	timedatectl set-timezone UTC
fi

echo ">>> Зависимости для графиков"
# Сбор тиков обходится polars и requests; графики требуют ещё matplotlib.
# Шрифт DejaVu идёт в комплекте с matplotlib и содержит кириллицу — отдельно
# ставить ничего не нужно.
"$APP_DIR/.venv/bin/pip" install -q matplotlib numpy

echo ">>> Токен"
mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
	cat >"$ENV_FILE" <<'EOF'
# Токен от @BotFather и канал, куда публиковать.
TG_BOT_TOKEN=ВСТАВЬТЕ_ТОКЕН
TG_CHAT_ID=@tradingnadannyh
EOF
	echo "создан $ENV_FILE — впишите токен перед первым запуском"
fi
# Токен даёт полный контроль над каналом, поэтому файл читает только root.
# Unit-файлы systemd доступны на чтение всем, класть токен туда нельзя.
chmod 600 "$ENV_FILE"

echo ">>> systemd"
cat >/etc/systemd/system/orderflow-tg.service <<EOF
[Unit]
Description=Ежедневная публикация замеров в Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=ORDERFLOW_DATA=$DATA_DIR
Environment=MPLCONFIGDIR=$DATA_DIR/mpl
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/tg_post.py daily
# Скрининг спредов снимает стакан несколько раз с паузами, это самый долгий отчёт.
TimeoutStartSec=600
EOF

cat >/etc/systemd/system/orderflow-tg.timer <<'EOF'
[Unit]
Description=Утренний пост по будням

[Timer]
# 06:00 UTC = 09:00 МСК. Выходные пропускает сам tg_post.py, но и таймер
# ограничен буднями: так пропуск виден в list-timers, а не только в логах.
OnCalendar=Mon-Fri 06:00:00
# Догоняет пропущенный запуск: пост с опозданием лучше, чем разрыв в расписании.
Persistent=true
AccuracySec=1m

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now orderflow-tg.timer

echo
echo "=== Готово ==="
echo "Расписание:   systemctl list-timers orderflow-tg"
echo "Логи:         journalctl -u orderflow-tg -n 50 --no-pager"
echo "Проверка:     set -a; . $ENV_FILE; set +a; \\"
echo "              ORDERFLOW_DATA=$DATA_DIR $APP_DIR/.venv/bin/python $APP_DIR/tg_post.py check"
echo "Пробный пост: systemctl start orderflow-tg.service"
