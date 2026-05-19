#!/usr/bin/env bash
# pdf-translate — script de inicialização
#
# Uso:
#   ./start.sh [local|docker|docker-update]
#
# Modos:
#   local           Executa a aplicação localmente via uv (padrão).
#                   Instala pdf2zh-next se ausente e sobe o servidor em
#                   http://0.0.0.0:8000 com hot-reload.
#                   Variáveis opcionais: HOST, PORT
#
#   docker          Constrói a imagem Docker (se desatualizada) e sobe a
#                   stack via Docker Compose. A URL com a porta aleatória
#                   é exibida no final.
#
#   docker-update   Reconstrói a imagem do zero (--no-cache) e recria os
#                   containers. Use após mudanças no Dockerfile ou
#                   dependências do sistema.
#
# Exemplos:
#   ./start.sh                  # modo local
#   ./start.sh docker           # sobe via Docker
#   ./start.sh docker-update    # rebuild completo
#   PORT=9000 ./start.sh local  # local em porta customizada
set -euo pipefail

MODE="${1:-local}"

# ─── Helpers Docker ───────────────────────────────────────────────────────────
_dc() {
  if docker compose version &>/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose &>/dev/null; then
    docker-compose "$@"
  else
    echo "[pdf-translate] ERRO: 'docker compose' não encontrado." >&2
    exit 1
  fi
}

_show_port() {
  echo "[pdf-translate] Aguardando o container iniciar..."
  local retries=10
  local port=""
  while [[ $retries -gt 0 ]]; do
    port=$(_dc port app 8000 2>/dev/null | cut -d: -f2 || true)
    [[ -n "$port" ]] && break
    sleep 1
    ((retries--))
  done
  if [[ -n "$port" ]]; then
    echo "[pdf-translate] ✓ Frontend disponível em: http://localhost:${port}"
  else
    echo "[pdf-translate] Container iniciado. Descubra a porta com:"
    echo "    docker compose port app 8000"
  fi
}

case "$MODE" in
  # ─── Modo Docker: constrói (se necessário) e sobe a stack ─────────────────
  docker)
    echo "[pdf-translate] Construindo e iniciando a stack Docker..."
    mkdir -p data/uploads
    _dc up -d --build
    _show_port
    ;;

  # ─── Modo Docker Update: rebuild forçado + recria containers ──────────────
  docker-update)
    echo "[pdf-translate] Atualizando a stack Docker (rebuild + force-recreate)..."
    mkdir -p data/uploads
    _dc build --no-cache
    _dc up -d --force-recreate
    _show_port
    ;;

  # ─── Modo local (padrão): executa via uv sem Docker ───────────────────────
  local)
    if ! command -v pdf2zh &>/dev/null; then
      echo "[pdf-translate] pdf2zh não encontrado. Instalando pdf2zh-next..."
      uv tool install --python 3.12 pdf2zh-next
    fi

    echo "[pdf-translate] Sincronizando dependências..."
    uv sync

    HOST="${HOST:-0.0.0.0}"
    PORT="${PORT:-8000}"
    echo "[pdf-translate] Iniciando servidor em http://${HOST}:${PORT}"
    exec uv run uvicorn app:app --host "$HOST" --port "$PORT" --reload
    ;;

  *)
    echo "Uso: $0 [local|docker|docker-update]"
    echo ""
    echo "  local          — executa localmente via uv (padrão)"
    echo "  docker         — constrói a imagem e sobe a stack via Docker Compose"
    echo "  docker-update  — reconstrói a imagem sem cache e recria os containers"
    exit 1
    ;;
esac
