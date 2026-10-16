"""Modo festa: fila de reprodução compartilhada + controle remoto em tempo real.

O anfitrião (PC) reproduz o áudio; os convidados (celulares pelo QR Code)
adicionam músicas na fila, reordenam e controlam o player. Tudo é publicado
para todos via WebSocket (``/ws/party``), então o que acontece em um celular
aparece na hora na tela do anfitrião — e vice-versa.

Decisões:
- a fila mora em SQLite (sobrevive a restart e é compartilhada entre
  processos/abas); a ordem é a coluna ``position``, reindexada a cada mudança;
- a fila guarda título/artista/capa desnormalizados, então continua legível
  mesmo que a faixa seja apagada depois;
- quem toca é sempre o anfitrião: os convidados enviam *comandos*
  (play/pause/próxima/volume), nunca movem o ``<audio>`` deles.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.anfitriao import exigir_anfitriao

try:  # Starlette novo separa "disconnect" (receive) de "disconnected" (socket morto)
    from starlette.websockets import WebSocketDisconnected
except ImportError:  # pragma: no cover - versões antigas só têm WebSocketDisconnect
    WebSocketDisconnected = WebSocketDisconnect

# Mesma regra de ID do main.py (path traversal bloqueado na origem).
_ID_VALIDO = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

# Comandos que o anfitrião sabe executar no player.
_COMANDOS_VALIDOS = ('play', 'pause', 'toggle', 'stop', 'next', 'prev', 'seek', 'volume')

Pendente = Callable[[], sqlite3.Connection]
_configurar_bd: Pendente | None = None


def configurar(get_db: Pendente) -> None:
    """Recebe a fábrica de conexões do main.py (evita import circular)."""
    global _configurar_bd
    _configurar_bd = get_db


def _db() -> sqlite3.Connection:
    if _configurar_bd is None:
        raise RuntimeError('app.party não foi configurado (chame party.configurar).')
    conexao = _configurar_bd()
    conexao.execute(
        'CREATE TABLE IF NOT EXISTS party_queue ('
        'position INTEGER NOT NULL, '
        'item_id TEXT PRIMARY KEY, '
        'track_id TEXT NOT NULL, '
        'title TEXT, artist TEXT, thumbnail TEXT, '
        'added_by TEXT, '
        'status TEXT NOT NULL DEFAULT \'pending\', '
        'created_at TEXT NOT NULL)'
    )
    conexao.commit()
    return conexao


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# --------------------------------------------------------------------------
# Persistência da fila
# --------------------------------------------------------------------------
def _linha_para_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'item_id': row['item_id'],
        'track_id': row['track_id'],
        'title': row['title'],
        'artist': row['artist'],
        'thumbnail': row['thumbnail'],
        'added_by': row['added_by'],
        'status': row['status'],
        'created_at': row['created_at'],
        'position': row['position'],
    }


def _reindexar(conexao: sqlite3.Connection) -> None:
    """Reatribui positions na ordem atual (fila curta: custo irrelevante)."""
    linhas = conexao.execute(
        'SELECT item_id FROM party_queue ORDER BY position ASC, created_at ASC'
    ).fetchall()
    for indice, linha in enumerate(linhas, start=1):
        conexao.execute('UPDATE party_queue SET position = ? WHERE item_id = ?',
                        (indice, linha['item_id']))


def listar_itens() -> list[dict[str, Any]]:
    with closing(_db()) as conexao:
        linhas = conexao.execute(
            'SELECT * FROM party_queue ORDER BY position ASC, created_at ASC'
        ).fetchall()
    return [_linha_para_item(linha) for linha in linhas]


def adicionar(track_id: str, added_by: str | None) -> dict[str, Any]:
    """Valida a faixa na biblioteca e coloca no fim da fila."""
    if not _ID_VALIDO.match(track_id or ''):
        raise HTTPException(400, 'Identificador de música inválido.')
    if not added_by or not added_by.strip():
        raise HTTPException(400, 'Informe quem está pedindo a música.')

    with closing(_db()) as conexao:
        faixa = conexao.execute(
            'SELECT t.id, t.title, t.artist, c.thumbnail '
            'FROM tracks t LEFT JOIN track_covers c ON c.track_id = t.id '
            'WHERE t.id = ?', (track_id,),
        ).fetchone()
        if not faixa:
            raise HTTPException(404, 'Música não encontrada na biblioteca. '
                                     'Baixe-a antes de colocar na fila.')

        item = {
            'item_id': uuid.uuid4().hex[:12],
            'track_id': track_id,
            'title': faixa['title'],
            'artist': faixa['artist'],
            'thumbnail': faixa['thumbnail'],
            'added_by': added_by.strip()[:40],
            'status': 'pending',
            'created_at': _agora(),
        }
        proximo = conexao.execute(
            'SELECT COALESCE(MAX(position), 0) + 1 FROM party_queue').fetchone()[0]
        conexao.execute(
            'INSERT INTO party_queue (position, item_id, track_id, title, artist, '
            'thumbnail, added_by, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (proximo, item['item_id'], item['track_id'], item['title'], item['artist'],
             item['thumbnail'], item['added_by'], item['status'], item['created_at']),
        )
        conexao.commit()
        item['position'] = proximo
    return item


def remover(item_id: str) -> None:
    with closing(_db()) as conexao, conexao:
        cursor = conexao.execute('DELETE FROM party_queue WHERE item_id = ?', (item_id,))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Item da fila não encontrado.')


def mover(item_id: str, direcao: str) -> list[dict[str, Any]]:
    with closing(_db()) as conexao:
        linhas = conexao.execute(
            'SELECT item_id FROM party_queue ORDER BY position ASC, created_at ASC'
        ).fetchall()
        ordem = [linha['item_id'] for linha in linhas]
        if item_id not in ordem:
            raise HTTPException(404, 'Item da fila não encontrado.')
        indice = ordem.index(item_id)
        destino = indice - 1 if direcao == 'up' else indice + 1
        if 0 <= destino < len(ordem):
            ordem[indice], ordem[destino] = ordem[destino], ordem[indice]
            with conexao:
                for posicao, identificador in enumerate(ordem, start=1):
                    conexao.execute(
                        'UPDATE party_queue SET position = ? WHERE item_id = ?',
                        (posicao, identificador))
    return listar_itens()


def proxima() -> dict[str, Any] | None:
    """Promove o próximo item pendente a 'tocando' e aposenta o atual."""
    with closing(_db()) as conexao:
        pendente = conexao.execute(
            'SELECT * FROM party_queue WHERE status = ? '
            'ORDER BY position ASC, created_at ASC LIMIT 1',
            ('pending',),
        ).fetchone()
        if not pendente:
            return None
        with conexao:
            conexao.execute(
                "UPDATE party_queue SET status = 'played' WHERE status = 'playing'")
            conexao.execute(
                "UPDATE party_queue SET status = 'playing' WHERE item_id = ?",
                (pendente['item_id'],),
            )
    return _linha_para_item(pendente)


def tocar_agora(item_id: str) -> None:
    """Salta direto para um item da fila (o anfitrião escolheu 'tocar agora')."""
    with closing(_db()) as conexao:
        existe = conexao.execute(
            'SELECT 1 FROM party_queue WHERE item_id = ?', (item_id,)).fetchone()
        if not existe:
            raise HTTPException(404, 'Item da fila não encontrado.')
        with conexao:
            conexao.execute(
                "UPDATE party_queue SET status = 'played' WHERE status = 'playing'")
            conexao.execute(
                "UPDATE party_queue SET status = 'playing' WHERE item_id = ?",
                (item_id,),
            )


def limpar() -> int:
    """Remove tudo que ainda está pendente (mantém quem está tocando)."""
    with closing(_db()) as conexao, conexao:
        cursor = conexao.execute("DELETE FROM party_queue WHERE status = 'pending'")
        return max(0, cursor.rowcount)


# --------------------------------------------------------------------------
# WebSocket: quem está conectado e broadcast
# --------------------------------------------------------------------------
class Gerenciador:
    """Guarda as conexões ativas e publica eventos para todas elas."""

    def __init__(self) -> None:
        self._clientes: dict[WebSocket, str] = {}

    def conectar(self, websocket: WebSocket, papel: str = 'guest') -> None:
        self._clientes[websocket] = papel

    def desconectar(self, websocket: WebSocket) -> None:
        self._clientes.pop(websocket, None)

    @property
    def total(self) -> int:
        return len(self._clientes)

    @property
    def anfitrioes(self) -> int:
        return sum(1 for papel in self._clientes.values() if papel == 'host')

    def contagem(self) -> dict[str, int]:
        hosts = self.anfitrioes
        return {'total': self.total, 'hosts': hosts, 'guests': self.total - hosts}

    async def broadcast(self, mensagem: dict[str, Any],
                        exceto: WebSocket | None = None) -> None:
        """Envia para todos; falha de um cliente nunca derruba o broadcast."""
        alvos = [ws for ws in self._clientes if ws is not exceto]
        if not alvos:
            return

        async def _enviar(ws: WebSocket) -> None:
            try:
                await asyncio.wait_for(ws.send_json(mensagem), timeout=2)
            except Exception:  # noqa: BLE001 - cliente lento/quebrado é descartado
                self.desconectar(ws)

        await asyncio.gather(*(_enviar(ws) for ws in alvos))


gerenciador = Gerenciador()


async def _avisar_fila(acao: str) -> None:
    await gerenciador.broadcast({
        'type': 'queue',
        'action': acao,
        'items': listar_itens(),
    })


async def _avisar_clientes() -> None:
    await gerenciador.broadcast({'type': 'clients', **gerenciador.contagem()})


# --------------------------------------------------------------------------
# Modelos de entrada
# --------------------------------------------------------------------------
class PedidoFila(BaseModel):
    track_id: str = Field(min_length=1, max_length=64, description='ID da faixa')
    added_by: str = Field(min_length=1, max_length=40, description='Quem pediu')


class MoverItem(BaseModel):
    direcao: str = Field(pattern='^(up|down)$', description='up ou down')


class ControleRequest(BaseModel):
    cmd: str = Field(pattern='^(play|pause|toggle|stop|next|prev|seek|volume)$')
    value: float | None = Field(default=None, ge=0, le=1000)
    de: str | None = Field(default=None, max_length=40)


# --------------------------------------------------------------------------
# Rotas REST
# --------------------------------------------------------------------------
router = APIRouter()


@router.get('/api/queue')
async def fila_atual() -> dict[str, Any]:
    itens = listar_itens()
    return {
        'items': itens,
        'pending': sum(1 for i in itens if i['status'] == 'pending'),
        'playing': next((i for i in itens if i['status'] == 'playing'), None),
        **gerenciador.contagem(),
    }


@router.post('/api/queue', status_code=201)
async def enfileirar(pedido: PedidoFila) -> dict[str, Any]:
    item = adicionar(pedido.track_id, pedido.added_by)
    await _avisar_fila('add')
    return {'status': 'ok', 'item': item, **gerenciador.contagem()}


@router.delete('/api/queue/{item_id}')
async def remover_da_fila(item_id: str) -> dict[str, Any]:
    remover(item_id)
    await _avisar_fila('remove')
    return {'status': 'ok'}


@router.post('/api/queue/{item_id}/move')
async def mover_na_fila(item_id: str, pedido: MoverItem) -> dict[str, Any]:
    itens = mover(item_id, pedido.direcao)
    await gerenciador.broadcast({'type': 'queue', 'action': 'move', 'items': itens})
    return {'status': 'ok', 'items': itens}


@router.post('/api/queue/next')
async def proxima_da_fila() -> dict[str, Any]:
    item = proxima()
    if not item:
        raise HTTPException(404, 'A fila está vazia.')
    await _avisar_fila('next')
    return {'status': 'ok', 'item': item}


@router.post('/api/queue/{item_id}/play')
async def tocar_item_agora(item_id: str) -> dict[str, Any]:
    tocar_agora(item_id)
    await _avisar_fila('next')
    return {'status': 'ok'}


@router.delete('/api/queue')
async def limpar_fila() -> dict[str, Any]:
    removidos = limpar()
    await _avisar_fila('clear')
    return {'status': 'ok', 'removed': removidos}


@router.get('/api/party/status')
async def status_festa() -> dict[str, Any]:
    itens = listar_itens()
    return {
        'items': itens,
        'pending': sum(1 for i in itens if i['status'] == 'pending'),
        'playing': next((i for i in itens if i['status'] == 'playing'), None),
        'cmds': list(_COMANDOS_VALIDOS),
        **gerenciador.contagem(),
    }


@router.post('/api/party/control', dependencies=[Depends(exigir_anfitriao)])
async def controle_rest(pedido: ControleRequest) -> dict[str, Any]:
    """Controle remoto sem WebSocket (usado pelo anfitrião e por testes)."""
    mensagem = {
        'type': 'control',
        'cmd': pedido.cmd,
        'value': pedido.value,
        'de': pedido.de or 'convidado',
    }
    await gerenciador.broadcast(mensagem)
    return {'status': 'ok', 'broadcast': gerenciador.total, **mensagem}


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------
@router.websocket('/ws/party')
async def websocket_festa(websocket: WebSocket) -> None:
    await websocket.accept()
    gerenciador.conectar(websocket, 'guest')
    try:
        await websocket.send_json({
            'type': 'welcome',
            'queue': listar_itens(),
            **gerenciador.contagem(),
        })
        await _avisar_clientes()

        while True:
            try:
                dados = await websocket.receive_json()
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                continue  # mensagem malformada não derruba a conexão
            if not isinstance(dados, dict):
                continue

            tipo = dados.get('type')
            if tipo == 'join':
                papel = 'host' if dados.get('papel') == 'host' else 'guest'
                gerenciador.conectar(websocket, papel)
                await _avisar_clientes()
            elif tipo == 'control':
                cmd = dados.get('cmd')
                if cmd in _COMANDOS_VALIDOS:
                    await gerenciador.broadcast({
                        'type': 'control',
                        'cmd': cmd,
                        'value': dados.get('value'),
                        'de': str(dados.get('de') or 'convidado')[:40],
                    }, exceto=websocket)
            elif tipo == 'ping':
                await websocket.send_json({'type': 'pong'})
    except (WebSocketDisconnect, WebSocketDisconnected):
        pass
    finally:
        gerenciador.desconectar(websocket)
        await _avisar_clientes()
