"""API do Karaokê de Casa."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import torch
import yt_dlp
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from yt_dlp.utils import DownloadError

from app import party
from app.anfitriao import exigir_anfitriao, ip_da_rede, veio_do_anfitriao
from app.lyrics import (
    completar_letra_com_whisper,
    fetch_lyrics,
    get_lyrics,
    normalizar_metadados,
    parse_lrc_lines,
    salvar_lrc_local,
    validar_conteudo_lrc,
)
from app.pitch import extract_pitch_reference
from app.tasks import fila

ROOT = Path(__file__).parent
DATA = ROOT / 'data'
DOWNLOADS = DATA / 'downloads'
SEPARATED = DATA / 'separated'
LYRICS = DATA / 'lyrics'
PITCHREF = DATA / 'pitchref'
# Fora de /data de propósito: /data é servido como estático e não deve expor o banco.
DB = ROOT / 'karaoke.sqlite3'

for _folder in (DOWNLOADS, SEPARATED, LYRICS, PITCHREF):
    _folder.mkdir(parents=True, exist_ok=True)


def _migrar_banco() -> None:
    """Move o banco antigo de dentro de /data para a raiz do projeto.

    Versões anteriores gravavam o SQLite em data/, que é servido como arquivo
    estático — o banco ficava baixável. Aqui preservamos os dados do usuário e
    removemos a cópia exposta.
    """
    antigo = DATA / 'karaoke.sqlite3'
    if not antigo.exists() or antigo.resolve() == DB.resolve():
        return
    if not DB.exists():
        antigo.replace(DB)
    else:
        antigo.unlink(missing_ok=True)


def _migrar_tabela_estados() -> None:
    """Corrige a PK da tabela states.

    A PK antiga era só ``track_id``; o ``INSERT OR REPLACE`` de um kind
    apagava o estado da outra categoria (separação × karaokê) da mesma música.
    """
    if not DB.exists():
        return
    with closing(sqlite3.connect(DB, timeout=30)) as conexao:
        row = conexao.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='states'"
        ).fetchone()
        sql = ((row[0] if row else '') or '').replace('`', '').replace('"', '')
        if not row or 'PRIMARY KEY (track_id, kind)' in sql:
            return
        conexao.execute('ALTER TABLE states RENAME TO states_legacy')
        conexao.execute(
            'CREATE TABLE states ('
            'track_id TEXT NOT NULL, kind TEXT NOT NULL, '
            'status TEXT NOT NULL, message TEXT NOT NULL, '
            'PRIMARY KEY (track_id, kind))'
        )
        conexao.execute(
            'INSERT OR REPLACE INTO states (track_id, kind, status, message) '
            'SELECT track_id, kind, status, message FROM states_legacy'
        )
        conexao.execute('DROP TABLE states_legacy')
        conexao.commit()


_migrar_banco()

# Identificadores do YouTube são alfanuméricos; qualquer outro caractere é
# rejeitado antes de compor caminhos no disco.
_ID_VALIDO = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

app = FastAPI(title='Karaokê Studio')
app.mount('/data', StaticFiles(directory=DATA), name='data')
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

_lock = threading.Lock()
_separation_states: dict[str, dict[str, str]] = {}
_karaoke_states: dict[str, dict[str, str]] = {}
_download_states: dict[str, dict[str, str]] = {}


# --------------------------------------------------------------------------
# Persistência
# --------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    """Abre uma conexão nova a cada uso (o SQLite não é seguro entre threads).

    Use sempre com `closing(db())`: o `with` do sqlite3 faz commit, mas não
    fecha a conexão.
    """
    connection = sqlite3.connect(DB, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE IF NOT EXISTS tracks ('
        'id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT, '
        'duration REAL, source_url TEXT)'
    )
    connection.execute(
        'CREATE TABLE IF NOT EXISTS states ('
        'track_id TEXT NOT NULL, kind TEXT NOT NULL, '
        'status TEXT NOT NULL, message TEXT NOT NULL, '
        'PRIMARY KEY (track_id, kind))'
    )
    connection.execute(
        'CREATE TABLE IF NOT EXISTS track_covers ('
        'track_id TEXT PRIMARY KEY, thumbnail TEXT)'
    )
    connection.execute(
        'CREATE TABLE IF NOT EXISTS scores ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, singer TEXT NOT NULL, '
        'score INTEGER NOT NULL, medal TEXT, track_id TEXT, '
        'track_title TEXT, created_at TEXT NOT NULL)'
    )
    connection.execute(
        'CREATE TABLE IF NOT EXISTS favoritas ('
        'track_id TEXT PRIMARY KEY, criada_em TEXT NOT NULL)'
    )
    connection.commit()
    return connection


def _recuperar_estados_orfaos() -> None:
    """Destrava estados presos em "em andamento" após um reinício.

    A fila de tarefas vive em memória: se o processo cai ou recarrega no meio
    de uma separação/preparação, o estado persistido fica preso em
    "queued"/"separating"/"preparing" para sempre (a música nunca aparece como
    pronta nem como falha). Aqui marcamos esses como interrompidos para o
    usuário poder tentar de novo.
    """
    if not DB.exists():
        return
    orfaos = (
        ('separation', ('queued', 'separating'),
         'Separação interrompida (o servidor reiniciou). Inicie novamente.'),
        ('karaoke', ('preparing',),
         'Preparação do karaokê interrompida (o servidor reiniciou). Tente de novo.'),
    )
    try:
        with closing(db()) as conexao, conexao:
            for kind, statuses, mensagem in orfaos:
                marcadores = ','.join('?' for _ in statuses)
                conexao.execute(
                    f'UPDATE states SET status = ?, message = ? '
                    f'WHERE kind = ? AND status IN ({marcadores})',
                    ('error', mensagem, kind, *statuses),
                )
    except sqlite3.Error:
        pass  # o app deve subir mesmo se o banco estiver indisponível


_migrar_tabela_estados()
_recuperar_estados_orfaos()


# Modo festa: fila compartilhada + WebSocket (precisa da fábrica de conexões).
party.configurar(db)
app.include_router(party.router)


def set_state(kind: str, track_id: str, status: str, message: str) -> dict[str, str]:
    """Grava o estado em memória e no banco, de forma atômica."""
    estado = {'status': status, 'message': message}
    with _lock:
        (_separation_states if kind == 'separation' else _karaoke_states)[track_id] = estado
    try:
        with closing(db()) as conexao, conexao:
            conexao.execute(
                'INSERT OR REPLACE INTO states (track_id, kind, status, message) '
                'VALUES (?, ?, ?, ?)',
                (track_id, kind, status, message),
            )
    except sqlite3.Error:  # estado em memória ainda é válido
        pass
    return estado


def set_download_state(chave: str, status: str, message: str) -> dict[str, str]:
    estado = {'status': status, 'message': message}
    with _lock:
        _download_states[chave] = estado
    return estado


def get_download_state(chave: str) -> dict[str, str] | None:
    with _lock:
        return _download_states.get(chave)


def get_state(kind: str, track_id: str) -> dict[str, str] | None:
    with _lock:
        estado = (_separation_states if kind == 'separation' else _karaoke_states).get(track_id)
    if estado:
        return estado
    try:  # recupera após reinício do servidor
        with closing(db()) as conexao:
            row = conexao.execute(
                'SELECT status, message FROM states WHERE track_id = ? AND kind = ?',
                (track_id, kind),
            ).fetchone()
    except sqlite3.Error:
        return None
    return {'status': row['status'], 'message': row['message']} if row else None


def track_row(track_id: str) -> dict[str, Any] | None:
    with closing(db()) as conexao:
        row = conexao.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
        if not row:
            return None
        resultado = dict(row)
        capa = conexao.execute(
            'SELECT thumbnail FROM track_covers WHERE track_id = ?', (track_id,)
        ).fetchone()
        resultado['thumbnail'] = capa['thumbnail'] if capa else None
    return resultado


def validar_id(track_id: str) -> str:
    """Bloqueia path traversal e IDs malformados."""
    if not _ID_VALIDO.match(track_id or ''):
        raise HTTPException(400, 'Identificador de música inválido.')
    return track_id


def exigir_faixa(track_id: str) -> dict[str, Any]:
    validar_id(track_id)
    row = track_row(track_id)
    if not row:
        raise HTTPException(404, 'Música não encontrada.')
    return row


def stems_dir(track_id: str) -> Path:
    return SEPARATED / track_id / 'htdemucs'


# --------------------------------------------------------------------------
# Modelos de entrada
# --------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=120, description='Termo de busca')
    limite: int = Field(default=15, ge=1, le=30, description='Quantidade de resultados')
    filtro: str = Field(
        default='qualquer',
        pattern='^(qualquer|estudio|ao_vivo|instrumental|karaoke)$',
        description='Tipo de resultado desejado',
    )
    fonte: str = Field(
        default='youtube',
        pattern='^(youtube|ytmusic)$',
        description='YouTube comum ou YouTube Music (metadados de música mais limpos)',
    )


class DownloadRequest(BaseModel):
    id: str | None = None
    url: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, max_length=300)
    artist: str | None = Field(default=None, max_length=300)
    duration: float | None = None
    qualidade: str | None = Field(default=None, pattern='^(rapida|equilibrada|alta|maxima)$')
    # Modo festa: depois de separar a voz, já busca a letra e a afinação para a
    # faixa ficar pronta pra cantar sem ninguém precisar clicar em "Cantar".
    preparar: bool = False


class PlaylistRequest(BaseModel):
    url: str = Field(min_length=5, max_length=512)
    qualidade: str | None = Field(default=None, pattern='^(rapida|equilibrada|alta|maxima)$')
    limite: int = Field(default=50, ge=1, le=200)
    preparar: bool = False


class PlaylistStatusRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class PrepareRequest(BaseModel):
    artist: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)


class MetadataRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    artist: str | None = Field(default=None, max_length=300)


class PreviewRequest(BaseModel):
    id: str | None = None
    url: str | None = Field(default=None, max_length=512)


class SeparationRequest(BaseModel):
    qualidade: str = Field(default='equilibrada', pattern='^(rapida|equilibrada|alta|maxima)$')


class ScoreRequest(BaseModel):
    singer: str = Field(min_length=1, max_length=40)
    score: int = Field(ge=0, le=100)
    medal: str | None = Field(default=None, max_length=16)
    track_id: str | None = Field(default=None, max_length=64)
    track_title: str | None = Field(default=None, max_length=300)


# --------------------------------------------------------------------------
# Tarefas de fundo
# --------------------------------------------------------------------------
# Perfis de qualidade da separação (modelo Demucs + esforço).
# - rápida:      sem shifts, overlap baixo (mais veloz)
# - equilibrada: shifts + overlap moderados (padrão)
# - alta:        muitos shifts e overlap alto (mais lento, menos vazamento de voz)
PERFIS_SEPARACAO: dict[str, dict[str, Any]] = {
    'rapida': {'modelo': 'htdemucs', 'shifts': 0, 'overlap': 0.10},
    'equilibrada': {'modelo': 'htdemucs', 'shifts': 2, 'overlap': 0.25},
    'alta': {'modelo': 'htdemucs_ft', 'shifts': 8, 'overlap': 0.75},
    'maxima': {'modelo': 'htdemucs_ft', 'shifts': 10, 'overlap': 0.85},
}


def run_separation(track_id: str, qualidade: str = 'equilibrada') -> None:
    """Separa os stems com Demucs em modo 2-stems (vocal/instrumental).

    Respeita o perfil escolhido pelo usuário (``qualidade``); o padrão é
    "equilibrada". Um valor desconhecido cai no padrão. Roda na GPU quando há
    CUDA disponível e, se a GPU falhar (VRAM curta/driver), refaz na CPU.
    """
    source = DOWNLOADS / f'{track_id}.mp3'
    if not source.exists():
        set_state('separation', track_id, 'error', 'Áudio baixado não encontrado.')
        return

    perfil = PERFIS_SEPARACAO.get(qualidade) or PERFIS_SEPARACAO['equilibrada']
    dispositivo = 'cuda' if torch.cuda.is_available() else 'cpu'
    onde = 'GPU (CUDA)' if dispositivo == 'cuda' else 'CPU'
    set_state('separation', track_id, 'separating',
              f'Demucs separando a voz ({qualidade}) na {onde}...')

    output = SEPARATED / track_id
    comando = [
        sys.executable, '-m', 'demucs',
        '--name', str(perfil['modelo']), '--two-stems', 'vocals',
        '--device', dispositivo,
        '--overlap', str(perfil['overlap']),
    ]
    if int(perfil['shifts']) > 0:
        comando += ['--shifts', str(perfil['shifts'])]
    comando += ['--out', str(output), str(source)]

    def _executar(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Roda o Demucs; devolve None (e grava o erro) se não deu para executar."""
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            set_state('separation', track_id, 'error',
                      'A separação excedeu 1 hora e foi cancelada.')
        except Exception as exc:  # noqa: BLE001 - precisa reportar qualquer falha
            set_state('separation', track_id, 'error',
                      f'Falha ao executar o Demucs: {exc}')
        return None

    result = _executar(comando)
    if result is None:
        return
    if result.returncode != 0 and dispositivo == 'cuda':
        # A GPU pode falhar (VRAM curta/driver). Tenta na CPU antes de desistir.
        set_state('separation', track_id, 'separating',
                  'A GPU falhou; refazendo a separação na CPU (mais lento)...')
        comando = ['cpu' if parte == 'cuda' else parte for parte in comando]
        result = _executar(comando)
        if result is None:
            return

    if result.returncode != 0:
        detalhe = (result.stderr or result.stdout or '').strip().splitlines()
        set_state('separation', track_id, 'error',
                  'O Demucs falhou: ' + (detalhe[-1][:200] if detalhe else 'sem detalhes'))
        return

    # O Demucs aninha em <out>/<modelo>/<nome_do_arquivo>/. Achatamos para um
    # nome canônico "htdemucs" para o resto do app não depender do modelo usado.
    modelo = str(perfil['modelo'])
    generated = output / modelo / source.stem
    target = output / 'htdemucs'
    target.mkdir(parents=True, exist_ok=True)
    if generated.is_dir():
        for stem in generated.glob('*.wav'):
            stem.replace(target / stem.name)
        for leftover in generated.glob('*'):
            leftover.unlink(missing_ok=True)
        generated.rmdir()

    # O instrumental é a soma de tudo menos os vocais.
    instrumental = target / 'no_vocals.wav'
    restantes = [target / nome for nome in ('drums.wav', 'bass.wav', 'other.wav')]
    if not instrumental.exists() and all(p.exists() for p in restantes):
        try:
            subprocess.run(
                ['ffmpeg', '-y', *sum((['-i', str(p)] for p in restantes), []),
                 '-filter_complex', 'amix=inputs=3:normalize=0', str(instrumental)],
                capture_output=True, timeout=600,
            )
        except Exception as exc:  # noqa: BLE001
            set_state('separation', track_id, 'error', f'Falha ao montar o instrumental: {exc}')
            return

    if instrumental.exists():
        set_state('separation', track_id, 'ready', 'Separação concluída.')
    else:
        set_state('separation', track_id, 'error',
                  'O Demucs terminou, mas os stems não foram gerados.')


def _karaoke_pronto(track_id: str) -> bool:
    """Já existem letra e afinação no disco para esta faixa?"""
    return ((LYRICS / f'{track_id}.lrc').exists()
            and (PITCHREF / f'{track_id}.json').exists())


def _precisa_preparo(track_id: str) -> bool:
    """Falta alguma coisa para esta faixa ficar pronta pra cantar?"""
    tem_separacao = (stems_dir(track_id) / 'no_vocals.wav').exists()
    return not tem_separacao or not _karaoke_pronto(track_id)


def _enfileirar_karaoke(track_id: str) -> None:
    """Coloca a busca de letra/afinação na fila (job próprio, nome ``karaoke``)."""
    if _karaoke_pronto(track_id):
        return
    row = track_row(track_id) or {}
    set_state('karaoke', track_id, 'queued', 'Na fila: buscando letra e afinação...')
    fila.enfileirar('karaoke', track_id, 'main.prepare_karaoke',
                    track_id, row.get('artist'), row.get('title'))


def run_separation_com_preparo(track_id: str, qualidade: str = 'equilibrada') -> None:
    """Separa a voz e, ao terminar, deixa a faixa pronta pra cantar.

    É o trabalho do "modo festa" (baixar a playlist já deixando tudo pronto). A
    letra/afinação entra como job próprio (``karaoke``) porque a transcrição do
    Whisper pode demorar e não deve segurar a vaga da fila de separação.

    Prepara mesmo quando a separação falha: a letra e a afinação ainda valem a
    pena (o karaokê toca o áudio original enquanto não há instrumental).
    """
    run_separation(track_id, qualidade)
    if track_row(track_id):  # a faixa pode ter sido removida no meio do caminho
        _enfileirar_karaoke(track_id)


def preparar_faixa(track_id: str, qualidade: str = 'equilibrada') -> None:
    """Deixa uma faixa já baixada pronta pra cantar (modo festa).

    Se a separação já existe, vai direto para a letra/afinação; senão enfileira
    a separação, que encadeia o preparo ao terminar.
    """
    if not track_row(track_id):
        return
    if (stems_dir(track_id) / 'no_vocals.wav').exists():
        _enfileirar_karaoke(track_id)
        return
    set_state('separation', track_id, 'queued', 'Na fila: separação + preparo...')
    fila.enfileirar('separation', track_id, 'main.run_separation_com_preparo',
                    track_id, qualidade)


def _completar_letra_local(track_id: str, duration: float | None,
                           audio_path: Path | None) -> None:
    """Completa com o Whisper o final da letra quando ela não cobre a música.

    Mantém a letra já existente (LRCLIB/manual) e só acrescenta os versos que
    faltam. Assim a letra acompanha até o fim em vez de "congelar" no meio.
    """
    lyric_file = LYRICS / f'{track_id}.lrc'
    if not lyric_file.exists():
        return
    try:
        linhas = parse_lrc_lines(lyric_file.read_text(encoding='utf-8'))
    except OSError:
        return
    if not linhas:
        return
    completas = completar_letra_com_whisper(linhas, duration, audio_path)
    if completas is linhas:
        return
    conteudo = '\n'.join(
        f"[{int(l['time'] // 60):02d}:{l['time'] % 60:05.2f}] {l.get('text', '')}"
        for l in completas
    )
    salvar_lrc_local(track_id, conteudo, LYRICS)
    print(f'[lyrics:info] {track_id}: final da letra completado com Whisper '
          f'({len(linhas)} -> {len(completas)} versos)', flush=True)


def prepare_karaoke(track_id: str, artist: str, title: str) -> None:
    """Busca a letra sincronizada e extrai o pitch de referência.

    Prefere o vocal isolado (``vocals.wav``) para a afinação; se a separação
    ainda não terminou, usa o áudio original como referência para permitir o
    "cantar já". Assim o karaokê fica pronto logo após o download e o
    instrumental entra automaticamente quando os stems terminarem.
    """
    row = track_row(track_id)
    if not row:
        return

    vocal = stems_dir(track_id) / 'vocals.wav'
    fonte_pitch = vocal if vocal.exists() else DOWNLOADS / f'{track_id}.mp3'
    if not fonte_pitch.exists():
        set_state('karaoke', track_id, 'error',
                  'Áudio da faixa não encontrado. Baixe a música antes de cantar.')
        return

    set_state('karaoke', track_id, 'preparing', 'Buscando letra sincronizada no LRCLIB...')
    lyric_file = LYRICS / f'{track_id}.lrc'

    # Letra enviada manualmente pelo usuário tem prioridade e nunca é
    # sobrescrita por uma busca automática.
    if lyric_file.exists() and lyric_file.stat().st_size > 0:
        letra_ok = True
        print(f'[lyrics:info] {track_id}: usando .lrc local, pulando a busca', flush=True)
    else:
        letra_ok = fetch_lyrics(artist, title, row.get('duration'), lyric_file)

    set_state('karaoke', track_id, 'preparing', 'Calculando a afinação de referência...')
    pitch_file = PITCHREF / f'{track_id}.json'
    try:
        extract_pitch_reference(fonte_pitch, pitch_file)
    except Exception as exc:  # noqa: BLE001
        set_state('karaoke', track_id, 'error', f'Falha ao extrair a afinação: {exc}')
        return

    if not letra_ok:
        # Nunca deixa um .lrc pela metade quebrando a checagem de status.
        lyric_file.unlink(missing_ok=True)
        set_state('karaoke', track_id, 'no_lyrics',
                  'Não encontrei letra sincronizada no LRCLIB para esta música. '
                  'Envie um arquivo .lrc.')
        return

    # A letra pode ter sido encontrada só em parte (ex.: o LRCLIB para no meio e
    # a música continua). Nesse caso o final é transcrito pelo Whisper.
    set_state('karaoke', track_id, 'preparing',
              'Completando o final da letra com o Whisper...')
    _completar_letra_local(track_id, row.get('duration'), fonte_pitch)

    set_state('karaoke', track_id, 'ready', 'Karaokê pronto.')


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@app.get('/')
def index() -> FileResponse:
    return FileResponse(ROOT / 'static' / 'index.html')


def _capa_do_youtube(entrada: dict[str, Any]) -> str | None:
    """Descobre a capa de um vídeo, com fallback pela URL pública do YouTube.

    A busca usa extract_flat, que às vezes não traz a miniatura; nesse caso
    montamos a URL a partir do ID (i.ytimg.com), que funciona sem requisição extra.
    """
    capa = entrada.get('thumbnail')
    if not capa:
        miniaturas = entrada.get('thumbnails') or []
        if isinstance(miniaturas, list) and miniaturas:
            ultima = miniaturas[-1]
            if isinstance(ultima, dict):
                capa = ultima.get('url')
    if not capa and entrada.get('id'):
        capa = f'https://i.ytimg.com/vi/{entrada["id"]}/mqdefault.jpg'
    return capa


# Palavras que indicam gravação ao vivo/show — filtradas no modo "estúdio".
_LIVE_REGEX = re.compile(
    r'\b(ao vivo|live|show|acústico|acustico|acoustic|cover|reme[d]?ix|'
    r'in concert|session|unplugged|sped up|slowed)\b',
    re.IGNORECASE,
)
# Termos extras anexados à consulta para enviesar o YouTube ao tipo desejado.
_SUFIXOS_BUSCA = {
    'estudio': 'audio oficial',
    'ao_vivo': 'ao vivo',
    'instrumental': 'instrumental',
    'karaoke': 'karaokê',
}


@app.post('/api/search')
def search(request: SearchRequest) -> list[dict[str, Any]]:
    sufixo = _SUFIXOS_BUSCA.get(request.filtro)
    termo = f'{request.query} {sufixo}' if sufixo else request.query
    if request.fonte == 'ytmusic':
        try:
            return _buscar_ytmusic(termo, request)
        except Exception as exc:  # noqa: BLE001
            # YouTube Music fora do ar ou lento: cai para o YouTube comum
            # em vez de devolver erro para o usuário.
            print(f'[busca] YouTube Music falhou ({exc}); usando YouTube comum.', flush=True)
    return _buscar_youtube(termo, request)


def _buscar_youtube(termo: str, request: SearchRequest) -> list[dict[str, Any]]:
    options = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'extract_flat': True}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f'ytsearch{request.limite}:{termo}', download=False)
    except DownloadError as exc:
        raise HTTPException(502, f'Não consegui consultar o YouTube: {exc}') from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, 'Falha inesperada ao consultar o YouTube.') from exc

    if not info:
        return []
    # O yt-dlp pode devolver entradas None para vídeos indisponíveis.
    entradas = [e for e in (info.get('entries') or []) if e]

    # No modo estúdio, descarta resultados com cara de show/ao vivo/capa.
    if request.filtro == 'estudio':
        filtradas = [e for e in entradas if not _LIVE_REGEX.search(e.get('title') or '')]
        entradas = filtradas or entradas  # se filtrar tudo, devolve o original

    return [_mapear_resultado(e) for e in entradas]


def _buscar_ytmusic(termo: str, request: SearchRequest) -> list[dict[str, Any]]:
    """Busca só na seção de músicas do YouTube Music (artista/duração reais).

    Usa a URL de busca em vez do prefixo ``ytmusicsearch:`` (nem toda versão
    do yt-dlp o implementa) e limita à seção #songs para responder em ~10s.
    """
    from urllib.parse import quote_plus

    url = f'https://music.youtube.com/search?q={quote_plus(termo)}#songs'
    options = {'quiet': True, 'no_warnings': True, 'skip_download': True,
               'playlist_items': f'1-{request.limite}'}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise HTTPException(502, f'Não consegui consultar o YouTube Music: {exc}') from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, 'Falha inesperada ao consultar o YouTube Music.') from exc

    if not info:
        return []
    entradas = [e for e in (info.get('entries') or []) if e]
    if request.filtro == 'estudio':
        filtradas = [e for e in entradas if not _LIVE_REGEX.search(e.get('title') or '')]
        entradas = filtradas or entradas
    return [_mapear_resultado(e) for e in entradas]


def _mapear_resultado(e: dict[str, Any]) -> dict[str, Any]:
    """Normaliza um resultado do yt-dlp (YouTube ou YouTube Music)."""
    artistas = e.get('artists')
    if isinstance(artistas, list) and artistas:
        primeiro = artistas[0]
        artista = primeiro if isinstance(primeiro, str) else primeiro.get('name')
    else:
        artista = e.get('artist') or e.get('uploader')
    return {'id': e.get('id'), 'title': e.get('track') or e.get('title'),
            'artist': artista, 'duration': e.get('duration'),
            'url': e.get('webpage_url'), 'thumbnail': _capa_do_youtube(e)}


@app.post('/api/preview')
def preview(request: PreviewRequest) -> dict[str, Any]:
    """Devolve a URL direta do áudio para pré-ouvir sem baixar."""
    alvo = request.url or request.id
    if not alvo:
        raise HTTPException(400, 'Informe a música para pré-visualizar.')
    options = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'noplaylist': True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(alvo, download=False)
    except DownloadError as exc:
        raise HTTPException(400, f'Não consegui preparar a prévia: {exc}') from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, 'Falha inesperada ao preparar a prévia.') from exc

    url = info.get('url') if info else None
    if not url:
        # Alguns formatos vêm em requested_formats.
        formatos = (info or {}).get('requested_formats') or []
        if formatos and isinstance(formatos[0], dict):
            url = formatos[0].get('url')
    if not url:
        raise HTTPException(404, 'Esta música não tem áudio disponível para prévia.')
    return {'url': url, 'title': info.get('title'), 'duration': info.get('duration')}


def _chave_download(request: DownloadRequest) -> str:
    """Chave estável para deduplicar downloads (ID do YouTube ou hash da URL)."""
    if request.id and _ID_VALIDO.match(request.id):
        return request.id
    base = (request.url or request.title or request.artist or '').encode('utf-8')
    return hashlib.sha1(base).hexdigest()[:22]


def _expandir_playlist(url: str, limite: int) -> list[dict[str, Any]]:
    """Lê a lista de vídeos de uma playlist sem baixar nada.

    Usa ``extract_flat`` para ser rápido: não resolve cada vídeo, só devolve
    id/título/URL. O download de cada faixa acontece depois, na fila — assim o
    progresso aparece música por música em vez de um bloco só no fim.
    """
    options = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': 'in_playlist', 'skip_download': True,
        'playlistend': max(1, int(limite)),
    }
    cookies_path = ROOT / 'cookies.txt'
    if cookies_path.exists():
        options['cookiefile'] = str(cookies_path)

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []

    brutas = (info.get('entries') or []) if info.get('_type') == 'playlist' else [info]
    entradas: list[dict[str, Any]] = []
    for bruta in brutas:
        if not bruta:
            continue  # vídeo removido/privado vem como None
        vid = str(bruta.get('id') or '')
        if not vid or not _ID_VALIDO.match(vid):
            continue
        alvo = str(bruta.get('url') or bruta.get('webpage_url') or '')
        if not alvo.startswith('http'):
            alvo = f'https://www.youtube.com/watch?v={vid}'
        entradas.append({
            'id': vid,
            'url': alvo,
            'title': bruta.get('title'),
            'artist': bruta.get('uploader') or bruta.get('channel') or bruta.get('artist'),
            'duration': bruta.get('duration'),
        })
    return entradas


def run_download(chave: str, source_url: str, request: DownloadRequest,
                 qualidade: str) -> None:
    """Baixa o áudio em background, grava a faixa e dispara a separação."""
    set_download_state(chave, 'downloading', 'Baixando o áudio do YouTube...')
    options = {
        'quiet': True, 'no_warnings': True, 'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOADS / '%(id)s.%(ext)s'),
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
    }
    # Se existir arquivo de cookies exportado do navegador, usa para evitar bot
    cookies_path = ROOT / 'cookies.txt'
    if cookies_path.exists():
        options['cookiefile'] = str(cookies_path)
        print('[download] Usando cookies exportados do navegador.')
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source_url, download=True)
    except DownloadError as exc:
        msg = str(exc)
        if "Sign in" in msg or "bot" in msg.lower() or "confirm" in msg.lower():
            set_download_state(chave, 'error', 'YouTube pediu verificação. Tente outro vídeo ou aguarde.')
        else:
            set_download_state(chave, 'error', f'Não consegui baixar este vídeo: {exc}')
        return
    except Exception as exc:  # noqa: BLE001
        set_download_state(chave, 'error', 'Falha inesperada durante o download.')
        return

    if not info or not info.get('id'):
        set_download_state(chave, 'error', 'O YouTube não devolveu os dados do vídeo.')
        return

    track_id = info['id']
    if not _ID_VALIDO.match(track_id):
        set_download_state(chave, 'error', 'O YouTube devolveu um identificador inesperado.')
        return

    titulo, artista = normalizar_metadados(
        info.get('title'), info.get('track'), info.get('artist'), info.get('uploader'))
    with closing(db()) as conexao, conexao:
        conexao.execute(
            'INSERT OR REPLACE INTO tracks '
            '(id, title, artist, duration, source_url) VALUES (?, ?, ?, ?, ?)',
            (track_id, titulo or request.title or track_id,
             artista or request.artist,
             info.get('duration') or request.duration,
             info.get('webpage_url') or request.url or source_url),
        )
        conexao.execute(
            'INSERT OR REPLACE INTO track_covers (track_id, thumbnail) VALUES (?, ?)',
            (track_id, _capa_do_youtube(info)),
        )

    set_download_state(chave, 'ready', 'Download concluído.')
    if track_id != chave:
        set_download_state(track_id, 'ready', 'Download concluído.')
    set_state('separation', track_id, 'queued',
              'Download concluído. Separação aguardando início...')
    # Separação vira um job próprio: o worker do download fica livre na hora
    # (senão um Demucs de 10 min segurava uma das vagas limitadas da fila).
    # No modo festa o mesmo job encadeia a busca da letra e da afinação.
    alvo = ('main.run_separation_com_preparo'
            if getattr(request, 'preparar', False) else 'main.run_separation')
    fila.enfileirar('separation', track_id, alvo, track_id, qualidade)


@app.post('/api/download', dependencies=[Depends(exigir_anfitriao)])
def download(request: DownloadRequest) -> dict[str, Any]:
    """Enfileira o download na fila de tarefas (não bloqueia a request)."""
    source_url = request.url or request.id
    if not source_url:
        raise HTTPException(400, 'Informe a URL ou o ID do vídeo.')

    chave = _chave_download(request)
    atual = get_download_state(chave)
    # Deduplica downloads em andamento (duplo clique / reenvio).
    if atual and atual.get('status') in ('queued', 'downloading'):
        return {**(track_row(chave) or {'id': chave}), 'download': atual}

    qualidade = request.qualidade or 'equilibrada'
    estado = set_download_state(chave, 'queued', 'Download na fila...')
    job = fila.enfileirar('download', chave, 'main.run_download',
                          chave, source_url, request, qualidade)
    return {**(track_row(chave) or {'id': chave}), 'download': estado,
            'job': {'id': job['id'], 'status': job['status']}}


@app.post('/api/playlist', dependencies=[Depends(exigir_anfitriao)])
def baixar_playlist(request: PlaylistRequest) -> dict[str, Any]:
    """Baixa todas as músicas de uma playlist do YouTube/YouTube Music.

    Expande a playlist e enfileira um download por faixa (a separação de cada
    uma entra na fila na sequência, igual a um download normal). Faixas que já
    estão na biblioteca são puladas.
    """
    url = (request.url or '').strip()
    if not url.lower().startswith(('http://', 'https://')):
        raise HTTPException(400, 'Informe a URL de uma playlist (começando com http).')

    try:
        entradas = _expandir_playlist(url, request.limite)
    except Exception as exc:  # noqa: BLE001 - o yt-dlp lança erros variados
        raise HTTPException(400, f'Não consegui ler essa playlist: {exc}') from exc
    if not entradas:
        raise HTTPException(400, 'Não encontrei músicas nessa playlist.')

    qualidade = request.qualidade or 'equilibrada'
    faixas: list[dict[str, str]] = []
    preparando: list[dict[str, str]] = []
    ja_tinha = 0
    for entrada in entradas:
        titulo = entrada.get('title') or entrada['id']
        if track_row(entrada['id']):
            ja_tinha += 1
            # "Já baixada" não quer dizer "pronta pra cantar": no modo festa,
            # quem ainda não tem letra/afinação entra na fila também.
            if request.preparar and not _karaoke_pronto(entrada['id']):
                preparar_faixa(entrada['id'], qualidade)
                preparando.append({'id': entrada['id'], 'title': titulo})
            continue
        atual = get_download_state(entrada['id'])
        if atual and atual.get('status') in ('queued', 'downloading'):
            continue
        pedido = DownloadRequest(
            id=entrada['id'], url=entrada['url'], title=entrada.get('title'),
            artist=entrada.get('artist'), duration=entrada.get('duration'),
            qualidade=qualidade, preparar=request.preparar)
        set_download_state(entrada['id'], 'queued', 'Na fila (playlist)...')
        fila.enfileirar('download', entrada['id'], 'main.run_download',
                        entrada['id'], entrada['url'], pedido, qualidade)
        faixas.append({'id': entrada['id'], 'title': titulo})

    return {
        'total': len(entradas),
        'enfileiradas': len(faixas),
        'ja_tinha': ja_tinha,
        'preparando': preparando,
        'limite': request.limite,
        'preparar': request.preparar,
        'faixas': faixas,
    }


@app.post('/api/playlist/status')
def playlist_status(request: PlaylistStatusRequest) -> dict[str, Any]:
    """Estado de cada faixa da playlist: download -> separação -> karaokê.

    Um pedido só para todas as faixas de uma vez. Acompanhar 50 músicas com uma
    chamada por faixa a cada poucos segundos sobrecarregaria justamente a
    máquina que nesse momento já está separando e transcrevendo.
    """
    itens: list[dict[str, Any]] = []
    for bruto in request.ids[:200]:
        track_id = (bruto or '').strip()
        if not _ID_VALIDO.match(track_id):
            continue
        download = get_download_state(track_id) or {'status': 'not_started', 'message': ''}
        if download.get('status') == 'not_started' and (DOWNLOADS / f'{track_id}.mp3').exists():
            download = {'status': 'ready', 'message': 'Download concluído.'}
        separacao = get_state('separation', track_id) or {}
        karaoke = get_state('karaoke', track_id) or {}
        pronto = ((LYRICS / f'{track_id}.lrc').exists()
                  and (PITCHREF / f'{track_id}.json').exists())
        itens.append({
            'id': track_id,
            'download': download.get('status', 'not_started'),
            'separation': separacao.get('status', 'not_started'),
            'karaoke': 'ready' if pronto else karaoke.get('status', 'not_started'),
            'message': (karaoke.get('message') or separacao.get('message')
                        or download.get('message') or ''),
            'ready': pronto,
        })
    return {'itens': itens}


@app.post('/api/library/prepare', dependencies=[Depends(exigir_anfitriao)])
def preparar_biblioteca(request: SeparationRequest) -> dict[str, Any]:
    """Deixa prontas pra cantar todas as faixas que ainda não estão.

    Percorre a biblioteca e, para cada faixa incompleta, enfileira a separação
    (se faltar) e a busca de letra/afinação. É o "preparar a festa" para quem
    já tem músicas baixadas, sem precisar colar playlist nenhuma. Faixas que já
    estão prontas são puladas.
    """
    qualidade = request.qualidade or 'equilibrada'
    with closing(db()) as conexao:
        faixas = [dict(linha) for linha in conexao.execute(
            'SELECT id, title FROM tracks ORDER BY title COLLATE NOCASE').fetchall()]

    enfileiradas: list[dict[str, str]] = []
    ja_prontas = 0
    sem_audio = 0
    for faixa in faixas:
        track_id = faixa['id']
        if not (DOWNLOADS / f'{track_id}.mp3').exists():
            sem_audio += 1  # sem áudio não há o que preparar
            continue
        if not _precisa_preparo(track_id):
            ja_prontas += 1
            continue
        preparar_faixa(track_id, qualidade)
        enfileiradas.append({'id': track_id, 'title': faixa.get('title') or track_id})

    return {
        'total': len(faixas),
        'enfileiradas': len(enfileiradas),
        'ja_prontas': ja_prontas,
        'sem_audio': sem_audio,
        'qualidade': qualidade,
        'faixas': enfileiradas,
    }


@app.get('/api/download/{track_id}/status')
def download_status(track_id: str) -> dict[str, Any]:
    """Estado do download em background para o frontend acompanhar."""
    if not _ID_VALIDO.match(track_id or ''):
        raise HTTPException(400, 'Identificador de música inválido.')
    estado = get_download_state(track_id) or {
        'status': 'not_started', 'message': 'O download ainda não foi iniciado.'}
    return {**estado, 'file': (DOWNLOADS / f'{track_id}.mp3').exists()}


@app.get('/api/jobs')
def jobs() -> dict[str, Any]:
    """Fila de tarefas do servidor: o que está rodando e o que espera."""
    return fila.resumo()


@app.post('/api/tracks/{track_id}/cover', dependencies=[Depends(exigir_anfitriao)])
def atualizar_capa(track_id: str) -> dict[str, Any]:
    """Busca e salva a miniatura de uma faixa já baixada sem baixar o áudio."""
    row = exigir_faixa(track_id)
    if row.get('thumbnail'):
        return {'status': 'ok', 'thumbnail': row['thumbnail'], 'cached': True}
    if not row.get('source_url'):
        raise HTTPException(400, 'Esta faixa não tem URL de origem para buscar a capa.')

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(row['source_url'], download=False)
        thumbnail = _capa_do_youtube(info) if info else None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, 'Não consegui buscar a capa desta faixa.') from exc

    if not thumbnail:
        raise HTTPException(404, 'Esta faixa não possui miniatura disponível.')
    with closing(db()) as conexao, conexao:
        conexao.execute(
            'INSERT OR REPLACE INTO track_covers (track_id, thumbnail) VALUES (?, ?)',
            (track_id, thumbnail),
        )
    return {'status': 'ok', 'thumbnail': thumbnail, 'cached': False}


@app.get('/api/tracks')
def tracks() -> list[dict[str, Any]]:
    with closing(db()) as conexao:
        linhas = conexao.execute('SELECT * FROM tracks ORDER BY rowid DESC').fetchall()
        capas = {
            row['track_id']: row['thumbnail']
            for row in conexao.execute('SELECT track_id, thumbnail FROM track_covers')
        }
        estados = {
            row['track_id']: row['status']
            for row in conexao.execute(
                "SELECT track_id, status FROM states WHERE kind = 'separation'")
        }
        favoritas = {
            row['track_id']
            for row in conexao.execute('SELECT track_id FROM favoritas')
        }
    resultado = []
    for registro in linhas:
        tid = registro['id']
        pronto = (stems_dir(tid) / 'no_vocals.wav').exists()
        faixa = {**dict(registro), 'thumbnail': capas.get(tid)}
        faixa['separacao'] = {
            'pronto': pronto,
            'status': 'ready' if pronto else estados.get(tid, 'nao_iniciado'),
        }
        faixa['favorita'] = tid in favoritas
        resultado.append(faixa)
    return resultado


@app.put('/api/favoritas/{track_id}')
def alternar_favorita(track_id: str) -> dict[str, Any]:
    """Adiciona/remove a faixa das favoritas (estrela no Player)."""
    exigir_faixa(track_id)
    with closing(db()) as conexao, conexao:
        ja = conexao.execute(
            'SELECT 1 FROM favoritas WHERE track_id = ?', (track_id,)).fetchone()
        if ja:
            conexao.execute('DELETE FROM favoritas WHERE track_id = ?', (track_id,))
            favorita = False
        else:
            conexao.execute(
                'INSERT INTO favoritas (track_id, criada_em) VALUES (?, ?)',
                (track_id, datetime.now(timezone.utc).isoformat(timespec='seconds')))
            favorita = True
    return {'track_id': track_id, 'favorita': favorita}


@app.patch('/api/tracks/{track_id}')
def corrigir_metadados(track_id: str, request: MetadataRequest) -> dict[str, Any]:
    """Corrige título/artista da faixa e limpa letra/afinação para buscar de novo.

    É a saída quando a letra não é encontrada por causa de metadados sujos
    do YouTube ("Artista - Música (Vídeo Oficial)"): o usuário informa o nome
    certo e o karaokê tenta as fontes novamente.
    """
    row = exigir_faixa(track_id)
    titulo = (request.title or '').strip() or row['title']
    artista = (request.artist or '').strip()
    if request.artist is not None and not artista:
        artista = None
    elif not artista:
        artista = row.get('artist')
    with closing(db()) as conexao, conexao:
        conexao.execute(
            'UPDATE tracks SET title = ?, artist = ? WHERE id = ?',
            (titulo, artista, track_id),
        )
    (LYRICS / f'{track_id}.lrc').unlink(missing_ok=True)
    (PITCHREF / f'{track_id}.json').unlink(missing_ok=True)
    set_state('karaoke', track_id, 'not_started',
              'Metadados corrigidos. Pronto para buscar a letra de novo.')
    return track_row(track_id)


@app.delete('/api/tracks/{track_id}', dependencies=[Depends(exigir_anfitriao)])
def remover_faixa(track_id: str) -> dict[str, str]:
    """Apaga a faixa do banco e todos os arquivos associados (áudio/stems/letra/pitch)."""
    exigir_faixa(track_id)
    with closing(db()) as conexao, conexao:
        conexao.execute('DELETE FROM tracks WHERE id = ?', (track_id,))
        conexao.execute('DELETE FROM track_covers WHERE track_id = ?', (track_id,))
        conexao.execute('DELETE FROM states WHERE track_id = ?', (track_id,))
        conexao.execute('DELETE FROM favoritas WHERE track_id = ?', (track_id,))
    (DOWNLOADS / f'{track_id}.mp3').unlink(missing_ok=True)
    (LYRICS / f'{track_id}.lrc').unlink(missing_ok=True)
    (PITCHREF / f'{track_id}.json').unlink(missing_ok=True)
    shutil.rmtree(SEPARATED / track_id, ignore_errors=True)
    return {'status': 'ok', 'message': 'Música apagada.'}


@app.post('/api/separation/{track_id}', dependencies=[Depends(exigir_anfitriao)])
def start_separation(track_id: str, request: SeparationRequest | None = None) -> dict[str, str]:
    exigir_faixa(track_id)
    atual = get_state('separation', track_id) or {}
    # Deduplica: 'queued' também conta como em andamento.
    if atual.get('status') in ('queued', 'separating'):
        return atual
    qualidade = (request.qualidade if request else None) or 'equilibrada'
    estado = set_state('separation', track_id, 'queued',
                       f'Separação ({qualidade}) aguardando início...')
    fila.enfileirar('separation', track_id, 'main.run_separation', track_id, qualidade)
    return estado


@app.get('/api/separation/{track_id}')
def separation_status(track_id: str) -> dict[str, Any]:
    exigir_faixa(track_id)
    pronto = (stems_dir(track_id) / 'no_vocals.wav').exists()
    if pronto:
        estado = {'status': 'ready', 'message': 'Separação concluída.'}
    else:
        estado = get_state('separation', track_id) or {
            'status': 'not_started', 'message': 'A separação ainda não foi iniciada.'}
    return {'ready': pronto, **estado,
            'path': f'/data/separated/{track_id}/htdemucs'}


def _detectar_yt_dlp() -> str:
    # Prioriza o binário, mas cai para o módulo instalado no ambiente virtual.
    caminho = shutil.which('yt-dlp')
    if caminho:
        return caminho
    try:
        import yt_dlp  # noqa: PLC0415
        return f'módulo {getattr(yt_dlp.version, "__version__", "ok")}'
    except Exception:  # noqa: BLE001
        return 'não encontrado'


def _modelo_demucs_baixado() -> tuple[bool, str]:
    """Detecta o modelo do Demucs no cache.

    Versões antigas gravam arquivos ``.th`` no cache do ``torch.hub``; as novas
    (Demucs 4.x) baixam do Hugging Face Hub. Verificamos os dois formatos e
    devolvemos também o caminho do cache para exibir no diagnóstico.
    """
    cache_torch = Path.home() / '.cache' / 'torch' / 'hub' / 'checkpoints'
    if cache_torch.exists() and any(cache_torch.glob('*.th')):
        return True, str(cache_torch)

    try:
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
        caminho = try_to_load_from_cache('adefossez/HTDemucs', '955717e8.safetensors')
        if isinstance(caminho, str) and Path(caminho).exists():
            # .../hub/models--adefossez--HTDemucs/snapshots/<hash>/arquivo
            return True, str(Path(caminho).parents[2])
    except Exception:  # noqa: BLE001 - diagnóstico nunca deve quebrar
        pass

    return False, str(cache_torch)


@app.get('/api/diagnostics')
def diagnostics() -> dict[str, Any]:
    baixado, cache_info = _modelo_demucs_baixado()
    return {
        'yt_dlp': _detectar_yt_dlp(),
        'ffmpeg': shutil.which('ffmpeg') or 'não encontrado',
        'demucs_cache': cache_info,
        'demucs_model': 'baixado' if baixado else 'não baixado',
        'gpu': bool(torch.cuda.is_available()),
        'directories': {p.name: p.exists() for p in (DOWNLOADS, SEPARATED, LYRICS, PITCHREF)},
    }


@app.get('/api/network')
def network(request: Request) -> dict[str, Any]:
    """Retorna o endereço da rede local para o QR Code funcionar.

    Mesmo acessando por localhost, o QR precisa apontar para o IP da rede,
    para que os celulares dos convidados consigam abrir a aplicação.
    """
    ip = ip_da_rede()
    porta = request.url.port or 8099
    return {
        'ip': ip,
        'port': porta,
        'url': f'http://{ip}:{porta}' if ip else None,
        'anfitriao': veio_do_anfitriao(request),
    }


def _ranking(limite: int) -> list[dict[str, Any]]:
    with closing(db()) as conexao:
        linhas = conexao.execute(
            'SELECT id, singer, score, medal, track_id, track_title, created_at '
            'FROM scores ORDER BY score DESC, created_at ASC, id ASC LIMIT ?',
            (limite,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


@app.post('/api/scores')
def salvar_pontuacao(dados: ScoreRequest) -> dict[str, Any]:
    """Registra a pontuação de um cantor e devolve a posição alcançada."""
    apelido = dados.singer.strip()
    if not apelido:
        raise HTTPException(400, 'Informe o nome do cantor.')
    if dados.track_id:
        validar_id(dados.track_id)

    criado_em = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with closing(db()) as conexao, conexao:
        cursor = conexao.execute(
            'INSERT INTO scores (singer, score, medal, track_id, track_title, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (apelido[:40], dados.score, (dados.medal or '')[:16],
             dados.track_id, dados.track_title, criado_em),
        )
        novo_id = cursor.lastrowid
        posicao = conexao.execute(
            'SELECT COUNT(*) + 1 FROM scores WHERE score > ?', (dados.score,),
        ).fetchone()[0]

    return {
        'id': novo_id,
        'singer': apelido,
        'score': dados.score,
        'medal': dados.medal or '',
        'rank': posicao,
    }


@app.get('/api/scores')
def ranking(limit: int = 20) -> dict[str, Any]:
    """Devolve o ranking ordenado por pontuação (melhor primeiro)."""
    limite = max(1, min(limit, 100))
    linhas = _ranking(limite)
    for posicao, linha in enumerate(linhas, start=1):
        linha['rank'] = posicao
    return {'ranking': linhas}


@app.delete('/api/scores', dependencies=[Depends(exigir_anfitriao)])
def limpar_ranking() -> dict[str, str]:
    """Zera o ranking (útil para começar uma nova festa)."""
    with closing(db()) as conexao, conexao:
        conexao.execute('DELETE FROM scores')
    return {'status': 'ok', 'message': 'Ranking zerado.'}


@app.post('/api/karaoke/{track_id}/prepare', dependencies=[Depends(exigir_anfitriao)])
def karaoke_prepare(track_id: str, request: PrepareRequest) -> dict[str, str]:
    row = exigir_faixa(track_id)
    artista = (request.artist or row.get('artist') or '').strip()
    titulo = (request.title or row['title'] or '').strip()
    if not titulo:
        raise HTTPException(400, 'Informe ao menos o título da música.')
    estado = set_state('karaoke', track_id, 'preparing', 'Preparando letra e afinação...')
    fila.enfileirar('karaoke', track_id, 'main.prepare_karaoke', track_id, artista, titulo)
    return estado


@app.get('/api/karaoke/{track_id}/status')
def karaoke_status(track_id: str) -> dict[str, Any]:
    exigir_faixa(track_id)
    pronto = ((LYRICS / f'{track_id}.lrc').exists()
              and (PITCHREF / f'{track_id}.json').exists())
    if pronto:
        return {'ready': True, 'status': 'ready', 'message': 'Karaokê pronto.'}
    estado = get_state('karaoke', track_id) or {
        'status': 'not_started', 'message': 'O karaokê ainda não foi preparado.'}
    return {'ready': False, **estado}


@app.get('/api/karaoke/{track_id}')
def karaoke(track_id: str) -> dict[str, Any]:
    exigir_faixa(track_id)
    lyric_file = LYRICS / f'{track_id}.lrc'
    pitch_file = PITCHREF / f'{track_id}.json'
    if not lyric_file.exists() or not pitch_file.exists():
        # Devolve 200 dizendo que ainda não está pronto (em vez de 404, que
        # poluía o console do navegador). O frontend então dispara o /prepare.
        estado = get_state('karaoke', track_id) or {
            'status': 'not_started',
            'message': 'O karaokê ainda não foi preparado.'}
        return {'ready': False, **estado}

    try:
        lyrics = lyric_file.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        raise HTTPException(500, 'Não consegui ler a letra salva.') from exc

    try:
        pitch_ref = json.loads(pitch_file.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            500, 'O arquivo de afinação está corrompido. Refaça a separação.') from exc
    if not isinstance(pitch_ref, list) or not pitch_ref:
        return {'ready': False, 'status': 'error',
                'message': 'A referência de afinação está vazia. Refaça a separação.'}

    # Se a separação ainda não terminou, o "cantar já" usa o áudio original.
    # O frontend troca para o instrumental automaticamente quando os stems.
    stems_prontos = (stems_dir(track_id) / 'no_vocals.wav').exists()
    return {'ready': True, 'lyrics': lyrics, 'pitch_ref': pitch_ref,
            'stems_prontos': stems_prontos,
            'instrumental_url': (f'/data/separated/{track_id}/htdemucs/no_vocals.wav'
                                 if stems_prontos
                                 else f'/data/downloads/{track_id}.mp3')}


@app.get('/api/lyrics/{track_id}')
def get_track_lyrics(track_id: str) -> dict[str, Any]:
    """Retorna a letra da música em formato JSON consistente via a cadeia de 6 fontes.

    Formatos de retorno:
    - Sincronizado: {"found": true, "synced": true, "source": "...", "lines": [{"time": 12.5, "text": "verso"}]}
    - Sem sync:     {"found": true, "synced": false, "source": "...", "texto": "..."}
    - Não achou:    {"found": false}
    """
    row = exigir_faixa(track_id)
    audio_path = DOWNLOADS / f'{track_id}.mp3'
    try:
        return get_lyrics(
            track_id=track_id,
            title=row.get('title'),
            artist=row.get('artist'),
            duration=row.get('duration'),
            pasta_lyrics=LYRICS,
            audio_path=audio_path if audio_path.exists() else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f'[letras] Erro inesperado no endpoint de letras: {exc}', flush=True)
        return {'found': False}


@app.post('/api/lyrics/{track_id}/upload', dependencies=[Depends(exigir_anfitriao)])
def upload_lyrics(track_id: str, arquivo: UploadFile = File(...)) -> dict[str, str]:
    """Permite o upload manual de um arquivo .lrc para a faixa."""
    exigir_faixa(track_id)
    if not arquivo.filename or not arquivo.filename.lower().endswith('.lrc'):
        raise HTTPException(400, 'Envie um arquivo no formato .lrc.')
    try:
        conteudo_bytes = arquivo.file.read()
    finally:
        arquivo.file.close()
    if not conteudo_bytes:
        raise HTTPException(400, 'O arquivo .lrc está vazio.')

    conteudo_txt = conteudo_bytes.decode('utf-8', errors='replace')
    if not conteudo_txt.strip():
        raise HTTPException(400, 'O arquivo .lrc está vazio.')
    # Aceita .lrc com timestamps OU texto de letra (sem sync) com pelo menos
    # 2 versos — o frontend trata os dois casos. Rejeita lixo de 1 linha.
    if not validar_conteudo_lrc(conteudo_txt):
        linhas_texto = [
            l.strip() for l in conteudo_txt.splitlines()
            if l.strip() and not l.strip().startswith('[')
        ]
        if len(linhas_texto) < 2:
            raise HTTPException(
                400,
                'O arquivo não tem timestamps LRC (ex.: [00:12.50] verso) '
                'nem texto de letra suficiente.',
            )

    LYRICS.mkdir(parents=True, exist_ok=True)
    lyric_file = LYRICS / f'{track_id}.lrc'
    try:
        lyric_file.write_text(conteudo_txt, encoding='utf-8')
    except OSError as exc:
        raise HTTPException(500, 'Não consegui salvar o arquivo .lrc.') from exc

    return {'status': 'ok', 'message': 'Letra enviada com sucesso.'}


@app.post('/api/render', dependencies=[Depends(exigir_anfitriao)])
def renderizar_audio(arquivo: UploadFile = File(...)) -> FileResponse:
    """Converte a gravação (WebM/Opus) em M4A normalizado via FFmpeg.

    A normalização usa loudnorm (EBU R128) para evitar distorções e nível baixo.
    O arquivo final é temporário e apagado após o envio.
    """
    if not arquivo.filename:
        raise HTTPException(400, 'Envie um arquivo de áudio para renderizar.')
    try:
        dados = arquivo.file.read()
    finally:
        arquivo.file.close()
    if not dados:
        raise HTTPException(400, 'O arquivo de áudio está vazio.')
    if not shutil.which('ffmpeg'):
        raise HTTPException(503, 'O FFmpeg não está disponível neste servidor.')

    with tempfile.TemporaryDirectory() as pasta:
        entrada = Path(pasta) / 'entrada.webm'
        saida = Path(pasta) / 'saida.m4a'
        entrada.write_bytes(dados)
        try:
            resultado = subprocess.run(
                ['ffmpeg', '-y', '-i', str(entrada), '-af',
                 'loudnorm=I=-16:TP=-1.5:LRA=11', '-c:a', 'aac', '-b:a', '192k',
                 str(saida)],
                capture_output=True, text=True, timeout=300,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HTTPException(500, 'Falha ao processar o áudio.') from exc
        if resultado.returncode != 0 or not saida.exists():
            detalhe = (resultado.stderr or '').strip().splitlines()
            raise HTTPException(
                500, 'Não consegui converter o áudio: '
                + (detalhe[-1][:200] if detalhe else 'erro desconhecido'))

        # Copia para um arquivo estável, pois o diretório temporário é removido.
        # mkstemp devolve (fd, path); o fd precisa ser fechado senão vaza handle.
        fd, caminho_permanente = tempfile.mkstemp(suffix='.m4a')
        os.close(fd)
        permanente = Path(caminho_permanente)
        permanente.write_bytes(saida.read_bytes())

    return FileResponse(
        permanente,
        media_type='audio/mp4',
        filename='karaoke-normalizado.m4a',
        background=BackgroundTask(permanente.unlink, missing_ok=True),
    )
