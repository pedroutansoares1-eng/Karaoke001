"""Sistema robusto de busca e obtenção de letras para karaokê.

Implementa uma cadeia de 6 fontes com fallbacks:
1. Arquivo local (.lrc em lyrics/)
2. Agregador syncedlyrics (Musixmatch, LRCLIB, NetEase, Deezer)
3. LRCLIB direto (https://lrclib.net/api/search?q=...)
4. Vagalume API (excelente para música brasileira, texto sem sync)
5. Lyrics.ovh API (texto sem sync)
6. Whisper (transcrição automática via áudio como último recurso)

Retorna sempre um dicionário JSON consistente:
- Sincronizado: {"found": True, "synced": True, "source": "...", "lines": [{"time": 12.5, "text": "verso"}]}
- Sem sync:     {"found": True, "synced": False, "source": "...", "texto": "letra inteira..."}
- Não achou:    {"found": False}
"""
import logging
import re
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("letras")

# Regex para validação e extração de linhas LRC
LRC_LINE_REGEX = re.compile(r"^\[(\d{1,2}):(\d{2}(?:[.,]\d{1,3})?)\](.*)$")
LRC_WORD_REGEX = re.compile(r"<(?:(\d+):)?(\d{1,2})(?:[.,](\d{1,3}))?>")

# Regex para limpeza rigorosa de títulos vindo do YouTube / YouTube Music
PATTERNS_LIMPEZA_TITULO = [
    # Remove parênteses e colchetes com termos de vídeos (ex: (Official Video), [HD], (Ao Vivo), (Lyrics))
    r"\([^)]*\)",
    r"\[[^\]]*\]",
    # Termos soltos de vídeo/áudio SOMENTE no fim do título (sem parênteses),
    # ex: "Infiel Official Video", "Evidências - Ao Vivo", "Song Official Audio"
    r"\s*[-–—|:‑]?\s*(?:official\s*(?:music\s*)?(?:video|audio|lyric|lyrics|clip|clipe|visualizer|visualiser|m\/v|mv)|"
    r"music\s*video|clipe\s*oficial|\bclipe\b|lyrics?\s*video|audio\s*oficial|"
    r"official\s*audio|\bhq\b|\bhd\b|4k|remaster(?:ed|izado)?|vers[aã]o\s*remasterizada|"
    r"ao\s*vivo|\blive\b|ac[uú]stico|unplugged|visualizer|visualiser)\s*\.?\s*$",
    # Remove sufixos de feat/ft/com e tudo depois
    r"\b(?:feat\.?|ft\.?|com)\b.*$",
    # Remove sufixo de canais comuns do YouTube tipo "- Topic", "- Vevo"
    r"\s*[-–—|]\s*(?:topic|vevo)\b.*$",
    # Remove sufixos de áudio/vídeo/remaster no fim da string sem parênteses
    r"\s*[-–—|]\s*(?:official\s*video|clipe\s*oficial|audio\s*oficial|lyric\s*video|remaster(?:ed)?|ao\s*vivo).*$",
]

# Sufixos de canal/uploader que não são nome de artista
SUFFIXOS_CANAL = re.compile(
    r"\s*[-–—|]\s*(?:topic|vevo|official|oficial|music|música|records|tv)$",
    flags=re.IGNORECASE,
)


def limpar_titulo(title: str | None) -> str:
    """Limpa o título removendo marcadores comuns do YouTube para aumentar o match nas APIs."""
    if not title:
        return ""
    texto = title
    for padrao in PATTERNS_LIMPEZA_TITULO:
        texto = re.sub(padrao, "", texto, flags=re.IGNORECASE)
    # Remove múltiplos espaços e traços soltos nas pontas
    texto = re.sub(r"\s{2,}", " ", texto)
    return texto.strip(" -–—_|").strip()


def limpar_artista(artist: str | None) -> str:
    """Limpa nome de canal/uploader para virar nome de artista buscável."""
    if not artist:
        return ""
    texto = SUFFIXOS_CANAL.sub("", artist).strip()
    texto = re.sub(r"\s{2,}", " ", texto)
    return texto.strip(" -–—_|").strip()


def separar_artista_titulo(titulo: str | None, artista: str | None) -> tuple[str, str]:
    """Divide títulos no formato "Artista - Música" vindos do YouTube.

    Retorna (titulo_limpo, artista_limpo). Quando o título já vem separado
    ou não há separador confiável, devolve os valores apenas limpos.
    """
    titulo_limpo = limpar_titulo(titulo)
    artista_limpo = limpar_artista(artista)
    if titulo_limpo and " - " not in (titulo or "") and " – " not in (titulo or ""):
        # Título sem o padrão "A - B": confia no artista como veio.
        return titulo_limpo, artista_limpo
    for sep in (" - ", " – ", " — "):
        if sep in (titulo or ""):
            possivel_artista, _, possivel_musica = (titulo or "").partition(sep)
            possivel_artista = limpar_artista(possivel_artista)
            possivel_musica = limpar_titulo(possivel_musica)
            if possivel_artista and possivel_musica:
                # Só troca o artista se o atual estiver vazio ou for genérico
                # (nome de canal) ou igual ao prefixo do título.
                artista_atual = (artista_limpo or "").lower()
                if (not artista_atual or artista_atual in ("unknown", "desconhecido")
                        or artista_atual in possivel_artista.lower()
                        or possivel_artista.lower() in artista_atual):
                    return possivel_musica, possivel_artista
                # Artista real diferente do prefixo: mantém o artista e usa a
                # parte da música como título (ex: "Coletânea - Infiel").
                return possivel_musica, artista_limpo
    return titulo_limpo, artista_limpo


def candidatos_busca(title: str | None, artist: str | None) -> list[tuple[str, str]]:
    """Gera combinações (titulo, artista) ordenadas por chance de acerto.

    O primeiro candidato é o mais limpo/provável; os seguintes são fallbacks
    com os valores originais para não perder nada que a limpeza estragou.
    """
    titulo_orig = (title or "").strip()
    artista_orig = (artist or "").strip()
    titulo_limpo, artista_limpo = separar_artista_titulo(titulo_orig, artista_orig)
    candidatos: list[tuple[str, str]] = []
    for cand in [
        (titulo_limpo, artista_limpo),
        (titulo_limpo, artista_orig.strip()),
        (titulo_orig, artista_limpo),
        (titulo_orig, artista_orig),
    ]:
        if cand[0] and cand not in candidatos:
            candidatos.append(cand)
    return candidatos


def normalizar_metadados(
    info_title: str | None,
    info_track: str | None,
    info_artist: str | None,
    info_uploader: str | None,
) -> tuple[str, str]:
    """Escolhe o melhor (título, artista) a partir dos metadados do yt-dlp.

    Prefere os campos de música do YouTube Music (`track`/`artist`); quando
    só há título de vídeo + canal, divide "Artista - Música" e limpa o canal.
    """
    titulo_bruto = (info_track or info_title or "").strip()
    artista_bruto = (info_artist or info_uploader or "").strip()
    titulo, artista = separar_artista_titulo(titulo_bruto, artista_bruto)
    return titulo or titulo_bruto, artista or artista_bruto


def parse_lrc_lines(conteudo_lrc: str) -> list[dict[str, Any]]:
    """Converte o texto de um arquivo .lrc em uma lista de objetos [{"time": float, "text": str}]."""
    linhas: list[dict[str, Any]] = []
    for linha in conteudo_lrc.splitlines():
        match = LRC_LINE_REGEX.match(linha.strip())
        if match:
            minutos = int(match.group(1))
            segundos = float(match.group(2).replace(",", "."))
            tempo_total = round(minutos * 60 + segundos, 2)
            texto_verso = match.group(3).strip()
            palavras = []
            marcadores = list(LRC_WORD_REGEX.finditer(texto_verso))
            if marcadores:
                for indice, marcador in enumerate(marcadores):
                    minutos_palavra = int(marcador.group(1) or 0)
                    segundos_palavra = float(f'{marcador.group(2)}.{marcador.group(3) or "0"}')
                    inicio_palavra = minutos_palavra * 60 + segundos_palavra
                    fim_palavra = marcadores[indice + 1].start() if indice + 1 < len(marcadores) else len(texto_verso)
                    palavra = texto_verso[marcador.end():fim_palavra].strip()
                    if palavra:
                        palavras.append({"time": round(inicio_palavra, 3), "text": palavra})
                texto_verso = " ".join(palavra["text"] for palavra in palavras)
            linha = {"time": tempo_total, "text": texto_verso}
            if palavras:
                linha["words"] = palavras
            linhas.append(linha)
    linhas.sort(key=lambda x: x["time"])
    return linhas


def enriquecer_palavras(linhas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Garante timestamps por palavra para o destaque karaokê.

    Se a linha já traz ``words`` (Enhanced LRC), é mantida. Caso contrário, as
    palavras são distribuídas uniformemente entre o início da linha e o início
    da próxima — dando um preenchimento por palavra sincronizado aos versos.
    """
    total = len(linhas)
    for i, linha in enumerate(linhas):
        if linha.get("words"):
            continue
        palavras = [p for p in re.split(r"\s+", str(linha.get("text", "")).strip()) if p]
        if not palavras:
            continue
        inicio = float(linha.get("time", 0.0))
        if i + 1 < total:
            fim = float(linhas[i + 1].get("time", inicio + 2.0))
        else:
            fim = inicio + max(2.0, len(palavras) * 0.4)
        if fim <= inicio:
            fim = inicio + max(1.0, len(palavras) * 0.3)
        passo = (fim - inicio) / len(palavras)
        linha["words"] = [
            {"time": round(inicio + passo * k, 3), "text": palavra}
            for k, palavra in enumerate(palavras)
        ]
    return linhas


def validar_conteudo_lrc(conteudo: str) -> bool:
    """Verifica se o conteúdo de um arquivo possui ao menos uma linha válida de timestamp LRC."""
    if not conteudo or not conteudo.strip():
        return False
    return any(LRC_LINE_REGEX.match(linha.strip()) for linha in conteudo.splitlines())


def salvar_lrc_local(track_id: str, conteudo_lrc: str, pasta_lyrics: Path) -> Path:
    """Salva a letra sincronizada no disco local em lyrics/{track_id}.lrc."""
    pasta_lyrics.mkdir(parents=True, exist_ok=True)
    caminho = pasta_lyrics / f"{track_id}.lrc"
    caminho.write_text(conteudo_lrc, encoding="utf-8")
    return caminho


# ==============================================================================
# FONTES DE BUSCA DE LETRAS
# ==============================================================================

def _buscar_fonte_1_local(track_id: str, pasta_lyrics: Path) -> dict[str, Any] | None:
    """Fonte 1: Leitura de arquivo .lrc local existente."""
    logger.info(f"[letras] Tentando Fonte 1 (Local): '{track_id}'")
    caminho = pasta_lyrics / f"{track_id}.lrc"
    if caminho.exists() and caminho.is_file():
        try:
            conteudo = caminho.read_text(encoding="utf-8", errors="replace")
            if validar_conteudo_lrc(conteudo):
                linhas = enriquecer_palavras(parse_lrc_lines(conteudo))
                logger.info(f"[letras] Fonte 1 (Local) Sucesso: {len(linhas)} linhas sincronizadas")
                return {"found": True, "synced": True, "source": "local", "lines": linhas}
            elif conteudo.strip():
                logger.info("[letras] Fonte 1 (Local) Sucesso: Texto sem sync encontrado")
                return {"found": True, "synced": False, "source": "local", "texto": conteudo.strip()}
        except Exception as exc:
            logger.warning(f"[letras] Erro ao ler arquivo local '{caminho}': {exc}")
    logger.info("[letras] Fonte 1 (Local): Não encontrado")
    return None


def _buscar_fonte_2_syncedlyrics(titulo: str, artista: str) -> str | None:
    """Fonte 2: Agregador syncedlyrics (Musixmatch, LRCLIB, NetEase, Deezer)."""
    try:
        import syncedlyrics
    except ImportError:
        logger.info("[letras] Fonte 2 (syncedlyrics): Módulo 'syncedlyrics' não instalado, pulando")
        return None
    for titulo_cand, artista_cand in candidatos_busca(titulo, artista):
        termos = f"{titulo_cand} {artista_cand}".strip()
        logger.info(f"[letras] Tentando Fonte 2 (syncedlyrics): '{termos}'")
        try:
            lrc = syncedlyrics.search(termos)
            if lrc and validar_conteudo_lrc(lrc):
                logger.info("[letras] Fonte 2 (syncedlyrics) Sucesso")
                return lrc
            logger.info("[letras] Fonte 2 (syncedlyrics): Nenhum resultado sincronizado")
        except Exception as exc:
            logger.warning(f"[letras] Fonte 2 (syncedlyrics) Exceção: {exc}")
    return None


def _extrair_item_lrclib(item: dict[str, Any]) -> dict[str, Any] | None:
    """Extrai letra sincronizada (ou texto) de um item do LRCLIB."""
    if not isinstance(item, dict):
        return None
    synced = item.get("syncedLyrics")
    if synced and validar_conteudo_lrc(synced):
        return {"synced": True, "raw_lrc": synced}
    plain = item.get("plainLyrics")
    if plain and plain.strip():
        return {"synced": False, "texto": plain.strip()}
    return None


def _ultimo_tempo_linhas(linhas: list[dict[str, Any]]) -> float:
    """Maior timestamp de uma lista de versos (0 se a lista estiver vazia)."""
    return max((float(l.get("time") or 0.0) for l in linhas), default=0.0)


def _cobertura_lrc(raw_lrc: str) -> tuple[float, int]:
    """(tempo do último verso, nº de versos) de uma letra sincronizada.

    Serve para comparar candidatos do LRCLIB: quando existem várias versões da
    mesma faixa, escolhemos a que cobre a maior parte da música (e, em empate,
    a que tem mais versos). Sem isso, uma versão parcial podia ser escolhida só
    por vir primeiro na busca.
    """
    try:
        linhas = parse_lrc_lines(raw_lrc)
    except Exception:  # noqa: BLE001 - texto malformado nunca deve derrubar a busca
        return (0.0, 0)
    if not linhas:
        return (0.0, 0)
    return (_ultimo_tempo_linhas(linhas), len(linhas))


def _deve_procurar_melhor(extraido: dict[str, Any], duracao: float | None) -> bool:
    """True se a letra sincronizada termina bem antes do fim da música.

    Nesse caso vale continuar procurando: o LRCLIB às vezes guarda uma versão
    parcial e uma completa para a mesma faixa (e a parcial podia ser a primeira
    a aparecer).
    """
    if not duracao or not extraido.get("synced") or not extraido.get("raw_lrc"):
        return False
    ultimo, _ = _cobertura_lrc(extraido["raw_lrc"])
    return ultimo > 0 and ultimo < float(duracao) - 25


def _buscar_fonte_3_lrclib(
    titulo: str, artista: str, duration: float | None = None
) -> dict[str, Any] | None:
    """Fonte 3: LRCLIB — primeiro o endpoint exato (/api/get), depois a busca difusa.

    O /api/get com track_name + artist_name (+ duration) tem taxa de acerto
    muito maior que o /api/search?q= genérico para títulos vindos do YouTube.
    """
    headers = {"User-Agent": "KaraokeStudio/1.0 (https://github.com/anomalyco/antigravity)"}
    duracao = round(duration) if duration else None
    reserva_get: dict[str, Any] | None = None

    for titulo_cand, artista_cand in candidatos_busca(titulo, artista):
        # Passe exato: /api/get
        if titulo_cand and artista_cand:
            params: dict[str, Any] = {"track_name": titulo_cand, "artist_name": artista_cand}
            if duracao:
                params["duration"] = duracao
            logger.info(f"[letras] Tentando Fonte 3 (LRCLIB /api/get): {params}")
            try:
                resp = requests.get(
                    "https://lrclib.net/api/get", params=params, headers=headers, timeout=10)
                if resp.status_code == 200:
                    extraido = _extrair_item_lrclib(resp.json())
                    if extraido:
                        # Se a versão exata termina muito antes do fim da música,
                        # guardamos como reserva e ainda procuramos uma mais
                        # completa na busca difusa.
                        if _deve_procurar_melhor(extraido, duracao):
                            logger.info(
                                "[letras] Fonte 3 (LRCLIB /api/get) versão parcial; "
                                "procurando uma mais completa")
                            reserva_get = extraido
                        else:
                            if extraido.get("synced"):
                                logger.info("[letras] Fonte 3 (LRCLIB /api/get) Sucesso sincronizado")
                            else:
                                logger.info("[letras] Fonte 3 (LRCLIB /api/get) Sucesso texto")
                            return extraido
            except Exception as exc:
                logger.warning(f"[letras] Fonte 3 (LRCLIB /api/get) Exceção: {exc}")

        # Passe difuso: /api/search?q= com filtro de duração (±8s)
        consultas = []
        if titulo_cand and artista_cand:
            consultas.append(f"{titulo_cand} {artista_cand}")
        if titulo_cand:
            consultas.append(titulo_cand)
        for idx, consulta in enumerate(consultas, start=1):
            logger.info(f"[letras] Tentando Fonte 3 (LRCLIB search '{consulta}')")
            try:
                resp = requests.get(
                    "https://lrclib.net/api/search", params={"q": consulta},
                    headers=headers, timeout=10)
                if resp.status_code != 200:
                    continue
                resultados = resp.json()
                if not isinstance(resultados, list) or not resultados:
                    continue
                melhor_dentro: dict[str, Any] | None = None
                melhor_chave: tuple[float, int] | None = None
                sincronizado_fora_duracao = None
                for item in resultados:
                    if not isinstance(item, dict):
                        continue
                    dur_item = item.get("duration")
                    dentro = (
                        duracao is None or dur_item is None
                        or abs(float(dur_item) - duracao) <= 8
                    )
                    extraido = _extrair_item_lrclib(item)
                    if not extraido:
                        continue
                    if dentro:
                        # Nunca fica com a primeira: escolhe a versão que cobre a
                        # maior parte da música (texto sem sync entra por último).
                        if extraido.get("synced") and extraido.get("raw_lrc"):
                            chave = _cobertura_lrc(extraido["raw_lrc"])
                        else:
                            chave = (-1.0, 0)
                        if melhor_chave is None or chave > melhor_chave:
                            melhor_chave = chave
                            melhor_dentro = extraido
                        continue
                    if extraido.get("synced") and sincronizado_fora_duracao is None:
                        sincronizado_fora_duracao = extraido
                if melhor_dentro:
                    ultimo, versos = melhor_chave or (0.0, 0)
                    logger.info(
                        f"[letras] Fonte 3 (LRCLIB search) Sucesso "
                        f"(cobre até {ultimo:.1f}s, {versos} versos)")
                    return melhor_dentro
                if sincronizado_fora_duracao:
                    logger.info("[letras] Fonte 3 (LRCLIB search) Sucesso (duração divergente)")
                    return sincronizado_fora_duracao
            except Exception as exc:
                logger.warning(f"[letras] Fonte 3 (LRCLIB search) Exceção: {exc}")

    if reserva_get:
        # Nada mais completo apareceu: devolve a versão parcial do /api/get.
        logger.info("[letras] Fonte 3 (LRCLIB): usando a versão parcial do /api/get")
        return reserva_get

    logger.info("[letras] Fonte 3 (LRCLIB): Nenhum resultado")
    return None


def _buscar_fonte_4_vagalume(titulo: str, artista: str) -> str | None:
    """Fonte 4: Vagalume API (excelente para músicas brasileiras, texto sem sync)."""
    for titulo_cand, artista_cand in candidatos_busca(titulo, artista):
        if not titulo_cand or not artista_cand:
            continue
        logger.info(f"[letras] Tentando Fonte 4 (Vagalume): '{titulo_cand}' / '{artista_cand}'")
        try:
            resp = requests.get(
                "https://api.vagalume.com.br/search.php",
                params={"art": artista_cand, "mus": titulo_cand},
                timeout=10,
            )
            if resp.status_code == 200:
                dados = resp.json()
                if (isinstance(dados, dict) and isinstance(dados.get("mus"), list)
                        and dados["mus"]):
                    texto = dados["mus"][0].get("text")
                    if texto and texto.strip():
                        logger.info("[letras] Fonte 4 (Vagalume) Sucesso: Letra sem sync obtida")
                        return texto.strip()
        except Exception as exc:
            logger.warning(f"[letras] Fonte 4 (Vagalume) Exceção: {exc}")
    logger.info("[letras] Fonte 4 (Vagalume): Não encontrado")
    return None


def _buscar_fonte_5_lyrics_ovh(titulo: str, artista: str) -> str | None:
    """Fonte 5: Lyrics.ovh API (texto sem sync)."""
    for titulo_cand, artista_cand in candidatos_busca(titulo, artista):
        if not titulo_cand or not artista_cand:
            continue
        logger.info(f"[letras] Tentando Fonte 5 (Lyrics.ovh): '{artista_cand}' / '{titulo_cand}'")
        try:
            url = f"https://api.lyrics.ovh/v1/{requests.utils.quote(artista_cand)}/{requests.utils.quote(titulo_cand)}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                dados = resp.json()
                texto = dados.get("lyrics")
                if texto and texto.strip():
                    logger.info("[letras] Fonte 5 (Lyrics.ovh) Sucesso: Letra sem sync obtida")
                    return texto.strip()
        except Exception as exc:
            logger.warning(f"[letras] Fonte 5 (Lyrics.ovh) Exceção tratada silenciosamente: {exc}")
    logger.info("[letras] Fonte 5 (Lyrics.ovh): Não encontrado")
    return None


# Guarda o dispositivo/compute que funcionou e o modelo já carregado, para não
# pagar o custo de carregar de novo (nem de tentar a GPU que falhou) a cada faixa.
_whisper_estado: dict[str, Any] = {}


def _modelo_whisper(device: str, compute: str) -> Any:
    """Carrega (e memoriza) o modelo faster-whisper para o dispositivo dado."""
    chave = f"{device}:{compute}"
    modelo = _whisper_estado.get(chave)
    if modelo is None:
        from faster_whisper import WhisperModel
        modelo = WhisperModel("medium", device=device, compute_type=compute)
        _whisper_estado[chave] = modelo
    return modelo


def _transcrever_whisper_segmentos(audio_path: Path | None) -> list[dict[str, Any]]:
    """Transcreve o áudio com faster-whisper e devolve ``[{"time", "text"}]``.

    Tenta a GPU e cai para a CPU quando o ctranslate2 não consegue carregar as
    bibliotecas CUDA (acontece quando o torch é cu13 e o ct2 espera cu12). O
    VAD remove trechos sem voz e o ``no_speech_prob`` descarta "falas"
    alucinadas em partes instrumentais.
    """
    if not audio_path:
        return []
    audio = Path(audio_path)
    if not audio.exists():
        return []
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        logger.info("[letras] faster-whisper não instalado")
        return []

    preferido = _whisper_estado.get("dispositivo")
    tentativas = (preferido,) if preferido else (("cuda", "float16"), ("cpu", "int8"))
    for device, compute in tentativas:
        try:
            model = _modelo_whisper(device, compute)
            segments, _info = model.transcribe(
                str(audio), beam_size=5, word_timestamps=True,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[letras] faster-whisper ({device}) indisponível: {exc}")
            _whisper_estado.pop(f"{device}:{compute}", None)
            continue
        resultado: list[dict[str, Any]] = []
        for seg in segments:
            texto = (seg.text or "").strip()
            if not texto or getattr(seg, "no_speech_prob", 0.0) >= 0.6:
                continue
            resultado.append({"time": round(float(seg.start), 2), "text": texto})
        _whisper_estado["dispositivo"] = (device, compute)
        logger.info(f"[letras] faster-whisper ({device}): {len(resultado)} versos")
        return resultado
    return []


def _buscar_fonte_6_whisper(audio_path: Path | None) -> str | None:
    """Fonte 6: transcrição automática via faster-whisper (último recurso)."""
    segmentos = _transcrever_whisper_segmentos(audio_path)
    if not segmentos:
        return None
    linhas_lrc = [
        f"[{int(s['time'] // 60):02d}:{s['time'] % 60:05.2f}] {s['text']}"
        for s in segmentos
    ]
    logger.info("[letras] faster-whisper: sucesso")
    return "\n".join(linhas_lrc)


def completar_letra_com_whisper(
    linhas: list[dict[str, Any]],
    duration: float | None,
    audio_path: Path | None,
    margem: float = 20.0,
) -> list[dict[str, Any]]:
    """Completa com o Whisper o final da letra quando ela não cobre a música.

    Mantém os versos já existentes (normalmente de fonte humana/LRCLIB) e
    acrescenta, a partir do último verso, a transcrição automática — assim a
    letra acompanha até o fim em vez de "congelar" no meio. Se nada novo for
    encontrado (ex.: final instrumental), devolve a lista original.
    """
    if not linhas or not duration or not audio_path:
        return linhas
    audio = Path(audio_path)
    if not audio.exists():
        return linhas
    ultimo = _ultimo_tempo_linhas(linhas)
    if ultimo >= float(duration) - margem:
        return linhas
    logger.info(
        f"[letras] letra cobre até {ultimo:.0f}s de {float(duration):.0f}s — "
        "completando o final com o Whisper")
    extras = [
        {"time": s["time"], "text": s["text"]}
        for s in _transcrever_whisper_segmentos(audio)
        if s["time"] > ultimo + 0.5
    ]
    if not extras:
        logger.info("[letras] Whisper não encontrou versos novos no final")
        return linhas
    combinadas = sorted([dict(l) for l in linhas] + extras, key=lambda l: l["time"])
    logger.info(f"[letras] final completado: {len(linhas)} -> {len(combinadas)} versos")
    return enriquecer_palavras(combinadas)


# ==============================================================================
# FUNÇÃO PRINCIPAL DE OBTENÇÃO DE LETRAS
# ==============================================================================

def get_lyrics(
    track_id: str,
    title: str | None,
    artist: str | None,
    duration: float | None = None,
    pasta_lyrics: Path | None = None,
    audio_path: Path | None = None,
) -> dict[str, Any]:
    """Cadeia de prioridade de busca de letras. Nunca dispara exceção 500.

    Retorna o contrato JSON padronizado.
    """
    if pasta_lyrics is None:
        pasta_lyrics = Path(__file__).parent.parent / "data" / "lyrics"
    pasta_lyrics.mkdir(parents=True, exist_ok=True)

    titulo_orig = (title or "").strip()
    artista_orig = (artist or "").strip()

    # --------------------------------------------------------------------------
    # Fonte 1: Arquivo Local
    # --------------------------------------------------------------------------
    res1 = _buscar_fonte_1_local(track_id, pasta_lyrics)
    if res1:
        return res1

    # --------------------------------------------------------------------------
    # Fonte 2: syncedlyrics (itera candidatos "Artista - Música" sozinha)
    # --------------------------------------------------------------------------
    lrc_s2 = _buscar_fonte_2_syncedlyrics(titulo_orig, artista_orig)
    if lrc_s2:
        salvar_lrc_local(track_id, lrc_s2, pasta_lyrics)
        return {
            "found": True,
            "synced": True,
            "source": "syncedlyrics",
            "lines": enriquecer_palavras(parse_lrc_lines(lrc_s2)),
        }

    # --------------------------------------------------------------------------
    # Fonte 3: LRCLIB Direto (endpoint exato + busca difusa, com duração)
    # --------------------------------------------------------------------------
    res3 = _buscar_fonte_3_lrclib(titulo_orig, artista_orig, duration)
    if res3:
        if res3.get("synced") and res3.get("raw_lrc"):
            lrc_content = res3["raw_lrc"]
            salvar_lrc_local(track_id, lrc_content, pasta_lyrics)
            return {
                "found": True,
                "synced": True,
                "source": "lrclib",
                "lines": enriquecer_palavras(parse_lrc_lines(lrc_content)),
            }
        elif not res3.get("synced") and res3.get("texto"):
            return {
                "found": True,
                "synced": False,
                "source": "lrclib",
                "texto": res3["texto"],
            }

    # --------------------------------------------------------------------------
    # Fonte 6 (IA): Whisper/faster-whisper — sincronização automática
    # --------------------------------------------------------------------------
    if audio_path:
        lrc_s6 = _buscar_fonte_6_whisper(audio_path)
        if lrc_s6:
            salvar_lrc_local(track_id, lrc_s6, pasta_lyrics)
            return {
                "found": True,
                "synced": True,
                "source": "whisper (auto)",
                "lines": enriquecer_palavras(parse_lrc_lines(lrc_s6)),
            }

    # --------------------------------------------------------------------------
    # Fonte 4: Vagalume (texto sem sync — só se IA não gerou)
    # --------------------------------------------------------------------------
    texto_s4 = _buscar_fonte_4_vagalume(titulo_orig, artista_orig)
    if texto_s4:
        return {
            "found": True,
            "synced": False,
            "source": "vagalume",
            "texto": texto_s4,
        }

    # --------------------------------------------------------------------------
    # Fonte 5: Lyrics.ovh (texto sem sync — apenas fallback)
    # --------------------------------------------------------------------------
    texto_s5 = _buscar_fonte_5_lyrics_ovh(titulo_orig, artista_orig)
    if texto_s5:
        return {
            "found": True,
            "synced": False,
            "source": "lyrics.ovh",
            "texto": texto_s5,
        }

    logger.info(f"[letras] Nenhuma fonte encontrou letra para '{titulo_orig}' / '{artista_orig}'")
    return {"found": False}


def fetch_lyrics(artist: str | None, title: str | None, duration: float | None, output_path: Path) -> bool:
    """Função legada mantida para compatibilidade com o fluxo de preparação do backend.

    Tenta buscar a letra usando get_lyrics e salva em output_path se encontrar.
    """
    track_id = output_path.stem
    resultado = get_lyrics(track_id, title, artist, duration, output_path.parent)
    if resultado.get("found"):
        if resultado.get("synced") and resultado.get("lines"):
            # Converte as linhas de volta em LRC se ainda não tiver salvado no disco
            if not output_path.exists():
                conteudo = "\n".join(
                    f"[{int(l['time']//60):02d}:{l['time']%60:05.2f}] {l['text']}" for l in resultado["lines"]
                )
                salvar_lrc_local(track_id, conteudo, output_path.parent)
            return True
        elif not resultado.get("synced") and resultado.get("texto"):
            if not output_path.exists():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(resultado["texto"], encoding="utf-8")
            return True
    return False
