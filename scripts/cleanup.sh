#!/usr/bin/env sh
# Desfaz a instalacao. Por padrao remove apenas o que o setup.sh recria:
# units systemd, container e imagem Docker, ambiente virtual e caches.
#
# O .env (token do Telegram) e o state/prices.sqlite3 (historico de precos)
# nao sao recriaveis, entao so saem com as opcoes --env e --state.
#
#   sh scripts/cleanup.sh              # remove o que e recriavel
#   sh scripts/cleanup.sh --state      # inclui o historico de precos
#   sh scripts/cleanup.sh --all -y     # remove tudo, sem perguntar
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

DROP_STATE=no
DROP_ENV=no
DROP_VENV=yes
ASSUME_YES=no

usage() {
  cat <<'TXT'
Uso: sh scripts/cleanup.sh [opcoes]

Sempre remove: units e timers systemd, container, imagem e rede Docker,
caches __pycache__.

  --state       remove tambem state/prices.sqlite3 (apaga o historico)
  --env         remove tambem .env (apaga token e chat do Telegram)
  --all         o mesmo que --state --env
  --keep-venv   preserva o ambiente virtual .venv
  -y, --yes     nao pergunta nada
  -h, --help    mostra esta ajuda
TXT
}

while [ $# -gt 0 ]; do
  case $1 in
    --state) DROP_STATE=yes ;;
    --env) DROP_ENV=yes ;;
    --all)
      DROP_STATE=yes
      DROP_ENV=yes
      ;;
    --keep-venv) DROP_VENV=no ;;
    -y | --yes) ASSUME_YES=yes ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Opcao desconhecida: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

confirm() {
  [ "$ASSUME_YES" = yes ] && return 0
  printf '%s [s/N]: ' "$1"
  IFS= read -r _answer || _answer=''
  case $_answer in
    s* | S* | y* | Y*) return 0 ;;
    *) return 1 ;;
  esac
}

docker_ready() {
  command -v docker >/dev/null 2>&1 &&
    docker compose version >/dev/null 2>&1 &&
    docker info >/dev/null 2>&1
}

observation_count() {
  [ -f "$ROOT/state/prices.sqlite3" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 - "$ROOT/state/prices.sqlite3" <<'PY' 2>/dev/null || :
import sqlite3
import sys

try:
    connection = sqlite3.connect(sys.argv[1])
    print(connection.execute("select count(*) from observations").fetchone()[0])
except Exception:
    pass
PY
}

echo "Projeto: $ROOT"
echo 'Serao removidos: units systemd, container, imagem e rede Docker, caches.'
[ "$DROP_VENV" = yes ] && echo 'Tambem: o ambiente virtual .venv.'
if [ "$DROP_STATE" = yes ]; then
  COUNT=$(observation_count)
  echo "Tambem: state/prices.sqlite3${COUNT:+ (contem $COUNT observacoes)}."
fi
[ "$DROP_ENV" = yes ] && echo 'Tambem: .env, com o token e o chat do Telegram.'
echo

if [ "$DROP_STATE" = yes ] || [ "$DROP_ENV" = yes ]; then
  confirm 'Confirmar a remocao, incluindo os dados nao recriaveis?' || {
    echo 'Nada foi removido.'
    exit 0
  }
fi

# --- systemd -----------------------------------------------------------
# Os nomes com hifen vem da versao anterior do projeto e sao limpos aqui
# para nao sobrar unit orfa de uma instalacao antiga.
echo 'systemd:'
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
FOUND_UNIT=no
for base in tchovis_promos tchovis_promos-docker tchovis_promos-bot \
  tchovis_promos-bot-docker tchovis-promos tchovis-promos-docker; do
  for kind in timer service; do
    unit="$base.$kind"
    if [ -f "$UNIT_DIR/$unit" ]; then
      FOUND_UNIT=yes
      systemctl --user disable --now "$unit" >/dev/null 2>&1 || :
      rm -f "$UNIT_DIR/$unit"
      echo "  $unit removida"
    fi
  done
done
if [ "$FOUND_UNIT" = yes ]; then
  systemctl --user daemon-reload || :
  systemctl --user reset-failed >/dev/null 2>&1 || :
else
  echo '  nenhuma unit instalada'
fi

# --- docker ------------------------------------------------------------
echo 'Docker:'
if docker_ready; then
  (cd "$ROOT" && docker compose down --remove-orphans >/dev/null 2>&1) || :
  for name in tchovis_promos tchovis_promos_bot tchovis-promos; do
    if docker rm -f "$name" >/dev/null 2>&1; then
      echo "  container $name removido"
    fi
  done
  for name in tchovis_promos tchovis-promos; do
    if docker image rm "$name:latest" >/dev/null 2>&1; then
      echo "  imagem $name:latest removida"
    fi
    if docker network rm "${name}_default" >/dev/null 2>&1; then
      echo "  rede ${name}_default removida"
    fi
  done
  echo '  recursos Docker do projeto limpos'
else
  echo '  daemon nao respondeu; container e imagem ficaram intactos'
fi

# --- arquivos locais ---------------------------------------------------
echo 'Arquivos:'
if [ "$DROP_VENV" = yes ] && [ -d "$ROOT/.venv" ]; then
  rm -rf "$ROOT/.venv"
  echo '  .venv removido'
fi
find "$ROOT" -name '__pycache__' -type d -not -path "$ROOT/.git/*" \
  -exec rm -rf {} + 2>/dev/null || :
echo '  caches __pycache__ removidos'

if [ "$DROP_STATE" = yes ] && [ -f "$ROOT/state/prices.sqlite3" ]; then
  rm -f "$ROOT/state/prices.sqlite3"
  echo '  state/prices.sqlite3 removido'
elif [ -f "$ROOT/state/prices.sqlite3" ]; then
  echo '  state/prices.sqlite3 preservado (use --state para remover)'
fi

if [ "$DROP_ENV" = yes ] && [ -f "$ROOT/.env" ]; then
  rm -f "$ROOT/.env"
  echo '  .env removido'
elif [ -f "$ROOT/.env" ]; then
  echo '  .env preservado (use --env para remover)'
fi

echo
echo 'Limpeza concluida. Para instalar de novo: sh scripts/setup.sh'
