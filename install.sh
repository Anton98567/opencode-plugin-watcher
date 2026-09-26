#!/usr/bin/env bash
#
# Установка OpenCode Plugin Watcher.
#
#   ./install.sh [weekly|daily|hourly]
#
# От root ставит в /opt/opencode-plugin-watcher и включает systemd-таймер.
# Без root ставит в ~/opencode-plugin-watcher и печатает строку для cron.
#
# Переопределить путь и пользователя:
#   INSTALL_DIR=/srv/watcher SERVICE_USER=myuser ./install.sh daily

set -euo pipefail

SCHEDULE="${1:-weekly}"
SERVICE_USER="${SERVICE_USER:-opencode-watcher}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || true)"

case "$SCHEDULE" in
  weekly) ONCAL="Sun *-*-* 07:00:00 UTC" ;;
  daily)  ONCAL="Daily *-*-* 07:00:00 UTC" ;;
  hourly) ONCAL="hourly" ;;
  *) echo "Расписание: weekly | daily | hourly (получено: $SCHEDULE)" >&2; exit 1 ;;
esac

if [[ -z "$PYTHON" ]]; then
  echo "Не найден python3. Нужен Python 3.9 или новее." >&2
  exit 1
fi

PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" - <<'EOF' || { echo "Нужен Python 3.9 или новее." >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
EOF
echo "==> Python $PYVER, режим: $SCHEDULE"

if [[ $EUID -eq 0 ]]; then
  INSTALL_DIR="${INSTALL_DIR:-/opt/opencode-plugin-watcher}"
else
  INSTALL_DIR="${INSTALL_DIR:-$HOME/opencode-plugin-watcher}"
fi

if [[ $EUID -eq 0 && ! -d /run/systemd/system ]]; then
  echo "!! systemd не найден — поставлю файлы и напечатаю строку для cron." >&2
fi

echo "==> Установка в $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/outbox"
cp "$SRC_DIR/watcher.py" "$SRC_DIR/config.json" "$INSTALL_DIR/"
cp "$SRC_DIR/README.md" "$INSTALL_DIR/" 2>/dev/null || true

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$SRC_DIR/.env.example" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
  echo "   создан $INSTALL_DIR/.env — впишите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"
else
  echo "   $INSTALL_DIR/.env уже есть, не трогаю"
fi

# Первый запуск создаёт SQLite со всеми таблицами
(cd "$INSTALL_DIR" && "$PYTHON" watcher.py stats >/dev/null 2>&1 || true)

# Непривилегированный пользователь для systemd-юнита
HAVE_SYSTEMD=0
if [[ $EUID -eq 0 && -d /run/systemd/system ]]; then
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    if command -v useradd >/dev/null 2>&1; then
      useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null \
        || useradd --system --no-create-home --shell /sbin/nologin "$SERVICE_USER"
      echo "   создан системный пользователь $SERVICE_USER"
    else
      echo "!! useradd не найден — юнит будет запускаться от root"
      SERVICE_USER="root"
    fi
  fi

  chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" 2>/dev/null || true
  chmod 700 "$INSTALL_DIR/data" "$INSTALL_DIR/outbox"
  chmod 600 "$INSTALL_DIR/.env"

  sed -e "s#/opt/opencode-plugin-watcher#${INSTALL_DIR}#g" \
      -e "s#^User=.*#User=${SERVICE_USER}#" \
      "$SRC_DIR/systemd/opencode-watcher.service" > /etc/systemd/system/opencode-watcher.service
  sed -e "s#^OnCalendar=.*#OnCalendar=${ONCAL}#" \
      "$SRC_DIR/systemd/opencode-watcher.timer" > /etc/systemd/system/opencode-watcher.timer

  systemctl daemon-reload
  systemctl enable --now opencode-watcher.timer
  HAVE_SYSTEMD=1
  echo "==> Таймер включён"
  systemctl list-timers opencode-watcher.timer --no-pager || true
else
  chown -R "$(id -un)" "$INSTALL_DIR" 2>/dev/null || true
fi

echo
echo "Готово. Дальше:"
echo "  1) nano $INSTALL_DIR/.env            # TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"
echo "  2) cd $INSTALL_DIR"
echo "  3) python3 watcher.py chat-id        # узнать chat_id, если ещё не знаете"
echo "  4) python3 watcher.py send-test      # проверка отправки"
echo "  5) python3 watcher.py run            # первый дайджест"
if [[ $HAVE_SYSTEMD -eq 1 ]]; then
  echo
  echo "  Следующий автозапуск:"
  systemctl list-timers opencode-watcher.timer --no-pager | head -3
  echo "  Запустить сейчас:  systemctl start opencode-watcher.service"
  echo "  Логи:             journalctl -u opencode-watcher.service -n 100 --no-pager"
else
  echo
  echo "  Для cron ($SCHEDULE):"
  echo "  crontab -e и добавить, подставив время под свой часовой пояс:"
  case "$SCHEDULE" in
    weekly) echo "  17 8 * * 1  cd $INSTALL_DIR && $PYTHON watcher.py run >> /tmp/opencode-watcher.log 2>&1" ;;
    daily)  echo "  17 8 * * *  cd $INSTALL_DIR && $PYTHON watcher.py run >> /tmp/opencode-watcher.log 2>&1" ;;
    hourly) echo "  17 * * * *  cd $INSTALL_DIR && $PYTHON watcher.py run >> /tmp/opencode-watcher.log 2>&1" ;;
  esac
fi
