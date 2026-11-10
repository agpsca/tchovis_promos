# Tchovis Promos

Monitor de preços controlado por um bot do Telegram. Você manda o termo e
as lojas, ele guarda a consulta, pesquisa na hora e volta a avisar quando o
preço chegar ao alvo ou cair. O histórico fica em SQLite.

Serve para qualquer categoria. Lojas atendidas: KaBuM, Amazon e Magalu.

Uma única dependência externa, o Playwright, e só para as lojas que exigem
navegador. O resto é biblioteca padrão do Python. Roda direto no host ou em
contêiner.

```
/monitorar cadeira gamer ate 500 em kabum,magalu
```

## Comandos do Telegram

| Comando | O que faz |
|---|---|
| `/monitorar <termo> [ate <preço>] [em <lojas>]` | cadastra uma consulta e pesquisa na hora |
| `/lista` | consultas cadastradas, com limite e lojas |
| `/remover <id>` | apaga uma consulta |
| `/pausar <id>` · `/ativar <id>` | suspende e retoma sem apagar |
| `/agora` | roda uma coleta imediatamente |
| `/lojas` | lojas que aceitam busca por termo neste ambiente |
| `/ajuda` | resumo dos comandos |

```
/monitorar monitor 27 144hz em kabum
/monitorar echo dot
/monitorar ssd 1tb nvme ate 400 em kabum,amazon
```

**`ate <preço>` é opcional.** Com preço, o alerta vem quando uma oferta
ficar igual ou abaixo dele. Sem preço, vem quando o valor cair pelo menos o
`minimum_price_drop` do `config.json`, hoje R$ 50. Aceita formato
brasileiro: `450`, `R$ 1.299,90`, `1.299`.

**`em <lojas>` é opcional.** Sem isso, usa todas as lojas disponíveis. O
trecho só é lido como lista de lojas quando todos os nomes depois de "em"
são lojas conhecidas, para não estragar uma busca como "suporte em L".

**Ao cadastrar, o bot responde com as cinco ofertas mais baratas que se
encaixam.** Todas as que se encaixam entram no histórico como já avisadas,
senão o ciclo seguinte repetiria a lista inteira no seu Telegram.

**Somente o chat em `TELEGRAM_CHAT_ID` é obedecido.** Mensagem de qualquer
outro chat é registrada no log e ignorada; sem isso, quem achasse o bot
poderia cadastrar consultas e ler os resultados.

A regra de alerta está em `offer_limit` e `History.should_alert`, em
`watcher.py`.

## Instalação

```bash
sh scripts/setup.sh
```

Isso prepara o diretório `state/`, monta o ambiente de execução, pede as
credenciais do Telegram se ainda não existirem e deixa o bot no ar. Sem
argumento, usa contêiner quando o daemon do Docker responde e Python local
quando não.

| Opção | Efeito |
|---|---|
| `--docker` | força contêiner: grava `TCHOVIS_UID`/`TCHOVIS_GID` no `.env` e constrói a imagem |
| `--native` | força host: cria o `.venv` e instala o Firefox do Playwright |
| `--bot` | deixa o bot no ar (padrão) |
| `--timer` | usa o timer de 30 minutos em vez do bot |
| `--no-schedule` | só prepara o ambiente, sem ligar nada |

O bot faz a coleta periódica por dentro, no intervalo de
`check_interval_minutes`, o que dispensa o timer. O timer continua
disponível para quem prefere um processo que roda e morre.

Os scripts avulsos valem quando você quer apenas uma etapa: `install.sh`
(venv), `configure-telegram.sh` e `configure-telegram-docker.sh`
(credenciais), `install-service.sh` e `install-service-docker.sh` (timer).

### Num VPS

Em servidor sem sessão gráfica, o caminho mais simples dispensa unidade
systemd: o serviço `bot` do Compose já tem `restart: unless-stopped`, e o
próprio Docker o levanta no boot.

```bash
# 1. no servidor, com Docker e o plugin compose instalados
git clone https://github.com/agpsca/tchovis_promos.git
cd tchovis_promos

# 2. credenciais. Do seu lado:
scp .env USUARIO@SERVIDOR:~/tchovis_promos/.env
#    no servidor:
chmod 600 ~/tchovis_promos/.env
#    sem um .env pronto, o passo 3 pede o token do @BotFather

# 3. prepara state/, UID/GID e a imagem, sem ligar nada
sh scripts/setup.sh --docker --no-schedule

# 4. sobe o bot
docker compose up -d bot
docker compose logs -f bot
```

Num plano de 2GB, desligue também a Amazon antes do passo 4, senão o
Firefox estoura a memória:

```bash
echo 'TCHOVIS_DISABLED_STORES=magalu,amazon' >> .env
```

Para atualizar depois de um `git push`:

```bash
git pull && docker compose build && docker compose up -d bot
```

### Dois tropeços comuns

**Um token, um bot.** Dois processos no mesmo token brigam pelo
`getUpdates`: cada chamada de um confirma as mensagens do outro, e os
comandos passam a sumir sem erro no log. Para rodar dois monitores, crie um
segundo bot no `@BotFather`.

**Grupo `docker` e unidades de usuário.** Se você acabou de entrar no grupo
`docker`, reinicie o gerenciador de sessão com
`loginctl terminate-user "$USER"`. Serviços `systemd --user` fixam os grupos
suplementares quando o gerenciador sobe, e sem isso o contêiner não acessa o
socket do Docker. Para o monitor sobreviver ao fim da sessão, habilite o
linger: `sudo loginctl enable-linger "$USER"`.

## Operação

```bash
# o bot em primeiro plano, para acompanhar
.venv/bin/python watcher.py --serve

# uma coleta única, sem enviar alertas
.venv/bin/python watcher.py --dry-run

# mensagem de teste
.venv/bin/python watcher.py --test-telegram
```

```bash
# em contêiner
docker compose up -d bot                    # bot no ar
docker compose logs -f bot                  # acompanhar
docker compose run --rm watcher --dry-run   # coleta única
docker compose build                        # reconstruir após mudar o código
```

```bash
# o bot como unidade systemd, quando instalado pelo setup.sh
systemctl --user status tchovis_promos-bot.service
journalctl --user -u tchovis_promos-bot.service -f
systemctl --user disable --now tchovis_promos-bot.service
```

## Como a coleta funciona

Cada loja usa o transporte mais leve que ela permite:

| Loja | Transporte | Observação |
|---|---|---|
| KaBuM | HTTP | catálogo em `__NEXT_DATA__`, sem navegador |
| Amazon | navegador | bloqueia HTTP puro de IP de datacenter |
| Magalu | navegador | responde 403 para IP de datacenter |

O Playwright só é iniciado se alguma fonte exigir navegador. Com apenas a
KaBuM ativa, a coleta leva cerca de 8 segundos e usa 50MB. Com a Amazon
incluída, sobe para cerca de 60 segundos e 1,7GB de pico. O transporte pode
ser forçado por entrada do `config.json` com o campo `transport`, aceitando
`http` ou `browser`.

A busca da KaBuM tem duas particularidades, tratadas em `kabum_catalog` e
`kabum_redirect`: o campo `pageProps.data` vem como objeto para alguns
termos e como texto JSON para outros, e um termo que casa com uma categoria
volta com lista vazia e um redirect a seguir.

## O que é monitorado

Dois caminhos convivem.

**Consultas do bot**, o caminho normal. Termo livre, preço opcional e lojas
escolhidas por você. Ficam na tabela `watches` e não precisam de nenhuma
edição de arquivo.

**Alvos fixos do `config.json`**, opcionais e vazios por padrão. A lista
`products` acompanha uma URL específica com `maximum_price` próprio, útil
quando você quer vigiar um item exato em vez de um termo de busca. A lista
`searches` faz buscas que rodam sempre, sem passar pelo bot, e cada entrada
aceita `require_any`, `require_all` e `exclude` para filtrar títulos.

Nos dois caminhos: usados, recondicionados e Open Box são ignorados por
padrão, e o alerta mostra preço, frete, total, loja, vendedor e cupom
quando a loja informa.

## Configuração

As consultas do bot ficam no banco, não no `config.json`. O arquivo guarda
só os parâmetros gerais e vem sem nenhum alvo:

| Chave | Para que serve |
|---|---|
| `project_name` | nome que aparece no cabeçalho do alerta |
| `check_interval_minutes` | intervalo da coleta no modo bot |
| `minimum_price_drop` | queda mínima para um novo alerta, em reais |
| `search_filters` | termos exigidos e proibidos, padrão das buscas do arquivo |
| `browser_delay_seconds` | pausa entre buscas no navegador |
| `products` | URLs específicas a vigiar, cada uma com `maximum_price` |
| `searches` | buscas por termo que rodam sem passar pelo bot |
| `disabled_stores` | lojas desligadas por arquivo |

Uma entrada de `products` precisa de `id`, `store`, `name`, `url` e
`maximum_price`, e aceita `shipping_cost` para entrar no total. Sem
`maximum_price` a oferta nunca gera alerta, porque não há alvo para comparar.

### Lojas desligadas por ambiente

A variável `TCHOVIS_DISABLED_STORES` permite usar a mesma imagem em
ambientes diferentes, sem editar o `config.json`. Em IP de datacenter a
Magalu responde 403, e é o padrão do `compose.yaml`:

```
TCHOVIS_DISABLED_STORES=magalu
```

Num IP residencial, remova a variável para voltar a monitorar a loja. Esses
bloqueios mudam com o tempo, vale testar de novo periodicamente.

## Desinstalar e limpar

```bash
sh scripts/cleanup.sh            # remove o que é recriável
sh scripts/cleanup.sh --state    # inclui o histórico de preços
sh scripts/cleanup.sh --all -y   # remove tudo, sem perguntar
```

Por padrão saem as unidades systemd, o container, a imagem e a rede do
Docker, o `.venv` e os caches. O `.env` e o `state/prices.sqlite3` ficam,
porque não são recriáveis: um guarda o token do Telegram, o outro o
histórico. Para removê-los é preciso `--env` e `--state`, que pedem
confirmação e mostram quantas observações serão perdidas. Use `--keep-venv`
para preservar o ambiente virtual e `-y` para não perguntar.

## Segurança

- `.env` está no `.gitignore` e com permissão `600`.
- O token não fica no código, na imagem nem nas unidades systemd.
- O `.dockerignore` impede que `.env` entre na imagem.
- O bot obedece somente o chat em `TELEGRAM_CHAT_ID`; qualquer outro é
  ignorado e registrado no log.
- O monitor nunca finaliza pedidos: só lê páginas públicas.
- Revogue um token comprometido pelo `@BotFather` e rode novamente
  `scripts/configure-telegram.sh`.

## Estrutura

```
watcher.py          coleta, alertas, comandos do bot e o modo --serve
config.json         parâmetros gerais; sem alvos por padrão
compose.yaml        serviços watcher (coleta única) e bot (residente)
Dockerfile          imagem sobre mcr.microsoft.com/playwright/python
requirements.txt    playwright==1.64.1
scripts/
  setup.sh          instalação em um comando
  cleanup.sh        desinstalação
  _env.sh           leitura e gravação de chaves no .env
  install.sh        só o venv e o Firefox do Playwright
  configure-telegram*.sh   token e chat ID
  install-service*.sh      timer de 30 minutos
systemd/            unidades de usuário: bot e timer, host e contêiner
state/              banco SQLite, fora do git
```

O banco tem quatro tabelas: `observations` (todo preço já visto),
`alert_state` (o que já foi avisado, por consulta), `watches` (as consultas
do bot) e `bot_state` (offset das mensagens e horário do último ciclo).
