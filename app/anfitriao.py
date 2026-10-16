"""Quem pode disparar as ações pesadas do karaokê?

O app roda no PC e fica aberto na rede local de propósito: os convidados
escolhem músicas pelo celular, usando o endereço que o QR Code entrega. Só que
baixar, separar e preparar consomem a máquina inteira — e o QR dá a qualquer
celular da rede acesso ao servidor.

Este módulo responde "esta requisição veio do próprio PC do karaokê?" e serve
de dependência do FastAPI para travar download, separação e preparo.

O critério é a origem da conexão:

    anfitrião -> abre o app por localhost (127.0.0.1 / ::1)
    convidado -> abre pelo IP da rede (192.168.x.x, 10.x.x.x, ...)

Como o anfitrião também pode abrir o app pelo IP da própria máquina, esse IP
conta como anfitrião — nenhum outro aparelho da rede tem o mesmo.
"""
from __future__ import annotations

import socket

from fastapi import HTTPException, Request


def ip_da_rede() -> str | None:
    """Descobre o IP local da máquina (sem enviar tráfego de verdade)."""
    soquete = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # O connect em UDP só define a rota; nenhum pacote é transmitido.
        soquete.connect(('8.8.8.8', 80))
        return soquete.getsockname()[0]
    except OSError:
        return None
    finally:
        soquete.close()


def veio_do_anfitriao(request: Request) -> bool:
    """A requisição partiu do próprio PC do karaokê (e não de um convidado)?"""
    cliente = (request.client.host if request.client else '') or ''
    if cliente.startswith('127.') or cliente in ('::1', 'localhost', '::ffff:127.0.0.1'):
        return True
    return bool(cliente) and cliente == ip_da_rede()


def exigir_anfitriao(request: Request) -> None:
    """Dependência do FastAPI: 403 para quem não é o PC do karaokê."""
    if veio_do_anfitriao(request):
        return
    raise HTTPException(
        status_code=403,
        detail='Só o computador do karaokê pode fazer isso. '
               'No PC, abra o app por http://localhost:8000 como anfitrião.',
    )
