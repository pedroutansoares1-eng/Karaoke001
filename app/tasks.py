"""Fila de tarefas em background com concorrência limitada.

Download (yt-dlp), separação (Demucs) e preparação do karaokê são pesados.
Antes eram disparados com ``BackgroundTasks`` sem limite: cinco downloads ao
mesmo tempo significavam cinco Demucs concorrendo por RAM e CPU, e a API não
tinha como dizer ao usuário o que estava na fila.

Aqui a fila:

- limita a concorrência (``max_simultaneos``) — um job por vez começa quando
  há vaga, então a máquina não engasga;
- deduplica pelo par (nome, chave) — duplo clique não enfileira o mesmo job 2×;
- guarda o estado em memória para ``GET /api/jobs`` mostrar a fila;
- roda inline quando ``inline=True`` (testes) para manter a determinismo.

A API pública é ``fila.enfileirar(nome, chave, alvo, *args)``. O ``alvo`` é o
caminho pontilhado de uma função (ex.: ``'main.run_download'``), resolvido na
hora da execução — assim um monkeypatch/teste que troca a função é respeitado.
Quando o projeto crescer e for valer a pena um broker externo (Celery +
Redis), basta trocar a implementação desta classe: os chamadores não mudam.
"""
from __future__ import annotations

import importlib
import itertools
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

# Estados de um job.
AGUARDANDO = 'queued'
EXECUTANDO = 'running'
CONCLUIDO = 'done'
FALHOU = 'error'
ATIVOS = (AGUARDANDO, EXECUTANDO)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _resolver(alvo: str | Callable[..., Any]) -> Callable[..., Any]:
    """Resolve ``'modulo.funcao'`` (ou devolve a função, se já for callável)."""
    if callable(alvo):
        return alvo
    modulo, separador, atributo = str(alvo).rpartition('.')
    if not separador or not modulo or not atributo:
        raise ValueError(f'Alvo de tarefa inválido: {alvo!r}')
    return getattr(importlib.import_module(modulo), atributo)


class FilaTarefas:
    """Fila limitada, deduplicada e observável."""

    # Atributo de classe (não de instância) para que o monkeypatch dos testes
    # possa alternar inline↔background e desfazer depois do teste.
    inline = False

    def __init__(self, max_simultaneos: int = 2) -> None:
        self.max_simultaneos = max(1, int(max_simultaneos))
        self._fila: queue.Queue[tuple[str, tuple[Any, ...], dict[str, Any]]] = queue.Queue()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._indices = itertools.count(1)
        self._lock = threading.RLock()
        self._trabalhadores: list[threading.Thread] = []

    # ------------------------------------------------------------- controle
    def _iniciar_trabalhadores(self) -> None:
        """Cria as threads sob demanda (evita thread ociosa em teste/import)."""
        with self._lock:
            if self._trabalhadores:
                return
            for indice in range(self.max_simultaneos):
                thread = threading.Thread(
                    target=self._trabalhar, name=f'tarefa-{indice}', daemon=True)
                thread.start()
                self._trabalhadores.append(thread)

    def enfileirar(self, nome: str, chave: str, alvo: str | Callable[..., Any],
                   *args: Any) -> dict[str, Any]:
        """Agenda uma tarefa e devolve o registro do job.

        Se já existir job ativo (na fila ou rodando) com o mesmo
        ``(nome, chave)``, devolve o job existente — é a deduplicação que
        impede dois Demucs para a mesma música.
        """
        with self._lock:
            for job in self._jobs.values():
                if job['nome'] == nome and job['chave'] == chave and job['status'] in ATIVOS:
                    return dict(job)

            job = {
                'id': f'{nome}-{next(self._indices)}',
                'nome': nome,
                'chave': chave,
                'status': AGUARDANDO,
                'message': 'Aguardando na fila...',
                'created_at': _agora(),
                'started_at': None,
                'finished_at': None,
            }
            self._jobs[job['id']] = job

        if self.inline:
            self._executar(job, alvo, args)
            return dict(job)

        self._fila.put((job['id'], args, {'alvo': alvo}))
        self._iniciar_trabalhadores()
        return dict(job)

    def listar(self) -> list[dict[str, Any]]:
        """Jobs do mais recente para o mais antigo."""
        with self._lock:
            return [dict(job) for job in
                    sorted(self._jobs.values(), key=lambda j: j['created_at'], reverse=True)]

    def resumo(self) -> dict[str, Any]:
        jobs = self.listar()
        return {
            'jobs': jobs,
            'running': sum(1 for j in jobs if j['status'] == EXECUTANDO),
            'queued': sum(1 for j in jobs if j['status'] == AGUARDANDO),
            'max_simultaneos': self.max_simultaneos,
        }

    def obter(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def aguardar(self, job_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
        """Espera um job terminar (usado por teste em modo background)."""
        limite = time.time() + timeout
        while time.time() < limite:
            job = self.obter(job_id)
            if job and job['status'] in (CONCLUIDO, FALHOU):
                return job
            time.sleep(0.02)
        return self.obter(job_id)

    def limpar(self) -> None:
        """Esquece os jobs (testes). As threads ficam — são persistentes."""
        with self._lock:
            self._jobs.clear()
            while True:
                try:
                    self._fila.get_nowait()
                except queue.Empty:
                    break

    # -------------------------------------------------------------- execução
    def _trabalhar(self) -> None:
        while True:
            job_id, args, meta = self._fila.get()
            job = self.obter(job_id)
            if not job:
                continue
            self._executar(job, meta['alvo'], args)

    def _executar(self, job: dict[str, Any], alvo: str | Callable[..., Any],
                  args: tuple[Any, ...]) -> None:
        with self._lock:
            atual = self._jobs.get(job['id'])
            if not atual or atual['status'] != AGUARDANDO:
                return  # já finalizado ou removido
            atual['status'] = EXECUTANDO
            atual['message'] = 'Executando...'
            atual['started_at'] = _agora()

        try:
            funcao = _resolver(alvo)
            funcao(*args)
        except Exception as exc:  # noqa: BLE001 - o job precisa reportar a falha
            with self._lock:
                job = self._jobs.get(job['id'])
                if job:
                    job['status'] = FALHOU
                    job['message'] = f'{type(exc).__name__}: {exc}'[:300]
                    job['finished_at'] = _agora()
            return

        with self._lock:
            if job['id'] in self._jobs:
                self._jobs[job['id']]['status'] = CONCLUIDO
                self._jobs[job['id']]['message'] = 'Concluído.'
                self._jobs[job['id']]['finished_at'] = _agora()


# Instância única usada pela aplicação.
fila = FilaTarefas()
