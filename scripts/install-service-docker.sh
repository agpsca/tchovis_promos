#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ ! -s "$ROOT/.env" ]; then
  echo 'Configure o Telegram primeiro: sh scripts/configure-telegram-docker.sh' >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo 'Docker Compose nao encontrado. Instale o Docker primeiro.' >&2
  exit 1
fi

mkdir -p "$USER_SYSTEMD_DIR"
# O Compose monta ./state como volume; sem o diretorio o Docker o cria como
# root e o conteiner nao consegue gravar o banco.
mkdir -p "$ROOT/state"
sed "s|@PROJECT_ROOT@|$ROOT|g" "$ROOT/systemd/tchovis_promos-docker.service" \
  > "$USER_SYSTEMD_DIR/tchovis_promos-docker.service"
cp "$ROOT/systemd/tchovis_promos-docker.timer" \
  "$USER_SYSTEMD_DIR/tchovis_promos-docker.timer"

systemctl --user daemon-reload
systemctl --user enable --now tchovis_promos-docker.timer

echo 'Timer do Tchovis Promos em conteiner ativado.'
systemctl --user list-timers tchovis_promos-docker.timer --no-pager
