"""Suíte de testes da API do Karaokê Studio.

Cobre: frontend, todos os endpoints e os caminhos de erro, sem tocar na rede
(yt-dlp é mockado) e sem a stack de ML (ver conftest.py).

    pip install pytest httpx fastapi requests yt-dlp numpy
    python -m pytest test_api.py -q
"""
import asyncio
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import main


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    """Cliente de teste com banco e pastas isolados em tmp_path."""
    monkeypatch.setattr(main, 'DB', tmp_path / 'teste.sqlite3')
    monkeypatch.setattr(main, '_separation_states', {})
    monkeypatch.setattr(main, '_karaoke_states', {})
    monkeypatch.setattr(main, '_download_states', {})

    # A fila de tarefas roda inline nos testes (determinismo, igual ao
    # BackgroundTasks do TestClient). Os testes que validam a fila em modo
    # background usam sua própria instância de FilaTarefas.
    main.fila.inline = True
    main.fila.limpar()
    # Conexões WebSocket não podem vazar entre testes.
    main.party.gerenciador._clientes.clear()

    # Nunca roda o Whisper de verdade nos testes: a transcrição é substituída
    # por um dublê. Os testes que exercitam a complementação monkeypatcham de
    # novo com os trechos que quiserem.
    monkeypatch.setattr('app.lyrics._transcrever_whisper_segmentos', lambda *a, **k: [])

    for nome in ('DOWNLOADS', 'SEPARATED', 'LYRICS', 'PITCHREF'):
        pasta = tmp_path / nome.lower()
        pasta.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(main, nome, pasta)

    # raise_server_exceptions=False reproduz produção: o cliente recebe 500
    # em vez de a exceção estourar dentro do teste.
    # client=('127.0.0.1', ...) faz o cliente se apresentar como o próprio PC:
    # é o que o anfitrião faz ao abrir o app por localhost.
    with TestClient(main.app, raise_server_exceptions=False,
                    client=('127.0.0.1', 51000)) as teste:
        yield teste


@pytest.fixture
def cliente_externo(client):
    """Cliente que se apresenta como um celular da rede (não é o anfitrião).

    Depende de `client` de propósito: reaproveita o banco e as pastas isoladas
    daquele teste.
    """
    with TestClient(main.app, raise_server_exceptions=False,
                    client=('192.168.1.50', 52000)) as teste:
        yield teste


@pytest.fixture
def faixa(client):
    """Insere uma faixa no banco sem passar pelo download."""
    with main.closing(main.db()) as conexao, conexao:
        conexao.execute(
            'INSERT OR REPLACE INTO tracks VALUES (?, ?, ?, ?, ?)',
            ('abc123', 'Evidências', 'Chitãozinho', 214.0, 'https://exemplo/abc123'),
        )
    return {'id': 'abc123', 'title': 'Evidências', 'artist': 'Chitãozinho'}


class FakeYDL:
    """Dublê do YoutubeDL: dados fixos, sem internet."""

    entradas = [{
        'id': 'abc123',
        'title': "Don't Stop",
        'uploader': 'Chitãozinho',
        'duration': 214,
        'webpage_url': 'https://exemplo/abc123',
    }]
    erro: Exception | None = None
    opcoes: dict = {}

    def __init__(self, options):
        self.options = options
        type(self).opcoes = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if type(self).erro:
            raise type(self).erro
        if url.startswith('ytsearch'):
            return {'entries': type(self).entradas}
        return type(self).entradas[0]


@pytest.fixture
def yt_dlp_mock(monkeypatch):
    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', FakeYDL)
    FakeYDL.erro = None
    FakeYDL.opcoes = {}
    return FakeYDL


def sem_500(response):
    assert response.status_code != 500, (
        f'Retornou 500 em {response.request.url}: {response.text[:300]}')
    return response


def achar_por(corpo, chave, valor):
    return next((item for item in corpo if item.get(chave) == valor), None)


# --------------------------------------------------------------------------
# 1. Frontend e estáticos
# --------------------------------------------------------------------------
def test_frontend_carrega(client):
    response = client.get('/')
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    assert 'Buscar' in response.text
    assert 'id="karaokeOverlay"' in response.text


def test_frontend_tem_botao_sair_do_karaoke(client):
    """Regressão do bug B1: o overlay era inescapable."""
    assert 'id="stopKaraoke"' in client.get('/').text


def test_karaoke_js_registra_listener_do_botao_sair(client):
    """Garante que o JS realmente liga o botão (B1)."""
    js = client.get('/static/karaoke.js').text
    assert 'stopKaraoke' in js and 'addEventListener' in js


def test_frontend_tem_seletor_de_jogador(client):
    """Ao entrar no karaokê, o modal permite escolher ou digitar o jogador."""
    texto = client.get('/').text
    assert 'id="playerModal"' in texto
    assert 'id="playerName"' in texto
    js = client.get('/static/app.js').text
    assert 'abrirSeletorJogador' in js
    assert 'vocalis_players' in js


def test_karaoke_nao_recria_mediaelement_source(client):
    """Regressão: recriar MediaElementSource lança InvalidStateError."""
    js = client.get('/static/karaoke.js').text
    assert 'fonteAudioCriada' in js


def test_karaoke_tem_vad_e_deteccao_de_seek(client):
    js = client.get('/static/karaoke.js').text
    assert 'RMS_MINIMO_VOZ' in js
    assert 'resetarSegmentoPorSeek' in js


def test_dueto_permite_segundo_microfone(client):
    assert 'id="duetMic"' in client.get('/').text
    assert 'conectarSegundoMicrofone' in client.get('/static/karaoke.js').text


def test_player_controles_acessiveis(client):
    """Tempo combinado, rótulo de perfil e a11y da seekbar."""
    html = client.get('/').text
    assert 'class="time-total"' in html
    assert 'aria-valuetext' in html
    assert 'profile-name' in html
    js = client.get('/static/app.js').text
    assert 'atualizarPerfil' in js
    assert 'aria-valuetext' in js


def test_busca_automatica_ao_digitar(client):
    """A aba Buscar pesquisa enquanto digita, com debounce e proteção contra respostas antigas."""
    js = client.get('/static/app.js').text
    assert 'buscaTimer' in js
    assert 'buscaSeq' in js
    assert "addEventListener('input'" in js


def test_busca_na_aba_player(client):
    """A aba Player tem busca própria: filtra a biblioteca e busca no YouTube."""
    html = client.get('/').text
    assert 'id="playerQuery"' in html
    assert 'id="playerSearchBtn"' in html
    assert 'id="playerResults"' in html
    js = client.get('/static/app.js').text
    assert 'buscarNoPlayer' in js
    assert 'renderTracks' in js
    assert 'download-player' in js


def test_voz_nao_retorna_nas_caixas_por_padrao(client):
    """A voz nunca vai para o alto-falante sem ligar 'Ouvir minha voz'."""
    js = client.get('/static/karaoke.js').text
    # A voz só chega à saída pelo monitor, que é condicional.
    assert "if ($('#monitorVoice')?.checked) ganhoMonitor.connect(ctx.destination)" in js
    assert 'compressorMix.connect(ctx.destination)' not in js
    assert 'panInstrumental.connect(ctx.destination)' not in js
    # Começa sempre desligado e alterna ao vivo.
    assert 'monitorVoice.checked = false' in js
    assert "addEventListener('change', atualizarMonitoramento)" in js


def test_afinacao_mantem_analisador_processado(client):
    """O analisador precisa de um caminho até a saída (ganho zero) para ser processado."""
    js = client.get('/static/karaoke.js').text
    assert 'analisador.connect(silencioAnalise).connect(ctx.destination)' in js
    assert 'silencioAnalise.gain.value = 0' in js
    # Instrumental audível de forma persistente (funciona fora do karaokê também).
    assert 'fonteAudio.connect(speakerGain)' in js


def test_karaoke_ajustes_em_menu_lateral(client):
    """Os controles do karaokê ficam em um drawer lateral acessível."""
    html = client.get('/').text
    assert 'id="karaokeSettingsBtn"' in html
    assert 'id="karaokePanel"' in html
    assert 'id="karaokePanelBackdrop"' in html
    js = client.get('/static/karaoke.js').text
    assert 'alternarPainelAjustes' in js
    assert '.karaoke-panel.open' in client.get('/static/styles.css').text


def test_karaoke_fundo_defocado(client):
    """Fundo do karaokê usa a capa da faixa, de forma segura."""
    html = client.get('/').text
    assert 'id="karaokeBackdrop"' in html
    js = client.get('/static/karaoke.js').text
    assert 'aplicarFundoKaraoke' in js
    assert 'JSON.stringify(url)' in js
    assert 'karaoke-backdrop' in client.get('/static/styles.css').text


def test_modo_palco_antimicrofonia(client):
    """Modo palco com high-pass, notch e detecção de apito."""
    html = client.get('/').text
    assert 'id="stageMode"' in html
    assert 'id="detectFeedback"' in html
    js = client.get('/static/karaoke.js').text
    assert 'notchFilter' in js
    assert 'detectarApito' in js
    assert "type = 'highpass'" in js


def test_player_tem_controle_de_volume(client):
    html = client.get('/').text
    assert 'id="volume"' in html
    assert 'id="muteToggle"' in html
    assert 'aria-label="Volume"' in html
    js = client.get('/static/app.js').text
    assert 'definirVolume' in js
    assert 'atualizarIconeVolume' in js


def test_service_worker_limita_cache(client):
    sw = client.get('/static/sw.js').text
    assert 'MAX_ITENS' in sw
    assert 'vocalis-audio-v2' in sw


def test_render_converte_para_m4a(client):
    """A exportação normaliza e converte para M4A usando FFmpeg."""
    import io
    import math
    import shutil
    import struct
    import wave

    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg não está disponível neste ambiente.')

    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b''.join(
            struct.pack('<h', int(3000 * math.sin(2 * math.pi * 440 * i / 8000)))
            for i in range(1600)
        ))

    resposta = client.post(
        '/api/render',
        files={'arquivo': ('gravacao.webm', buffer.getvalue(), 'audio/webm')},
    )
    assert resposta.status_code == 200
    assert resposta.headers['content-type'].startswith('audio/mp4')
    assert len(resposta.content) > 0


def test_render_arquivo_vazio_400(client):
    resposta = client.post(
        '/api/render',
        files={'arquivo': ('vazio.webm', b'', 'audio/webm')},
    )
    assert resposta.status_code == 400


def test_karaoke_js_faz_scroll_da_letra(client):
    """Regressão do bug B2: a letra não rolava."""
    assert 'scrollIntoView' in client.get('/static/karaoke.js').text


def test_app_js_nao_interpola_json_em_onclick(client):
    """Regressão do bug B3: títulos com apóstrofo quebravam a UI."""
    js = client.get('/static/app.js').text
    assert 'onclick=${JSON.stringify' not in js
    assert "onclick='downloadTrack" not in js


def test_karaoke_js_iife_com_sintaxe_valida(client):
    """Regressão: `(() => {…}())` é sintaxe inválida e derrubava o arquivo
    inteiro — o botão Sair e o módulo Karaoke deixavam de existir."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which('node')
    if not node:  # pragma: no cover
        pytest.skip('node indisponível')
    with tempfile.TemporaryDirectory() as pasta:
        destino = pathlib.Path(pasta) / 'karaoke.js'
        destino.write_text(client.get('/static/karaoke.js').text, encoding='utf-8')
        resultado = subprocess.run([node, '--check', str(destino)],
                                   capture_output=True, text=True)
        assert resultado.returncode == 0, resultado.stderr


def test_app_js_sintaxe_valida(client):
    """Garante que app.js também é parseável pelo navegador."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which('node')
    if not node:  # pragma: no cover
        pytest.skip('node indisponível')
    with tempfile.TemporaryDirectory() as pasta:
        destino = pathlib.Path(pasta) / 'app.js'
        destino.write_text(client.get('/static/app.js').text, encoding='utf-8')
        resultado = subprocess.run([node, '--check', str(destino)],
                                   capture_output=True, text=True)
        assert resultado.returncode == 0, resultado.stderr


def test_assets_estaticos_servidos(client):
    for caminho in ('/static/app.js', '/static/karaoke.js', '/static/styles.css'):
        assert sem_500(client.get(caminho)).status_code == 200


# --------------------------------------------------------------------------
# 2. Busca
# --------------------------------------------------------------------------
def test_busca_retorna_resultados(client, yt_dlp_mock):
    response = client.post('/api/search', json={'query': 'Evidências'})
    assert response.status_code == 200
    corpo = response.json()
    assert len(corpo) == 1
    assert corpo[0]['id'] == 'abc123'
    # Título com apóstrofo deve chegar intacto (bug B3).
    assert corpo[0]['title'] == "Don't Stop"
    assert yt_dlp_mock.opcoes.get('skip_download') is True


def test_busca_filtro_estudio_filtra_ao_vivo(client, yt_dlp_mock, monkeypatch):
    class ComLive(FakeYDL):
        entradas = [
            {'id': 'a1', 'title': 'Música (Ao Vivo)'},
            {'id': 'a2', 'title': 'Música (Áudio Oficial)'},
        ]

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', ComLive)
    corpo = sem_500(client.post('/api/search', json={'query': 'musica', 'filtro': 'estudio'})).json()
    ids = [x['id'] for x in corpo]
    assert 'a1' not in ids
    assert 'a2' in ids


def test_busca_filtro_estudio_envia_sufixo(client, yt_dlp_mock, monkeypatch):
    capturado = {}

    class Conta(FakeYDL):
        def extract_info(self, url, download=False):
            capturado['url'] = url
            return {'entries': type(self).entradas}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', Conta)
    client.post('/api/search', json={'query': 'musica', 'filtro': 'estudio'})
    assert 'audio oficial' in capturado['url']


def test_preview_retorna_url(client, monkeypatch):
    class ComAudio(FakeYDL):
        def extract_info(self, url, download=False):
            return {'url': 'https://audio.test/x.m4a', 'title': 'Ok', 'duration': 100}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', ComAudio)
    corpo = sem_500(client.post('/api/preview', json={'id': 'abc'})).json()
    assert corpo['url'] == 'https://audio.test/x.m4a'


def test_preview_sem_alvo_400(client):
    assert client.post('/api/preview', json={}).status_code == 400


def test_frontend_tem_previa(client):
    assert 'id="previewAudio"' in client.get('/').text
    js = client.get('/static/app.js').text
    assert 'tocarPrevia' in js
    assert "action === 'preview'" in js
    assert 'data-action="preview"' in js


def test_busca_filtro_invalido_422(client, yt_dlp_mock):
    assert client.post('/api/search', json={'query': 'musica', 'filtro': 'zap'}).status_code == 422


def test_frontend_tem_filtro_de_busca(client):
    assert 'id="searchFilter"' in client.get('/').text
    js = client.get('/static/app.js').text
    assert "searchFilter" in js


def test_busca_respeita_limite(client, monkeypatch):
    """O campo limite define quantos resultados pedir ao YouTube."""
    chamadas = {}

    class Conta(FakeYDL):
        def extract_info(self, url, download=False):
            chamadas['url'] = url
            return {'entries': type(self).entradas}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', Conta)
    sem_500(client.post('/api/search', json={'query': 'evidencias', 'limite': 25}))
    assert chamadas['url'].startswith('ytsearch25:')


def test_busca_limite_invalido_422(client, yt_dlp_mock):
    assert client.post('/api/search', json={'query': 'evidencias', 'limite': 99}).status_code == 422


def test_botao_mostrar_mais(client):
    assert 'id="moreResults"' in client.get('/').text
    js = client.get('/static/app.js').text
    assert 'limiteBusca' in js
    assert 'LIMITE_MAX' in js


def test_busca_deriva_capa_pelo_id(client, yt_dlp_mock):
    """Sem miniatura no flat extract, a capa é derivada do ID do YouTube."""
    corpo = sem_500(client.post('/api/search', json={'query': 'evidencias'})).json()
    assert corpo[0]['thumbnail'] == 'https://i.ytimg.com/vi/abc123/mqdefault.jpg'


def test_busca_respeita_thumbnail_existente(client, monkeypatch):
    class ComCapa(FakeYDL):
        entradas = [{'id': 'x1', 'title': 'ok', 'thumbnail': 'https://img.test/c.jpg'}]

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', ComCapa)
    corpo = sem_500(client.post('/api/search', json={'query': 'evidencias'})).json()
    assert corpo[0]['thumbnail'] == 'https://img.test/c.jpg'


@pytest.mark.parametrize('query', ['', 'a'])
def test_busca_query_curta_422(client, yt_dlp_mock, query):
    """Regressão do R5: query vazia chegava ao YouTube."""
    assert client.post('/api/search', json={'query': query}).status_code == 422


def test_busca_query_longa_422(client, yt_dlp_mock):
    assert client.post('/api/search', json={'query': 'x' * 200}).status_code == 422


def test_busca_json_invalido_422(client):
    response = client.post('/api/search', content='nao-e-json',
                           headers={'Content-Type': 'application/json'})
    assert response.status_code == 422


def test_busca_campo_faltando_422(client):
    assert client.post('/api/search', json={}).status_code == 422


def test_busca_erro_do_ytdlp_vira_502(client, monkeypatch):
    """Regressão do B5: DownloadError virava 500."""
    import yt_dlp.utils

    class Quebrado(FakeYDL):
        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError('vídeo indisponível')

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', Quebrado)
    response = sem_500(client.post('/api/search', json={'query': 'evidencias'}))
    assert response.status_code == 502


def test_busca_entradas_none_nao_quebra(client, monkeypatch):
    """Regressão do B6: entrada None virava 500."""
    class ComNone(FakeYDL):
        def extract_info(self, url, download=False):
            return {'entries': [None, {'id': 'x1', 'title': 'ok'}]}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', ComNone)
    corpo = sem_500(client.post('/api/search', json={'query': 'evidencias'})).json()
    assert len(corpo) == 1 and corpo[0]['id'] == 'x1'


def test_busca_sem_entradas(client, monkeypatch):
    class Vazio(FakeYDL):
        def extract_info(self, url, download=False):
            return {}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', Vazio)
    assert client.post('/api/search', json={'query': 'evidencias'}).json() == []


# --------------------------------------------------------------------------
# 3. Download
# --------------------------------------------------------------------------
def test_download_grava_no_banco(client, yt_dlp_mock):
    response = client.post('/api/download', json={
        'url': 'https://exemplo/abc123', 'id': 'abc123', 'title': 'Evidências'})
    assert response.status_code == 200
    corpo = response.json()
    assert corpo['id'] == 'abc123'
    assert corpo['download']['status'] in ('queued', 'downloading', 'ready')
    # Roda o job de forma síncrona (não depende do scheduler do TestClient).
    req = main.DownloadRequest(url='https://exemplo/abc123', id='abc123', title='Evidências')
    main.run_download('abc123', req.url or req.id, req, 'equilibrada')
    assert achar_por(client.get('/api/tracks').json(), 'id', 'abc123')
    assert client.get('/api/download/abc123/status').json()['status'] == 'ready'


def test_download_sem_url_400(client):
    assert client.post('/api/download', json={'title': 'sem url'}).status_code == 400


def test_download_erro_do_ytdlp_vira_estado(client, monkeypatch):
    """Download em background: erro do yt-dlp vira estado, não 400 na request."""
    import yt_dlp.utils

    class Quebrado(FakeYDL):
        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError('URL inválida')

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', Quebrado)
    resp = sem_500(client.post('/api/download', json={
        'url': 'https://exemplo/ruim', 'id': 'ruim01', 'title': 'Ruim'}))
    assert resp.status_code == 200
    assert resp.json()['download']['status'] in ('queued', 'downloading', 'error')
    # Força a execução síncrona do job de fundo.
    req = main.DownloadRequest(url='https://exemplo/ruim', id='ruim01', title='Ruim')
    main.run_download('ruim01', req.url or req.id, req, 'equilibrada')
    estado = client.get('/api/download/ruim01/status').json()
    assert estado['status'] == 'error'
    assert 'não consegui' in estado['message'].lower()


def test_download_deduplica_jobs(client, yt_dlp_mock, monkeypatch):
    """Duplo clique não enfileira dois downloads da mesma faixa."""
    chamados = []

    def fake_run_download(chave, source_url, request, qualidade):
        chamados.append(chave)
        # Fica 'downloading' (nunca 'ready') para simular job ainda em curso.
        main.set_download_state(chave, 'downloading', 'Baixando...')

    monkeypatch.setattr(main, 'run_download', fake_run_download)
    client.post('/api/download', json={
        'url': 'https://exemplo/abc123', 'id': 'abc123', 'title': 'Evidências'})
    segundo = client.post('/api/download', json={
        'url': 'https://exemplo/abc123', 'id': 'abc123', 'title': 'Evidências'}).json()
    assert segundo['download']['status'] == 'downloading'
    assert chamados == ['abc123']


def test_download_status_id_invalido_400(client):
    assert client.get('/api/download/..%2Fetc/status').status_code in (400, 404)


def test_expandir_playlist_le_entradas(monkeypatch):
    """A playlist é lida sem baixar e entradas vazias (vídeo removido) caem."""

    class PlaylistYDL:
        opcoes = {}

        def __init__(self, options):
            PlaylistYDL.opcoes = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            return {'_type': 'playlist', 'entries': [
                {'id': 'v1', 'title': 'Um', 'url': 'https://youtu.be/v1'},
                {'id': 'v2', 'title': 'Dois'},
                None,
                {'id': 'v3', 'title': 'Três', 'duration': 180},
            ]}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', PlaylistYDL)
    entradas = main._expandir_playlist('https://youtube.com/playlist?list=X', 50)

    assert [e['id'] for e in entradas] == ['v1', 'v2', 'v3']
    # Sem URL na entrada, monta a URL canônica do vídeo.
    assert entradas[1]['url'] == 'https://www.youtube.com/watch?v=v2'
    assert PlaylistYDL.opcoes.get('extract_flat')
    assert PlaylistYDL.opcoes.get('playlistend') == 50


def test_playlist_enfileira_cada_musica(client, monkeypatch):
    """Cada música da playlist vira um download na fila."""
    faixas = [
        {'id': 'v1', 'url': 'https://youtu.be/v1', 'title': 'Um',
         'artist': 'A', 'duration': 100.0},
        {'id': 'v2', 'url': 'https://youtu.be/v2', 'title': 'Dois',
         'artist': 'B', 'duration': 200.0},
    ]
    monkeypatch.setattr(main, '_expandir_playlist', lambda url, limite: faixas)
    baixados = []
    monkeypatch.setattr(main, 'run_download',
                        lambda chave, *a, **k: baixados.append(chave))

    resp = client.post('/api/playlist', json={'url': 'https://youtube.com/playlist?list=X'})

    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo['enfileiradas'] == 2
    assert corpo['ja_tinha'] == 0
    assert [f['id'] for f in corpo['faixas']] == ['v1', 'v2']
    assert baixados == ['v1', 'v2']


def test_playlist_pula_ja_baixadas(client, faixa, monkeypatch):
    """Faixa que já está na biblioteca não é baixada de novo."""
    monkeypatch.setattr(main, '_expandir_playlist', lambda url, limite: [
        {'id': 'abc123', 'url': 'https://youtu.be/abc123', 'title': 'Evidências'},
        {'id': 'v9', 'url': 'https://youtu.be/v9', 'title': 'Nova'},
    ])
    baixados = []
    monkeypatch.setattr(main, 'run_download',
                        lambda chave, *a, **k: baixados.append(chave))

    corpo = client.post('/api/playlist', json={
        'url': 'https://youtube.com/playlist?list=X'}).json()

    assert corpo['ja_tinha'] == 1
    assert corpo['enfileiradas'] == 1
    assert baixados == ['v9']


def test_playlist_url_invalida_400(client):
    assert client.post('/api/playlist', json={'url': 'nao-e-url'}).status_code in (400, 422)


def test_playlist_sem_musicas_400(client, monkeypatch):
    monkeypatch.setattr(main, '_expandir_playlist', lambda url, limite: [])
    resp = client.post('/api/playlist', json={'url': 'https://youtube.com/playlist?list=X'})
    assert resp.status_code == 400


def test_playlist_erro_do_ytdlp_vira_400(client, monkeypatch):
    """Playlist ilegível devolve erro claro, sem estourar 500."""
    def quebrado(url, limite):
        raise main.yt_dlp.utils.DownloadError('playlist privada')

    monkeypatch.setattr(main, '_expandir_playlist', quebrado)
    resp = sem_500(client.post('/api/playlist', json={
        'url': 'https://youtube.com/playlist?list=X'}))
    assert resp.status_code == 400
    assert 'playlist' in resp.json()['detail'].lower()


# --------------------------------------------------------------------------
# 3b. Modo festa (deixar tudo pronto pra cantar)
# --------------------------------------------------------------------------
def test_download_modo_festa_encadeia_preparo(client, yt_dlp_mock, monkeypatch):
    """Com preparar=True o download manda separar E preparar, não só separar."""
    chamadas = []
    monkeypatch.setattr(main, 'run_separation',
                        lambda tid, q='equilibrada': chamadas.append(('simples', tid)))
    monkeypatch.setattr(main, 'run_separation_com_preparo',
                        lambda tid, q='equilibrada': chamadas.append(('festa', tid)))

    req = main.DownloadRequest(url='https://exemplo/abc123', id='abc123',
                               title='Evidências', preparar=True)
    main.run_download('abc123', req.url, req, 'equilibrada')

    assert chamadas == [('festa', 'abc123')]


def test_download_normal_nao_encadeia_preparo(client, yt_dlp_mock, monkeypatch):
    """Sem preparar=True o comportamento antigo (só separação) continua."""
    chamadas = []
    monkeypatch.setattr(main, 'run_separation',
                        lambda tid, q='equilibrada': chamadas.append(('simples', tid)))
    monkeypatch.setattr(main, 'run_separation_com_preparo',
                        lambda tid, q='equilibrada': chamadas.append(('festa', tid)))

    req = main.DownloadRequest(url='https://exemplo/abc123', id='abc123',
                               title='Evidências')
    main.run_download('abc123', req.url, req, 'equilibrada')

    assert chamadas == [('simples', 'abc123')]


def test_separacao_com_preparo_enfileira_karaoke(client, faixa, monkeypatch):
    """Terminada a separação, letra/afinação entram na fila como job próprio."""
    separadas, preparadas = [], []
    monkeypatch.setattr(main, 'run_separation',
                        lambda tid, q='equilibrada': separadas.append((tid, q)))
    monkeypatch.setattr(main, 'prepare_karaoke',
                        lambda tid, artista, titulo: preparadas.append((tid, artista, titulo)))

    main.run_separation_com_preparo('abc123', 'rapida')

    assert separadas == [('abc123', 'rapida')]
    assert preparadas == [('abc123', 'Chitãozinho', 'Evidências')]
    assert main.get_state('karaoke', 'abc123')['status'] == 'queued'


def test_separacao_com_preparo_nao_refaz_quando_ja_pronto(client, faixa, monkeypatch):
    """Faixa que já tem letra e afinação não é preparada de novo."""
    (main.LYRICS / 'abc123.lrc').write_text('[00:01.00] verso', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text('{}', encoding='utf-8')
    preparadas = []
    monkeypatch.setattr(main, 'run_separation', lambda tid, q='equilibrada': None)
    monkeypatch.setattr(main, 'prepare_karaoke',
                        lambda tid, a, t: preparadas.append(tid))

    main.run_separation_com_preparo('abc123')

    assert preparadas == []


def test_preparar_faixa_vai_direto_ao_karaoke_se_ja_separada(client, faixa, monkeypatch):
    """Se os stems já existem, não manda separar de novo (economiza minutos)."""
    stems = main.stems_dir('abc123')
    stems.mkdir(parents=True, exist_ok=True)
    (stems / 'no_vocals.wav').write_bytes(b'wav')

    chamadas = []
    monkeypatch.setattr(main, 'run_separation_com_preparo',
                        lambda tid, q='equilibrada': chamadas.append('separa'))
    monkeypatch.setattr(main, 'prepare_karaoke',
                        lambda tid, a, t: chamadas.append('karaoke'))

    main.preparar_faixa('abc123')

    assert chamadas == ['karaoke']


def test_preparar_faixa_enfileira_separacao_quando_falta(client, faixa, monkeypatch):
    chamadas = []
    monkeypatch.setattr(main, 'run_separation_com_preparo',
                        lambda tid, q='equilibrada': chamadas.append((tid, q)))

    main.preparar_faixa('abc123', 'alta')

    assert chamadas == [('abc123', 'alta')]


def test_playlist_modo_festa_prepara_ja_baixadas(client, faixa, monkeypatch):
    """Música já na biblioteca e sem letra entra no preparo (aparece em 'preparando')."""
    monkeypatch.setattr(main, '_expandir_playlist', lambda url, limite: [
        {'id': 'abc123', 'url': 'https://youtu.be/abc123', 'title': 'Evidências'},
    ])
    preparadas = []
    monkeypatch.setattr(main, 'preparar_faixa',
                        lambda tid, q='equilibrada': preparadas.append(tid))

    corpo = client.post('/api/playlist', json={
        'url': 'https://youtube.com/playlist?list=X', 'preparar': True}).json()

    assert corpo['ja_tinha'] == 1
    assert corpo['enfileiradas'] == 0
    assert [f['id'] for f in corpo['preparando']] == ['abc123']
    assert preparadas == ['abc123']


def test_playlist_sem_preparar_nao_mexe_nas_ja_baixadas(client, faixa, monkeypatch):
    monkeypatch.setattr(main, '_expandir_playlist', lambda url, limite: [
        {'id': 'abc123', 'url': 'https://youtu.be/abc123', 'title': 'Evidências'},
    ])
    preparadas = []
    monkeypatch.setattr(main, 'preparar_faixa',
                        lambda tid, q='equilibrada': preparadas.append(tid))

    corpo = client.post('/api/playlist', json={
        'url': 'https://youtube.com/playlist?list=X'}).json()

    assert corpo['ja_tinha'] == 1
    assert corpo['preparando'] == []
    assert preparadas == []


def test_playlist_status_agrega_estados(client, faixa):
    """Um pedido só devolve download/separação/karaokê de várias faixas."""
    main.set_download_state('abc123', 'ready', 'Download concluído.')
    main.set_state('separation', 'abc123', 'ready', 'Separação concluída.')
    main.set_state('karaoke', 'abc123', 'preparing', 'Buscando letra...')

    corpo = client.post('/api/playlist/status',
                        json={'ids': ['abc123', 'nao!', 'zzz999']}).json()

    itens = {i['id']: i for i in corpo['itens']}
    assert 'nao!' not in itens  # id inválido é ignorado
    assert itens['abc123']['download'] == 'ready'
    assert itens['abc123']['separation'] == 'ready'
    assert itens['abc123']['karaoke'] == 'preparing'
    assert itens['abc123']['ready'] is False
    assert itens['zzz999']['download'] == 'not_started'


def test_playlist_status_marca_pronta_com_letra_e_pitch(client, faixa):
    (main.LYRICS / 'abc123.lrc').write_text('[00:01.00] verso', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text('{}', encoding='utf-8')

    corpo = client.post('/api/playlist/status', json={'ids': ['abc123']}).json()

    assert corpo['itens'][0]['ready'] is True


def test_playlist_status_reconhece_download_feito_antes_do_restart(client, faixa):
    """Sem estado em memória (servidor reiniciado), o mp3 no disco conta como baixado."""
    (main.DOWNLOADS / 'abc123.mp3').write_bytes(b'mp3')

    corpo = client.post('/api/playlist/status', json={'ids': ['abc123']}).json()

    assert corpo['itens'][0]['download'] == 'ready'


# --------------------------------------------------------------------------
# 3c. Preparar a biblioteca inteira
# --------------------------------------------------------------------------
def test_library_prepare_enfileira_incompletas(client, faixa, monkeypatch):
    """Faixa sem stems entra como separação + preparo, com a qualidade escolhida."""
    (main.DOWNLOADS / 'abc123.mp3').write_bytes(b'mp3')
    chamadas = []
    monkeypatch.setattr(main, 'run_separation_com_preparo',
                        lambda tid, q='equilibrada': chamadas.append((tid, q)))
    monkeypatch.setattr(main, 'prepare_karaoke', lambda *a, **k: chamadas.append(a))

    corpo = client.post('/api/library/prepare', json={'qualidade': 'alta'}).json()

    assert corpo['total'] == 1
    assert corpo['enfileiradas'] == 1
    assert corpo['ja_prontas'] == 0
    assert corpo['sem_audio'] == 0
    assert [f['id'] for f in corpo['faixas']] == ['abc123']
    assert chamadas == [('abc123', 'alta')]


def test_library_prepare_vai_direto_ao_karaoke_quando_ja_separada(client, faixa, monkeypatch):
    """Com os stems no disco, pula a separação e vai direto para a letra."""
    (main.DOWNLOADS / 'abc123.mp3').write_bytes(b'mp3')
    stems = main.stems_dir('abc123')
    stems.mkdir(parents=True, exist_ok=True)
    (stems / 'no_vocals.wav').write_bytes(b'wav')
    chamadas = []
    monkeypatch.setattr(main, 'run_separation_com_preparo',
                        lambda *a, **k: chamadas.append('separou'))
    monkeypatch.setattr(main, 'prepare_karaoke',
                        lambda tid, artista, titulo: chamadas.append(('karaoke', tid)))

    corpo = client.post('/api/library/prepare', json={}).json()

    assert corpo['enfileiradas'] == 1
    assert chamadas == [('karaoke', 'abc123')]


def test_library_prepare_pula_quem_ja_esta_pronta(client, faixa, monkeypatch):
    """Faixa completa (stems + letra + afinação) não gera trabalho nenhum."""
    (main.DOWNLOADS / 'abc123.mp3').write_bytes(b'mp3')
    stems = main.stems_dir('abc123')
    stems.mkdir(parents=True, exist_ok=True)
    (stems / 'no_vocals.wav').write_bytes(b'wav')
    (main.LYRICS / 'abc123.lrc').write_text('[00:01.00] verso', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text('{}', encoding='utf-8')
    chamadas = []
    monkeypatch.setattr(main, 'run_separation_com_preparo',
                        lambda *a, **k: chamadas.append(a))
    monkeypatch.setattr(main, 'prepare_karaoke', lambda *a, **k: chamadas.append(a))

    corpo = client.post('/api/library/prepare', json={}).json()

    assert corpo['ja_prontas'] == 1
    assert corpo['enfileiradas'] == 0
    assert corpo['faixas'] == []
    assert chamadas == []


def test_library_prepare_ignora_faixa_sem_audio(client, faixa):
    """Faixa sem mp3 baixado não pode ser preparada — é contada e pulada."""
    corpo = client.post('/api/library/prepare', json={}).json()

    assert corpo['sem_audio'] == 1
    assert corpo['enfileiradas'] == 0
    assert corpo['ja_prontas'] == 0


def test_library_prepare_biblioteca_vazia(client):
    assert client.post('/api/library/prepare', json={}).json() == {
        'total': 0, 'enfileiradas': 0, 'ja_prontas': 0, 'sem_audio': 0,
        'qualidade': 'equilibrada', 'faixas': [],
    }


def test_library_prepare_qualidade_invalida_422(client):
    assert client.post('/api/library/prepare',
                       json={'qualidade': 'turbo'}).status_code == 422


def test_states_persistem_separadamente(client, faixa):
    """PK composta: separação e karaokê coexistem no banco da mesma faixa."""
    main.set_state('separation', 'abc123', 'separating', 'Separando...')
    main.set_state('karaoke', 'abc123', 'ready', 'Karaokê pronto.')
    # Limpa memória para forçar a leitura do SQLite.
    main._separation_states.clear()
    main._karaoke_states.clear()
    sep = main.get_state('separation', 'abc123')
    kar = main.get_state('karaoke', 'abc123')
    assert sep and sep['status'] == 'separating'
    assert kar and kar['status'] == 'ready'


def test_atualizar_capa_faixa_antiga(client, faixa, monkeypatch):
    class ComCapa(FakeYDL):
        def extract_info(self, url, download=False):
            return {'thumbnail': 'https://img.test/capa.jpg'}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', ComCapa)
    resposta = sem_500(client.post('/api/tracks/abc123/cover'))
    assert resposta.json()['thumbnail'] == 'https://img.test/capa.jpg'
    assert client.get('/api/tracks').json()[0]['thumbnail'] == 'https://img.test/capa.jpg'


def test_atualizar_capa_retorna_cache(client, faixa, monkeypatch):
    conexao = main.db()
    with conexao:
        conexao.execute('INSERT OR REPLACE INTO track_covers VALUES (?, ?)',
                        ('abc123', 'https://img.test/cache.jpg'))
    class NaoDeveSerChamado(FakeYDL):
        def extract_info(self, url, download=False):
            raise AssertionError('não deveria consultar o YouTube')

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', NaoDeveSerChamado)
    resposta = sem_500(client.post('/api/tracks/abc123/cover'))
    assert resposta.json()['cached'] is True


def test_atualizar_capa_sem_url_400(client, monkeypatch):
    with main.closing(main.db()) as conexao, conexao:
        conexao.execute('INSERT OR REPLACE INTO tracks VALUES (?, ?, ?, ?, ?)',
                        ('semurl', 'Sem URL', 'Artista', 10, None))
    assert client.post('/api/tracks/semurl/cover').status_code == 400


def test_tracks_vazio(client):
    assert client.get('/api/tracks').json() == []


# --------------------------------------------------------------------------
# 4. Separação
# --------------------------------------------------------------------------
def test_separacao_status_faixa_inexistente_404(client):
    """Regressão do R3: GET devolvia 200 para ID desconhecido."""
    assert sem_500(client.get('/api/separation/nao-existe')).status_code == 404


def test_separacao_inicia_faixa_inexistente_404(client):
    assert client.post('/api/separation/nao-existe').status_code == 404


def test_separacao_inicia_e_reporta_estado(client, faixa, monkeypatch):
    monkeypatch.setattr(main, 'run_separation', lambda *a, **k: None)
    corpo = client.post('/api/separation/abc123').json()
    assert corpo['status'] in ('queued', 'ready')


def test_separacao_deduplica_jobs(client, faixa, monkeypatch):
    """Regressão do R2: clicar 2× enfileirava 2 Demucs."""
    chamados = []
    monkeypatch.setattr(main, 'run_separation', lambda tid, *a, **k: chamados.append(tid))
    client.post('/api/separation/abc123')
    segundo = client.post('/api/separation/abc123').json()
    assert segundo['status'] == 'queued'
    client.post('/api/separation/abc123')
    assert len(chamados) == 1


def test_apagar_faixa_remove_arquivos(client, faixa):
    (main.DOWNLOADS / 'abc123.mp3').write_bytes(b'x')
    (main.LYRICS / 'abc123.lrc').write_text('[00:01.00] x', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text('[]', encoding='utf-8')
    pasta = main.SEPARATED / 'abc123' / 'htdemucs'
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / 'no_vocals.wav').write_bytes(b'RIFF')

    resp = sem_500(client.delete('/api/tracks/abc123'))
    assert resp.status_code == 200
    assert client.get('/api/tracks').json() == []
    assert not (main.DOWNLOADS / 'abc123.mp3').exists()
    assert not (main.LYRICS / 'abc123.lrc').exists()
    assert not (main.SEPARATED / 'abc123').exists()


def test_apagar_faixa_inexistente_404(client):
    assert client.delete('/api/tracks/nao-existe').status_code == 404


def test_tracks_inclui_status_de_separacao(client, faixa):
    """Regressão UX: a lista de faixas informa o estágio da separação por faixa."""
    corpo = client.get('/api/tracks').json()
    assert 'separacao' in corpo[0]
    assert corpo[0]['separacao']['pronto'] in (True, False)
    assert corpo[0]['separacao']['status']


def test_tracks_marca_separacao_pronta_com_stems(client, faixa):
    pasta = main.SEPARATED / 'abc123' / 'htdemucs'
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / 'no_vocals.wav').write_bytes(b'RIFF')
    corpo = client.get('/api/tracks').json()
    assert corpo[0]['separacao']['pronto'] is True
    assert corpo[0]['separacao']['status'] == 'ready'


def test_favoritas_alterna_e_aparece_na_lista(client, faixa):
    corpo = client.get('/api/tracks').json()
    assert corpo[0]['favorita'] is False
    resp = sem_500(client.put('/api/favoritas/abc123'))
    assert resp.json()['favorita'] is True
    assert client.get('/api/tracks').json()[0]['favorita'] is True
    assert client.put('/api/favoritas/abc123').json()['favorita'] is False


def test_favoritas_faixa_inexistente_404(client):
    assert client.put('/api/favoritas/nao-existe').status_code == 404


def test_apagar_faixa_remove_favorita(client, faixa):
    client.put('/api/favoritas/abc123')
    sem_500(client.delete('/api/tracks/abc123'))
    assert client.get('/api/tracks').json() == []


def test_frontend_tem_botao_apagar(client):
    js = client.get('/static/app.js').text
    assert "action === 'delete'" in js
    assert 'data-action="delete"' in js
    assert 'apagarFaixa' in js


def test_separacao_aceita_qualidade(client, faixa, monkeypatch):
    recebido = {}

    def fake(track_id, qualidade='equilibrada'):
        recebido['q'] = qualidade

    monkeypatch.setattr(main, 'run_separation', fake)
    resp = client.post('/api/separation/abc123', json={'qualidade': 'alta'})
    assert resp.status_code == 200
    assert 'alta' in resp.json()['message']
    assert recebido.get('q') == 'alta'


def test_separacao_qualidade_invalida_422(client, faixa):
    assert client.post('/api/separation/abc123', json={'qualidade': 'turbo'}).status_code == 422


def test_perfis_de_separacao_progridem():
    perfis = main.PERFIS_SEPARACAO
    assert perfis['alta']['shifts'] > perfis['equilibrada']['shifts'] > perfis['rapida']['shifts']
    assert perfis['alta']['overlap'] >= perfis['rapida']['overlap']


def test_separacao_reporta_pronto_quando_existe(client, faixa):
    pasta = main.SEPARATED / 'abc123' / 'htdemucs'
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / 'no_vocals.wav').write_bytes(b'RIFF')
    corpo = client.get('/api/separation/abc123').json()
    assert corpo['ready'] is True
    assert corpo['status'] == 'ready'


def test_separacao_id_invalido_bloqueado(client):
    """Regressão do S1: path traversal nunca pode virar 200."""
    for alvo in ('..%2F..%2Fetc', 'abc%00', 'a' * 100):
        resposta = sem_500(client.get(f'/api/separation/{alvo}'))
        assert resposta.status_code in (400, 404, 405)


# --------------------------------------------------------------------------
# 5. Karaokê
# --------------------------------------------------------------------------
def test_prepare_faixa_inexistente_404(client):
    assert client.post('/api/karaoke/nao-existe/prepare', json={}).status_code == 404


def test_prepare_sem_artista_aceita(client, faixa, monkeypatch):
    """Artista pode ser vazio — busca pelo título é feita mesmo assim."""
    with main.closing(main.db()) as conexao, conexao:
        conexao.execute('UPDATE tracks SET artist = NULL WHERE id = ?', ('abc123',))
    resp = client.post('/api/karaoke/abc123/prepare', json={})
    assert resp.status_code == 200


def test_prepare_sem_audio_registra_erro(client, faixa):
    """Regressão: sem nenhum áudio (stems ou download) o motivo precisa aparecer."""
    main.prepare_karaoke('abc123', 'Chitãozinho', 'Evidências')
    estado = main._karaoke_states.get('abc123', {})
    assert estado.get('status') == 'error'
    assert 'áudio' in estado.get('message', '').lower()


def test_prepare_usa_original_quando_sem_stems(client, faixa, monkeypatch):
    """Cantar já: sem stems, a afinação usa o áudio original (mp3 baixado)."""
    (main.DOWNLOADS / 'abc123.mp3').write_bytes(b'x')
    monkeypatch.setattr(main, 'fetch_lyrics', lambda *a, **k: True)
    monkeypatch.setattr(main, 'extract_pitch_reference',
                        lambda origem, saida: saida.write_text(
                            json.dumps([{'t': 0.0, 'midi': 60.0}])))
    main.prepare_karaoke('abc123', 'Chitãozinho', 'Evidências')
    assert main._karaoke_states['abc123']['status'] == 'ready'


def test_prepare_usa_estado_proprio(client, faixa):
    """Regressão do R4: karaokê não pode sobrescrever o estado da separação."""
    main.set_state('separation', 'abc123', 'separating', 'Separando...')
    main.prepare_karaoke('abc123', 'Chitãozinho', 'Evidências')
    assert main._separation_states['abc123']['status'] == 'separating'


def test_prepare_sem_letra_registra_no_lyrics(client, faixa, monkeypatch):
    monkeypatch.setattr(main, 'fetch_lyrics', lambda *a, **k: False)
    monkeypatch.setattr(main, 'extract_pitch_reference', lambda *a, **k: [{'t': 0, 'midi': 60}])
    vocal = main.SEPARATED / 'abc123' / 'htdemucs' / 'vocals.wav'
    vocal.parent.mkdir(parents=True, exist_ok=True)
    vocal.write_bytes(b'RIFF')
    main.prepare_karaoke('abc123', 'Chitãozinho', 'Evidências')
    assert main._karaoke_states['abc123']['status'] == 'no_lyrics'


def test_status_karaoke_nao_preparado(client, faixa):
    corpo = client.get('/api/karaoke/abc123/status').json()
    assert corpo['ready'] is False


def test_status_karaoke_faixa_inexistente_404(client):
    assert client.get('/api/karaoke/nao-existe/status').status_code == 404


def test_karaoke_sem_preparacao_ready_false(client, faixa):
    """Sem letra/afinação: 200 com ready=False (antes era 404, que poluía o console)."""
    corpo = sem_500(client.get('/api/karaoke/abc123'))
    assert corpo.status_code == 200
    assert corpo.json()['ready'] is False


def test_karaoke_devolve_dados(client, faixa):
    main.LYRICS.mkdir(parents=True, exist_ok=True)
    main.PITCHREF.mkdir(parents=True, exist_ok=True)
    (main.LYRICS / 'abc123.lrc').write_text('[00:10.00] Teste', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text(
        json.dumps([{'t': 0.0, 'midi': 60.0}]), encoding='utf-8')
    pasta = main.SEPARATED / 'abc123' / 'htdemucs'
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / 'no_vocals.wav').write_bytes(b'RIFF')
    corpo = client.get('/api/karaoke/abc123').json()
    assert 'Teste' in corpo['lyrics']
    assert corpo['pitch_ref'][0]['midi'] == 60.0
    assert corpo['stems_prontos'] is True
    assert corpo['instrumental_url'].endswith('no_vocals.wav')


def test_karaoke_cantar_ja_sem_stems_usa_mp3(client, faixa):
    """Cantar já: sem stems prontos, o instrumental vira o áudio original."""
    main.LYRICS.mkdir(parents=True, exist_ok=True)
    main.PITCHREF.mkdir(parents=True, exist_ok=True)
    (main.LYRICS / 'abc123.lrc').write_text('[00:10.00] Teste', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text(
        json.dumps([{'t': 0.0, 'midi': 60.0}]), encoding='utf-8')
    corpo = client.get('/api/karaoke/abc123').json()
    assert corpo['stems_prontos'] is False
    assert corpo['instrumental_url'].endswith('abc123.mp3')


def test_karaoke_json_corrompido_500_tratado(client, faixa):
    """Regressão do B7: JSON inválido virava 500 com traceback."""
    main.LYRICS.mkdir(parents=True, exist_ok=True)
    main.PITCHREF.mkdir(parents=True, exist_ok=True)
    (main.LYRICS / 'abc123.lrc').write_text('[00:10.00] x', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text('{nao eh json', encoding='utf-8')
    response = client.get('/api/karaoke/abc123')
    assert response.status_code == 500
    assert 'corrompido' in response.json()['detail']


def test_karaoke_pitch_vazio_ready_false(client, faixa):
    main.LYRICS.mkdir(parents=True, exist_ok=True)
    main.PITCHREF.mkdir(parents=True, exist_ok=True)
    (main.LYRICS / 'abc123.lrc').write_text('[00:10.00] x', encoding='utf-8')
    (main.PITCHREF / 'abc123.json').write_text('[]', encoding='utf-8')
    corpo = client.get('/api/karaoke/abc123')
    assert corpo.status_code == 200
    assert corpo.json()['ready'] is False


def test_karaoke_path_traversal_bloqueado(client, faixa):
    """Regressão do S1: IDs fora do padrão são rejeitados, nunca lidos do disco."""
    for alvo in ('..%2F..%2F..%2Fetc%2Fpasswd', '....//etc', 'abc%00'):
        resposta = sem_500(client.get(f'/api/karaoke/{alvo}'))
        assert resposta.status_code in (400, 404)


# --------------------------------------------------------------------------
# 6. Diagnóstico
# --------------------------------------------------------------------------
def test_diagnostics(client):
    corpo = sem_500(client.get('/api/diagnostics')).json()
    for chave in ('yt_dlp', 'ffmpeg', 'gpu', 'demucs_cache', 'demucs_model', 'directories'):
        assert chave in corpo
    assert all(isinstance(v, bool) for v in corpo['directories'].values())


def test_network_para_o_qr_code(client):
    """O QR precisa do IP da rede mesmo quando o PC abre em localhost."""
    corpo = sem_500(client.get('/api/network')).json()
    for chave in ('ip', 'port', 'url'):
        assert chave in corpo
    assert isinstance(corpo['port'], int)
    # Sem IP detectado o campo url é None; com IP, é uma URL http utilizável.
    if corpo['url'] is not None:
        assert corpo['url'].startswith('http://')
        assert str(corpo['port']) in corpo['url']


# --------------------------------------------------------------------------
# 7. Segurança — banco não pode ser servido estaticamente
# --------------------------------------------------------------------------
def test_banco_nao_exposto_em_data(client, faixa):
    """Regressão do S2: o SQLite ficava em /data e era baixável."""
    assert main.DB.parent != main.DATA
    assert sem_500(client.get('/data/karaoke.sqlite3')).status_code == 404


# --------------------------------------------------------------------------
# 8. Upload manual de .lrc
# --------------------------------------------------------------------------
def test_upload_lrc_sucesso(client, faixa):
    """Envia um .lrc válido e verifica que o arquivo é salvo."""
    conteudo = b'[00:05.00] primeiro verso\n[00:10.00] segundo verso'
    resp = client.post(
        '/api/lyrics/abc123/upload',
        files={'arquivo': ('letra.lrc', conteudo, 'application/octet-stream')},
    )
    assert resp.status_code == 200
    assert (main.LYRICS / 'abc123.lrc').read_bytes() == conteudo


def test_upload_lrc_extensao_errada(client, faixa):
    resp = client.post(
        '/api/lyrics/abc123/upload',
        files={'arquivo': ('letra.txt', b'foo', 'text/plain')},
    )
    assert resp.status_code == 400


def test_upload_lrc_vazio(client, faixa):
    resp = client.post(
        '/api/lyrics/abc123/upload',
        files={'arquivo': ('letra.lrc', b'', 'application/octet-stream')},
    )
    assert resp.status_code == 400


def test_upload_lrc_conteudo_invalido(client, faixa):
    """Lixo de 1 linha sem timestamps LRC é rejeitado."""
    resp = client.post(
        '/api/lyrics/abc123/upload',
        files={'arquivo': ('letra.lrc', b'oi', 'application/octet-stream')},
    )
    assert resp.status_code == 400


def test_upload_lrc_texto_sem_sync_aceito(client, faixa):
    """Texto de letra (sem timestamps) com 2+ versos é aceito — frontend trata."""
    conteudo = b'primeiro verso\nsegundo verso\n\n[meta: x]'
    resp = client.post(
        '/api/lyrics/abc123/upload',
        files={'arquivo': ('letra.lrc', conteudo, 'application/octet-stream')},
    )
    assert resp.status_code == 200


def test_upload_lrc_faixa_inexistente(client):
    resp = client.post(
        '/api/lyrics/nao-existe/upload',
        files={'arquivo': ('letra.lrc', b'ok', 'application/octet-stream')},
    )
    assert resp.status_code == 404


def test_karaoke_js_trata_no_lyrics(client):
    """Regressão: no_lyrics não pode travar o polling por 2 minutos."""
    js = client.get('/static/karaoke.js').text
    assert 'no_lyrics' in js
    assert '_semLetra' in js
    assert 'montarCardUpload' in js


def test_app_js_acompanha_download_background(client):
    js = client.get('/static/app.js').text
    assert 'acompanharDownload' in js
    assert '/api/download/' in js
    assert 'download?' in js or "download?.status" in js or "resp.download" in js


def test_interface_tem_baixar_playlist(client):
    html = client.get('/').text
    assert 'id="playlistUrl"' in html
    assert 'id="playlistBtn"' in html
    assert 'id="playlistList"' in html
    assert 'id="playlistPreparar"' in html  # caixinha do modo festa
    js = client.get('/static/app.js').text
    assert 'baixarPlaylist' in js
    assert '/api/playlist' in js
    assert '/api/playlist/status' in js


def test_modo_convidado_esconde_baixar_playlist(client):
    """Convidado não baixa: a caixa de playlist some no modo convidado."""
    css = client.get('/static/styles.css').text
    assert 'body.convidado .playlist-box' in css


def test_interface_tem_preparar_biblioteca(client):
    html = client.get('/').text
    assert 'id="prepareAllBtn"' in html
    assert 'id="prepareAllInfo"' in html
    js = client.get('/static/app.js').text
    assert 'prepararBiblioteca' in js
    assert '/api/library/prepare' in js


def test_prepare_usa_lrc_local(client, faixa, monkeypatch):
    """Se já existe .lrc no disco, prepare_karaoke não chama fetch_lyrics."""
    main.LYRICS.mkdir(parents=True, exist_ok=True)
    (main.LYRICS / 'abc123.lrc').write_text('[00:05.00] verso', encoding='utf-8')
    vocal = main.SEPARATED / 'abc123' / 'htdemucs' / 'vocals.wav'
    vocal.parent.mkdir(parents=True, exist_ok=True)
    vocal.write_bytes(b'RIFF')
    chamado = []
    monkeypatch.setattr(main, 'fetch_lyrics', lambda *a, **k: chamado.append(1) or True)
    monkeypatch.setattr(main, 'extract_pitch_reference', lambda *a, **k: None)
    main.prepare_karaoke('abc123', 'Chitãozinho', 'Evidências')
    assert chamado == [], 'fetch_lyrics não deveria ser chamado quando .lrc local existe'


# --------------------------------------------------------------------------
# 9. Lyrics: cadeia de fallbacks e limpeza de título
# --------------------------------------------------------------------------
def test_limpar_titulo():
    from app.lyrics import limpar_titulo
    assert limpar_titulo('Evidências (Official Video)') == 'Evidências'
    assert limpar_titulo('Song [HD] [Clipe Oficial]') == 'Song'
    assert limpar_titulo('Track (feat. Artista)') == 'Track'
    assert limpar_titulo('Faixa - Remastered 2011') == 'Faixa'
    assert limpar_titulo('Canção (Ao Vivo)') == 'Canção'
    assert limpar_titulo('Música - Topic') == 'Música'
    assert limpar_titulo(None) == ''
    assert limpar_titulo('') == ''


def test_parse_lrc_lines():
    from app.lyrics import parse_lrc_lines
    texto = "[00:12.50] Primeiro verso\n[01:05.00] Segundo verso"
    linhas = parse_lrc_lines(texto)
    assert len(linhas) == 2
    assert linhas[0] == {'time': 12.5, 'text': 'Primeiro verso'}
    assert linhas[1] == {'time': 65.0, 'text': 'Segundo verso'}


def test_enriquecer_palavras_distribui():
    from app.lyrics import enriquecer_palavras

    linhas = [{'time': 10.0, 'text': 'uma letra teste'}, {'time': 12.0, 'text': 'fim'}]
    resultado = enriquecer_palavras(linhas)
    palavras = resultado[0]['words']
    assert [p['text'] for p in palavras] == ['uma', 'letra', 'teste']
    assert palavras[0]['time'] == 10.0
    assert 10.0 < palavras[1]['time'] < palavras[2]['time'] < 12.0


def test_enriquecer_palavras_mantem_enhanced():
    from app.lyrics import enriquecer_palavras

    palavras = [{'time': 5.0, 'text': 'oi'}]
    linhas = [{'time': 5.0, 'text': 'oi', 'words': palavras}]
    assert enriquecer_palavras(linhas)[0]['words'] is palavras


def test_completar_letra_com_whisper_preenche_o_final(monkeypatch, tmp_path):
    """Quando a letra não cobre a música, o Whisper preenche o final."""
    from app.lyrics import completar_letra_com_whisper

    audio = tmp_path / 'audio.mp3'
    audio.write_bytes(b'x')
    monkeypatch.setattr('app.lyrics._transcrever_whisper_segmentos',
                        lambda *a, **k: [{'time': 150.0, 'text': 'verso novo'},
                                         {'time': 154.0, 'text': 'outro verso'}])
    linhas = [{'time': 10.0, 'text': 'começo'}, {'time': 120.0, 'text': 'meio'}]

    resultado = completar_letra_com_whisper(linhas, 200.0, audio)

    assert [l['time'] for l in resultado] == [10.0, 120.0, 150.0, 154.0]
    assert resultado[0]['text'] == 'começo'          # original preservado
    assert resultado[-1]['text'] == 'outro verso'
    assert resultado[-1]['words']                     # palavras geradas p/ karaokê


def test_completar_letra_com_whisper_descarta_trecho_ja_coberto(monkeypatch, tmp_path):
    """Verso que o Whisper repete antes do fim conhecido não duplica a letra."""
    from app.lyrics import completar_letra_com_whisper

    audio = tmp_path / 'audio.mp3'
    audio.write_bytes(b'x')
    monkeypatch.setattr('app.lyrics._transcrever_whisper_segmentos',
                        lambda *a, **k: [{'time': 100.0, 'text': 'repetido'},
                                         {'time': 180.0, 'text': 'novo'}])
    linhas = [{'time': 10.0, 'text': 'começo'}, {'time': 120.0, 'text': 'meio'}]

    resultado = completar_letra_com_whisper(linhas, 200.0, audio)

    assert [l['time'] for l in resultado] == [10.0, 120.0, 180.0]


def test_completar_letra_com_whisper_nao_mexe_se_cobre(monkeypatch, tmp_path):
    """Letra que já cobre a música inteira não dispara o Whisper."""
    from app.lyrics import completar_letra_com_whisper

    audio = tmp_path / 'audio.mp3'
    audio.write_bytes(b'x')
    chamou = []
    monkeypatch.setattr('app.lyrics._transcrever_whisper_segmentos',
                        lambda *a, **k: chamou.append(1) or [])
    linhas = [{'time': 10.0, 'text': 'começo'}, {'time': 190.0, 'text': 'fim'}]

    resultado = completar_letra_com_whisper(linhas, 200.0, audio)

    assert resultado is linhas
    assert chamou == [], 'Whisper não deveria ser chamado se a letra já cobre'


def test_completar_letra_com_whisper_mantem_quando_nada_novo(monkeypatch, tmp_path):
    """Final instrumental: sem versos novos, a letra não muda."""
    from app.lyrics import completar_letra_com_whisper

    audio = tmp_path / 'audio.mp3'
    audio.write_bytes(b'x')
    monkeypatch.setattr('app.lyrics._transcrever_whisper_segmentos', lambda *a, **k: [])
    linhas = [{'time': 10.0, 'text': 'começo'}, {'time': 100.0, 'text': 'fim'}]

    assert completar_letra_com_whisper(linhas, 200.0, audio) is linhas


def test_completar_letra_com_whisper_sem_duracao_nao_mexe(monkeypatch, tmp_path):
    """Sem duração conhecida não há como saber se falta letra: não mexe."""
    from app.lyrics import completar_letra_com_whisper

    audio = tmp_path / 'audio.mp3'
    audio.write_bytes(b'x')
    linhas = [{'time': 10.0, 'text': 'começo'}]

    assert completar_letra_com_whisper(linhas, None, audio) is linhas


def test_prepare_grava_final_completado_no_lrc(client, faixa, monkeypatch):
    """prepare_karaoke grava no .lrc o final que o Whisper completou."""
    (main.LYRICS / 'abc123.lrc').write_text('[00:05.00] verso\n[00:10.00] outro',
                                            encoding='utf-8')
    vocal = main.SEPARATED / 'abc123' / 'htdemucs' / 'vocals.wav'
    vocal.parent.mkdir(parents=True, exist_ok=True)
    vocal.write_bytes(b'RIFF')
    monkeypatch.setattr(main, 'fetch_lyrics', lambda *a, **k: True)
    monkeypatch.setattr(main, 'extract_pitch_reference', lambda *a, **k: None)
    monkeypatch.setattr('app.lyrics._transcrever_whisper_segmentos',
                        lambda *a, **k: [{'time': 200.0, 'text': 'final cantado'}])

    main.prepare_karaoke('abc123', 'Chitãozinho', 'Evidências')

    conteudo = (main.LYRICS / 'abc123.lrc').read_text(encoding='utf-8')
    assert 'final cantado' in conteudo
    assert '[00:05.00] verso' in conteudo
    assert main._karaoke_states['abc123']['status'] == 'ready'


def test_get_lyrics_local_gera_palavras(tmp_path):
    from app.lyrics import get_lyrics

    pasta = tmp_path / 'lyrics'
    pasta.mkdir()
    (pasta / 't2.lrc').write_text('[00:05.00] primeira linha aqui\n[00:08.00] segunda', encoding='utf-8')
    resultado = get_lyrics('t2', 'Título', 'Artista', pasta_lyrics=pasta)
    assert resultado['synced'] is True
    assert len(resultado['lines'][0]['words']) == 3


def test_karaoke_tem_autosync_e_preserva_palavras(client):
    js = client.get('/static/karaoke.js').text
    assert 'sincronizarAutomatico' in js
    assert 'id="autoSync"' in client.get('/').text
    assert 'words: l.words' in js


def test_validar_conteudo_lrc():
    from app.lyrics import validar_conteudo_lrc
    assert validar_conteudo_lrc('[00:10.00] ok') is True
    assert validar_conteudo_lrc('texto sem sync') is False
    assert validar_conteudo_lrc('') is False


def test_get_lyrics_fonte_1_local(tmp_path):
    from app.lyrics import get_lyrics
    pasta = tmp_path / 'lyrics'
    pasta.mkdir()
    (pasta / 't1.lrc').write_text('[00:05.00] linha 1', encoding='utf-8')
    res = get_lyrics('t1', 'Título', 'Artista', pasta_lyrics=pasta)
    assert res['found'] is True
    assert res['synced'] is True
    assert res['source'] == 'local'
    assert res['lines'][0]['text'] == 'linha 1'


def test_get_lyrics_nao_encontrado(tmp_path, monkeypatch):
    from app.lyrics import get_lyrics

    class FakeResponse:
        status_code = 404
        def json(self):
            return []

    monkeypatch.setattr('requests.get', lambda *a, **k: FakeResponse())
    res = get_lyrics('t_vazio', 'Desconhecida', 'Desconhecido', pasta_lyrics=tmp_path)
    assert res == {'found': False}


def test_endpoint_get_lyrics_retorna_json_consistente(client, faixa):
    main.LYRICS.mkdir(parents=True, exist_ok=True)
    (main.LYRICS / 'abc123.lrc').write_text('[00:10.00] Verso teste', encoding='utf-8')
    resp = client.get('/api/lyrics/abc123')
    assert resp.status_code == 200
    dados = resp.json()
    assert dados['found'] is True
    assert dados['synced'] is True
    assert dados['source'] == 'local'
    assert dados['lines'][0]['text'] == 'Verso teste'


# --------------------------------------------------------------------------
# 8. Ranking de pontuações
# --------------------------------------------------------------------------
def test_ranking_comeca_vazio(client):
    assert sem_500(client.get('/api/scores')).json() == {'ranking': []}


def test_salvar_e_listar_ranking(client):
    resp = sem_500(client.post('/api/scores', json={
        'singer': 'Rafa', 'score': 88, 'medal': '🥇',
        'track_id': 'abc123', 'track_title': 'Evidências',
    }))
    assert resp.status_code == 200
    dados = resp.json()
    assert dados['singer'] == 'Rafa'
    assert dados['rank'] == 1

    sem_500(client.post('/api/scores', json={'singer': 'Maria', 'score': 95}))
    sem_500(client.post('/api/scores', json={'singer': 'João', 'score': 70}))

    ranking = sem_500(client.get('/api/scores')).json()['ranking']
    assert [r['singer'] for r in ranking] == ['Maria', 'Rafa', 'João']
    assert [r['rank'] for r in ranking] == [1, 2, 3]


def test_score_fora_do_intervalo_422(client):
    assert client.post('/api/scores', json={'singer': 'X', 'score': 101}).status_code == 422
    assert client.post('/api/scores', json={'singer': 'X', 'score': -1}).status_code == 422


def test_score_sem_nome_422(client):
    assert client.post('/api/scores', json={'score': 50}).status_code == 422


def test_score_nome_em_branco_400(client):
    assert client.post('/api/scores', json={'singer': '   ', 'score': 50}).status_code == 400


def test_score_track_id_invalido_400(client):
    resp = client.post('/api/scores', json={'singer': 'X', 'score': 50, 'track_id': '../etc'})
    assert resp.status_code == 400


def test_zerar_ranking(client):
    sem_500(client.post('/api/scores', json={'singer': 'Ana', 'score': 60}))
    assert sem_500(client.delete('/api/scores')).status_code == 200
    assert sem_500(client.get('/api/scores')).json() == {'ranking': []}


# --------------------------------------------------------------------------
# 10. Fila de tarefas em background (app/tasks.py)
# --------------------------------------------------------------------------
import threading
import time

from app.tasks import FilaTarefas


def test_fila_inline_executa_na_hora():
    fila = FilaTarefas()
    fila.inline = True
    marcado = []
    funcao = lambda: marcado.append('ok')  # noqa: E731
    job = fila.enfileirar('teste', '1', funcao)
    assert marcado == ['ok']
    assert job['status'] == 'done'


def test_fila_deduplica_job_em_andamento():
    fila = FilaTarefas(max_simultaneos=1)
    inicio = threading.Event()
    soltar = threading.Event()
    chamados = []

    def lenta():
        chamados.append('x')
        inicio.set()
        soltar.wait(2)

    primeiro = fila.enfileirar('sep', 'abc', lenta)
    assert inicio.wait(2), 'o job deveria ter começado'
    segundo = fila.enfileirar('sep', 'abc', lenta)
    assert primeiro['id'] == segundo['id']
    assert len(chamados) == 1, 'duplo clique não pode enfileirar 2 jobs'
    soltar.set()
    fila.aguardar(primeiro['id'], timeout=5)


def test_fila_resolve_caminho_pontilhado(monkeypatch):
    """'main.funcao' é resolvido na execução -> monkeypatch é respeitado."""
    fila = FilaTarefas(max_simultaneos=1)
    marcado = []

    def alvo():
        marcado.append('ok')

    monkeypatch.setattr(main, '_alvo_teste_fila', alvo, raising=False)
    job = fila.enfileirar('t', '1', 'main._alvo_teste_fila')
    resultado = fila.aguardar(job['id'], timeout=5)
    assert resultado['status'] == 'done'
    assert marcado == ['ok']


def test_fila_limita_concorrencia():
    fila = FilaTarefas(max_simultaneos=2)
    estado = {'n': 0, 'max': 0}
    trava = threading.Lock()

    def lenta():
        with trava:
            estado['n'] += 1
            estado['max'] = max(estado['max'], estado['n'])
        time.sleep(0.05)
        with trava:
            estado['n'] -= 1

    for indice in range(4):
        fila.enfileirar('carga', str(indice), lenta)

    limite = time.time() + 5
    while time.time() < limite:
        resumo = fila.resumo()
        if resumo['running'] == 0 and resumo['queued'] == 0:
            break
        time.sleep(0.01)
    assert estado['max'] <= 2, 'concorrência deve respeitar max_simultaneos'


def test_fila_falha_reporta_error():
    fila = FilaTarefas(max_simultaneos=1)

    def quebra():
        raise RuntimeError('explodiu')

    job = fila.enfileirar('t', '1', quebra)
    resultado = fila.aguardar(job['id'], timeout=5)
    assert resultado['status'] == 'error'
    assert 'explodiu' in resultado['message']


def test_api_jobs_lista_vazia(client):
    corpo = sem_500(client.get('/api/jobs')).json()
    assert corpo['jobs'] == []
    assert corpo['running'] == 0
    assert corpo['queued'] == 0
    assert corpo['max_simultaneos'] >= 1


def test_api_jobs_reflete_job_executado(client):
    main.fila.enfileirar('download', 'abc123', lambda: None)
    corpo = sem_500(client.get('/api/jobs')).json()
    assert len(corpo['jobs']) == 1
    assert corpo['jobs'][0]['nome'] == 'download'
    assert corpo['jobs'][0]['status'] in ('done', 'queued', 'running', 'error')


# --------------------------------------------------------------------------
# 11. Modo Festa — fila de reprodução compartilhada
# --------------------------------------------------------------------------
def _enfileirar_festa(client, track_id='abc123', added_by='Rafa'):
    return client.post('/api/queue', json={
        'track_id': track_id, 'added_by': added_by})


def test_fila_festa_comeca_vazia(client):
    assert sem_500(client.get('/api/queue')).json()['items'] == []


def test_adicionar_e_listar_fila(client, faixa):
    resp = sem_500(_enfileirar_festa(client))
    assert resp.status_code == 201
    corpo = resp.json()
    assert corpo['item']['track_id'] == 'abc123'
    assert corpo['item']['added_by'] == 'Rafa'
    assert corpo['item']['status'] == 'pending'
    fila = sem_500(client.get('/api/queue')).json()
    assert fila['pending'] == 1
    assert fila['items'][0]['title'] == 'Evidências'


def test_adicionar_faixa_inexistente_404(client):
    assert _enfileirar_festa(client, track_id='nao-existe').status_code == 404


def test_adicionar_sem_quem_pediu_422(client, faixa):
    resp = client.post('/api/queue', json={'track_id': 'abc123'})
    assert resp.status_code == 422


def test_adicionar_id_invalido_400(client):
    resp = client.post('/api/queue', json={
        'track_id': '../etc', 'added_by': 'X'})
    assert resp.status_code == 400


def test_remover_item_da_fila(client, faixa):
    criado = _enfileirar_festa(client).json()['item']
    assert sem_500(client.delete(f"/api/queue/{criado['item_id']}")).status_code == 200
    assert sem_500(client.get('/api/queue')).json()['items'] == []


def test_remover_item_inexistente_404(client):
    assert sem_500(client.delete('/api/queue/nao-existe')).status_code == 404


def test_mover_item_vira_ordem(client, faixa):
    _enfileirar_festa(client)
    segundo = _enfileirar_festa(client, added_by='Maria').json()['item']
    sem_500(client.post(f"/api/queue/{segundo['item_id']}/move", json={'direcao': 'up'}))
    itens = sem_500(client.get('/api/queue')).json()['items']
    assert itens[0]['added_by'] == 'Maria'


def test_proxima_da_fila(client, faixa):
    primeiro = _enfileirar_festa(client, added_by='Rafa').json()['item']
    _enfileirar_festa(client, added_by='Maria')
    resp = sem_500(client.post('/api/queue/next')).json()
    assert resp['item']['item_id'] == primeiro['item_id']
    itens = sem_500(client.get('/api/queue')).json()['items']
    assert itens[0]['status'] == 'playing'
    assert itens[1]['status'] == 'pending'
    # A próxima música promove outro e aposenta a atual.
    sem_500(client.post('/api/queue/next'))
    itens = sem_500(client.get('/api/queue')).json()['items']
    assert [i['status'] for i in itens] == ['played', 'playing']


def test_proxima_fila_vazia_404(client):
    assert sem_500(client.post('/api/queue/next')).status_code == 404


def test_tocar_agora_pula_para_item(client, faixa):
    _enfileirar_festa(client, added_by='Rafa')
    segundo = _enfileirar_festa(client, added_by='Maria').json()['item']
    sem_500(client.post(f"/api/queue/{segundo['item_id']}/play"))
    tocando = sem_500(client.get('/api/queue')).json()['playing']
    assert tocando['item_id'] == segundo['item_id']


def test_limpar_fila_mantem_tocando(client, faixa):
    _enfileirar_festa(client, added_by='Rafa')
    _enfileirar_festa(client, added_by='Maria')
    sem_500(client.post('/api/queue/next'))
    corpo = sem_500(client.delete('/api/queue')).json()
    assert corpo['removed'] == 1
    itens = sem_500(client.get('/api/queue')).json()['items']
    assert len(itens) == 1
    assert itens[0]['status'] == 'playing'


def test_status_festa(client):
    corpo = sem_500(client.get('/api/party/status')).json()
    assert 'items' in corpo and 'total' in corpo
    assert 'play' in corpo['cmds']


# --------------------------------------------------------------------------
# 12. WebSocket do Modo Festa
# --------------------------------------------------------------------------
def _esperar_tipo(websocket, tipo, max_iter=20):
    for _ in range(max_iter):
        msg = websocket.receive_json()
        if msg.get('type') == tipo:
            return msg
    raise AssertionError(f'promessa {tipo} não chegou')


def test_websocket_festa_envia_welcome(client):
    with client.websocket_connect('/ws/party') as ws:
        msg = ws.receive_json()
        assert msg['type'] == 'welcome'
        assert 'queue' in msg and 'guests' in msg and 'hosts' in msg


def test_websocket_desconexao_nao_estoura(monkeypatch):
    """Fechar a aba não pode levantar exceção dentro do handler.

    ``WebSocketDisconnected`` (Starlette novo) herda de ``RuntimeError``, e não
    de ``WebSocketDisconnect``: se o ``except`` do handler não citar os dois,
    cada desconexão vira um traceback de ASGI no log. Chamamos o handler com um
    socket que "morre" no receive para exercitar exatamente esse caminho.
    """
    from app import party

    class FakeSocket:
        async def accept(self):
            pass

        async def send_json(self, _msg):
            pass

        async def receive_json(self):
            raise party.WebSocketDisconnected('socket morto')

    async def _sem_aviso():
        pass

    monkeypatch.setattr(party, 'listar_itens', lambda: [])
    monkeypatch.setattr(party, '_avisar_clientes', _sem_aviso)
    party.gerenciador._clientes.clear()
    socket = FakeSocket()

    asyncio.run(party.websocket_festa(socket))  # não pode levantar

    assert socket not in party.gerenciador._clientes


def test_websocket_anfitriao_recebe_musica_nova(client, faixa):
    with client.websocket_connect('/ws/party') as anfitriao:
        _esperar_tipo(anfitriao, 'welcome')
        _enfileirar_festa(client)
        msg = _esperar_tipo(anfitriao, 'queue')
        assert msg['items'][0]['track_id'] == 'abc123'


def test_websocket_convidado_controla_anfitriao(client):
    with (client.websocket_connect('/ws/party') as anfitriao,
          client.websocket_connect('/ws/party') as convidado):
        _esperar_tipo(anfitriao, 'welcome')
        msg = _esperar_tipo(convidado, 'welcome')
        assert msg['type'] == 'welcome'
        # O convidado publica um comando; o anfitrião deve recebê-lo.
        convidado.send_json({'type': 'control', 'cmd': 'pause', 'value': None})
        controle = _esperar_tipo(anfitriao, 'control')
        assert controle['cmd'] == 'pause'


def test_controle_rest_publica_para_todos(client):
    with client.websocket_connect('/ws/party') as ws:
        _esperar_tipo(ws, 'welcome')
        resp = sem_500(client.post('/api/party/control', json={'cmd': 'toggle'}))
        assert resp.json()['cmd'] == 'toggle'
        msg = _esperar_tipo(ws, 'control')
        assert msg['cmd'] == 'toggle'


def test_controle_cmd_invalido_422(client):
    assert client.post('/api/party/control', json={'cmd': 'formatar_disco'}).status_code == 422


# --------------------------------------------------------------------------
# 13. Frontend do Modo Festa + PWA/offline
# --------------------------------------------------------------------------
def test_frontend_tem_aba_festa(client):
    html = client.get('/').text
    assert 'data-tab="party"' in html
    assert 'id="party"' in html
    assert 'id="partyList"' in html
    js = client.get('/static/app.js').text
    assert 'conectarFesta' in js
    assert 'recarregarFila' in js


def test_frontend_tem_controle_remoto(client):
    html = client.get('/').text
    for identificador in ('rcToggle', 'rcPrev', 'rcNext', 'rcVolume', 'rcOpen'):
        assert f'id="{identificador}"' in html
    js = client.get('/static/app.js').text
    assert 'enviarControle' in js
    assert 'aplicarControle' in js


def test_frontend_tem_modo_convidado(client):
    html = client.get('/').text
    assert 'guestBanner' in html
    js = client.get('/static/app.js').text
    assert 'ehConvidado' in js
    assert 'aplicarModoAcesso' in js
    assert 'vocalis_guest' in js


def test_frontend_tem_pwa_manifest(client):
    html = client.get('/').text
    assert 'rel="manifest"' in html
    manifest = client.get('/static/manifest.webmanifest')
    assert manifest.status_code == 200
    dados = manifest.json()
    assert dados['name'] and dados['display'] == 'standalone'
    assert dados['icons'][0]['src']


def test_service_worker_guarda_casco(client):
    sw = client.get('/static/sw.js').text
    assert 'CACHE_NAME' in sw
    assert 'CASCO' in sw and '/static/app.js' in sw
    assert 'redeOuCache' in sw
    assert 'vocalis-audio-v2' in sw  # comportamento de áudio preservado


def test_frontend_carrega_modulo_offline(client):
    html = client.get('/').text
    assert 'offline.js' in html
    modulo = client.get('/static/offline.js')
    assert modulo.status_code == 200
    assert 'VocalisOffline' in modulo.text
    assert 'indexedDB' in modulo.text


def test_app_js_guarda_faixas_offline(client):
    js = client.get('/static/app.js').text
    assert "VocalisOffline.salvar('tracks'" in js
    assert 'cache offline' in js


def test_karaoke_js_tem_fallback_offline(client):
    js = client.get('/static/karaoke.js').text
    assert 'carregarKaraoke' in js
    assert 'VocalisOffline' in js


def test_index_tem_icone_e_core_language(client):
    html = client.get('/').text
    assert 'icon.svg' in html
    assert 'apple-touch-icon' in html
    assert 'lang="pt-BR"' in html


# --------------------------------------------------------------------------
# 10. Letras: metadados do YouTube/YouTube Music e correção manual
# --------------------------------------------------------------------------
def test_separar_artista_titulo_caso_real():
    from app.lyrics import separar_artista_titulo
    titulo, artista = separar_artista_titulo(
        'Marília Mendonça - Infiel (Vídeo Oficial)', 'Marília Mendonça')
    assert titulo == 'Infiel'
    assert artista == 'Marília Mendonça'


def test_separar_artista_titulo_canal_topic():
    from app.lyrics import separar_artista_titulo
    titulo, artista = separar_artista_titulo(
        'Evidências (Official Video)', 'Chitãozinho & Xororó - Topic')
    assert titulo == 'Evidências'
    assert 'Topic' not in artista


def test_separar_artista_titulo_sem_padrao():
    from app.lyrics import separar_artista_titulo
    assert separar_artista_titulo('Infiel', 'Marília Mendonça') == ('Infiel', 'Marília Mendonça')
    assert separar_artista_titulo(None, None) == ('', '')


def test_candidatos_busca_primeiro_eh_o_mais_limpo():
    from app.lyrics import candidatos_busca
    cands = candidatos_busca('Marília Mendonça - Infiel (Vídeo Oficial)', 'Marília Mendonça')
    assert cands[0] == ('Infiel', 'Marília Mendonça')
    assert len(cands) == len(set(cands))


def test_normalizar_metadados_prefere_track_artist():
    from app.lyrics import normalizar_metadados
    assert normalizar_metadados('Video bagunçado (Official Video)', 'Infiel',
                                'Marília Mendonça', 'Canal X') == ('Infiel', 'Marília Mendonça')


def test_normalizar_metadados_divide_titulo():
    from app.lyrics import normalizar_metadados
    titulo, artista = normalizar_metadados(
        'Marília Mendonça - Infiel (Vídeo Oficial)', None, None, 'Marília Mendonça')
    assert titulo == 'Infiel'
    assert artista == 'Marília Mendonça'


def test_limpar_titulo_termos_soltos_no_fim():
    from app.lyrics import limpar_titulo
    assert limpar_titulo('Infiel Official Video') == 'Infiel'
    assert limpar_titulo('Evidências - Ao Vivo') == 'Evidências'
    assert limpar_titulo('Live Your Life') == 'Live Your Life'


def test_lrclib_get_exato_antes_do_search(monkeypatch, tmp_path):
    from app.lyrics import get_lyrics
    # Desliga a fonte 2 (syncedlyrics): se estiver instalada, ela responde antes
    # e o teste nunca chega ao LRCLIB — que é justamente o que queremos exercitar.
    monkeypatch.setattr('app.lyrics._buscar_fonte_2_syncedlyrics', lambda *a, **k: None)
    chamadas = []

    class FakeResponse:
        def __init__(self, status_code, corpo):
            self.status_code = status_code
            self._corpo = corpo

        def json(self):
            return self._corpo

    def fake_get(url, params=None, headers=None, timeout=None):
        chamadas.append(url)
        if url.endswith('/api/get'):
            assert params['track_name'] == 'Infiel'
            return FakeResponse(200, {'syncedLyrics': '[00:10.00] verso exato'})
        return FakeResponse(404, [])

    monkeypatch.setattr('requests.get', fake_get)
    res = get_lyrics('t1', 'Marília Mendonça - Infiel (Vídeo Oficial)',
                     'Marília Mendonça', pasta_lyrics=tmp_path)
    assert res['found'] is True and res['synced'] is True
    assert chamadas[0].endswith('/api/get')


def test_busca_ytmusic_usa_prefixo_e_mapeia_artista(client, monkeypatch):
    capturado = {}

    class YTMusic(FakeYDL):
        def extract_info(self, url, download=False):
            capturado['url'] = url
            return {'entries': [{
                'id': 'm1', 'track': 'Infiel', 'artists': [{'name': 'Marília Mendonça'}],
                'duration': 200, 'webpage_url': 'https://music.youtube.com/watch?v=m1',
            }]}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', YTMusic)
    corpo = sem_500(client.post('/api/search', json={'query': 'infiel', 'fonte': 'ytmusic'})).json()
    assert 'music.youtube.com/search' in capturado['url']
    assert capturado['url'].endswith('#songs')
    assert corpo[0]['title'] == 'Infiel'
    assert corpo[0]['artist'] == 'Marília Mendonça'


def test_busca_ytmusic_com_falha_cai_para_youtube(client, monkeypatch):
    """YouTube Music fora do ar não pode virar erro: usa o YouTube comum."""
    class YTMQuebrado(FakeYDL):
        def extract_info(self, url, download=False):
            if 'music.youtube.com' in url:
                import yt_dlp.utils
                raise yt_dlp.utils.DownloadError('YTM fora do ar')
            return {'entries': type(self).entradas}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', YTMQuebrado)
    corpo = sem_500(client.post('/api/search', json={'query': 'xyz', 'fonte': 'ytmusic'})).json()
    assert corpo[0]['id'] == 'abc123'


def test_busca_fonte_invalida_422(client):
    assert client.post('/api/search', json={'query': 'musica', 'fonte': 'spotify'}).status_code == 422


def test_download_normaliza_metadados_sujos(client, monkeypatch):
    class Sujo(FakeYDL):
        entradas = [{
            'id': 'sujo01', 'title': 'Marília Mendonça - Infiel (Vídeo Oficial)',
            'uploader': 'Marília Mendonça', 'duration': 200,
            'webpage_url': 'https://exemplo/sujo01',
        }]

        def extract_info(self, url, download=False):
            return dict(type(self).entradas[0])

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', Sujo)
    req = main.DownloadRequest(url='https://exemplo/sujo01', id='sujo01')
    main.run_download('sujo01', req.url, req, 'equilibrada')
    faixa = achar_por(client.get('/api/tracks').json(), 'id', 'sujo01')
    assert faixa['title'] == 'Infiel'
    assert faixa['artist'] == 'Marília Mendonça'


def test_patch_metadados_limpa_letra_e_libera_rebusca(client, faixa):
    main.LYRICS.mkdir(parents=True, exist_ok=True)
    (main.LYRICS / 'abc123.lrc').write_text('[00:10.00] x', encoding='utf-8')
    corpo = sem_500(client.patch('/api/tracks/abc123',
                                 json={'title': 'Infiel', 'artist': 'Marília Mendonça'})).json()
    assert corpo['title'] == 'Infiel'
    assert corpo['artist'] == 'Marília Mendonça'
    assert not (main.LYRICS / 'abc123.lrc').exists()


def test_patch_metadados_inexistente_404(client):
    assert client.patch('/api/tracks/nao-existe', json={'title': 'X'}).status_code == 404


def test_frontend_tem_fonte_ytmusic_e_correcao_letra(client):
    html = client.get('/').text
    assert 'id="searchSource"' in html
    assert 'ytmusic' in html
    js_busca = client.get('/static/app.js').text
    assert 'fonteBusca()' in js_busca
    js_karaoke = client.get('/static/karaoke.js').text
    assert 'corrigirBuscar' in js_karaoke
    assert '/api/tracks/' in js_karaoke


# --------------------------------------------------------------------------
# 11. Trava de anfitrião: o celular do convidado não dispara ação pesada
# --------------------------------------------------------------------------
# O app fica aberto na rede de propósito (é assim que o convidado escolhe
# música), mas baixar, separar e preparar consomem a máquina inteira. Essas
# ações só aceitam pedidos vindos do próprio PC do karaokê.
ACOES_DO_ANFITRIAO = [
    ('post', '/api/download',
     {'id': 'abc123', 'url': 'https://exemplo/abc123', 'title': 'Faixa', 'artist': 'Alguém'}),
    ('post', '/api/playlist', {'url': 'https://youtube.com/playlist?list=PLabc123'}),
    ('post', '/api/library/prepare', {}),
    ('post', '/api/tracks/abc123/cover', {}),
    ('delete', '/api/tracks/abc123', None),
    ('post', '/api/separation/abc123', {}),
    ('post', '/api/karaoke/abc123/prepare', None),
    ('delete', '/api/scores', None),
    ('post', '/api/party/control', {'cmd': 'pause'}),
]


@pytest.mark.parametrize('metodo,caminho,corpo', ACOES_DO_ANFITRIAO,
                         ids=[caminho for _, caminho, _ in ACOES_DO_ANFITRIAO])
def test_convidado_nao_dispara_acao_do_anfitriao(cliente_externo, metodo, caminho, corpo):
    """Vindo de um celular da rede, é 403 com recado claro (não é 500)."""
    chamada = getattr(cliente_externo, metodo)
    resposta = chamada(caminho, **({'json': corpo} if corpo is not None else {}))

    assert resposta.status_code == 403
    assert 'computador do karaokê' in resposta.json()['detail']


@pytest.mark.parametrize('caminho', ['/api/render', '/api/lyrics/abc123/upload'])
def test_convidado_nao_envia_arquivo_pesado(cliente_externo, caminho):
    resposta = cliente_externo.post(
        caminho, files={'arquivo': ('teste.lrc', b'[00:01.00] oi', 'text/plain')})

    assert resposta.status_code == 403
    assert 'computador do karaokê' in resposta.json()['detail']


def test_anfitriao_em_localhost_pode_disparar(client, faixa, monkeypatch):
    """A mesma chamada que o convidado leva 403 funciona no PC do karaokê."""
    monkeypatch.setattr(main, 'run_separation_com_preparo', lambda *a, **k: None)
    (main.DOWNLOADS / 'abc123.mp3').write_bytes(b'mp3')

    assert client.post('/api/separation/abc123', json={}).status_code == 200
    assert client.post('/api/library/prepare', json={}).status_code == 200


def test_mesmo_ip_da_maquina_tambem_e_anfitriao(cliente_externo, monkeypatch):
    """Abrir o app pelo IP da própria máquina (e não localhost) também vale."""
    monkeypatch.setattr('app.anfitriao.ip_da_rede', lambda: '192.168.1.50')

    assert cliente_externo.post('/api/library/prepare', json={}).status_code == 200


def test_convidado_continua_escolhendo_musicas(cliente_externo, faixa, monkeypatch):
    """O que a festa precisa segue livre: buscar, ouvir prévia e enfileirar."""
    class ComAudio(FakeYDL):
        def extract_info(self, url, download=False):
            return {'url': 'https://audio.test/x.m4a', 'title': 'Ok', 'duration': 100}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', ComAudio)

    assert sem_500(cliente_externo.post(
        '/api/search', json={'query': 'evidencias', 'limite': 3})).status_code == 200
    assert sem_500(cliente_externo.post(
        '/api/preview', json={'id': 'abc123'})).status_code == 200
    assert sem_500(cliente_externo.post('/api/queue', json={
        'track_id': 'abc123', 'added_by': 'Ana'})).status_code == 201
    assert sem_500(cliente_externo.get('/api/tracks')).status_code == 200
    assert sem_500(cliente_externo.get('/api/queue')).status_code == 200


def test_convidado_pode_escolher_a_proxima_da_fila(cliente_externo, faixa):
    """Mexer na fila é combinado entre todos — inclusive tocar agora/remover."""
    item = cliente_externo.post('/api/queue', json={
        'track_id': 'abc123', 'added_by': 'Ana'}).json()['item']

    assert sem_500(cliente_externo.post(
        f"/api/queue/{item['item_id']}/move", json={'direcao': 'up'})).status_code == 200
    assert sem_500(cliente_externo.post(
        f"/api/queue/{item['item_id']}/play")).status_code == 200
    assert sem_500(cliente_externo.delete(f"/api/queue/{item['item_id']}")).status_code == 200


def test_network_avisa_se_quem_pergunta_e_o_anfitriao(client, cliente_externo):
    """O app usa isso para saber o que pode oferecer a quem abriu."""
    assert client.get('/api/network').json()['anfitriao'] is True
    assert cliente_externo.get('/api/network').json()['anfitriao'] is False


def test_reconhecimento_de_anfitriao_por_endereco(monkeypatch):
    """Teste direto da regra: loopback e o IP da máquina são o anfitrião."""
    from app import anfitriao

    monkeypatch.setattr(anfitriao, 'ip_da_rede', lambda: '192.168.0.10')

    def pedido(ip):
        class Cliente:
            host = ip

        class Pedido:
            client = Cliente()

        return Pedido()

    assert anfitriao.veio_do_anfitriao(pedido('127.0.0.1')) is True
    assert anfitriao.veio_do_anfitriao(pedido('::1')) is True
    assert anfitriao.veio_do_anfitriao(pedido('192.168.0.10')) is True
    assert anfitriao.veio_do_anfitriao(pedido('192.168.1.50')) is False
    assert anfitriao.veio_do_anfitriao(pedido('10.0.0.9')) is False

