#!/usr/bin/env sh
# Instalacao completa em um comando: prepara o diretorio de estado, monta o
# ambiente de execucao, grava as credenciais do Telegram se faltarem e
# coloca o bot no ar.
#
#   sh scripts/setup.sh                # escolhe conteiner ou local sozinho
#   sh scripts/setup.sh --docker       # forca conteiner
#   sh scripts/setup.sh --native       # forca Python local
#   sh scripts/setup.sh --timer        # agendamento antigo, sem bot
#   sh scripts/setup.sh --no-schedule  # so prepara o ambiente
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/scripts/_env.sh"

MODE=auto
SCHEDULE=bot

usage() {
  cat <<'TXT'
Uso: sh scripts/setup.sh [opcoes]

  --docker        instala a versao em conteiner (Docker Compose)
  --native        instala a versao local (venv + Playwright no host)
  --bot           deixa o bot do Telegram no ar (padrao)
  --timer         usa o timer de 30 minutos, sem bot
  --no-schedule   prepara o ambiente sem ligar nada
  -h, --help      mostra esta ajuda

Sem --docker nem --native, usa conteiner quando o daemon do Docker
responde e Python local caso contrario.

O bot atende /monitorar, /lista e /agora e faz a coleta periodica por
dentro, o que dispensa o timer.
TXT
}

while [ $# -gt 0 ]; do
  case $1 in
    --docker) MODE=docker ;;
    --native) MODE=native ;;
    --bot) SCHEDULE=bot ;;
    --timer) SCHEDULE=timer ;;
    --no-schedule | --no-timer) SCHEDULE=none ;;
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

docker_ready() {
  command -v docker >/dev/null 2>&1 &&
    docker compose version >/dev/null 2>&1 &&
    docker info >/dev/null 2>&1
}

if [ "$MODE" = auto ]; then
  if docker_ready; then
    MODE=docker
    echo 'Modo escolhido: conteiner (o daemon do Docker respondeu).'
  else
    MODE=native
    echo 'Modo escolhido: Python local (o daemon do Docker nao respondeu).'
  fi
fi

# O Compose monta ./state como volume. Se o diretorio nao existir, o Docker
# o cria como root e o conteiner, que roda com UID comum, nao consegue
# gravar o SQLite.
mkdir -p "$ROOT/state"
echo "Diretorio de estado pronto: $ROOT/state"

if [ "$MODE" = docker ]; then
  if ! docker_ready; then
    echo 'Docker Compose nao respondeu. Instale o Docker, coloque seu' >&2
    echo 'usuario no grupo docker e tente de novo.' >&2
    exit 1
  fi

  # O conteiner roda com o UID do host para que o banco no volume fique
  # legivel fora dele. O Compose le estas chaves do .env.
  env_set "$ROOT/.env" TCHOVIS_UID "$(id -u)"
  env_set "$ROOT/.env" TCHOVIS_GID "$(id -g)"
  echo "UID/GID gravados no .env: $(id -u):$(id -g)"

  echo 'Construindo a imagem...'
  (cd "$ROOT" && docker compose build)
else
  if [ -x "$ROOT/.venv/bin/python" ] &&
    "$ROOT/.venv/bin/python" -c 'import playwright' 2>/dev/null; then
    echo 'Ambiente virtual ja instalado; pulando.'
  else
    echo 'Instalando o ambiente virtual e o Firefox do Playwright...'
    sh "$ROOT/scripts/install.sh"
  fi
fi

if [ -z "$(env_get "$ROOT/.env" TELEGRAM_BOT_TOKEN)" ]; then
  echo
  echo 'Credenciais do Telegram ausentes; vamos configurar agora.'
  if [ "$MODE" = docker ]; then
    sh "$ROOT/scripts/configure-telegram-docker.sh"
  else
    sh "$ROOT/scripts/configure-telegram.sh"
  fi
else
  echo 'Credenciais do Telegram ja presentes no .env; mantidas como estao.'
fi

if [ "$SCHEDULE" = bot ]; then
  UNIT=tchovis_promos-bot
  [ "$MODE" = docker ] && UNIT=tchovis_promos-bot-docker
  USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$USER_SYSTEMD_DIR"
  sed "s|@PROJECT_ROOT@|$ROOT|g" "$ROOT/systemd/$UNIT.service" \
    > "$USER_SYSTEMD_DIR/$UNIT.service"
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT.service"
  echo "Bot no ar: $UNIT.service"
  systemctl --user --no-pager --lines=0 status "$UNIT.service" || :
elif [ "$SCHEDULE" = timer ]; then
  if [ "$MODE" = docker ]; then
    sh "$ROOT/scripts/install-service-docker.sh"
  else
    sh "$ROOT/scripts/install-service.sh"
  fi
  UNIT=tchovis_promos
  [ "$MODE" = docker ] && UNIT=tchovis_promos-docker
else
  echo 'Nada agendado (--no-schedule).'
  UNIT=tchovis_promos
fi

if [ "$SCHEDULE" != none ]; then
  # Sem linger, o gerenciador de sessao do usuario morre no logout e leva o
  # bot junto. Em servidor sem sessao aberta isso e obrigatorio.
  if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo no)" != yes ]; then
    echo
    echo 'ATENCAO: linger desligado. Isso para o monitor quando a sessao fecha.'
    echo "Habilite com: sudo loginctl enable-linger $(id -un)"
  fi
fi

if [ "$SCHEDULE" = bot ]; then
  cat <<TXT

Instalacao concluida no modo $MODE, com o bot ligado.

No Telegram:
  /ajuda                                      lista os comandos
  /monitorar cadeira gamer ate 500 em kabum   cadastra uma consulta
  /lista                                      consultas cadastradas
  /agora                                      coleta imediata

No servidor:
  systemctl --user status $UNIT.service
  journalctl --user -u $UNIT.service -f
Desfazer tudo:
  sh scripts/cleanup.sh
TXT
elif [ "$SCHEDULE" = timer ]; then
  cat <<TXT

Instalacao concluida no modo $MODE.

Conferir o agendamento:
  systemctl --user list-timers $UNIT.timer
Rodar agora e acompanhar:
  systemctl --user start $UNIT.service
  journalctl --user -u $UNIT.service -f
Desfazer tudo:
  sh scripts/cleanup.sh
TXT
elif [ "$MODE" = docker ]; then
  cat <<'TXT'

Ambiente pronto, nada ligado. Para subir o bot sem unidade systemd,
deixando o proprio Docker reinicia-lo no boot:

  docker compose up -d bot
  docker compose logs -f bot

Coleta unica, sem alertas:
  docker compose run --rm watcher --dry-run
TXT
else
  cat <<'TXT'

Ambiente pronto, nada ligado. Para rodar o bot em primeiro plano:

  .venv/bin/python watcher.py --serve

Coleta unica, sem alertas:
  .venv/bin/python watcher.py --dry-run
TXT
fi
