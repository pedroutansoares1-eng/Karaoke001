/* Karaokê Studio — modo karaokê (IIFE exposta como Karaoke).
 *
 * Integração com o backend de busca de letras em 6 fontes (/api/lyrics/{track_id}):
 * - Card elegante quando a letra não for encontrada (com input file + dica do Google).
 * - Suporte a letras com sync e sem sync (verso a verso, avanço manual por clique/setas/Enter,
 *   ou distribuição automática pela duração da música).
 * - Toast de feedback em cada caso ("Letra encontrada via Vagalume", "Usando arquivo local", etc.).
 */
const Karaoke = (() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const TOM_CERTO = 0.6;
  const JANELA = 0.18;
  const HZ_MIN = 60;
  const HZ_MAX = 1200;
  const CLARIDADE_MIN = 0.82;
  const MIDI_MIN = 55;
  const MIDI_MAX = 85;
  const MAX_HISTORICO = 5000;
  const INTERVALO_DESENHO = 33;
  const SUAVIZACAO_PITCH = 0.28;
  const MINIMO_AMOSTRA_SCORE = 0.04;
  const RMS_MINIMO_VOZ = 0.018;
  const SALTO_SEEK = 0.8;

  const NOMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

  let ctx = null;
  let stream = null;
  let streamDueto = null;
  let fonte = null;
  let analisador = null;
  let raf = null;
  let buffer = null;
  let ref = [];
  let temposRef = [];
  let cantados = [];
  let erros = [];
  let saltos = 0;
  let linhas = [];
  let ativo = false;
  let linhaAtual = -1;
  let track = null;
  let ultimoDesenho = 0;
  let canvasRef = null;
  let duracaoRef = 1;
  let encerrado = false;
  let modoManual = false;   // true = sem sync, avanço manual
  let modoUniforme = false; // true = sem sync, distribuição uniforme
  let indiceManual = -1;
  let duracaoTotal = 180;   // duração da música em segundos
  let pontuacaoEnviada = false; // evita registrar a mesma performance duas vezes
  let pitchSuavizado = null;
  let ultimoTempoAudio = null;
  let fimLetraT = null;      // instante do último verso COM texto (p/ avisar o fim)
  let ajusteLatencia = 0;
  let audioMic = null;
  let ganhoMonitor = null;
  let dspNodes = [];
  let notchFilter = null;
  let nosSessao = [];
  let fonteAudioCriada = false;
  let gravador = null;
  let blocosGravacao = [];
  let arquivoGravacao = null;
  let gravacaoDueto = false;
  let fonteAudio = null;
  let speakerGain = null;
  let silencioAnalise = null;
  let mixMaster = null;
  let mixDestino = null;
  let ganhoGravacaoVoz = null;
  let fonteInstrumental = null;
  let ganhoInstrumental = null;
  let panInstrumental = null;
  let panVoz = null;

  const CHAVE_CANTOR = 'vocalis_singer';

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c]));

  const nomeNota = (midi) => {
    const r = Math.round(midi);
    return `${NOMES[((r % 12) + 12) % 12]}${Math.floor(r / 12) - 1}`;
  };

  function aviso(texto) {
    if (window.toast) window.toast(texto, 'erro');
  }

  function infoToast(texto) {
    if (window.toast) window.toast(texto, 'ok');
  }

  /* ------------------------------------------------------------------- LRC */
  function parseLrc(texto) {
    return texto.split(/\r?\n/).map((l) => {
      const m = l.match(/^\[(\d+):(\d+(?:[.,]\d+)?)\](.*)$/);
      return m ? { t: Number(m[1]) * 60 + Number(m[2].replace(',', '.')), texto: m[3].trim() } : null;
    }).filter(Boolean).sort((a, b) => a.t - b.t);
  }

  function parseTextoSimples(texto) {
    return texto.split(/\r?\n/).map((l) => {
      const limpo = l.replace(/^\[[a-zA-Z#]+:.*?\]\s*/, '').trim();
      return limpo || null;
    }).filter(Boolean);
  }

  function temSync(texto) {
    return /^\[\d{1,2}:\d{2}(?:[.,]\d{1,3})?\]/m.test(texto);
  }

  function indiceDaLinha(t) {
    let i = -1;
    for (let k = 0; k < linhas.length; k += 1) {
      if (linhas[k].t <= t) i = k; else break;
    }
    return i;
  }

  function atualizarPalavras(tempo) {
    const linha = linhas[linhaAtual];
    if (linhaAtual < 0 || !linha?.words?.length) return;
    // Se não tem _words, tenta recompor do DOM
    const elAtivo = $$('.lyrics p.active')[0];
    if (!elAtivo) return;
    const spans = elAtivo.querySelectorAll('.lyric-word');
    if (!spans.length) return;
    const indice = (linha.words || []).reduce((res, palavra, i) => (
      palavra.time + ajusteLatencia <= tempo ? i : res
    ), -1);
    spans.forEach((el, i) => el.classList.toggle('active-word', i <= indice));
  }

  // Avisa discretamente quando a letra termina e a música ainda continua: sem
  // isso a última linha fica parada e parece que o app travou.
  function atualizarFimLetra(t) {
    const el = $('#lyricsFim');
    if (!el) return;
    if (fimLetraT === null) {
      fimLetraT = linhas.reduce(
        (maior, l) => (l && l.texto && l.t > maior ? l.t : maior), 0);
    }
    const mostrar = fimLetraT > 0 && t > fimLetraT + 6;
    el.hidden = !mostrar;
  }

  /* -------------------------------------------------- referência de pitch */
  function prepararReferencia(dados) {
    ref = (dados.pitch_ref || [])
      .filter((p) => p && Number.isFinite(p.t) && Number.isFinite(p.midi))
      .map((p) => ({ t: p.t, midi: p.midi }))
      .sort((a, b) => a.t - b.t);
    temposRef = ref.map((p) => p.t);
    duracaoRef = temposRef.length ? temposRef[temposRef.length - 1] : 1;
  }

  function referenciaMaisProxima(t) {
    if (!temposRef.length) return null;
    let ini = 0;
    let fim = temposRef.length - 1;
    while (ini < fim) {
      const meio = (ini + fim) >> 1;
      if (temposRef[meio] < t) ini = meio + 1; else fim = meio;
    }
    let melhor = null;
    for (let k = Math.max(0, ini - 2); k <= Math.min(temposRef.length - 1, ini + 2); k += 1) {
      const delta = Math.abs(temposRef[k] - t);
      if (delta <= JANELA && (!melhor || delta < melhor.delta)) {
        melhor = { delta, midi: ref[k].midi };
      }
    }
    return melhor;
  }

  /* ------------------------------------------------------------------ score */
  function calcularScore() {
    if (!erros.length) return 0;

    // A pontuação considera tempo real cantado, não apenas quantidade de frames.
    const duracaoCantada = cantados.length > 1
      ? Math.max(0, cantados[cantados.length - 1].t - cantados[0].t)
      : 0;
    const duracaoReferencia = Math.max(1, duracaoRef);
    const cobertura = Math.min(1, duracaoCantada / duracaoReferencia) * 100;

    // Erros pequenos são aceitáveis musicalmente; erros acima de 2 tons zeram
    // a contribuição daquele trecho de forma gradual, sem saltos artificiais.
    const qualidade = erros.reduce((total, erro) => {
      const normalizado = Math.min(1, Math.abs(erro) / 2.5);
      return total + (1 - (normalizado * normalizado));
    }, 0) / erros.length * 100;

    // Saltos de mais de 2 semitons penalizam estabilidade, mas não anulam tudo.
    const estabilidade = cantados.length > 1
      ? Math.max(0, 1 - saltos / (cantados.length - 1) * 1.8) * 100 : 100;

    // Precisão é o fator principal; cobertura impede pontuação alta cantando pouco.
    const bruto = qualidade * 0.6 + cobertura * 0.25 + estabilidade * 0.15;
    const fatorAmostra = Math.min(1, cantados.length / Math.max(1, ref.length * MINIMO_AMOSTRA_SCORE));
    return Math.round(Math.max(0, Math.min(100, bruto * fatorAmostra)));
  }

  function medalha(score) {
    if (score >= 90) return { emoji: '🏆', texto: 'Lenda da voz!' };
    if (score >= 75) return { emoji: '🥇', texto: 'Excelente!' };
    if (score >= 60) return { emoji: '🥈', texto: 'Muito bom!' };
    if (score >= 40) return { emoji: '🥉', texto: 'Dá pra melhorar' };
    return { emoji: '🎤', texto: 'Continue praticando' };
  }

  function nomeDoCantor() {
    try {
      return (window.localStorage.getItem(CHAVE_CANTOR) || '').trim() || 'Convidado';
    } catch (_) {
      return 'Convidado';
    }
  }

  // Envia a pontuação para o ranking (/api/scores) uma única vez por música.
  async function enviarPontuacao(score, m) {
    if (pontuacaoEnviada) return;
    pontuacaoEnviada = true;
    try {
      const resposta = await fetch('/api/scores', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          singer: nomeDoCantor(),
          score,
          medal: m.emoji,
          track_id: track?.id || null,
          track_title: track?.title || null,
        }),
      });
      if (!resposta.ok) return;
      const dados = await resposta.json();
      if (window.toast) {
        window.toast(`🏆 ${dados.singer}: ${dados.score} pontos — ${dados.rank}º lugar no ranking!`, 'ok');
      }
      document.dispatchEvent(new CustomEvent('karaoke:score', { detail: dados }));
    } catch (_) { /* o ranking é opcional; nunca quebra o karaokê */ }
  }

  /* ----------------------------------------------------------------- canvas */
  function prepararCanvas() {
    const canvas = $('#pitchChart');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const largura = canvas.clientWidth || 1100;
    const altura = canvas.clientHeight || 180;
    canvas.width = Math.round(largura * dpr);
    canvas.height = Math.round(altura * dpr);
    const ctxC = canvas.getContext('2d');
    ctxC.setTransform(dpr, 0, 0, dpr, 0, 0);
    canvasRef = document.createElement('canvas');
    canvasRef.width = canvas.width;
    canvasRef.height = canvas.height;
    const c = canvasRef.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.strokeStyle = 'rgba(255,255,255,.08)';
    c.lineWidth = 1;
    for (let i = 0; i <= 3; i += 1) {
      const y = (i * altura) / 3;
      c.beginPath(); c.moveTo(0, y); c.lineTo(largura, y); c.stroke();
    }
    c.fillStyle = 'rgba(255,255,255,.28)';
    c.font = '10px DM Sans, sans-serif';
    [MIDI_MAX, 70, MIDI_MIN].forEach((nota) => {
      c.fillText(String(nota), 4, ((MIDI_MAX - nota) / (MIDI_MAX - MIDI_MIN)) * altura + 10);
    });
    if (ref.length) {
      c.strokeStyle = '#8bedb0';
      c.lineWidth = 2;
      c.beginPath();
      ref.forEach((p, i) => {
        const x = (p.t / duracaoRef) * largura;
        const y = altura - ((Math.min(MIDI_MAX, Math.max(MIDI_MIN, p.midi)) - MIDI_MIN)
          / (MIDI_MAX - MIDI_MIN)) * altura;
        if (i) c.lineTo(x, y); else c.moveTo(x, y);
      });
      c.stroke();
    }
  }

  function desenhar(forcar = false) {
    const agora = performance.now();
    if (!forcar && agora - ultimoDesenho < INTERVALO_DESENHO) return;
    ultimoDesenho = agora;
    const canvas = $('#pitchChart');
    if (!canvas) return;
    const ctxC = canvas.getContext('2d');
    const largura = canvas.clientWidth || 1100;
    const altura = canvas.clientHeight || 180;
    ctxC.clearRect(0, 0, largura, altura);
    if (canvasRef) ctxC.drawImage(canvasRef, 0, 0, largura, altura);
    ctxC.strokeStyle = '#62a5ff';
    ctxC.lineWidth = 3;
    ctxC.beginPath();
    cantados.forEach((p, i) => {
      const x = (p.t / duracaoRef) * largura;
      const y = altura - ((Math.min(MIDI_MAX, Math.max(MIDI_MIN, p.midi)) - MIDI_MIN)
        / (MIDI_MAX - MIDI_MIN)) * altura;
      if (i) ctxC.lineTo(x, y); else ctxC.moveTo(x, y);
    });
    ctxC.stroke();
  }

  /* --------------------------------------------- card de letra não encontrada */
  function sugerirDivisao(titulo, artista) {
    const t = String(titulo || '');
    const m = t.match(/^(.*?)\s+[-–—]\s+(.+)$/);
    if (m && m[1].trim() && m[2].trim() && !String(artista || '').trim()) {
      return {
        titulo: m[2].replace(/\s*\(.*?\)\s*/g, '').replace(/\s*\[.*?\]\s*/g, '').trim(),
        artista: m[1].trim(),
      };
    }
    return {
      titulo: t.replace(/\s*\(.*?\)\s*/g, '').replace(/\s*\[.*?\]\s*/g, '').trim(),
      artista: String(artista || '').trim(),
    };
  }

  function montarCardUpload(nomeMusica) {
    const lyrics = $('#lyrics');
    if (!lyrics) return;
    const buscaGoogle = encodeURIComponent(`${nomeMusica} lrc`);
    const sugestao = sugerirDivisao(nomeMusica, track?.artist || '');
    lyrics.innerHTML = `
      <div class="upload-card">
        <div class="upload-icon" aria-hidden="true">❌</div>
        <h3>Letra não encontrada para "${esc(nomeMusica)}"</h3>
        <p>Procuramos em todas as fontes (LRCLIB, Vagalume, Lyrics.ovh), mas a letra não foi localizada automaticamente.</p>

        <div class="correcao-meta">
          <h4>Corrija o nome e busque de novo</h4>
          <p class="muted">O título muitas vezes vem do YouTube com o nome do canal junto
            ("Artista - Música (Vídeo Oficial)"). Ajuste abaixo e tente outra vez:</p>
          <label class="campo-meta">Título
            <input id="fixTitulo" type="text" value="${esc(sugestao.titulo)}" autocomplete="off">
          </label>
          <label class="campo-meta">Artista
            <input id="fixArtista" type="text" value="${esc(sugestao.artista)}" autocomplete="off">
          </label>
          <button class="primary" id="corrigirBuscar" style="margin-top:10px;">Corrigir e buscar de novo</button>
        </div>

        <label class="file-label">
          <input type="file" id="lrcInput" accept=".lrc" hidden>
          <span>📁 Enviar arquivo .lrc</span>
        </label>

        <p class="upload-dica" style="margin-top: 18px;">
          💡 <strong>Dica:</strong> Procure no Google por:
          <a href="https://www.google.com/search?q=${buscaGoogle}" target="_blank" rel="noopener" style="color: #c184fa; text-decoration: underline;">
            "${esc(nomeMusica)} lrc"
          </a>
        </p>

        <button class="secondary" id="tentarNovamente" style="margin-top:14px;">Tentar buscar novamente</button>
      </div>`;

    const input = $('#lrcInput');
    if (input) {
      input.addEventListener('change', () => {
        const arquivo = input.files?.[0];
        if (arquivo) enviarLrc(arquivo);
      });
    }

    const btn = $('#tentarNovamente');
    if (btn) {
      btn.addEventListener('click', async () => {
        if (!track) return;
        lyrics.innerHTML = '<div class="empty-state"><p>Reconsultando fontes de letras…</p></div>';
        start(track, true);
      });
    }

    const btnCorrigir = $('#corrigirBuscar');
    if (btnCorrigir) {
      btnCorrigir.addEventListener('click', async () => {
        if (!track) return;
        const novoTitulo = ($('#fixTitulo')?.value || '').trim();
        const novoArtista = ($('#fixArtista')?.value || '').trim();
        if (!novoTitulo) { aviso('Informe ao menos o título da música.'); return; }
        btnCorrigir.disabled = true;
        btnCorrigir.textContent = 'Buscando…';
        try {
          const resp = await fetch(`/api/tracks/${encodeURIComponent(track.id)}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title: novoTitulo, artist: novoArtista || null}),
          });
          if (!resp.ok) {
            let detalhe = 'Não consegui corrigir os dados.';
            try { detalhe = (await resp.json()).detail || detalhe; } catch (_) {}
            throw new Error(detalhe);
          }
          const atualizada = await resp.json();
          track.title = atualizada.title || novoTitulo;
          track.artist = atualizada.artist || novoArtista;
          infoToast('Nome corrigido! Buscando a letra de novo…');
          lyrics.innerHTML = '<div class="empty-state"><p>Reconsultando fontes de letras…</p></div>';
          start(track, true);
        } catch (e) {
          aviso(e.message || 'Não consegui corrigir os dados.');
          btnCorrigir.disabled = false;
          btnCorrigir.textContent = 'Corrigir e buscar de novo';
        }
      });
    }
  }

  async function enviarLrc(arquivo) {
    if (!track) return;
    const form = new FormData();
    form.append('arquivo', arquivo);
    try {
      const resp = await fetch(`/api/lyrics/${encodeURIComponent(track.id)}/upload`, {
        method: 'POST',
        body: form,
      });
      if (!resp.ok) {
        let errStr = 'Erro ao enviar arquivo.';
        try { errStr = (await resp.json()).detail || errStr; } catch (_) {}
        throw new Error(errStr);
      }
      infoToast('Usando arquivo local enviado!');
      await new Promise((r) => setTimeout(r, 600));
      start(track, true);
    } catch (e) {
      aviso(e.message || 'Não consegui enviar o arquivo .lrc.');
    }
  }

  /* ------------------------------------------------------- modo sem sync */
  function montarLetraManual(versos) {
    const lyrics = $('#lyrics');
    if (!lyrics) return;
    lyrics.innerHTML = versos.map((v, i) =>
      `<p data-i="${i}" class="${i === indiceManual ? 'active' : ''}" style="cursor:pointer;">${esc(v.texto || v)}</p>`
    ).join('');

    // Permite clicar diretamente em qualquer verso para pular para ele
    lyrics.querySelectorAll('p[data-i]').forEach((el) => {
      el.addEventListener('click', () => {
        const idx = Number(el.dataset.i);
        if (!Number.isNaN(idx)) {
          indiceManual = idx;
          montarLetraManual(linhas);
        }
      });
    });

    if (indiceManual >= 0) {
      const el = lyrics.querySelector(`p[data-i="${indiceManual}"]`);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  function avancarManual(delta) {
    if (!modoManual) return;
    const max = linhas.length - 1;
    indiceManual = Math.min(max, Math.max(0, indiceManual + delta));
    montarLetraManual(linhas);
  }

  function setupModoManual(versos, duracao) {
    modoManual = true;
    modoUniforme = false;
    indiceManual = 0;
    linhas = versos;
    montarLetraManual(linhas);

    const barra = document.createElement('div');
    barra.className = 'manual-controls';
    barra.innerHTML = `
      <button id="mvPrev" aria-label="Verso anterior">◀ Anterior</button>
      <span id="manualInfo">${indiceManual + 1} / ${versos.length}</span>
      <button id="mvNext" aria-label="Próximo verso">Próximo ▶ (ou Setas / Enter)</button>`;
    $('#lyrics').appendChild(barra);

    $('#mvPrev').addEventListener('click', () => avancarManual(-1));
    $('#mvNext').addEventListener('click', () => avancarManual(1));
  }

  function setupModoUniforme(versos, duracao) {
    modoManual = false;
    modoUniforme = true;
    duracaoTotal = Math.max(1, duracao);
    indiceManual = 0;
    linhas = versos.map((v) => ({ t: 0, texto: typeof v === 'string' ? v : v.texto }));
    montarLetraManual(linhas);
  }

  function montarEscolhaSemSync(versos, duracao) {
    modoManual = false;
    modoUniforme = false;
    indiceManual = -1;
    linhas = [];

    const lyrics = $('#lyrics');
    if (!lyrics) return;
    lyrics.innerHTML = `
      <div class="upload-card">
        <div class="upload-icon" aria-hidden="true">🎼</div>
        <h3>Letra encontrada (Sem sincronia)</h3>
        <p>A letra desta música foi encontrada, mas não contém horários (timestamps).</p>
        <p>Como você deseja acompanhar os versos?</p>
        <div class="modo-botoes" style="margin-top:20px;">
          <button id="modoUniformeBtn" class="primary">Distribuir automaticamente</button>
          <button id="modoManualBtn" class="secondary">Avanço manual (Setas/Clique/Enter)</button>
        </div>
      </div>`;

    const btnAuto = $('#modoUniformeBtn');
    const btnManual = $('#modoManualBtn');

    const iniciar = (fn) => {
      lyrics.innerHTML = '';
      fn();
      $('#audio').play().catch(() => {});
      conectarMicrofone();
    };

    if (btnAuto) btnAuto.addEventListener('click', () => iniciar(() => setupModoUniforme(versos, duracao)));
    if (btnManual) btnManual.addEventListener('click', () => iniciar(() => setupModoManual(versos, duracao)));
  }

  /* ------------------------------------------------- conexao mic e ciclo */
  async function conectarMicrofone() {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      aviso('O microfone só funciona em localhost ou HTTPS.');
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
    } catch (_) {
      $('#audio').pause();
      $('#karaokeOverlay').style.display = 'none';
      aviso('Permita o acesso ao microfone para usar o karaokê.');
      return;
    }
    ctx = ctx || new AudioContext({ latencyHint: 'interactive' });
    if (ctx.state === 'suspended') await ctx.resume();
    gravacaoDueto = Boolean($('#duetMode')?.checked);
    fonte = ctx.createMediaStreamSource(stream);
    analisador = ctx.createAnalyser();
    analisador.fftSize = 2048;
    fonte.connect(analisador);
    // O AnalyserNode precisa de um caminho até a saída para ser processado
    // pelo navegador. Usamos ganho zero para não gerar retorno audível.
    silencioAnalise = ctx.createGain();
    silencioAnalise.gain.value = 0;
    analisador.connect(silencioAnalise).connect(ctx.destination);
    buffer = new Float32Array(analisador.fftSize);

    // Cria uma saída mixada: instrumental + voz, com compressor e normalização.
    // O MediaElementSource é criado uma única vez por elemento <audio>, pois
    // recriá-lo no mesmo contexto lança InvalidStateError.
    const audio = $('#audio');
    if (!fonteAudioCriada) {
      fonteAudio = ctx.createMediaElementSource(audio);
      fonteAudioCriada = true;
      // Caminho audível persistente do instrumental (funciona também fora do karaokê).
      speakerGain = ctx.createGain();
      speakerGain.gain.value = 1;
      speakerGain.connect(ctx.destination);
      fonteAudio.connect(speakerGain);
    }
    mixMaster = ctx.createGain();
    mixMaster.gain.value = 0.82;
    mixDestino = ctx.createMediaStreamDestination();
    fonteInstrumental = fonteAudio;
    ganhoInstrumental = ctx.createGain();
    ganhoInstrumental.gain.value = 0.78;
    panInstrumental = ctx.createStereoPanner();
    panInstrumental.pan.value = gravacaoDueto ? -0.15 : 0;
    fonteInstrumental.connect(ganhoInstrumental).connect(panInstrumental);
    const compressorMix = ctx.createDynamicsCompressor();
    compressorMix.threshold.value = -12;
    compressorMix.knee.value = 12;
    compressorMix.ratio.value = 3;
    compressorMix.attack.value = 0.005;
    compressorMix.release.value = 0.15;
    // O instrumental audível já sai pelo speakerGain (ligado uma única vez);
    // aqui ele entra apenas no mix de gravação, junto com a voz.
    panInstrumental.connect(mixMaster);
    mixMaster.connect(compressorMix).connect(mixDestino);
    nosSessao = [mixMaster, compressorMix, mixDestino, ganhoInstrumental, panInstrumental, silencioAnalise];

    configurarMonitoramento();
    await conectarSegundoMicrofone();
    iniciarGravacao();
    ativo = true;
    ciclo();
  }

  async function conectarSegundoMicrofone() {
    if (!gravacaoDueto) return;
    const deviceId = $('#duetMic')?.value;
    if (!deviceId) return;
    try {
      streamDueto = await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: { exact: deviceId }, echoCancellation: false, noiseSuppression: false },
      });
      const fonteDueto = ctx.createMediaStreamSource(streamDueto);
      const ganhoDueto = ctx.createGain();
      const panDueto = ctx.createStereoPanner();
      ganhoDueto.gain.value = 1;
      panDueto.pan.value = 0.15;
      fonteDueto.connect(ganhoDueto).connect(panDueto).connect(mixMaster);
      nosSessao.push(fonteDueto, ganhoDueto, panDueto);
    } catch (_) {
      aviso('Não foi possível conectar o segundo microfone. O dueto seguirá com um microfone.');
    }
  }

  function iniciarGravacao() {
    if (!$('#recordVoice')?.checked || !stream || !window.MediaRecorder) return;
    const tipos = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
    const tipo = tipos.find((candidato) => MediaRecorder.isTypeSupported(candidato));
    if (!tipo) {
      aviso('Este navegador não oferece um formato compatível para gravação.');
      return;
    }
    try {
      if (!mixDestino) throw new Error('Saída de mixagem indisponível.');
      gravador = new MediaRecorder(mixDestino.stream, { mimeType: tipo, audioBitsPerSecond: 192000 });
      blocosGravacao = [];
      gravador.addEventListener('dataavailable', (evento) => {
        if (evento.data.size) blocosGravacao.push(evento.data);
      });
      gravador.addEventListener('stop', () => {
        if (!blocosGravacao.length) return;
        arquivoGravacao = new Blob(blocosGravacao, { type: tipo });
        const botao = $('#downloadRecording');
        if (botao) { botao.hidden = false; botao.disabled = false; }
        const botaoRender = $('#renderRecording');
        if (botaoRender) { botaoRender.hidden = false; botaoRender.disabled = false; }
        const botaoShare = $('#shareRecording');
        if (botaoShare) botaoShare.hidden = false;
        infoToast(gravacaoDueto ? 'Gravação do dueto pronta para baixar.' : 'Gravação pronta para baixar.');
      });
      gravador.start(250);
    } catch (_) {
      aviso('Não foi possível iniciar a gravação neste navegador.');
    }
  }

  function pararGravacao() {
    if (gravador && gravador.state !== 'inactive') gravador.stop();
    gravador = null;
  }

  function baixarGravacao() {
    if (!arquivoGravacao) return;
    const url = URL.createObjectURL(arquivoGravacao);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${(track?.title || 'karaoke').replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.webm`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function exportarNormalizado() {
    if (!arquivoGravacao) return;
    const botao = $('#renderRecording');
    if (botao) { botao.disabled = true; botao.textContent = 'Convertendo...'; }
    try {
      const form = new FormData();
      form.append('arquivo', arquivoGravacao, 'gravacao.webm');
      const resposta = await fetch('/api/render', { method: 'POST', body: form });
      if (!resposta.ok) throw new Error('Não consegui converter o áudio para M4A.');
      const blob = await resposta.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${(track?.title || 'karaoke').replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.m4a`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      infoToast('Áudio normalizado exportado em M4A.');
    } catch (erro) {
      aviso(erro.message || 'Falha ao exportar o M4A.');
    } finally {
      if (botao) { botao.disabled = false; botao.textContent = 'M4A normalizado'; }
    }
  }

  async function compartilharGravacao() {
    if (!arquivoGravacao) return;
    if (!navigator.share) {
      aviso('Compartilhamento não suportado neste dispositivo.');
      return;
    }
    const nome = `${(track?.title || 'karaoke').replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.webm`;
    const arquivo = new File([arquivoGravacao], nome, { type: arquivoGravacao.type || 'audio/webm' });
    try {
      if (navigator.canShare?.({ files: [arquivo] })) {
        await navigator.share({ files: [arquivo], title: track?.title || 'Karaokê' });
      } else {
        await navigator.share({ title: track?.title || 'Karaokê', text: 'Ouça meu karaokê!' });
      }
    } catch (_) { /* usuário cancelou */ }
  }

  // Aplica a capa da faixa como fundo defocado; aceita apenas URLs http(s).
  function aplicarFundoKaraoke(trackAtual) {
    const fundo = $('#karaokeBackdrop');
    if (!fundo) return;
    const url = trackAtual?.thumbnail;
    if (typeof url === 'string' && /^https?:\/\//i.test(url)) {
      // JSON.stringify escapa aspas/barra de forma segura para o token CSS url().
      fundo.style.backgroundImage = `url(${JSON.stringify(url)})`;
      fundo.classList.add('has-image');
    } else {
      fundo.style.backgroundImage = '';
      fundo.classList.remove('has-image');
    }
  }

  function limparFundoKaraoke() {
    const fundo = $('#karaokeBackdrop');
    if (!fundo) return;
    fundo.style.backgroundImage = '';
    fundo.classList.remove('has-image');
  }

  function criarImpulsoReverb(duracao = 1.1, decaimento = 2.4) {
    const tamanho = Math.floor(ctx.sampleRate * duracao);
    const impulso = ctx.createBuffer(2, tamanho, ctx.sampleRate);
    for (let canal = 0; canal < 2; canal += 1) {
      const dados = impulso.getChannelData(canal);
      for (let i = 0; i < tamanho; i += 1) {
        dados[i] = (Math.random() * 2 - 1) * ((1 - i / tamanho) ** decaimento);
      }
    }
    return impulso;
  }

  function configurarMonitoramento() {
    audioMic = ctx.createMediaStreamSource(stream);
    ganhoGravacaoVoz = ctx.createGain();
    ganhoGravacaoVoz.gain.value = 1;
    panVoz = ctx.createStereoPanner();
    panVoz.pan.value = gravacaoDueto ? 0.15 : 0;
    audioMic.connect(ganhoGravacaoVoz).connect(panVoz).connect(mixMaster);
    ganhoMonitor = ctx.createGain();
    ganhoMonitor.gain.value = Number($('#monitorVolume')?.value) || 0.18;
    const compressor = ctx.createDynamicsCompressor();
    compressor.threshold.value = -24;
    compressor.knee.value = 18;
    compressor.ratio.value = 4;
    compressor.attack.value = 0.003;
    compressor.release.value = 0.18;
    audioMic.connect(compressor);

    // Modo palco: high-pass + notch + limiter no caminho para a caixa,
    // para cortar graves que alimentam o loop e atenuar a frequência do apito.
    let saidaMic = compressor;
    notchFilter = null;
    if ($('#stageMode')?.checked) {
      const highpass = ctx.createBiquadFilter();
      highpass.type = 'highpass';
      highpass.frequency.value = 100;
      highpass.Q.value = 0.7;
      notchFilter = ctx.createBiquadFilter();
      notchFilter.type = 'notch';
      notchFilter.frequency.value = Number($('#feedbackFreq')?.dataset.hz) || 1200;
      notchFilter.Q.value = 12;
      const limiter = ctx.createDynamicsCompressor();
      limiter.threshold.value = -6;
      limiter.knee.value = 0;
      limiter.ratio.value = 20;
      limiter.attack.value = 0.002;
      limiter.release.value = 0.1;
      compressor.connect(highpass).connect(notchFilter).connect(limiter);
      saidaMic = limiter;
      dspNodes.push(highpass, notchFilter, limiter);
    }

    const dsp = $('#dspEnabled')?.checked;
    if (dsp) {
      const delay = ctx.createDelay(0.6);
      delay.delayTime.value = 0.22;
      const retorno = ctx.createGain();
      retorno.gain.value = Number($('#delayAmount')?.value) || 0.12;
      const reverb = ctx.createConvolver();
      reverb.buffer = criarImpulsoReverb();
      const reverberacao = ctx.createGain();
      reverberacao.gain.value = Number($('#reverbAmount')?.value) || 0.18;
      saidaMic.connect(delay).connect(retorno).connect(ganhoMonitor);
      saidaMic.connect(reverb).connect(reverberacao).connect(ganhoMonitor);
      dspNodes.push(compressor, delay, retorno, reverb, reverberacao);
    } else {
      saidaMic.connect(ganhoMonitor);
      dspNodes.push(compressor);
    }
    atualizarMonitoramento();
    nosSessao.push(audioMic, ganhoGravacaoVoz, panVoz, ganhoMonitor, compressor, ...dspNodes);
  }

  // Liga/desliga a voz no alto-falante em tempo real. Por padrão fica desligado.
  function atualizarMonitoramento() {
    if (!ganhoMonitor || !ctx) return;
    try { ganhoMonitor.disconnect(ctx.destination); } catch (_) { /* ainda sem conexão */ }
    if ($('#monitorVoice')?.checked) ganhoMonitor.connect(ctx.destination);
  }

  // Detecta um possível apito pelo pico espectral e aplica o notch.
  async function detectarApito() {
    if (!analisador || !ctx) { aviso('Microfone indisponível.'); return; }
    if (!$('#stageMode')?.checked) { aviso('Ative o "Modo palco" para usar a detecção.'); return; }
    const dados = new Float32Array(analisador.frequencyBinCount);
    const media = new Float32Array(dados.length).fill(-Infinity);
    const amostras = 6;
    for (let a = 0; a < amostras; a += 1) {
      analisador.getFloatFrequencyData(dados);
      for (let i = 0; i < dados.length; i += 1) {
        media[i] = Math.max(media[i], dados[i]);
      }
      await new Promise((r) => setTimeout(r, 60));
    }
    const nyquist = ctx.sampleRate / 2;
    const hzPorBin = nyquist / media.length;
    const minBin = Math.floor(300 / hzPorBin);
    const maxBin = Math.min(media.length - 1, Math.floor(8000 / hzPorBin));
    let melhorBin = -1;
    let melhorValor = -Infinity;
    for (let i = minBin; i <= maxBin; i += 1) {
      if (media[i] > melhorValor) { melhorValor = media[i]; melhorBin = i; }
    }
    if (melhorBin < 0 || melhorValor < -55) {
      infoToast('Nenhum apito detectado no momento.');
      return;
    }
    const frequencia = melhorBin * hzPorBin;
    aplicarNotch(frequencia);
    infoToast(`Notch aplicado em ${Math.round(frequencia)} Hz.`);
  }

  function aplicarNotch(frequencia) {
    const hz = Math.round(frequencia);
    if (notchFilter && ctx) notchFilter.frequency.setTargetAtTime(hz, ctx.currentTime, 0.05);
    const saida = $('#feedbackFreq');
    if (saida) {
      saida.dataset.hz = String(hz);
      saida.value = `${hz} Hz`;
      saida.textContent = `${hz} Hz`;
    }
  }

  function aoTerminar() {
    if (encerrado) return;
    encerrado = true;
    const score = calcularScore();
    const m = medalha(score);
    $('#badge').innerHTML = `${m.emoji} <span>${score}</span> — ${esc(m.texto)}`;
    $('#feedback').textContent = 'Fim da música!';
    $('#feedback').style.color = '#ffd66b';
    desenhar(true);
    if (window.toast) window.toast(`Pontuação final: ${score} — ${m.texto}`, 'ok');
    enviarPontuacao(score, m);
    pararGravacao();
    limpar();
    // Deixa a pontuação na tela por um tempo antes de fechar o overlay.
    setTimeout(() => { $('#karaokeOverlay').style.display = 'none'; }, 4000);
  }

  // Reforço do fim da música: encerra a sessão assim que a faixa termina, sem
  // depender do laço de animação (ele fica parado enquanto o áudio está pausado).
  const audioFimSessao = $('#audio');
  if (audioFimSessao) {
    audioFimSessao.addEventListener('ended', () => { if (ativo) aoTerminar(); });
  }

  function calcularRms() {
    let energia = 0;
    for (let i = 0; i < buffer.length; i += 1) energia += buffer[i] * buffer[i];
    return Math.sqrt(energia / buffer.length);
  }

  function resetarSegmentoPorSeek(t) {
    if (ultimoTempoAudio !== null && Math.abs(t - ultimoTempoAudio) > SALTO_SEEK) {
      cantados = [];
      erros = [];
      saltos = 0;
      pitchSuavizado = null;
      linhaAtual = -1;
      desenhar(true);
    }
    ultimoTempoAudio = t;
  }

  function ciclo() {
    if (!ativo) return;
    // O áudio que terminou fica "ended" E "paused". O fim da música precisa ser
    // checado ANTES do pause; senão o laço fica girando e a sessão nunca
    // encerra (não mostra "Fim da música!" nem a pontuação).
    if ($('#audio').ended) { aoTerminar(); return; }
    if ($('#audio').paused) {
      raf = requestAnimationFrame(ciclo);
      return;
    }
    const t = $('#audio').currentTime;
    resetarSegmentoPorSeek(t);

    // Letra com sync
    if (!modoManual && !modoUniforme) {
       const nova = indiceDaLinha(Math.max(0, t + ajusteLatencia));
      if (nova !== linhaAtual) {
        linhaAtual = nova;
        $$('.lyrics p').forEach((p, i) => p.classList.toggle('active', i === nova));
        if (nova >= 0) {
          const el = $$('.lyrics p')[nova];
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
         }
       }
       // Atualiza a iluminação das palavras dentro da linha ativa
       atualizarPalavras(t + ajusteLatencia);
       atualizarFimLetra(t);
     }

     // Modo sem sync distribuído uniformemente
    if (modoUniforme && linhas.length) {
      const esperado = Math.floor(t / (duracaoTotal / linhas.length));
      if (esperado !== indiceManual && esperado >= 0 && esperado < linhas.length) {
        indiceManual = esperado;
        montarLetraManual(linhas);
      }
    }

    // Pitch
    if (analisador && window.Pitchy && ref.length) {
      analisador.getFloatTimeDomainData(buffer);
      const rms = calcularRms();
      const [hz, claridade] = Pitchy.findPitch(buffer, ctx.sampleRate);
      if (rms >= RMS_MINIMO_VOZ && claridade >= CLARIDADE_MIN && hz >= HZ_MIN && hz <= HZ_MAX) {
        const alvo = referenciaMaisProxima(t);
        if (alvo) {
          const midiBruto = 69 + 12 * Math.log2(hz / 440);
          // Suavização exponencial reduz tremores do microfone sem atrasar demais
          // a resposta musical. Saltos muito grandes reiniciam a nota suavizada.
          if (pitchSuavizado === null || Math.abs(midiBruto - pitchSuavizado) > 7) {
            pitchSuavizado = midiBruto;
          } else {
            pitchSuavizado += (midiBruto - pitchSuavizado) * SUAVIZACAO_PITCH;
          }
          const midi = pitchSuavizado;
          const erro = midi - alvo.midi;
          cantados.push({ t, midi });
          erros.push(Math.abs(erro));
          const anterior = cantados[cantados.length - 2];
          if (anterior && Math.abs(midi - anterior.midi) > 2) saltos += 1;
           if (cantados.length > MAX_HISTORICO) {
             cantados.shift();
             erros.shift();
           }
           const feedback = $('#feedback');
          if (Math.abs(erro) <= TOM_CERTO) {
            feedback.textContent = `PERFEITO! ✅ ${nomeNota(midi)}`;
            feedback.style.color = '#8bedb0';
          } else if (erro > 0) {
            feedback.textContent = `MUITO AGUDO ⬆ ${nomeNota(midi)}`;
            feedback.style.color = '#fb7185';
          } else {
            feedback.textContent = `MUITO GRAVE ⬇ ${nomeNota(midi)}`;
            feedback.style.color = '#60a5fa';
          }
          $('#score').textContent = calcularScore();
          desenhar();
        }
      }
    }
    if ($('#audio').ended) { aoTerminar(); return; }
    raf = requestAnimationFrame(ciclo);
  }

  function limpar() {
    ativo = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    fimLetraT = null;
    const elFim = $('#lyricsFim');
    if (elFim) elFim.hidden = true;
    document.dispatchEvent(new CustomEvent('karaoke:stop'));
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (streamDueto) streamDueto.getTracks().forEach((t) => t.stop());
    stream = null; streamDueto = null; fonte = null; analisador = null; buffer = null;
    audioMic = null; ganhoMonitor = null; dspNodes = []; notchFilter = null;
    mixMaster = null; mixDestino = null;
    fonteInstrumental = null; ganhoInstrumental = null; panInstrumental = null;
    ganhoGravacaoVoz = null; panVoz = null;
    // Desconecta apenas os nós da sessão; o AudioContext e o MediaElementSource
    // permanecem vivos para serem reutilizados na próxima música.
    nosSessao.forEach((no) => { try { no.disconnect(); } catch (_) { /* já desconectado */ } });
    nosSessao = [];
    limparFundoKaraoke();
    alternarPainelAjustes(false);
  }

  /* --------------------------------------------------- carregamento offline */
  async function carregarKaraoke(trackId) {
    const url = `/api/karaoke/${encodeURIComponent(trackId)}`;
    const chave = window.VocalisOffline?.chaveKaraoke
      ? window.VocalisOffline.chaveKaraoke(trackId) : `karaoke-${trackId}`;
    try {
      const resposta = await fetch(url);
      if (!resposta.ok) throw new Error('karaokê não encontrado');
      const dados = await resposta.json();
      // O endpoint agora responde 200 com {ready:false} quando o karaokê ainda
      // não foi preparado. Tratamos como "não pronto" para seguir ao /prepare
      // (e não cacheamos um resultado incompleto).
      if (dados && dados.ready === false) {
        throw new Error(dados.message || 'O karaokê ainda não foi preparado.');
      }
      if (window.VocalisOffline) window.VocalisOffline.salvar(chave, dados).catch(() => {});
      return dados;
    } catch (erro) {
      if (window.VocalisOffline) {
        const salvo = await window.VocalisOffline.ler(chave);
        if (salvo) return salvo;
      }
      throw erro;
    }
  }

  /* ------------------------------------------------------------------ início */
  async function preparar(trackAtual, forcar = false) {
    if (!forcar) {
      try {
        return await carregarKaraoke(trackAtual.id);
      } catch (_) { /* não preparado ainda: segue para o prepare */ }
    }

    $('#karaokeOverlay').style.display = 'block';
    $('#feedback').textContent = 'Preparando áudio e afinação...';
    $('#lyrics').innerHTML = '<div class="empty-state"><p>Preparando…</p></div>';

    const respostaPrepara = await fetch(`/api/karaoke/${encodeURIComponent(trackAtual.id)}/prepare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist: trackAtual.artist || '', title: trackAtual.title || '' }),
    });
    if (!respostaPrepara.ok) {
      let detalhe = 'Não foi possível preparar o karaokê.';
      try { detalhe = (await respostaPrepara.json()).detail || detalhe; } catch (_) {}
      throw new Error(detalhe);
    }

    for (let i = 0; i < 60; i += 1) {
      await new Promise((r) => setTimeout(r, 2000));
      const s = await fetch(`/api/karaoke/${encodeURIComponent(trackAtual.id)}/status`)
        .then((r) => r.json()).catch(() => ({}));
      if (s.ready) {
        return carregarKaraoke(trackAtual.id);
      }
      if (s.status === 'error') throw new Error(s.message || 'Falha ao preparar o karaokê.');
      // Sem letra: para o polling e deixa o start() montar o card de upload
      // (antes o app travava ~2 min até estourar o timeout).
      if (s.status === 'no_lyrics') {
        return {
          _semLetra: true,
          message: s.message || 'Letra não encontrada. Envie um arquivo .lrc.',
          instrumental_url: null,
          pitch_ref: [],
        };
      }
      // Feedback honesto com o estágio atual (letra/afinação). Se a separação
      // ainda roda, o "cantar já" usa o original e o instrumental entra sozinho.
      let msg = (s.message || 'Preparando karaokê…').replace(/^Preparando /, '');
      try {
        const sep = await fetch(`/api/separation/${encodeURIComponent(trackAtual.id)}`)
          .then((r) => r.json()).catch(() => null);
        if (sep && !sep.ready) msg += ' · a voz segue separando em segundo plano';
      } catch (_) { /* ignora */ }
      $('#feedback').textContent = `✨ ${msg}`;
    }
    throw new Error('Tempo esgotado ao preparar áudio do karaokê.');
  }

  /* ------------------------------------------------------------- cantar já */
  // Enquanto a separação ainda roda, o karaokê toca o áudio original; quando
  // o instrumental fica pronto, trocamos por trás sem perder a posição.
  let timerStems = null;

  function atualizarCantarJa(stemsProntos) {
    const hint = $('#stemsHint');
    if (hint) hint.hidden = !!stemsProntos;
    if (stemsProntos) pararTimerStems();
    else iniciarTimerStems();
  }

  function iniciarTimerStems() {
    if (timerStems) return;
    timerStems = window.setInterval(async () => {
      if (!track) return;
      let status = null;
      try {
        status = await fetch(`/api/separation/${encodeURIComponent(track.id)}`)
          .then((r) => r.json()).catch(() => null);
      } catch (_) { status = null; }
      if (!status || !status.ready) return;
      pararTimerStems();
      const audioEl = $('#audio');
      const posicao = audioEl ? audioEl.currentTime : 0;
      if (audioEl) {
        audioEl.src = `/data/separated/${encodeURIComponent(track.id)}/htdemucs/no_vocals.wav`;
        audioEl.currentTime = posicao;
        audioEl.play().catch(() => {});
      }
      const hint = $('#stemsHint');
      if (hint) hint.hidden = true;
      if (window.infoToast) infoToast('O instrumental ficou pronto — cantando sem a voz agora! 🎤');
      else if (window.toast) window.toast('O instrumental ficou pronto — cantando sem a voz. 🎤', 'ok');
    }, 5000);
  }

  function pararTimerStems() {
    if (timerStems) {
      window.clearInterval(timerStems);
      timerStems = null;
    }
  }

  async function start(trackAtual, forcar = false) {
    if (ativo) return;
    track = trackAtual;
    encerrado = false;
    pontuacaoEnviada = false;
    $('#badge').innerHTML = '';
    modoManual = false;
    modoUniforme = false;
    indiceManual = -1;

    let dados;
    try {
      dados = await preparar(trackAtual, forcar);
    } catch (e) {
      $('#karaokeOverlay').style.display = 'none';
      aviso(e.message);
      return;
    }

    prepararReferencia(dados);
    cantados = []; erros = []; saltos = 0; linhaAtual = -1;
    pitchSuavizado = null;
    ultimoTempoAudio = null;
    arquivoGravacao = null;
    blocosGravacao = [];
    gravacaoDueto = false;
    const botaoDownload = $('#downloadRecording');
    if (botaoDownload) botaoDownload.hidden = true;
    const botaoRender = $('#renderRecording');
    if (botaoRender) botaoRender.hidden = true;
    const botaoShare = $('#shareRecording');
    if (botaoShare) botaoShare.hidden = true;
    // Sempre começa sem retorno de voz: só sai no alto-falante se o usuário ligar.
    const monitorVoice = $('#monitorVoice');
    if (monitorVoice) monitorVoice.checked = false;
    ultimoDesenho = 0;

    $('#songTitle').textContent = trackAtual.title;
    aplicarFundoKaraoke(trackAtual);
    $('#score').textContent = '0';
    $('#feedback').textContent = 'Prepare sua voz';
    $('#feedback').style.color = '#8bedb0';

    const audio = $('#audio');
    // Sem letra preparada: toca o instrumental (se existir) e mostra o card
    // de upload — o usuário pode mandar .lrc ou tentar de novo.
    if (dados._semLetra) {
      audio.src = `/data/separated/${encodeURIComponent(trackAtual.id)}/htdemucs/no_vocals.wav`;
      $('#karaokeOverlay').style.display = 'block';
      $('#feedback').textContent = dados.message || 'Letra não encontrada.';
      montarCardUpload(trackAtual.title);
      try { await audio.play(); } catch (_) { /* requer interacao do usuario */ }
      return;
    }

    audio.src = dados.instrumental_url || `/data/downloads/${encodeURIComponent(trackAtual.id)}.mp3`;
    atualizarCantarJa(dados.stems_prontos !== false);

    $('#karaokeOverlay').style.display = 'block';
    prepararCanvas();

    // Consulta o endpoint unificado de letras /api/lyrics/{track_id}
    let lyricsData = null;
    try {
      const respLyrics = await fetch(`/api/lyrics/${encodeURIComponent(trackAtual.id)}`);
      if (respLyrics.ok) {
        lyricsData = await respLyrics.json();
      }
    } catch (e) {
      console.warn('Falha ao consultar /api/lyrics:', e);
    }

    const duracao = Number(trackAtual.duration) || 180;

    // Se o backend não encontrou nenhuma letra
    if (!lyricsData || !lyricsData.found) {
      montarCardUpload(trackAtual.title);
      try { await audio.play(); } catch (_) {}
      return;
    }

    // Exibe toast com a fonte da letra
    const fontesNomes = {
      local: 'Usando arquivo local',
      syncedlyrics: 'Letra encontrada via syncedlyrics',
      lrclib: 'Letra encontrada via LRCLIB',
      vagalume: 'Letra encontrada via Vagalume',
      'lyrics.ovh': 'Letra encontrada via Lyrics.ovh',
      'whisper (auto)': 'Transcrição gerada via Whisper (auto)',
    };
    infoToast(fontesNomes[lyricsData.source] || `Letra via ${lyricsData.source || 'internet'}`);

    if (lyricsData.synced && lyricsData.lines && lyricsData.lines.length) {
      // Letra Sincronizada
      linhas = lyricsData.lines.map((l) => ({ t: l.time, texto: l.text, words: l.words }));
      fimLetraT = null;
      $('#lyrics').innerHTML = linhas
        .map((l, i) => `<p data-i="${i}">${l.words?.length
          ? l.words.map((palavra) => `<span class="lyric-word">${esc(palavra.text)}</span>`).join(' ')
          : esc(l.texto) || '&nbsp;'}</p>`).join('');
    } else if (lyricsData.texto) {
      // Letra Sem Sincronia
      const versos = parseTextoSimples(lyricsData.texto);
      montarEscolhaSemSync(versos, duracao);
    } else {
      montarCardUpload(trackAtual.title);
      try { await audio.play(); } catch (_) {}
      return;
    }

    try { await audio.play(); } catch (_) { /* requer interacao do usuario */ }

    // Se a letra for com sync, inicia a escuta do mic direto; se for sem sync, aguarda a escolha do usuário
    if (lyricsData.synced) {
      await conectarMicrofone();
    }
  }

  function stop() {
    if (!ativo && $('#karaokeOverlay').style.display === 'none') return;
    if (erros.length) {
      const score = calcularScore();
      const m = medalha(score);
      $('#badge').innerHTML = `${m.emoji} <span>${score}</span> — ${esc(m.texto)}`;
      desenhar(true);
      enviarPontuacao(score, m);
    } else {
      $('#badge').innerHTML = '<span class="muted">Sem amostras válidas para pontuar.</span>';
    }
    $('#audio').pause();
    pararGravacao();
    pararTimerStems();
    const hintStems = $('#stemsHint');
    if (hintStems) hintStems.hidden = true;
    limpar();
    setTimeout(() => { $('#karaokeOverlay').style.display = 'none'; }, 1200);
  }

  const controlePalco = $('#stageMode');
  if (controlePalco) controlePalco.addEventListener('change', () => {
    const botao = $('#detectFeedback');
    const saida = $('#feedbackFreq');
    if (botao) botao.hidden = !controlePalco.checked;
    if (saida && !controlePalco.checked) { saida.hidden = true; delete saida.dataset.hz; saida.textContent = '—'; }
  });

  const botaoApito = $('#detectFeedback');
  if (botaoApito) botaoApito.addEventListener('click', async () => {
    botaoApito.disabled = true;
    botaoApito.textContent = 'Ouvindo...';
    try {
      await detectarApito();
      const saida = $('#feedbackFreq');
      if (saida && saida.dataset.hz) saida.hidden = false;
    } finally {
      botaoApito.disabled = false;
      botaoApito.textContent = 'Detectar apito';
    }
  });

  const controleDueto = $('#duetMode');
  if (controleDueto) controleDueto.addEventListener('change', () => {
    const campo = $('#duetName');
    const seletor = $('#duetMic');
    if (campo) {
      campo.hidden = !controleDueto.checked;
      if (controleDueto.checked) campo.focus();
    }
    if (seletor) {
      seletor.hidden = !controleDueto.checked;
      if (controleDueto.checked) listarMicrofones(seletor);
    }
  });

  const botaoDownload = $('#downloadRecording');
  if (botaoDownload) botaoDownload.addEventListener('click', baixarGravacao);

  const botaoRender = $('#renderRecording');
  if (botaoRender) botaoRender.addEventListener('click', exportarNormalizado);

  const botaoShare = $('#shareRecording');
  if (botaoShare) botaoShare.addEventListener('click', compartilharGravacao);

  // ------------------------------------------- drawer lateral de ajustes
  function alternarPainelAjustes(abrir) {
    const painel = $('#karaokePanel');
    if (!painel) return;
    const aberto = typeof abrir === 'boolean' ? abrir : !painel.classList.contains('open');
    painel.classList.toggle('open', aberto);
    const botao = $('#karaokeSettingsBtn');
    if (botao) botao.setAttribute('aria-expanded', String(aberto));
    const fundo = $('#karaokePanelBackdrop');
    if (fundo) fundo.hidden = !aberto;
  }

  const botaoAjustes = $('#karaokeSettingsBtn');
  if (botaoAjustes) botaoAjustes.addEventListener('click', () => alternarPainelAjustes());
  const fecharAjustes = $('#closeKaraokePanel');
  if (fecharAjustes) fecharAjustes.addEventListener('click', () => alternarPainelAjustes(false));
  const fundoAjustes = $('#karaokePanelBackdrop');
  if (fundoAjustes) fundoAjustes.addEventListener('click', () => alternarPainelAjustes(false));

  async function listarMicrofones(seletor) {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    try {
      const dispositivos = await navigator.mediaDevices.enumerateDevices();
      const micros = dispositivos.filter((d) => d.kind === 'audioinput' && d.deviceId);
      seletor.innerHTML = '<option value="">Segundo microfone</option>'
        + micros.map((d) => `<option value="${esc(d.deviceId)}">${esc(d.label || 'Microfone')}</option>`).join('');
    } catch (_) { /* seleção opcional */ }
  }

  const volumeMonitor = $('#monitorVolume');
  if (volumeMonitor) volumeMonitor.addEventListener('input', () => {
    if (ganhoMonitor) ganhoMonitor.gain.value = Number(volumeMonitor.value);
  });

  // Liga/desliga o retorno de voz imediatamente, sem precisar reiniciar a música.
  $('#monitorVoice')?.addEventListener('change', atualizarMonitoramento);

  // Sincroniza a letra com a VOZ ORIGINAL da música (referência de afinação do
  // vocal separado), ignorando totalmente o microfone de quem canta.
  function sincronizarAutomatico() {
    if (!ref.length || !linhas.length) {
      aviso('Esta música não tem referência de voz para sincronizar.');
      return;
    }
    const BIN = 0.05; // 50 ms
    const totalBins = Math.ceil(ref[ref.length - 1].t / BIN) + 1;
    const energia = new Float32Array(totalBins);
    ref.forEach((p) => {
      const k = Math.floor(p.t / BIN);
      if (k >= 0 && k < totalBins) energia[k] += 1;
    });
    // Suaviza a envoltória de canto.
    const suave = new Float32Array(totalBins);
    const raio = 3;
    for (let i = 0; i < totalBins; i += 1) {
      let soma = 0;
      let conta = 0;
      for (let d = -raio; d <= raio; d += 1) {
        const j = i + d;
        if (j >= 0 && j < totalBins) { soma += energia[j]; conta += 1; }
      }
      suave[i] = soma / conta;
    }
    let media = 0;
    for (let i = 0; i < totalBins; i += 1) media += suave[i];
    media /= totalBins;
    const limiar = media * 1.6 + 1e-6;
    const onsets = [];
    for (let i = 1; i < totalBins - 1; i += 1) {
      if (suave[i] >= limiar && suave[i] >= suave[i - 1] && suave[i] >= suave[i + 1]) {
        onsets.push(i * BIN);
      }
    }
    if (onsets.length < 3) {
      aviso('Pouca voz detectada na música para sincronizar.');
      return;
    }
    // Cada verso é casado com o início de canto mais próximo; a mediana dá o
    // deslocamento global robusto (descarta outliers).
    const candidatos = [];
    const JANELA = 3;
    for (const linha of linhas) {
      let melhor = null;
      let dist = JANELA;
      for (const o of onsets) {
        const d = Math.abs(o - linha.t);
        if (d < dist) { dist = d; melhor = o; }
      }
      if (melhor !== null) candidatos.push(melhor - linha.t);
    }
    if (candidatos.length < Math.max(3, linhas.length * 0.3)) {
      aviso('Não consegui sincronizar com segurança.');
      return;
    }
    candidatos.sort((a, b) => a - b);
    let off = candidatos[Math.floor(candidatos.length / 2)];
    off = Math.max(-8, Math.min(8, off));
    off = Math.round(off * 20) / 20; // passo de 0,05s
    ajusteLatencia = off;
    const slider = $('#latencyAdjust');
    if (slider) slider.value = String(ajusteLatencia);
    const saida = $('#latencyValue');
    if (saida) {
      saida.textContent = `${ajusteLatencia >= 0 ? '+' : ''}${ajusteLatencia.toFixed(2).replace('.', ',')}s`;
    }
    infoToast(`Letra alinhada à voz da música (${ajusteLatencia >= 0 ? '+' : ''}${ajusteLatencia.toFixed(2)}s).`);
  }

  const botaoSync = $('#autoSync');
  if (botaoSync) botaoSync.addEventListener('click', sincronizarAutomatico);

  const controleLatencia = $('#latencyAdjust');
  if (controleLatencia) {
    controleLatencia.addEventListener('input', () => {
      ajusteLatencia = Number(controleLatencia.value) || 0;
      const saida = $('#latencyValue');
      if (saida) saida.textContent = `${ajusteLatencia >= 0 ? '+' : ''}${ajusteLatencia.toFixed(2).replace('.', ',')}s`;
    });
  }

  window.addEventListener('resize', () => {
    if ($('#karaokeOverlay').style.display === 'block') { prepararCanvas(); desenhar(true); }
  });

  const botaoSair = document.getElementById('stopKaraoke');
  if (botaoSair) botaoSair.addEventListener('click', stop);
  document.addEventListener('keydown', (e) => {
    if ($('#karaokeOverlay').style.display !== 'block') return;
    if (e.key === 'Escape') {
      if ($('#karaokePanel')?.classList.contains('open')) { alternarPainelAjustes(false); return; }
      stop();
    }
    if (modoManual) {
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'Enter') {
        e.preventDefault();
        avancarManual(1);
      }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        e.preventDefault();
        avancarManual(-1);
      }
    }
  });

  return { start, stop };
})();

window.Karaoke = Karaoke;
