#!/bin/bash
# Script de início - Karaokê Studio (uso familiar)
cd "$(dirname "$0")"
source .venv/bin/activate
echo "Iniciando Karaokê Studio na porta 8000..."

# Sem --reload de propósito: o modo de recarga derruba o servidor (e as
# conexões do WebSocket da festa) toda vez que um arquivo é salvo. Em noite de
# festa isso significa a música parar no meio. Para desenvolver, rode:
#     RECARGA=1 ./start.sh
if [ "${RECARGA:-0}" = "1" ]; then
  echo "(modo desenvolvimento: recarregando quando um arquivo muda)"
  exec python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
fi

exec python -m uvicorn main:app --host 0.0.0.0 --port 8000
