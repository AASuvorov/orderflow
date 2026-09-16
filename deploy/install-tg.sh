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

for m in tg_post.py tg_events.py tg_board.py funding.py mm_screen.py \
	moex_feasibility.py moex.py; do
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
Description=Ежедневный пост, понедельник–суббота

[Timer]
# 06:00 UTC = 09:00 МСК. Суббота включена: в этот день выходит недельный
# дайджест. Воскресенье пропускает и таймер, и расписание в tg_post.py —
# держать список дней в двух местах согласованным обязательно, иначе отчёт
# просто не выйдет и никакой ошибки при этом не будет.
OnCalendar=Mon-Sat 06:00:00
# Догоняет пропущенный запуск: пост с опозданием лучше, чем разрыв в расписании.
Persistent=true
AccuracySec=1m

[Install]
WantedBy=timers.target
EOF

# Живая сводка в закрепе. Отдельным юнитом, а не внутри ежедневного поста, потому
# что закреп надо освежать часто, а постить часто нельзя.
cat >/etc/systemd/system/orderflow-board.service <<EOF
[Unit]
Description=Обновление живой сводки в закрепе
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=ORDERFLOW_DATA=$DATA_DIR
Environment=MPLCONFIGDIR=$DATA_DIR/mpl
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/tg_board.py
TimeoutStartSec=180
EOF

cat >/etc/systemd/system/orderflow-board.timer <<'EOF'
[Unit]
Description=Живая сводка в закрепе, каждый час

[Timer]
# Каждый час: правка закрепа не шлёт уведомлений, поэтому частота никого не
# беспокоит, а цифры в первом же экране канала всегда свежие. Persistent здесь не
# нужен — пропущенное обновление бессмысленно догонять, следующее актуальнее.
OnCalendar=hourly
AccuracySec=2m

[Install]
WantedBy=timers.target
EOF

# Проверка событий в течение дня. Запускается ПОСЛЕ ежедневного поста: в 09:00 МСК
# события проверяет сам tg_post.py, и если событие есть, оно вытесняет отчёт дня.
cat >/etc/systemd/system/orderflow-events.service <<EOF
[Unit]
Description=Проверка событий и публикация, если есть повод
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=ORDERFLOW_DATA=$DATA_DIR
Environment=MPLCONFIGDIR=$DATA_DIR/mpl
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/tg_events.py
# Перекос фандинга считается по фактическим выплатам — запрос на контракт, около
# минуты на всю выборку.
TimeoutStartSec=600
EOF

cat >/etc/systemd/system/orderflow-events.timer <<'EOF'
[Unit]
Description=Проверка событий днём, каждые три часа

[Timer]
# 08:00–17:00 UTC = 11:00–20:00 МСК, четыре проверки. Ночью не проверяем: пост в
# три часа ночи прочтут единицы, а охват делится на всех подписчиков независимо
# от того, спали они или нет. Суточный предел постов задан в tg_events.py, здесь
# только частота попыток — событий может не быть вовсе, и это нормальный исход.
OnCalendar=Mon-Sat 08,11,14,17:00:00
Persistent=false
AccuracySec=5m

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now orderflow-tg.timer
systemctl enable --now orderflow-board.timer
systemctl enable --now orderflow-events.timer

echo
echo "=== Готово ==="
echo "Расписание:   systemctl list-timers 'orderflow-*'"
echo "Логи поста:   journalctl -u orderflow-tg -n 50 --no-pager"
echo "Логи событий: journalctl -u orderflow-events -n 50 --no-pager"
echo "Логи закрепа: journalctl -u orderflow-board -n 20 --no-pager"
echo "Проверка:     set -a; . $ENV_FILE; set +a; \\"
echo "              ORDERFLOW_DATA=$DATA_DIR $APP_DIR/.venv/bin/python $APP_DIR/tg_post.py check"
echo "Пробный пост: systemctl start orderflow-tg.service"
