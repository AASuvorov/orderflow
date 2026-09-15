#!/usr/bin/env bash
# Установка сборщика тиков МОЕХ на сервер (Ubuntu/Debian).
#
# Запускать на СЕРВЕРЕ от root:
#   bash install.sh
#
# Что делает: ставит python-окружение, кладёт код в /opt/orderflow, создаёт
# systemd-таймер на запуск каждые 15 минут. Скрипт сбора сам выходит вне
# торговых часов МОЕХ, поэтому таймер может тикать круглосуточно.
#
# Часовой пояс сервера не важен: сборщик считает время сессии в МСК сам.

set -euo pipefail

APP_DIR=/opt/orderflow
DATA_DIR=/var/lib/orderflow
# Список отобран по издержкам полного круга (см. moex_feasibility.py), а не по
# оборотам: Eu 0.54, Si 0.59, GD 0.94, ED 1.30, MM 1.43, GN 1.43, MX 1.51,
# BR 1.84, CR 2.63 б.п. Дешёвые контракты дают низкий порог безубыточности.
CONTRACTS="${CONTRACTS:-Eu Si GD ED MM GN MX BR CR}"

echo ">>> Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync >/dev/null

echo ">>> Каталоги"
mkdir -p "$APP_DIR" "$DATA_DIR"

# На тарифах с 1 ГБ памяти swap страхует от обрыва первой полной выкачки сессии.
if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
	echo ">>> Swap 1 ГБ"
	fallocate -l 1G /swapfile
	chmod 600 /swapfile
	mkswap -q /swapfile
	swapon /swapfile
	grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

if [ ! -f "$APP_DIR/moex_ticks.py" ]; then
	echo "ОШИБКА: положите moex_ticks.py и moex.py в $APP_DIR перед запуском." >&2
	echo "С ноутбука: bash deploy/sync.sh push root@IP" >&2
	exit 1
fi

echo ">>> Python-окружение"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q polars requests

echo ">>> systemd"
cat >/etc/systemd/system/orderflow-ticks.service <<EOF
[Unit]
Description=Сбор тиков МОЕХ со стороной агрессора
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=ORDERFLOW_DATA=$DATA_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/moex_ticks.py $CONTRACTS
# Сессия длинная, но один проход укладывается в пару минут.
TimeoutStartSec=900
EOF

cat >/etc/systemd/system/orderflow-ticks.timer <<'EOF'
[Unit]
Description=Запуск сбора тиков МОЕХ каждые 15 минут

[Timer]
OnCalendar=*:0/15
# Догоняет пропущенный запуск, если сервер был недоступен.
Persistent=true
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

# Сторож: ловит молчаливый отказ сбора, который иначе обнаружится через месяц.
cat >/etc/systemd/system/orderflow-watchdog.service <<EOF
[Unit]
Description=Проверка целостности собранных тиков МОЕХ

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=ORDERFLOW_DATA=$DATA_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/watchdog.py
TimeoutStartSec=600
EOF

cat >/etc/systemd/system/orderflow-watchdog.timer <<'EOF'
[Unit]
Description=Ежедневная проверка сбора тиков

[Timer]
# 20:30 UTC = 23:30 МСК, после закрытия вечерней сессии.
OnCalendar=*-*-* 20:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now orderflow-ticks.timer
systemctl enable --now orderflow-watchdog.timer

# Автоматические обновления безопасности: сервер должен жить без присмотра.
apt-get install -y -qq unattended-upgrades >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

echo ">>> Пробный запуск"
systemctl start orderflow-ticks.service || true
sleep 5

echo
echo "=== Готово ==="
echo "Данные:       $DATA_DIR/moex_ticks"
echo "Расписание:   systemctl list-timers orderflow-ticks"
echo "Логи:         journalctl -u orderflow-ticks -n 50 --no-pager"
echo "Состояние:    ORDERFLOW_DATA=$DATA_DIR $APP_DIR/.venv/bin/python $APP_DIR/moex_ticks.py status"
