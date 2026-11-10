#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ ! -f "$ROOT/.env" ]; then
  echo 'Configure o Telegram primeiro: scripts/configure-telegram.sh' >&2
  exit 1
fi

mkdir -p "$USER_SYSTEMD_DIR"
sed "s|@PROJECT_ROOT@|$ROOT|g" "$ROOT/systemd/tchovis_promos.service" > "$USER_SYSTEMD_DIR/tchovis_promos.service"
cp "$ROOT/systemd/tchovis_promos.timer" "$USER_SYSTEMD_DIR/tchovis_promos.timer"

systemctl --user daemon-reload
systemctl --user enable --now tchovis_promos.timer

echo 'Timer Tchovis Promos ativado.'
systemctl --user list-timers tchovis_promos.timer --no-pager
