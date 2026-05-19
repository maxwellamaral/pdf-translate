#!/usr/bin/env bash
set -euo pipefail

# ─── Verifica se pdf2zh (pdf2zh-next) está disponível ────────────────────────
if ! command -v pdf2zh &>/dev/null; then
  echo "[pdf-translate] pdf2zh não encontrado. Instalando pdf2zh-next..."
  uv tool install --python 3.12 pdf2zh-next
fi

# ─── Sincroniza dependências do projeto ──────────────────────────────────────
echo "[pdf-translate] Sincronizando dependências..."
uv sync

# ─── Inicia o servidor ────────────────────────────────────────────────────────
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo "[pdf-translate] Iniciando servidor em http://${HOST}:${PORT}"
exec uv run uvicorn app:app --host "$HOST" --port "$PORT" --reload
