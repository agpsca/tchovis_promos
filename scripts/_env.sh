#!/usr/bin/env sh
# Funcoes compartilhadas para ler e gravar chaves no .env sem perder as
# demais linhas do arquivo. Este arquivo e carregado com "." pelos outros
# scripts; nao deve ser executado direto.

# env_get <arquivo> <chave>
# Imprime o valor da chave, ou nada se ela nao existir.
env_get() {
  _file=$1
  _key=$2
  [ -f "$_file" ] || return 0
  sed -n "s/^${_key}=//p" "$_file" | tail -1
}

# env_set <arquivo> <chave> <valor>
# Grava a chave preservando as outras. Cria o arquivo com permissao 600
# quando ele ainda nao existe, porque o .env guarda o token do Telegram.
env_set() {
  _file=$1
  _key=$2
  _value=$3
  _tmp="${_file}.tmp.$$"
  umask 077
  [ -f "$_file" ] || : > "$_file"
  grep -v "^${_key}=" "$_file" > "$_tmp" 2>/dev/null || :
  printf '%s=%s\n' "$_key" "$_value" >> "$_tmp"
  mv "$_tmp" "$_file"
  chmod 600 "$_file"
}
