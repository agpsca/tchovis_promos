#!/usr/bin/env sh
# Grava as credenciais do Telegram em .env e envia uma mensagem de teste
# usando o conteiner. O token e digitado sem aparecer na tela.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/scripts/_env.sh"

printf 'Token fornecido pelo @BotFather: '
stty -echo
IFS= read -r TOKEN
stty echo
printf '\n'

if [ -z "$TOKEN" ]; then
  echo 'Token vazio; nenhuma alteracao foi feita.' >&2
  exit 1
fi

printf 'Envie uma mensagem qualquer ao seu bot e pressione Enter aqui.'
IFS= read -r _

CHAT_ID=$(TELEGRAM_BOT_TOKEN="$TOKEN" python3 - <<'PY'
import json
import os
import urllib.request

url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getUpdates"
with urllib.request.urlopen(url, timeout=30) as response:
    data = json.load(response)
for update in reversed(data.get("result", [])):
    message = update.get("message") or update.get("channel_post")
    if message and message.get("chat", {}).get("id") is not None:
        print(message["chat"]["id"])
        break
PY
)

if [ -z "$CHAT_ID" ]; then
  echo 'Nao encontrei conversa. Abra o bot, toque em Iniciar, envie uma mensagem e repita.' >&2
  exit 1
fi

# Grava somente as duas chaves do Telegram, preservando as demais linhas do
# .env (por exemplo TCHOVIS_UID e TCHOVIS_GID, usadas pelo Compose).
env_set "$ROOT/.env" TELEGRAM_BOT_TOKEN "$TOKEN"
env_set "$ROOT/.env" TELEGRAM_CHAT_ID "$CHAT_ID"

echo "Credenciais salvas com permissao privada. Chat ID: $CHAT_ID"

cd "$ROOT"
docker compose run --rm watcher --test-telegram
