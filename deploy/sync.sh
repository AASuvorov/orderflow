#!/usr/bin/env bash
# Обмен с сервером сбора тиков.
#
#   bash deploy/sync.sh push root@IP    — отправить код на сервер
#   bash deploy/sync.sh pull root@IP    — забрать накопленные тики на ноутбук
#
# Тики забираются в тот же data/moex_ticks, где их ждут footprint.py и edge.py,
# поэтому после pull анализ запускается без каких-либо правок.

set -euo pipefail

MODE="${1:-}"
HOST="${2:-}"
APP_DIR=/opt/orderflow
DATA_DIR=/var/lib/orderflow

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/src/orderflow"
LOCAL_DATA="$ROOT/data/moex_ticks"

if [ -z "$MODE" ] || [ -z "$HOST" ]; then
	echo "использование: bash deploy/sync.sh {push|pull} user@host" >&2
	exit 1
fi

case "$MODE" in
push)
	echo ">>> Код на $HOST:$APP_DIR"
	ssh "$HOST" "mkdir -p $APP_DIR"
	# Серверу нужны сбор, сторож и публикация: тяжёлый анализ остаётся локально.
	# Публикация здесь потому, что отчёт о сессии читает свежие тики, а на
	# ноутбуке они появляются только после pull.
	rsync -avz "$SRC/moex_ticks.py" "$SRC/moex.py" "$SRC/watchdog.py" \
		"$SRC/tg_post.py" "$SRC/tg_video.py" "$SRC/tg_events.py" \
		"$SRC/funding.py" "$SRC/mm_screen.py" "$SRC/moex_feasibility.py" \
		"$HOST:$APP_DIR/"
	rsync -avz "$ROOT/deploy/install.sh" "$ROOT/deploy/install-tg.sh" \
		"$HOST:$APP_DIR/"
	echo
	echo "Дальше на сервере:"
	echo "  ssh $HOST 'bash $APP_DIR/install.sh'      # сбор тиков"
	echo "  ssh $HOST 'bash $APP_DIR/install-tg.sh'   # публикация в Telegram"
	;;
pull)
	# Сначала в промежуточный каталог, потом объединение по TRADENO: так
	# серверные файлы не затрут то, что уже собрано локально.
	INCOMING="$ROOT/data/moex_ticks_incoming"
	echo ">>> Тики с $HOST в промежуточный каталог"
	mkdir -p "$INCOMING" "$LOCAL_DATA"
	rsync -avz --progress "$HOST:$DATA_DIR/moex_ticks/" "$INCOMING/"

	echo
	echo ">>> Объединение с локальной базой"
	cd "$SRC" && uv run python moex_ticks.py merge "$INCOMING"
	rm -rf "$INCOMING"
	;;
*)
	echo "неизвестный режим: $MODE (ожидается push или pull)" >&2
	exit 1
	;;
esac
