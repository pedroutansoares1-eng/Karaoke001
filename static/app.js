/* Karaokê Studio — abas, busca, player, separação e diagnóstico.
 * Correções de QA: delegação de eventos (sem interpolação de JSON no onclick,
 * o que quebrava com títulos como "Don't Stop"), escape de todo valor vindo
 * da API, toasts no lugar de alert() e a11y das abas. */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c]));

  const formatTime = (s) => {
    s = Number(s) || 0;
    return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  };

  const audio = $('#audio');
  const previewAudio = $('#previewAudio');
  let previaAtual = null;
  let tracks = [];
  let resultados = [];
  let jaTocouAuto = false;
  let current = null;
  let currentIndex = -1;
  const timers = new Set();

  // ------------------------------------------------------------------ toasts
  const TOAST_MAX = 4; // evita que muitos avisos cubram a tela
  function toast(mensagem, tipo = 'info') {
    let caixa = $('#toasts');
    if (!caixa) {
      caixa = document.createElement('div');
      caixa.id = 'toasts';
      caixa.className = 'toasts';
      caixa.setAttribute('aria-live', 'polite');
      document.body.appendChild(caixa);
    }
    const item = document.createElement('div');
    item.className = `toast ${tipo}`;
    item.setAttribute('role', tipo === 'erro' ? 'alert' : 'status');

    const texto = document.createElement('span');
    texto.className = 'toast-text';
    texto.textContent = mensagem;

    const fechar = document.createElement('button');
    fechar.type = 'button';
    fechar.className = 'toast-close';
    fechar.setAttribute('aria-label', 'Fechar aviso');
    fechar.textContent = '×';

    item.append(texto, fechar);
    caixa.appendChild(item);

    // Mantém no máximo TOAST_MAX avisos visíveis.
    while (caixa.children.length > TOAST_MAX) {
      caixa.firstElementChild.remove();
    }

    let removido = false;
    const remover = () => {
      if (removido) return;
      removido = true;
      item.classList.add('saindo');
      setTimeout(() => item.remove(), 300);
    };
    fechar.addEventListener('click', remover);
    setTimeout(remover, 4200);
  }
  window.toast = toast;

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  }

  // Lê a mensagem de erro da API de forma segura.
  async function erroDaApi(response, padrao) {
    try {
      const corpo = await response.json();
      if (typeof corpo?.detail === 'string') return corpo.detail;
      if (Array.isArray(corpo?.detail) && corpo.detail[0]?.msg) return corpo.detail[0].msg;
    } catch (_) { /* resposta sem JSON */ }
    return padrao;
  }

  const api = {
    async get(url) {
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error(await erroDaApi(r, 'Erro ao consultar o servidor.'));
        sinalizarOnline();
        return r.json();
      } catch (e) {
        if (e instanceof TypeError) sinalizarOffline();
        throw e;
      }
    },
    async post(url, body) {
      try {
        const r = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body ?? {}),
        });
        if (!r.ok) throw new Error(await erroDaApi(r, 'Erro na requisição.'));
        sinalizarOnline();
        return r.json();
      } catch (e) {
        if (e instanceof TypeError) sinalizarOffline();
        throw e;
      }
    },
    async put(url) {
      try {
        const r = await fetch(url, { method: 'PUT' });
        if (!r.ok) throw new Error(await erroDaApi(r, 'Erro na requisição.'));
        sinalizarOnline();
        return r.json();
      } catch (e) {
        if (e instanceof TypeError) sinalizarOffline();
        throw e;
      }
    },
    async del(url) {
      try {
        const r = await fetch(url, { method: 'DELETE' });
        if (!r.ok) throw new Error(await erroDaApi(r, 'Erro ao remover.'));
        sinalizarOnline();
        return r.json();
      } catch (e) {
        if (e instanceof TypeError) sinalizarOffline();
        throw e;
      }
    },
  };

  // --------------------------------------------------------- conexão/anfitrião
  // Detecta quando o servidor (anfitrião) fica indisponível, mostra aviso e
  // re-sincroniza tudo quando ele volta. O WebSocket já se reconecta sozinho.
  let ultimaConexaoOk = 0;
  const INTERVALO_CONEXAO = 8000;

  function sinalizarOffline() {
    const banner = $('#conexaoBanner');
    if (banner && banner.hidden) banner.hidden = false;
  }
  function sinalizarOnline() {
    ultimaConexaoOk = Date.now();
    const banner = $('#conexaoBanner');
    if (banner && !banner.hidden) banner.hidden = true;
  }
  async function verificarConexao() {
    try {
      const r = await fetch('/api/party/status', { cache: 'no-store' });
      if (!r.ok) throw new Error();
      if (Date.now() - ultimaConexaoOk > INTERVALO_CONEXAO) {
        toast('Anfitrião de volta! Re-sincronizando… ✅', 'ok');
        loadTracks();
        if (typeof recarregarFila === 'function') recarregarFila().catch(() => {});
        conectarFesta();
      }
      ultimaConexaoOk = Date.now();
      sinalizarOnline();
    } catch (_) {
      sinalizarOffline();
    }
  }
  window.setInterval(verificarConexao, INTERVALO_CONEXAO);
  $('#reconnectBtn')?.addEventListener('click', () => verificarConexao());

  // -------------------------------------------------------------------- abas
  function pararPolling() {
    timers.forEach(clearTimeout);
    timers.clear();
  }

  function abrirAba(nome) {
    pararPolling(); // evita acúmulo de timers a cada troca de aba
    $$('.tab').forEach((b) => {
      const ativo = b.dataset.tab === nome;
      b.classList.toggle('active', ativo);
      b.setAttribute('aria-selected', String(ativo));
    });
    $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === nome));
    if (nome === 'player') loadTracks();
    if (nome === 'separation') loadSeparations();
    if (nome === 'ranking') carregarRanking();
    if (nome === 'party' && !timerFesta) { conectarFesta(); recarregarFila(); loadTracks(); }
  }

  $$('.tab').forEach((button) => {
    button.addEventListener('click', () => abrirAba(button.dataset.tab));
  });

  // ------------------------------------------------------------------- busca
  function renderResultados(lista) {
    resultados = lista;
    const grade = $('#results');
    $('#resultCount').textContent = `${lista.length} ${lista.length === 1 ? 'encontrado' : 'encontrados'}`;
    if (!lista.length) {
      grade.className = 'card-grid empty-state';
      grade.innerHTML = '<div class="empty-icon">⌕</div><p>Nenhuma faixa encontrada. Tente outro termo.</p>';
      return;
    }
    grade.className = 'card-grid';
    grade.innerHTML = lista.map((x, i) => `
      <article class="track-card" style="animation:fadeUp .3s ease ${i * 50}ms both">
        <div class="cover${x.thumbnail ? ' has-image' : ''}">
          ${x.thumbnail ? `<img src="${esc(x.thumbnail)}" alt="Capa de ${esc(x.title)}" loading="lazy" onerror="this.parentElement.classList.remove('has-image');this.remove();this.parentElement.insertAdjacentText('beforeend','♪')">` : '♪'}
        </div>
        <h3>${esc(x.title)}</h3>
        <p>${esc(x.artist || 'Artista desconhecido')} · ${formatTime(x.duration)}</p>
        <div class="card-actions">
          <button data-action="preview" data-id="${esc(x.id)}">▶ Prévia</button>
          <button data-action="${ehConvidado() ? 'queue-search' : 'download'}" data-id="${esc(x.id)}">${ehConvidado() ? '⏭ Add à festa' : 'Baixar'}</button>
          ${ehConvidado() ? '' : `
          <select class="qualidade-mini" data-q="${esc(x.id)}" aria-label="Qualidade da separação de ${esc(x.title)}" title="Quão rápido separar a voz">
            <option value="rapida"${prefQualidade === 'rapida' ? ' selected' : ''}>⚡ Rápida</option>
            <option value="equilibrada"${prefQualidade === 'equilibrada' ? ' selected' : ''}>🔋 Equilibrada</option>
            <option value="alta"${prefQualidade === 'alta' ? ' selected' : ''}>🌟 Alta</option>
          </select>`}
        </div>
      </article>`).join('');
  }

  // Qualidade escolhida no card de busca (persiste para os próximos downloads).
  let qualidadesEscolhidas = {};
  let prefQualidade = 'equilibrada';
  try { prefQualidade = window.localStorage.getItem('vocalis_qualidade') || 'equilibrada'; } catch (_) { /* modo privado */ }
  function qualidadeDoDownload(id) {
    return qualidadesEscolhidas[id] || prefQualidade || 'equilibrada';
  }
  document.addEventListener('change', (ev) => {
    const sel = ev.target.closest('.qualidade-mini');
    if (!sel) return;
    qualidadesEscolhidas[sel.dataset.q] = sel.value;
    prefQualidade = sel.value;
    try { window.localStorage.setItem('vocalis_qualidade', sel.value); } catch (_) { /* modo privado */ }
  });

  function fonteBusca() {
    return $('#searchSource')?.value === 'youtube' ? 'youtube' : 'ytmusic';
  }

  let buscaSeq = 0;
  let buscaTimer = null;
  let limiteBusca = 15;
  const LIMITE_MAX = 30;

  // Cards "fantasma" exibidos enquanto a busca está em andamento (evita o salto de layout).
  function esqueletoCards(qtd = 6) {
    return Array.from({ length: qtd }, () => `
      <article class="track-card skeleton-card" aria-hidden="true">
        <div class="cover skeleton"></div>
        <h3 class="skeleton skeleton-text"></h3>
        <p class="skeleton skeleton-text short"></p>
        <div class="card-actions">
          <span class="skeleton skeleton-btn"></span>
          <span class="skeleton skeleton-btn"></span>
        </div>
      </article>`).join('');
  }
  function esqueletoLinhas(qtd = 4) {
    return Array.from({ length: qtd }, () => `
      <div class="player-result skeleton-row" aria-hidden="true">
        <span class="skeleton skeleton-avatar"></span>
        <div style="flex:1;min-width:0">
          <span class="skeleton skeleton-text"></span>
          <span class="skeleton skeleton-text short"></span>
        </div>
      </div>`).join('');
  }

  async function buscar(silencioso = false) {
    const termo = $('#query').value.trim();
    const grade = $('#results');

    if (termo.length < 2) {
      if (!silencioso) { toast('Digite pelo menos 2 caracteres.', 'erro'); $('#query').focus(); }
      grade.className = 'card-grid empty-state';
      grade.innerHTML = '<div class="empty-icon">⌕</div><p>Digite pelo menos 2 caracteres para começar sua busca.</p>';
      return;
    }

    const seq = ++buscaSeq;
    grade.className = 'card-grid';
    grade.innerHTML = esqueletoCards(6);
    try {
      const lista = await api.post('/api/search',
        { query: termo, limite: limiteBusca, filtro: $('#searchFilter')?.value || 'qualquer',
          fonte: fonteBusca() });
      if (seq !== buscaSeq) return; // ignora resposta de busca antiga
      renderResultados(lista);
      const mais = $('#moreResults');
      if (mais) mais.hidden = !(lista.length >= limiteBusca && limiteBusca < LIMITE_MAX);
    } catch (e) {
      if (seq !== buscaSeq) return;
      grade.className = 'card-grid empty-state';
      grade.innerHTML = '<div class="empty-icon">⚠</div><p>Não foi possível buscar agora.</p>';
      toast(e.message, 'erro');
    }
  }

  $('#searchBtn').addEventListener('click', () => buscar(false));
  $('#query').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(buscaTimer); buscar(false); }
  });
  // Busca automática enquanto digita (com debounce para não sobrecarregar a API).
  $('#query').addEventListener('input', () => {
    clearTimeout(buscaTimer);
    limiteBusca = 15;
    const mais = $('#moreResults');
    if (mais) mais.hidden = true;
    buscaTimer = setTimeout(() => buscar(true), 450);
  });

  $('#moreResults')?.addEventListener('click', () => {
    limiteBusca = Math.min(LIMITE_MAX, limiteBusca + 15);
    buscar(false);
  });

  $('#searchFilter')?.addEventListener('change', () => {
    const termo = ($('#query')?.value || '').trim();
    if (termo.length >= 2) buscar(true);
  });
  $('#searchSource')?.addEventListener('change', () => {
    const termo = ($('#query')?.value || '').trim();
    if (termo.length >= 2) buscar(true);
  });

  // --------------------------------------------------------------- download
  // O download roda em background no servidor; aqui só enfileiramos e
  // acompanhamos o estado até 'ready' (ou 'error').
  async function acompanharDownload(id, titulo, tentativas = 90) {
    for (let i = 0; i < tentativas; i += 1) {
      await new Promise((r) => setTimeout(r, 2000));
      let d = null;
      try {
        d = await api.get(`/api/download/${encodeURIComponent(id)}/status`);
      } catch (_) { continue; }
      if (d.status === 'ready') {
        toast(`“${titulo}” baixado. Preparando os stems...`, 'ok');
        await loadTracks();
        return true;
      }
      if (d.status === 'error') {
        toast(d.message || 'Falha no download.', 'erro');
        return false;
      }
    }
    toast('O download demorou mais que o esperado. Acompanhe na aba Separar.', 'erro');
    return false;
  }

  async function baixarFaixa(faixa, botao, aoPronto) {
    if (!faixa) return false;
    const rotulo = botao?.textContent || 'Baixar';
    if (botao) { botao.disabled = true; botao.textContent = 'Baixando...'; }
    try {
      const resp = await api.post('/api/download', { ...faixa, qualidade: qualidadeDoDownload(faixa.id) });
      const id = resp.id || faixa.id;
      const st = resp.download?.status || 'ready';
      if (st === 'ready') {
        if (botao) { botao.disabled = false; botao.textContent = '✓ Baixado'; }
        toast(`“${faixa.title}” baixado. Preparando os stems...`, 'ok');
        await loadTracks();
        if (typeof aoPronto === 'function') aoPronto();
        return true;
      }
      toast(`“${faixa.title}” na fila de download...`, 'ok');
      const ok = await acompanharDownload(id, faixa.title);
      if (ok) {
        if (botao) { botao.disabled = false; botao.textContent = '✓ Baixado'; }
        if (typeof aoPronto === 'function') aoPronto();
        return true;
      }
      if (botao) { botao.disabled = false; botao.textContent = rotulo; }
      return false;
    } catch (e) {
      if (botao) { botao.disabled = false; botao.textContent = rotulo; }
      toast(e.message, 'erro');
      return false;
    }
  }

  async function baixar(id, botao) {
    await baixarFaixa(resultados.find((x) => x.id === id), botao, async () => {
      await loadTracks();
      // Só o primeiro download da sessão abre o Player e toca; os seguintes
      // ficam na busca com "✓ Baixado" para baixar várias de uma vez.
      if (!jaTocouAuto) {
        jaTocouAuto = true;
        const baixada = tracks.find((t) => t.id === id);
        if (baixada) {
          tocar(baixada);
          abrirAba('player');
          toast('Baixada! Tocando agora ↗', 'ok');
        }
      } else {
        toast('Baixada! ✓', 'ok');
      }
    });
  }

  async function baixarNoPlayer(id, botao) {
    await baixarFaixa(resultadosPlayer.find((x) => x.id === id), botao);
  }

  // ---------------------------------------------------------- baixar playlist
  // Preenche a biblioteca de uma vez: cada música da playlist vira um download
  // na fila do servidor. No "modo festa" (caixinha marcada) o servidor ainda
  // separa a voz e busca a letra/afinação de cada faixa, então aqui a lista
  // acompanha download -> separação -> karaokê até a música ficar pronta.
  let playlistItens = [];
  let playlistTimer = null;

  function renderPlaylist() {
    const alvo = $('#playlistList');
    if (!alvo) return;
    if (!playlistItens.length) { alvo.hidden = true; alvo.innerHTML = ''; return; }
    const prontas = playlistItens.filter((it) => it.fim).length;
    const verbo = playlistItens.some((it) => it.preparar) ? 'prontas pra cantar' : 'baixadas';
    alvo.hidden = false;
    alvo.innerHTML = `
      <div class="playlist-resumo">${prontas} de ${playlistItens.length} ${verbo}</div>
      <div class="playlist-itens">${playlistItens.map((it) => `
        <div class="playlist-item">
          <span class="playlist-nome" title="${esc(it.title)}">${esc(it.title)}</span>
          <span class="playlist-estado ${it.classe}">${esc(it.estado)}</span>
        </div>`).join('')}</div>`;
  }

  function pararAcompanharPlaylist() {
    if (playlistTimer) { clearTimeout(playlistTimer); playlistTimer = null; }
  }

  function estadoDoItem(it, st) {
    // Traduz o estado que o servidor devolveu para o rótulo da linha.
    // Devolve true quando a faixa terminou a linha de montagem (deu certo ou não).
    const baixada = st.download === 'ready';
    if (it.preparar) {
      if (st.ready) { it.estado = '✓ pronta pra cantar'; it.classe = 'ok'; return true; }
      if (st.karaoke === 'no_lyrics') { it.estado = '✓ baixada (sem letra)'; it.classe = 'ok'; return true; }
      if (st.karaoke === 'error') { it.estado = '⚠ falhou na letra'; it.classe = 'erro'; return true; }
      if (st.download === 'error') { it.estado = '⚠ download falhou'; it.classe = 'erro'; return true; }
      if (!baixada) {
        it.estado = st.download === 'downloading' ? '⬇ baixando' : '⏳ na fila';
        it.classe = 'ativo';
        return false;
      }
      if (st.separation === 'separating' || st.separation === 'queued') {
        it.estado = '⚙ separando a voz'; it.classe = 'ativo'; return false;
      }
      it.estado = st.separation === 'error'
        ? '🎤 letra (sem separação)' : '🎤 buscando letra e afinação';
      it.classe = 'ativo';
      return false;
    }
    if (baixada) { it.estado = '✓ baixada'; it.classe = 'ok'; return true; }
    if (st.download === 'error') { it.estado = '⚠ download falhou'; it.classe = 'erro'; return true; }
    it.estado = st.download === 'downloading' ? '⬇ baixando' : '⏳ na fila';
    it.classe = 'ativo';
    return false;
  }

  async function acompanharPlaylist() {
    playlistTimer = null;
    const pendentes = playlistItens.filter((it) => !it.fim);
    if (!pendentes.length) return;
    let dados = null;
    try {
      // Um pedido só para a lista inteira: 50 faixas não valem 50 requisições.
      dados = await api.post('/api/playlist/status', { ids: pendentes.map((it) => it.id) });
    } catch (_) { /* servidor oscilou: tenta de novo no próximo ciclo */ }

    let algumaPronta = false;
    const porId = new Map((dados?.itens || []).map((i) => [i.id, i]));
    pendentes.forEach((it) => {
      const st = porId.get(it.id);
      if (!st) return;
      if (estadoDoItem(it, st)) { it.fim = true; algumaPronta = true; }
    });
    renderPlaylist();
    if (algumaPronta) await loadTracks();
    if (playlistItens.some((it) => !it.fim)) {
      playlistTimer = setTimeout(acompanharPlaylist, 3500);
    }
  }

  async function baixarPlaylist() {
    const url = ($('#playlistUrl')?.value || '').trim();
    if (!url) { toast('Cole a URL de uma playlist do YouTube.', 'info'); return; }
    const botao = $('#playlistBtn');
    const rotulo = botao ? botao.textContent : 'Baixar playlist';
    if (botao) { botao.disabled = true; botao.textContent = 'Lendo a playlist...'; }
    const preparar = $('#playlistPreparar') ? $('#playlistPreparar').checked : true;
    try {
      const dados = await api.post('/api/playlist',
        { url, qualidade: qualidadeDoDownload('playlist'), preparar });
      const montar = (lista) => (lista || []).map((f) => ({
        id: f.id, title: f.title, preparar,
        estado: preparar ? '⏳ preparando...' : '⏳ na fila', classe: 'ativo', fim: false,
      }));
      // 'faixas' = baixadas agora; 'preparando' = já estavam na biblioteca e só
      // vão ganhar letra/afinação. As duas entram na mesma lista de acompanhamento.
      playlistItens = [...montar(dados.faixas), ...montar(dados.preparando)];
      pararAcompanharPlaylist();
      renderPlaylist();
      if (playlistItens.length) acompanharPlaylist();
      const campo = $('#playlistUrl');
      if (campo) campo.value = '';
      const partes = [`${dados.enfileiradas} música(s) na fila`];
      if ((dados.preparando || []).length) {
        partes.push(`${dados.preparando.length} já baixada(s) entrando no preparo`);
      } else if (dados.ja_tinha) {
        partes.push(preparar
          ? `${dados.ja_tinha} já estavam prontas`
          : `${dados.ja_tinha} já estavam na biblioteca`);
      }
      if (dados.total >= dados.limite) partes.push(`limite de ${dados.limite} por vez`);
      toast(partes.join(' · ') + (preparar ? ' — deixando tudo pronto 🎤' : ''), 'ok');
    } catch (e) {
      toast(e.message || 'Não consegui ler essa playlist.', 'erro');
    } finally {
      if (botao) { botao.disabled = false; botao.textContent = rotulo; }
    }
  }

  $('#playlistBtn')?.addEventListener('click', baixarPlaylist);
  $('#playlistUrl')?.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); baixarPlaylist(); }
  });

  // ----------------------------------------------------------------- player
  let filtroFaixas = '';
  let soloFavoritas = false;
  let resultadosPlayer = [];

  function renderTracks() {
    const lista = $('#tracks');
    if (!lista) return;
    const termo = filtroFaixas.trim().toLowerCase();
    let visiveis = termo
      ? tracks.filter((x) => `${x.title} ${x.artist || ''}`.toLowerCase().includes(termo))
      : tracks.slice();
    if (soloFavoritas) visiveis = visiveis.filter((x) => x.favorita);
    if (!visiveis.length) {
      if (!tracks.length) {
        // Estado vazio convidativo: ilustração + CTA para explorar.
        lista.innerHTML = `
          <div class="empty-cta">
            <span class="mic-art" aria-hidden="true">🎤</span>
            <h3>Sua estante de karaokê está vazia</h3>
            <p>Busque uma música, baixe o áudio e a separação da voz é feita para você cantar por cima.</p>
            <button class="primary" type="button" data-action="explore">Explorar músicas</button>
          </div>`;
      } else {
        lista.innerHTML = soloFavoritas
          ? '<div class="empty-state"><p>Nenhuma favorita ainda — toque na ★ das faixas para guardar aqui.</p></div>'
          : '<div class="empty-state"><p>Nenhuma faixa corresponde à busca.</p></div>';
      }
      return;
    }
    lista.innerHTML = visiveis.map((x) => `
      <div class="track-row ${current?.id === x.id ? 'active' : ''}"
           data-action="play" data-id="${esc(x.id)}" role="button" tabindex="0">
        ${x.thumbnail ? `<img class="track-thumb" src="${esc(x.thumbnail)}" alt="" loading="lazy">` : '<span class="track-thumb-placeholder">♪</span>'}
        <span class="track-row-info"><strong>${esc(x.title)}</strong>
        <small>${esc(x.artist || 'Artista desconhecido')}${chipsSeparacao(x)}</small></span>
        <span class="eq" aria-hidden="true"><i></i><i></i><i></i></span>
        <button class="mini fav ${x.favorita ? 'on' : ''}" data-action="favoritar" data-id="${esc(x.id)}"
                aria-pressed="${x.favorita ? 'true' : 'false'}"
                aria-label="${x.favorita ? `Remover ${esc(x.title)} dos favoritos` : `Favoritar ${esc(x.title)}`}"
                title="${x.favorita ? 'Remover dos favoritos' : 'Adicionar às favoritas'}">★</button>
        <button class="mini danger" data-action="delete" data-id="${esc(x.id)}"
                aria-label="Apagar ${esc(x.title)}" title="Apagar música">🗑</button>
      </div>`).join('');
  }

  function chipsSeparacao(x) {
    const s = x && x.separacao;
    if (!s || s.pronto || s.status === 'nao_iniciado') return '';
    if (s.status === 'error') {
      return ` <button class="sep-chip sep-error" data-action="retry-separation" data-id="${esc(x.id)}"
                     title="A separação falhou — toque para tentar de novo">⚠ erro · tentar de novo</button>`;
    }
    const job = jobsSeparacao[x.id];
    if (s.status === 'separating') {
      const tempo = job ? ` · há ${formatarTempoJob(job.inicio)}` : '';
      return ` <span class="sep-chip sep-separating" aria-hidden="true">⚙ separando a voz${tempo}</span>`;
    }
    if (s.status === 'queued') {
      const posicao = job ? ` · #${job.posicao}` : '';
      return ` <span class="sep-chip sep-queued" aria-hidden="true">⏳ na fila${posicao}</span>`;
    }
    return '';
  }

  async function loadTracks() {
    try {
      tracks = await api.get('/api/tracks');
      if (window.VocalisOffline) {
        window.VocalisOffline.salvar('tracks', tracks).catch(() => {});
      }
    } catch (e) {
      // Offline ou servidor fora do ar: usa a última lista salva.
      if (window.VocalisOffline) {
        const salvo = await window.VocalisOffline.ler('tracks');
        if (Array.isArray(salvo)) {
          tracks = salvo;
          toast('Lista de faixas carregada do cache offline.', 'info');
        } else {
          toast(e.message, 'erro');
        }
      } else {
        toast(e.message, 'erro');
      }
      renderTracks();
      atualizarBotaoKaraoke();
      return;
    }
    if ('caches' in window) {
      tracks.slice(0, 3).forEach((faixa) => préCarregarAudio(faixa));
    }
    renderTracks();
    renderBibliotecaFesta();
    atualizarBotaoKaraoke();
    if (temSeparacaoPendente()) { iniciarVigiaSeparacoes(); atualizarJobsSeparacao(); }
    else pararVigiaSeparacoes();
  }

  // ---------------------------------------------------- vigia de separação
  // Mantém os chips de "separando a voz", o ETA e o botão de karaokê
  // atualizados; avisa quando a faixa que está tocando fica pronta.
  let vigiandoSeparacoes = null;
  let jobsSeparacao = {};

  function formatarTempoJob(inicioMs) {
    const seg = Math.max(0, Math.round((Date.now() - inicioMs) / 1000));
    const m = Math.floor(seg / 60);
    const ss = seg % 60;
    return `${m}:${String(ss).padStart(2, '0')}`;
  }

  async function atualizarJobsSeparacao() {
    try {
      const r = await api.get('/api/jobs');
      const fila = (r.jobs || [])
        .filter((j) => j.nome === 'separation' && ['queued', 'running'].includes(j.status))
        .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
      const mapa = {};
      fila.forEach((j, indice) => {
        mapa[j.chave] = {
          posicao: indice + 1,
          inicio: Date.parse(j.created_at || '') || Date.now(),
        };
      });
      jobsSeparacao = mapa;
    } catch (_) { /* anfitrião fora do ar: segue sem metadados de fila */ }
  }

  function temSeparacaoPendente() {
    return tracks.some((t) => t.separacao && !t.separacao.pronto
      && ['queued', 'separating', 'error'].includes(t.separacao.status));
  }

  function iniciarVigiaSeparacoes() {
    if (vigiandoSeparacoes) return;
    vigiandoSeparacoes = window.setInterval(async () => {
      let dados;
      try { dados = await api.get('/api/tracks'); } catch (_) { return; }
      await atualizarJobsSeparacao();
      const tocando = current?.id ? tracks.find((t) => t.id === current.id) : null;
      const antigoPronto = tocando?.separacao?.pronto;
      tracks = dados;
      if (window.VocalisOffline) {
        window.VocalisOffline.salvar('tracks', tracks).catch(() => {});
      }
      const atual = current?.id ? tracks.find((t) => t.id === current.id) : null;
      const emKaraoke = $('#karaokeOverlay')?.style.display === 'block';
      if (atual && !antigoPronto && atual.separacao?.pronto && !emKaraoke) {
        toast('A voz foi separada! Agora é só entrar no karaokê. 🎤', 'ok');
      }
      renderTracks();
      renderBibliotecaFesta();
      atualizarBotaoKaraoke();
      if (!temSeparacaoPendente()) pararVigiaSeparacoes();
    }, 4000);
  }

  function pararVigiaSeparacoes() {
    if (vigiandoSeparacoes) {
      window.clearInterval(vigiandoSeparacoes);
      vigiandoSeparacoes = null;
    }
  }

  function atualizarBotaoKaraoke() {
    const botao = $('#karaokeButton');
    if (!botao) return;
    const rotulo = botao.querySelector('span:not(.tab-icon)');
    if (rotulo) rotulo.textContent = 'Entrar no Karaokê';
    botao.title = 'Se a voz ainda estiver sendo separada, você canta com o áudio original e o instrumental entra sozinho.';
    botao.classList.remove('preparando');
  }

  async function apagarFaixa(id, botao) {
    const faixa = tracks.find((x) => x.id === id);
    const nome = faixa ? `“${faixa.title}”` : 'esta música';
    if (!window.confirm(`Apagar ${nome}? Remove o áudio, a letra e a separação.`)) return;
    if (botao) { botao.disabled = true; botao.textContent = '…'; }
    try {
      await api.del(`/api/tracks/${encodeURIComponent(id)}`);
      if (current?.id === id) {
        current = null;
        currentIndex = -1;
        audio.pause();
        audio.removeAttribute('src');
        audio.load();
        document.body.classList.toggle('tocando', false);
        limparPreviewLetra();
        $('#playerTitle').textContent = 'Selecione uma faixa';
        $('#playerArtist').textContent = 'PRONTO PARA CANTAR';
        const capa = $('#albumArt');
        if (capa) { capa.innerHTML = '<span>♪</span>'; capa.classList.remove('has-image'); }
        $('#playPause').classList.remove('tocando');
        $('#duration').textContent = '0:00 / 0:00';
        $('#progress').value = 0;
      }
      toast('Música apagada.', 'ok');
      await loadTracks();
    } catch (e) {
      if (botao) { botao.disabled = false; botao.textContent = '🗑'; }
      toast(e.message, 'erro');
    }
  }

  // ------------------------------------------------------------ prévia
  function restaurarBotoesPrevia() {
    document.querySelectorAll('[data-action="preview"]').forEach((b) => {
      b.textContent = '▶ Prévia';
      b.classList.remove('tocando');
    });
    const mini = document.querySelectorAll('.player-result [data-action="preview"]');
    mini.forEach((b) => { b.textContent = '▶'; });
  }

  async function tocarPrevia(id, botao) {
    if (!previewAudio) return;
    if (previaAtual === id && !previewAudio.paused) {
      previewAudio.pause();
      restaurarBotoesPrevia();
      return;
    }
    const rotulo = botao?.textContent;
    if (botao) { botao.disabled = true; }
    try {
      const dados = await api.post('/api/preview', { id });
      restaurarBotoesPrevia();
      previewAudio.src = dados.url;
      await previewAudio.play();
      previaAtual = id;
      if (botao) {
        botao.textContent = botao.classList.contains('mini') ? '❚❚' : '❚❚ Pausar';
        botao.classList.add('tocando');
      }
    } catch (e) {
      if (botao) botao.textContent = rotulo || '▶ Prévia';
      toast(e.message || 'Não consegui pré-ouvir.', 'erro');
    } finally {
      if (botao) botao.disabled = false;
    }
  }

  previewAudio?.addEventListener('ended', () => {
    previaAtual = null;
    restaurarBotoesPrevia();
  });

  // ------------------------------------------------- busca na aba Player
  function renderResultadosPlayer(lista) {
    resultadosPlayer = lista;
    const alvo = $('#playerResults');
    if (!alvo) return;
    if (!lista.length) {
      alvo.hidden = false;
      alvo.innerHTML = '<div class="empty-state"><p>Nenhuma faixa encontrada no YouTube.</p></div>';
      return;
    }
    alvo.hidden = false;
    alvo.innerHTML = `
      <div class="player-results-head"><span>Do YouTube</span>
        <button class="mini" id="limparResultadosPlayer" type="button" aria-label="Fechar resultados">×</button></div>
      ${lista.map((x) => `
        <div class="player-result">
          ${x.thumbnail ? `<img class="track-thumb" src="${esc(x.thumbnail)}" alt="" loading="lazy">` : '<span class="track-thumb-placeholder">♪</span>'}
          <span class="track-row-info"><strong>${esc(x.title)}</strong>
          <small>${esc(x.artist || 'Artista desconhecido')} · ${formatTime(x.duration)}</small></span>
          <button class="mini" data-action="preview" data-id="${esc(x.id)}" aria-label="Pré-ouvir ${esc(x.title)}" title="Prévia">▶</button>
          <button class="mini primary" data-action="download-player" data-id="${esc(x.id)}" aria-label="Baixar ${esc(x.title)}">Baixar</button>
        </div>`).join('')}`;
    $('#limparResultadosPlayer')?.addEventListener('click', () => {
      alvo.hidden = true;
      alvo.innerHTML = '';
    });
  }

  async function buscarNoPlayer() {
    const termo = ($('#playerQuery')?.value || '').trim();
    if (termo.length < 2) { toast('Digite pelo menos 2 caracteres.', 'erro'); return; }
    const alvo = $('#playerResults');
    if (alvo) { alvo.hidden = false; alvo.innerHTML = esqueletoLinhas(4); }
    try {
      renderResultadosPlayer(await api.post('/api/search',
        { query: termo, filtro: $('#searchFilter')?.value || 'qualquer', fonte: fonteBusca() }));
    } catch (e) {
      if (alvo) alvo.innerHTML = '<div class="empty-state"><p>Não foi possível buscar agora.</p></div>';
      toast(e.message, 'erro');
    }
  }

  $('#playerQuery')?.addEventListener('input', (e) => {
    filtroFaixas = e.target.value;
    renderTracks();
  });
  $('#playerQuery')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); buscarNoPlayer(); }
  });
  $('#playerSearchBtn')?.addEventListener('click', buscarNoPlayer);

  // Alterna o filtro "só favoritas" da lista.
  $('#favFilter')?.addEventListener('click', (e) => {
    soloFavoritas = !soloFavoritas;
    const botao = $('#favFilter');
    if (botao) {
      botao.classList.toggle('on', soloFavoritas);
      botao.setAttribute('aria-pressed', String(soloFavoritas));
      botao.title = soloFavoritas ? 'Mostrar todas as faixas' : 'Mostrar só favoritas';
    }
    if (soloFavoritas && !tracks.some((t) => t.favorita)) {
      toast('Nenhuma favorita ainda — toque na ★ das faixas.', 'info');
    }
    renderTracks();
  });

  async function toggleFavorita(id, botao) {
    try {
      const r = await api.put(`/api/favoritas/${encodeURIComponent(id)}`);
      if (botao) {
        botao.setAttribute('aria-pressed', String(r.favorita));
        botao.title = r.favorita ? 'Remover dos favoritos' : 'Adicionar às favoritas';
        botao.classList.toggle('on', r.favorita);
      }
      toast(r.favorita ? 'Adicionada às favoritas. ★' : 'Removida das favoritas.', 'ok');
      loadTracks();
    } catch (e) {
      toast(e.message, 'erro');
    }
  }

  async function préCarregarAudio(track) {
    if (!track?.id || !('caches' in window)) return;
    const url = `/data/downloads/${encodeURIComponent(track.id)}.mp3`;
    try {
      const cache = await caches.open('vocalis-audio-v2');
      const existente = await cache.match(url);
      if (!existente) await cache.add(url);
    } catch (_) { /* o áudio normal continua funcionando */ }
  }

  function tocar(track) {
    if (!track) return;
    current = track;
    préCarregarAudio(track);
    currentIndex = tracks.findIndex((t) => t.id === track.id);
    removerVoz = false;
    const rotuloVoz = $('#vocalBtn span:not(.tab-icon)');
    if (rotuloVoz) rotuloVoz.textContent = 'Voz';
    $('#vocalBtn')?.setAttribute('aria-pressed', 'false');
    audio.src = `/data/downloads/${encodeURIComponent(track.id)}.mp3`;
    $('#playerTitle').textContent = track.title;
    $('#playerArtist').textContent = (track.artist || 'ARTISTA DESCONHECIDO').toUpperCase();
    const capa = $('#albumArt');
    if (capa) {
      capa.innerHTML = track.thumbnail
        ? `<img src="${esc(track.thumbnail)}" alt="Capa de ${esc(track.title)}">`
        : '<span>♪</span>';
      capa.classList.toggle('has-image', Boolean(track.thumbnail));
    }
    carregarPreviewLetra(track);
    $('#playPause').classList.add('tocando');
    audio.play().catch(() => toast('Não consegui iniciar a reprodução.', 'erro'));
    loadTracks();
  }

  function mover(step) {
    if (!tracks.length) return;
    const indice = currentIndex < 0 ? 0 : currentIndex;
    tocar(tracks[(indice + step + tracks.length) % tracks.length]);
  }

  audio.addEventListener('play', () => {
    $('#playPause').classList.add('tocando');
    document.body.classList.toggle('tocando', true);
  });
  audio.addEventListener('pause', () => {
    $('#playPause').classList.remove('tocando');
    document.body.classList.toggle('tocando', false);
  });
  audio.addEventListener('ended', () => {
    // Com o karaokê aberto, quem encerra a música é o próprio karaokê (mostra a
    // pontuação e fecha o overlay). Se o player pulasse de faixa aqui, o
    // karaokê ficaria preso na letra antiga enquanto o próximo áudio tocava.
    if ($('#karaokeOverlay')?.style.display === 'block') return;
    // No Modo Festa, uma música que termina dá lugar automático à próxima.
    if (filaFesta.some((i) => i.status === 'pending')) { tocarProxima(); return; }
    mover(1);
  });
  audio.addEventListener('error', () => {
    if (audio.src) toast('Áudio indisponível. Tente baixar novamente.', 'erro');
  });
  audio.addEventListener('timeupdate', () => {
    const atual = formatTime(audio.currentTime);
    const total = formatTime(audio.duration);
    $('#progress').value = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
    $('#progress').setAttribute('aria-valuetext', `${atual} de ${total}`);
    $('#currentTime').textContent = atual;
    $('#duration').textContent = `${atual} / ${total}`;
    atualizarPreviewLetra();
  });
  audio.addEventListener('loadedmetadata', () => {
    const total = formatTime(audio.duration);
    $('#duration').textContent = `0:00 / ${total}`;
    $('#progress').setAttribute('aria-valuetext', `0:00 de ${total}`);
  });

  // ------------------------------------------------------------------ letra
  // Preview sincronizado (linha atual + próxima) abaixo do título da faixa.
  let linhasLetra = [];
  let indiceLetra = -1;

  function limparPreviewLetra() {
    linhasLetra = [];
    indiceLetra = -1;
    const el = $('#lyricsPreview');
    if (el) el.hidden = true;
  }

  async function carregarPreviewLetra(faixa) {
    limparPreviewLetra();
    if (!faixa) return;
    const chave = `lyrics-${faixa.id}`;
    let dados = null;
    try {
      const resp = await fetch(`/api/lyrics/${encodeURIComponent(faixa.id)}`);
      if (resp.ok) dados = await resp.json();
    } catch (e) { dados = null; }
    if (!dados && window.VocalisOffline) {
      try { dados = await window.VocalisOffline.ler(chave); } catch (e) { dados = null; }
    }
    if (dados && dados.synced && Array.isArray(dados.lines) && dados.lines.length) {
      if (dados.source && dados.source !== 'local' && window.VocalisOffline) {
        window.VocalisOffline.salvar(chave, dados).catch(() => {});
      }
      if (current?.id !== faixa.id) return;
      linhasLetra = dados.lines;
      const el = $('#lyricsPreview');
      if (el) el.hidden = false;
      atualizarPreviewLetra(true);
    }
  }

  function atualizarPreviewLetra(forcar = false) {
    const el = $('#lyricsPreview');
    const atual = $('#lyricsCurrent');
    const proxima = $('#lyricsNext');
    if (!el || el.hidden || !linhasLetra.length) return;
    let i = -1;
    for (let k = 0; k < linhasLetra.length; k++) {
      if (Number(linhasLetra[k].time) <= audio.currentTime) i = k;
    }
    if (i === indiceLetra && !forcar) return;
    indiceLetra = i;
    const a = i < 0 ? (linhasLetra[0] || {}) : (linhasLetra[i] || {});
    const b = linhasLetra[i + 1] || {};
    if (atual) atual.textContent = a.text || '';
    if (proxima) proxima.textContent = b.text || '';
  }

  $('#progress').addEventListener('input', () => {
    if (audio.duration) audio.currentTime = (Number($('#progress').value) / 100) * audio.duration;
  });
  $('#playPause').addEventListener('click', () => {
    if (!current) { toast('Escolha uma faixa primeiro.', 'erro'); return; }
    if (audio.paused) audio.play().catch(() => toast('Não consegui tocar.', 'erro'));
    else audio.pause();
  });
  $('#prevTrack').addEventListener('click', () => mover(-1));
  $('#nextTrack').addEventListener('click', () => mover(1));
  $('#refreshTracks').addEventListener('click', loadTracks);

  // -------------------------------------------------------- ferramentas
  $('#queueBtn')?.addEventListener('click', alternarFilaPlayer);
  function alternarFilaPlayer() {
    const painel = $('#playerQueueDrawer');
    if (!painel) return;
    if (painel.hasAttribute('hidden')) {
      painel.removeAttribute('hidden');
      recarregarFila();
    } else {
      painel.setAttribute('hidden', '');
    }
  }
  $('#playerQueueClose')?.addEventListener('click', () => {
    $('#playerQueueDrawer')?.setAttribute('hidden', '');
  });
  $('#playerQueueSearch')?.addEventListener('click', () => {
    $('#playerQueueDrawer')?.setAttribute('hidden', '');
    abrirAba('search');
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') $('#playerQueueDrawer')?.setAttribute('hidden', '');
  });
  $('#fullscreenBtn')?.addEventListener('click', () => {
    const painel = document.querySelector('.main-player');
    if (!painel) return;
    if (!document.fullscreenElement) painel.requestFullscreen?.().catch(() => {});
    else document.exitFullscreen?.();
  });

  // Remover voz: alterna entre o áudio original e a versão sem vocais.
  let removerVoz = false;
  async function alternarRemoverVoz() {
    if (!current) { toast('Escolha uma faixa primeiro.', 'erro'); return; }
    if (!removerVoz) {
      try {
        const st = await api.get(`/api/separation/${encodeURIComponent(current.id)}`);
        if (!st || !st.ready) { toast('Os vocais ainda não foram separados. Use a aba Separar.', 'erro'); return; }
      } catch (e) { toast(e.message, 'erro'); return; }
    }
    removerVoz = !removerVoz;
    const posicao = audio.currentTime;
    audio.src = removerVoz
      ? `/data/separated/${encodeURIComponent(current.id)}/htdemucs/no_vocals.wav`
      : `/data/downloads/${encodeURIComponent(current.id)}.mp3`;
    audio.addEventListener('loadedmetadata', () => { audio.currentTime = posicao; }, { once: true });
    audio.play().catch(() => toast('Não consegui tocar a versão.', 'erro'));
    const rotulo = $('#vocalBtn span:not(.tab-icon)');
    if (rotulo) rotulo.textContent = removerVoz ? 'Vocais' : 'Voz';
    $('#vocalBtn')?.setAttribute('aria-pressed', String(removerVoz));
    toast(removerVoz ? 'Tocando sem a voz.' : 'Voz restaurada.', 'ok');
  }
  $('#vocalBtn')?.addEventListener('click', alternarRemoverVoz);

  // Tooltip de tempo na barra de progresso.
  function posicaoTipProgresso(ev) {
    const tip = $('#progressTip');
    const slider = $('#progress');
    const wrap = $('#progressSlider');
    if (!tip || !slider || !wrap) return;
    const rect = slider.getBoundingClientRect();
    if (!rect.width) return;
    let fracao = ev.type === 'input'
      ? (Number(slider.value) / 100)
      : (ev.clientX - rect.left) / rect.width;
    fracao = Math.min(1, Math.max(0, fracao));
    tip.textContent = formatTime(audio.duration ? fracao * audio.duration : fracao * 100);
    tip.hidden = false;
    tip.style.left = `${fracao * 100}%`;
    tip.style.transform = `translateX(${fracao > 0.85 ? '-100%' : fracao < 0.15 ? '0%' : '-50%'})`;
  }
  $('#progressSlider')?.addEventListener('mousemove', posicaoTipProgresso);
  $('#progressSlider')?.addEventListener('mouseleave', () => {
    const tip = $('#progressTip');
    if (tip) tip.hidden = true;
  });
  $('#progress')?.addEventListener('input', posicaoTipProgresso);

  // ------------------------------------------------------------- volume
  const CHAVE_VOLUME = 'vocalis_volume';
  let volumeAnterior = 1;

  function atualizarIconeVolume() {
    const botao = $('#muteToggle');
    if (!botao) return;
    const mudo = audio.muted || audio.volume === 0;
    botao.classList.toggle('mudo', mudo);
    botao.setAttribute('aria-pressed', String(mudo));
    botao.setAttribute('aria-label', mudo ? 'Ativar som' : 'Silenciar');
    botao.title = mudo ? 'Ativar som' : 'Silenciar';
  }

  function definirVolume(valor, salvar = true) {
    const volume = Math.min(1, Math.max(0, Number(valor)));
    audio.volume = volume;
    audio.muted = volume === 0;
    const slider = $('#volume');
    if (slider) slider.value = String(volume);
    atualizarIconeVolume();
    if (salvar) {
      try { window.localStorage.setItem(CHAVE_VOLUME, String(volume)); } catch (_) { /* modo privado */ }
    }
  }

  try {
    const salvo = Number(window.localStorage.getItem(CHAVE_VOLUME));
    if (Number.isFinite(salvo) && salvo >= 0 && salvo <= 1) definirVolume(salvo, false);
    else definirVolume(1, false);
  } catch (_) {
    definirVolume(1, false);
  }

  $('#volume')?.addEventListener('input', (evento) => definirVolume(evento.target.value));
  $('#muteToggle')?.addEventListener('click', () => {
    if (audio.muted || audio.volume === 0) definirVolume(volumeAnterior || 0.8);
    else { volumeAnterior = audio.volume; definirVolume(0); }
  });

  async function atualizarCapas() {
    const botao = $('#refreshCovers');
    if (botao) { botao.disabled = true; botao.textContent = '…'; }
    let atualizadas = 0;
    try {
      if (!tracks.length) await loadTracks();
      for (const faixa of tracks) {
        if (faixa.thumbnail) continue;
        try {
          const resultado = await api.post(`/api/tracks/${encodeURIComponent(faixa.id)}/cover`);
          if (resultado.thumbnail) {
            faixa.thumbnail = resultado.thumbnail;
            atualizadas += 1;
          }
        } catch (_) { /* uma faixa sem capa não impede as demais */ }
      }
      await loadTracks();
      toast(atualizadas ? `${atualizadas} capa(s) atualizada(s).` : 'As capas já estão atualizadas.', 'ok');
    } finally {
      if (botao) { botao.disabled = false; botao.textContent = '▣'; }
    }
  }

  $('#refreshCovers').addEventListener('click', atualizarCapas);

  $('#karaokeButton').addEventListener('click', async () => {
    if (!current) { toast('Escolha uma faixa primeiro.', 'erro'); return; }
    // Aviso antecipado quando a letra não tem sincronia (o usuário escolhe
    // como acompanhar dentro do karaokê — melhor cantar já sabendo).
    try {
      const ly = await api.get(`/api/lyrics/${encodeURIComponent(current.id)}`);
      if (ly && ly.found && ly.synced === false) {
        toast('Essa letra não tem sincronia: você escolherá como acompanhar os versos. 🎼', 'info');
      }
    } catch (_) { /* segue o fluxo normal */ }
    try {
      if (!$('#playerModal')) {
        if (!window.Karaoke) throw new Error('Módulo de karaokê indisponível.');
        await window.Karaoke.start(current);
      } else {
        await abrirSeletorJogador();
      }
    } catch (erro) {
      toast(erro.message || 'Não consegui abrir o modo karaokê.', 'erro');
    }
  });

  // -------------------------------------------------------------- separação
  const ROTULOS = {
    queued: '⏳ Aguardando início',
    separating: '⚙ Separando stems...',
    ready: '✅ Pronto',
    error: '❌ Erro na separação',
    not_started: '— Não separada',
  };

  async function loadSeparations() {
    let data;
    try { data = await api.get('/api/tracks'); } catch (e) { toast(e.message, 'erro'); return; }
    const lista = $('#separations');
    lista.innerHTML = data.length
      ? data.map((x) => `
        <div class="stack-item">
          <div class="cover-sm">♪</div>
          <div class="item-info">
            <strong>${esc(x.title)}</strong>
            <small>${esc(x.artist || 'Artista desconhecido')}</small>
          </div>
          <span class="status" id="status-${esc(x.id)}" role="status">Consultando...</span>
          <button data-action="separate" data-id="${esc(x.id)}">Iniciar / atualizar</button>
        </div>`).join('')
      : '<div class="empty-state"><p>Nenhuma faixa disponível.</p></div>';
    data.forEach((x) => consultarSeparacao(x.id));
  }

  async function consultarSeparacao(id, tentativas = 40) {
    const el = document.getElementById(`status-${id}`);
    try {
      const d = await api.get(`/api/separation/${encodeURIComponent(id)}`);
      if (el) {
        el.textContent = ROTULOS[d.status] || d.message;
        el.title = d.message;
        el.className = `status ${d.status === 'ready' ? 'ready' : d.status === 'error' ? 'error' : 'andando'}`;
      }
      if (['queued', 'separating'].includes(d.status) && tentativas > 0) {
        const t = setTimeout(() => consultarSeparacao(id, tentativas - 1), 3000);
        timers.add(t);
      } else if (d.status === 'error' && el) {
        toast(d.message || 'Falha na separação.', 'erro');
      }
    } catch (e) {
      if (el) { el.textContent = '❌ Erro ao consultar'; el.className = 'status error'; }
    }
  }

  function qualidadeSeparacao() {
    return $('#separationQuality')?.value || 'equilibrada';
  }

  async function iniciarSeparacao(id, botao) {
    if (botao) { botao.disabled = true; botao.textContent = 'Iniciando...'; }
    try {
      await api.post(`/api/separation/${encodeURIComponent(id)}`, { qualidade: qualidadeSeparacao() });
      toast('Separação iniciada. Pode levar alguns minutos.', 'ok');
      consultarSeparacao(id);
    } catch (e) {
      toast(e.message, 'erro');
    } finally {
      if (botao) { botao.disabled = false; botao.textContent = 'Iniciar / atualizar'; }
    }
  }

  // ---------------------------------------------- preparar a biblioteca toda
  // Um clique para deixar prontas pra cantar todas as faixas já baixadas:
  // separa a voz (se faltar) e busca letra + afinação. O progresso por música
  // aparece na lista de baixo; a linha de resumo mostra o total de prontas.
  let prepAllIds = [];
  let prepAllTimer = null;

  function pararResumoPreparo() {
    if (prepAllTimer) { clearTimeout(prepAllTimer); prepAllTimer = null; }
  }

  async function mostrarResumoPreparo() {
    prepAllTimer = null;
    if (!prepAllIds.length) return;
    let itens = [];
    try {
      // Um pedido só para a biblioteca inteira (não uma chamada por faixa).
      itens = (await api.post('/api/playlist/status', { ids: prepAllIds })).itens || [];
    } catch (_) { /* servidor oscilou: tenta de novo no próximo ciclo */ }

    if (!itens.length) {
      prepAllTimer = setTimeout(mostrarResumoPreparo, 5000);
      return;
    }
    const prontas = itens.filter((i) => i.ready).length;
    const falhas = itens.filter((i) => i.download === 'error' || i.karaoke === 'error').length;
    const acabou = prontas + falhas >= itens.length;
    const info = $('#prepareAllInfo');
    if (info) {
      info.hidden = false;
      info.innerHTML = `🎤 <strong>${prontas} de ${itens.length}</strong> prontas pra cantar`
        + (falhas ? ` · <span class="erro">${falhas} com falha</span>` : '')
        + (acabou
            ? (falhas ? ' — veja o que aconteceu na lista abaixo.' : ' — festa pronta! 🎉')
            : ' — pode usar o app normalmente enquanto isso.');
    }
    if (acabou) loadSeparations();  // atualiza os status por música
    else prepAllTimer = setTimeout(mostrarResumoPreparo, 4000);
  }

  async function prepararBiblioteca() {
    const botao = $('#prepareAllBtn');
    const rotulo = botao ? botao.textContent : '🎤 Preparar tudo pra cantar';
    if (botao) { botao.disabled = true; botao.textContent = 'Enfileirando...'; }
    try {
      const dados = await api.post('/api/library/prepare',
        { qualidade: qualidadeSeparacao() });
      prepAllIds = (dados.faixas || []).map((f) => f.id);
      pararResumoPreparo();
      const info = $('#prepareAllInfo');
      if (!prepAllIds.length && info) {
        info.hidden = false;
        info.textContent = dados.sem_audio
          ? `Nada para preparar — ${dados.sem_audio} faixa(s) sem áudio baixado.`
          : '🎉 Todas as músicas já estão prontas pra cantar!';
      }
      const partes = [dados.enfileiradas
        ? `${dados.enfileiradas} faixa(s) entrando no preparo`
        : 'nada faltando'];
      if (dados.ja_prontas) partes.push(`${dados.ja_prontas} já estavam prontas`);
      if (dados.sem_audio) partes.push(`${dados.sem_audio} sem áudio`);
      toast(partes.join(' · '), dados.enfileiradas ? 'ok' : 'info');
      if (prepAllIds.length) mostrarResumoPreparo();
      await loadSeparations();
    } catch (e) {
      toast(e.message || 'Não consegui preparar a biblioteca.', 'erro');
    } finally {
      if (botao) { botao.disabled = false; botao.textContent = rotulo; }
    }
  }

  $('#prepareAllBtn')?.addEventListener('click', prepararBiblioteca);

  // ------------------------------------------------------------ diagnóstico
  const cardDiag = (nome, ok, valor) => `
    <div class="glass diag-card ${ok ? 'ok' : 'bad'}">
      <span aria-hidden="true">${ok ? '✅' : '⚠️'}</span>
      <strong>${esc(nome)}</strong>
      <small>${esc(valor)}</small>
    </div>`;

  async function rodarDiagnostico() {
    const botao = $('#diagnosticsButton');
    botao.disabled = true;
    botao.textContent = 'Verificando...';
    try {
      const d = await api.get('/api/diagnostics');
      const itens = [
        ['yt-dlp', !String(d.yt_dlp).includes('não'), d.yt_dlp],
        ['FFmpeg', !String(d.ffmpeg).includes('não'), d.ffmpeg],
        ['GPU (CUDA)', d.gpu, d.gpu ? 'Disponível' : 'Não disponível (CPU)'],
        ['Modelo Demucs', !String(d.demucs_model).includes('não'), d.demucs_model],
        ['Cache do modelo', true, d.demucs_cache],
      ];
      Object.entries(d.directories || {}).forEach(([nome, existe]) => {
        itens.push([`Pasta ${nome}`, existe, existe ? 'OK' : 'Ausente']);
      });
      $('#diag').innerHTML = itens.map(([n, ok, v]) => cardDiag(n, ok, v)).join('');
    } catch (e) {
      toast(e.message, 'erro');
    } finally {
      botao.disabled = false;
      botao.textContent = 'Rodar verificação';
    }
  }

  $('#diagnosticsButton').addEventListener('click', rodarDiagnostico);

  // ------------------------------------------- delegação de eventos (segura)
  document.addEventListener('click', (ev) => {
    const alvo = ev.target.closest('[data-action]');
    if (!alvo) return;
    const { action, id } = alvo.dataset;
    if (action === 'download') baixar(id, alvo);
    if (action === 'download-player') baixarNoPlayer(id, alvo);
    if (action === 'queue-search') baixarEEnfilar(id, alvo);
    if (action === 'separate') iniciarSeparacao(id, alvo);
    if (action === 'retry-separation') {
      // Tentar de novo a separação com 1 toque, direto no chip da faixa.
      const botao = alvo;
      botao.disabled = true;
      api.post(`/api/separation/${encodeURIComponent(id)}`)
        .then(() => toast('Separação reiniciada.', 'ok'))
        .catch((e) => toast(e.message, 'erro'))
        .finally(() => { loadTracks(); });
      return;
    }
    if (action === 'delete') apagarFaixa(id, alvo);
    if (action === 'favoritar') toggleFavorita(id, alvo);
    if (action === 'preview') tocarPrevia(id, alvo);
    if (action === 'explore') abrirAba('search');
    if (action === 'play') {
      const faixa = tracks.find((t) => t.id === id);
      if (faixa) tocar(faixa);
    }
  });

  // Ações da fila da festa e da biblioteca de adicionar.
  document.addEventListener('click', (ev) => {
    const botao = ev.target.closest('[data-qadd]');
    if (botao) { adicionarFesta(botao.dataset.qadd, botao); return; }
    const alvo = ev.target.closest('[data-q]');
    if (!alvo) return;
    const acao = alvo.dataset.q;
    const item = alvo.closest('.queue-item')?.dataset.item;
    if (acao === 'remove') { removerFesta(item, alvo); return; }
    if (acao === 'up' || acao === 'down') moverFesta(item, acao);
    if (acao === 'play') tocarAgora(item);
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    if (ev.target.closest('[data-action="delete"]')) return;
    const alvo = ev.target.closest('[data-action="play"]');
    if (!alvo) return;
    ev.preventDefault();
    const faixa = tracks.find((t) => t.id === alvo.dataset.id);
    if (faixa) tocar(faixa);
  });

  // ------------------------------------------------------------------ Ranking
  const CHAVE_CANTOR = 'vocalis_singer';
  const CHAVE_JOGADORES = 'vocalis_players';

  function lerCantor() {
    try { return window.localStorage.getItem(CHAVE_CANTOR) || ''; } catch (_) { return ''; }
  }

  function lerJogadores() {
    try {
      const bruto = JSON.parse(window.localStorage.getItem(CHAVE_JOGADORES) || '[]');
      return Array.isArray(bruto) ? bruto.filter((n) => typeof n === 'string' && n.trim()) : [];
    } catch (_) { return []; }
  }

  function guardarJogador(nome) {
    const limpo = String(nome || '').trim().slice(0, 40);
    if (!limpo) return;
    try { window.localStorage.setItem(CHAVE_CANTOR, limpo); } catch (_) { /* modo privado */ }
    const lista = lerJogadores().filter((n) => n.toLowerCase() !== limpo.toLowerCase());
    lista.unshift(limpo);
    try {
      window.localStorage.setItem(CHAVE_JOGADORES, JSON.stringify(lista.slice(0, 20)));
    } catch (_) { /* modo privado */ }
  }

  function guardarCantor(nome) {
    guardarJogador(nome);
  }

  function renderRanking(lista) {
    const alvo = $('#rankingList');
    if (!alvo) return;
    if (!lista.length) {
      alvo.innerHTML = '<div class="empty-state"><div class="empty-icon" aria-hidden="true">🏆</div>'
        + '<p>Ninguém cantou ainda. Seja o primeiro!</p></div>';
      return;
    }
    alvo.innerHTML = lista.map((item, i) => {
      const posicao = i + 1;
      const classeTopo = posicao <= 3 ? ` top${posicao}` : '';
      return `
        <div class="ranking-row${classeTopo}">
          <span class="ranking-pos">${posicao}º</span>
          <span class="ranking-medal" aria-hidden="true">${esc(item.medal || '🎤')}</span>
          <span class="ranking-singer">${esc(item.singer)}</span>
          <span class="ranking-track">${esc(item.track_title || '')}</span>
          <span class="ranking-score">${item.score}</span>
        </div>`;
    }).join('');
  }

  async function carregarRanking() {
    const alvo = $('#rankingList');
    if (!alvo) return;
    try {
      const dados = await fetch('/api/scores?limit=50').then((r) => r.json());
      renderRanking(Array.isArray(dados.ranking) ? dados.ranking : []);
    } catch (_) {
      alvo.innerHTML = '<div class="empty-state"><p>Não consegui carregar o ranking.</p></div>';
    }
  }

  const inputCantor = $('#singerName');
  if (inputCantor) inputCantor.value = lerCantor();

  const btnSalvarCantor = $('#saveSinger');
  if (btnSalvarCantor) {
    btnSalvarCantor.addEventListener('click', () => {
      const nome = ($('#singerName')?.value || '').trim();
      if (!nome) { toast('Digite um nome para o ranking.', 'erro'); return; }
      guardarCantor(nome);
      atualizarPerfil(nome);
      toast(`Nome salvo: ${nome}`, 'ok');
    });
  }

  const btnAtualizarRanking = $('#refreshRanking');
  if (btnAtualizarRanking) btnAtualizarRanking.addEventListener('click', carregarRanking);

  const btnZerarRanking = $('#clearRanking');
  if (btnZerarRanking) {
    btnZerarRanking.addEventListener('click', async () => {
      if (!window.confirm('Zerar o ranking de todos os cantores?')) return;
      try {
        await fetch('/api/scores', { method: 'DELETE' });
        toast('Ranking zerado.', 'ok');
        carregarRanking();
      } catch (_) {
        toast('Não consegui zerar o ranking.', 'erro');
      }
    });
  }

  // Atualiza o ranking quando uma música termina.
  document.addEventListener('karaoke:score', carregarRanking);

  // ------------------------------------------------------- rótulo do perfil
  function atualizarPerfil(nome, extra) {
    const alvo = $('#profileName');
    if (!alvo) return;
    const apelido = (nome || lerCantor() || 'Convidado').trim();
    const dica = extra || (apelido === 'Convidado' ? 'Toque para ver seu perfil' : 'No ranking');
    alvo.innerHTML = `${esc(apelido)}<small id="profileHint">${esc(dica)}</small>`;
    const avatar = document.querySelector('.profile-avatar');
    if (avatar) avatar.textContent = apelido.charAt(0).toUpperCase();
  }

  atualizarPerfil();
  document.addEventListener('karaoke:score', (ev) => {
    const dados = ev.detail || {};
    const detalhe = typeof dados.rank === 'number'
      ? `${dados.score} pts · ${dados.rank}º lugar` : '';
    atualizarPerfil(dados.singer, detalhe);
  });

  // ------------------------------------------------------- seletor de jogador
  let jogadorSelecionado = '';

  async function jogadoresConhecidos() {
    const nomes = lerJogadores();
    const vistos = new Set(nomes.map((n) => n.toLowerCase()));
    try {
      const dados = await fetch('/api/scores?limit=50').then((r) => r.json());
      (dados.ranking || []).forEach((linha) => {
        const nome = (linha.singer || '').trim();
        if (nome && !vistos.has(nome.toLowerCase())) {
          vistos.add(nome.toLowerCase());
          nomes.push(nome);
        }
      });
    } catch (_) { /* sem ranking, usa só os nomes locais */ }
    return nomes.slice(0, 20);
  }

  function marcarJogador(nome) {
    jogadorSelecionado = nome;
    $$('#playerChips .player-chip').forEach((c) => {
      c.classList.toggle('selecionado', c.dataset.nome === nome);
    });
    const input = $('#playerName');
    if (input) input.value = nome;
  }

  async function abrirSeletorJogador() {
    const modal = $('#playerModal');
    if (!modal) return;
    const chips = $('#playerChips');
    const input = $('#playerName');
    jogadorSelecionado = '';

    // Exibe imediatamente; a consulta dos nomes não pode bloquear o botão.
    modal.hidden = false;
    modal.removeAttribute('hidden');
    if (chips) chips.innerHTML = '<span class="muted">Carregando jogadores…</span>';
    const ultimo = lerCantor();
    if (input) input.value = ultimo;
    if (ultimo) jogadorSelecionado = ultimo;
    setTimeout(() => input?.focus(), 50);

    const nomes = await jogadoresConhecidos();
    if (modal.hidden) return;
    if (chips) {
      chips.innerHTML = nomes.length
        ? nomes.map((n) => `<button type="button" class="player-chip" role="listitem" data-nome="${esc(n)}">${esc(n)}</button>`).join('')
        : '<span class="muted">Nenhum jogador ainda — digite um nome abaixo.</span>';
      chips.querySelectorAll('.player-chip').forEach((c) => {
        c.addEventListener('click', () => marcarJogador(c.dataset.nome));
      });
    }
    if (ultimo) marcarJogador(ultimo);
  }

  function fecharSeletorJogador() {
    const modal = $('#playerModal');
    if (modal) {
      modal.hidden = true;
      modal.setAttribute('hidden', '');
    }
  }

  async function confirmarJogador() {
    const nome = ($('#playerName')?.value || jogadorSelecionado || '').trim();
    if (!nome) { toast('Escolha ou digite o nome do jogador.', 'erro'); return; }
    if (!current) { fecharSeletorJogador(); toast('Escolha uma faixa primeiro.', 'erro'); return; }
    if (!window.Karaoke || typeof window.Karaoke.start !== 'function') {
      toast('O módulo de karaokê ainda não carregou. Atualize a página.', 'erro');
      return;
    }
    guardarJogador(nome);
    fecharSeletorJogador();
    try {
      await window.Karaoke.start(current);
    } catch (erro) {
      toast(erro.message || 'Não consegui iniciar o modo karaokê.', 'erro');
    }
  }

  const modalJogador = $('#playerModal');
  if (modalJogador) {
    $('#playerConfirm')?.addEventListener('click', confirmarJogador);
    $('#playerCancel')?.addEventListener('click', fecharSeletorJogador);
    $('#playerName')?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); confirmarJogador(); }
    });
    // Fecha ao clicar fora do cartão.
    modalJogador.addEventListener('click', (e) => {
      if (e.target === modalJogador) fecharSeletorJogador();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !modalJogador.hidden) fecharSeletorJogador();
    });
  }

  // ------------------------------------------------------------------ Festa
  // Fila compartilhada + controle remoto via WebSocket. O anfitrião (PC)
  // reproduz o áudio pelas caixas; os convidados (celulares pelo QR Code)
  // pedem músicas, reordenam e comandam o player em tempo real.
  let wsFesta = null;
  let filaFesta = [];
  let timerFesta = null;
  let reconexaoFesta = 0;

  function ehConvidado() {
    try { return window.localStorage.getItem('vocalis_guest') === '1'; } catch (_) { return false; }
  }

  function aplicarModoAcesso() {
    const params = new URLSearchParams(window.location.search);
    const modo = params.get('convidado');
    try {
      if (modo === '1') window.localStorage.setItem('vocalis_guest', '1');
      else if (modo === '0') window.localStorage.setItem('vocalis_guest', '0');
    } catch (_) { /* modo privado */ }
    const convidado = ehConvidado();
    document.body.classList.toggle('convidado', convidado);
    const banner = $('#guestBanner');
    if (banner) banner.hidden = !convidado;
    if (convidado) {
      // Microcopy de convidado: tudo toca no notebook do anfitrião.
      const heroP = document.querySelector('.welcome-hero p');
      if (heroP) heroP.textContent = 'Você está no Modo Convidado. Escolha as músicas e elas tocam no som do anfitrião — só cantar!';
      const cta = $('#startPartyBtn');
      if (cta) cta.textContent = 'Escolher músicas para a festa';
    }
  }

  function atualizarContagem(dados) {
    const total = dados && dados.total != null ? dados.total : 0;
    const pill = $('#partyGuests');
    if (pill) pill.textContent = `${total} ${total === 1 ? 'aparelho' : 'aparelhos'} conectado${total === 1 ? '' : 's'}`;
  }

  function atualizarPill(conectado) {
    const pill = $('#partyConn');
    if (!pill) return;
    pill.textContent = conectado ? '● conectado' : '● desconectado';
    pill.classList.toggle('off', !conectado);
  }

  function atualizarContadorRemoto(msg) {
    if (msg.cmd === 'volume') {
      const v = Math.min(1, Math.max(0, Number(msg.value) || 0));
      const slider = $('#rcVolume');
      if (slider) slider.value = String(v);
      const saida = $('#rcVolumeOut');
      if (saida) saida.textContent = `${Math.round(v * 100)}%`;
    }
  }

  function tratarMensagemFesta(msg) {
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'welcome') {
      if (Array.isArray(msg.queue)) { filaFesta = msg.queue; renderFila(); }
      atualizarContagem(msg);
    } else if (msg.type === 'queue') {
      if (Array.isArray(msg.items)) { filaFesta = msg.items; renderFila(); }
    } else if (msg.type === 'clients') {
      atualizarContagem(msg);
    } else if (msg.type === 'control') {
      if (!ehConvidado()) aplicarControle(msg.cmd, msg.value);
      atualizarContadorRemoto(msg);
    }
  }

  function conectarFesta() {
    if (wsFesta && wsFesta.readyState <= 1) return;
    const protocolo = window.location.protocol === 'https:' ? 'wss' : 'ws';
    try {
      wsFesta = new WebSocket(`${protocolo}://${window.location.host}/ws/party`);
    } catch (_) {
      atualizarPill(false);
      return;
    }
    wsFesta.onopen = () => {
      reconexaoFesta = 0;
      atualizarPill(true);
      enviarFesta({ type: 'join', papel: ehConvidado() ? 'guest' : 'host', nome: lerCantor() });
    };
    wsFesta.onmessage = (evento) => {
      try { tratarMensagemFesta(JSON.parse(evento.data)); } catch (_) { /* json inválido ignorado */ }
    };
    wsFesta.onclose = () => {
      atualizarPill(false);
      if (!timerFesta) {
        reconexaoFesta += 1;
        timerFesta = setTimeout(() => { timerFesta = null; conectarFesta(); },
          Math.min(10000, 1500 * reconexaoFesta));
      }
    };
    wsFesta.onerror = () => { try { wsFesta.close(); } catch (_) { /* já fechado */ } };
  }

  function enviarFesta(mensagem) {
    if (wsFesta && wsFesta.readyState === WebSocket.OPEN) {
      try { wsFesta.send(JSON.stringify(mensagem)); } catch (_) { /* usado em testes */ }
    }
  }

  function enviarControle(cmd, value) {
    // No anfitrião o comando age no player local e ainda é publicado; os
    // convidados apenas publicam e o anfitrião executa.
    if (!ehConvidado()) aplicarControle(cmd, value);
    enviarFesta({ type: 'control', cmd, value, de: lerCantor() || 'convidado' });
  }

  function aplicarControle(cmd, value) {
    switch (cmd) {
      case 'play':
        if (current) audio.play().catch(() => {});
        break;
      case 'pause':
        audio.pause();
        break;
      case 'toggle':
        if (!current) break;
        if (audio.paused) audio.play().catch(() => {});
        else audio.pause();
        break;
      case 'stop':
        audio.pause();
        audio.currentTime = 0;
        break;
      case 'next':
        if (filaFesta.some((i) => i.status === 'pending')) tocarProxima();
        else mover(1);
        break;
      case 'prev':
        mover(-1);
        break;
      case 'seek':
        if (audio.duration) {
          audio.currentTime = Math.min(1, Math.max(0, Number(value))) * audio.duration;
        }
        break;
      case 'volume':
        definirVolume(Number(value));
        break;
      case 'karaoke':
        $('#karaokeButton')?.click();
        break;
    }
  }

  async function tocarProxima() {
    try {
      const dados = await api.post('/api/queue/next');
      const item = dados.item;
      const faixa = tracks.find((t) => t.id === item.track_id)
        || { id: item.track_id, title: item.title, artist: item.artist, thumbnail: item.thumbnail };
      tocar(faixa);
      toast(`Tocando agora: ${item.title}`, 'ok');
      await recarregarFila();
    } catch (e) {
      toast(e.message || 'A fila está vazia.', 'erro');
    }
  }

  function renderFilaAgora(tocando) {
    const alvo = $('#partyNow');
    if (!alvo) return;
    alvo.classList.toggle('empty', !tocando);
    alvo.innerHTML = tocando
      ? `${tocando.thumbnail ? `<img class="now-track-img" src="${esc(tocando.thumbnail)}" alt="" loading="lazy">` : '<span class="now-track-placeholder">♪</span>'}
      <span class="now-track-info">
        <span class="now-badge">● tocando</span>
        <strong>${esc(tocando.title)}</strong>
        <small>${esc(tocando.artist || '')} · pedida por ${esc(tocando.added_by || 'convidado')}</small>
      </span>`
      : '<span class="now-track-placeholder">♪</span><p>Nada tocando — escolham em "Próximas".</p>';
  }

  function renderFila() {
    const ativos = filaFesta.filter((i) => i.status !== 'played');
    renderFilaAgora(ativos.find((i) => i.status === 'playing') || null);
    const pendentes = ativos.filter((i) => i.status === 'pending');
    const alvo = $('#partyList');
    if (!alvo) return;
    if (!pendentes.length) {
      alvo.innerHTML = '<div class="empty-state"><div class="empty-icon" aria-hidden="true">🎉</div>'
        + '<p>A fila está vazia. Adicione músicas da biblioteca ao lado.</p></div>';
      return;
    }
    alvo.innerHTML = pendentes.map((item) => `
      <div class="queue-item" data-item="${esc(item.item_id)}">
        ${item.thumbnail ? `<img class="track-thumb" src="${esc(item.thumbnail)}" alt="" loading="lazy">` : '<span class="track-thumb-placeholder">♪</span>'}
        <span class="queue-info">
          <strong>${esc(item.title)}</strong>
          <small>${esc(item.artist || 'Artista desconhecido')} · por ${esc(item.added_by || 'convidado')}</small>
        </span>
        <span class="queue-actions">
          <button class="mini" data-q="down" aria-label="Descer ${esc(item.title)}" title="Descer">↓</button>
          <button class="mini" data-q="up" aria-label="Subir ${esc(item.title)}" title="Subir">↑</button>
          <button class="mini primary" data-q="play" aria-label="Tocar agora ${esc(item.title)}" title="Tocar agora">▶</button>
          <button class="mini danger" data-q="remove" aria-label="Remover ${esc(item.title)}" title="Remover da fila">🗑</button>
        </span>
      </div>`).join('');
    renderFilaPlayer();
  }

  function renderFilaPlayer() {
    const alvo = $('#playerQueueList');
    if (!alvo) return;
    const pendentes = filaFesta.filter((i) => i.status === 'pending');
    if (!pendentes.length) {
      alvo.innerHTML = '<div class="empty-state"><div class="empty-icon" aria-hidden="true">🎉</div>'
        + '<p>A fila está vazia. Adicione músicas pela busca.</p></div>';
      return;
    }
    alvo.innerHTML = pendentes.map((item, indice) => `
      <div class="queue-item" data-item="${esc(item.item_id)}">
        <span class="queue-num">${indice + 1}</span>
        <span class="queue-info">
          <strong>${esc(item.title)}</strong>
          <small>${esc(item.artist || 'Artista desconhecido')} · ${esc(item.added_by || 'convidado')}</small>
        </span>
        <span class="queue-actions">
          <button class="mini primary" data-q="play" aria-label="Tocar agora ${esc(item.title)}" title="Tocar agora">▶</button>
          <button class="mini danger" data-q="remove" aria-label="Remover ${esc(item.title)}" title="Remover da fila">✕</button>
        </span>
      </div>`).join('');
  }

  async function recarregarFila() {
    try {
      const dados = await api.get('/api/queue');
      filaFesta = Array.isArray(dados.items) ? dados.items : [];
      renderFila();
      atualizarContagem(dados);
    } catch (e) {
      toast(e.message, 'erro');
    }
  }

  async function adicionarFesta(id, botao) {
    const nome = lerCantor() || 'Convidado';
    if (botao) { botao.disabled = true; botao.textContent = '…'; }
    try {
      await api.post('/api/queue', { track_id: id, added_by: nome });
      toast('Adicionada à fila da festa!', 'ok');
      await recarregarFila();
    } catch (e) {
      toast(e.message, 'erro');
    } finally {
      if (botao) { botao.disabled = false; botao.textContent = '＋'; }
    }
  }

  async function removerFesta(item, botao) {
    if (!item) return;
    if (botao) botao.disabled = true;
    try {
      await api.del(`/api/queue/${encodeURIComponent(item)}`);
      await recarregarFila();
    } catch (e) {
      toast(e.message, 'erro');
    }
  }

  async function moverFesta(item, direcao) {
    if (!item) return;
    try {
      await api.post(`/api/queue/${encodeURIComponent(item)}/move`, { direcao });
      await recarregarFila();
    } catch (e) {
      toast(e.message, 'erro');
    }
  }

  async function tocarAgora(item) {
    if (!item) return;
    try {
      await api.post(`/api/queue/${encodeURIComponent(item)}/play`);
      await recarregarFila();
    } catch (e) {
      toast(e.message, 'erro');
    }
  }

  function renderBibliotecaFesta() {
    const alvo = $('#partyLibrary');
    if (!alvo) return;
    const termo = ($('#partySearch')?.value || '').trim().toLowerCase();
    const visiveis = termo
      ? (tracks || []).filter((t) => `${t.title} ${t.artist || ''}`.toLowerCase().includes(termo))
      : (tracks || []).slice(0, 30);
    if (!visiveis.length) {
      alvo.innerHTML = '<p class="muted">Nenhuma faixa na biblioteca. Busque e baixe primeiro.</p>';
      return;
    }
    alvo.innerHTML = visiveis.map((t) => `
      <div class="lib-row">
        ${t.thumbnail ? `<img class="track-thumb" src="${esc(t.thumbnail)}" alt="" loading="lazy">` : '<span class="track-thumb-placeholder">♪</span>'}
        <span class="track-row-info"><strong>${esc(t.title)}</strong><small>${esc(t.artist || 'Artista desconhecido')}</small></span>
        <button class="mini primary" data-qadd="${esc(t.id)}" title="Colocar na fila" aria-label="Colocar na fila ${esc(t.title)}">＋</button>
      </div>`).join('');
  }

  // Convidado: baixa (se preciso) e já coloca a música na fila da festa.
  async function baixarEEnfilar(id, botao) {
    const faixa = resultados.find((x) => x.id === id);
    if (!faixa) return;
    if (tracks.some((t) => t.id === id)) { adicionarFesta(id); return; }
    await baixarFaixa(faixa, botao, () => adicionarFesta(id));
  }

  $('#partySearch')?.addEventListener('input', renderBibliotecaFesta);
  $('#rcToggle')?.addEventListener('click', () => enviarControle('toggle'));
  $('#rcPrev')?.addEventListener('click', () => enviarControle('prev'));
  $('#rcNext')?.addEventListener('click', () => enviarControle('next'));
  $('#rcVolume')?.addEventListener('input', (e) => {
    const v = Number(e.target.value);
    const saida = $('#rcVolumeOut');
    if (saida) saida.textContent = `${Math.round(v * 100)}%`;
    enviarControle('volume', v);
  });
  $('#rcOpen')?.addEventListener('click', () => enviarControle('karaoke'));
  $('#partyNext')?.addEventListener('click', tocarProxima);
  $('#partyClear')?.addEventListener('click', async () => {
    if (!window.confirm('Limpar toda a fila de músicas pendentes?')) return;
    try {
      await api.del('/api/queue');
      toast('Fila limpa.', 'ok');
      await recarregarFila();
    } catch (e) {
      toast(e.message, 'erro');
    }
  });

  // ------------------------------------------------------------------ QR Code
  // Desenha o QR Code localmente (static/vendor), sem depender de internet.
  // Quando o computador abre em localhost, o QR aponta para o IP da rede local,
  // para que os celulares consigam acessar. Assim o PC pode continuar em
  // localhost e o microfone/pontuação seguem funcionando (contexto seguro).
  async function initQrCode() {
    const alvo = $('#qrCode');
    const qrLabel = $('#qrUrlLabel');
    const qrHint = $('#qrHint');
    const origem = window.location.origin || `${window.location.protocol}//${window.location.host}`;
    const ehLocalhost = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origem);

    let urlQrBase = origem;
    if (ehLocalhost) {
      try {
        const rede = await fetch('/api/network').then((r) => r.json());
        if (rede && rede.url) urlQrBase = rede.url;
      } catch (_) { /* sem rede: mantém localhost */ }
    }
    // Quem escaneia entra já no Modo Convidado (fila + controle remoto).
    const urlQr = `${urlQrBase}?convidado=1`;

    if (qrLabel) qrLabel.textContent = urlQrBase;
    if (qrHint) {
      qrHint.textContent = ehLocalhost
        ? 'No computador use localhost para ser o anfitrião; no celular escaneie este QR.'
        : 'Escaneie com a câmera do celular e entre no Modo Festa.';
    }

    if (!alvo) return;

    // Se ainda restou um endereço local, o celular não alcança: apenas avisa.
    if (/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(urlQrBase)) {
      alvo.innerHTML = '<p class="qr-aviso">Não encontrei o IP da rede. '
        + 'Acesse pelo IP do computador (ex.: <code>http://192.168.0.10:8099</code>).</p>';
      return;
    }

    try {
      // typeNumber 0 = escolhe automaticamente a versão; nível M de correção.
      const gerador = window.qrcode(0, 'M');
      gerador.addData(urlQr);
      gerador.make();
      alvo.innerHTML = gerador.createSvgTag({ cellSize: 8, margin: 2, scalable: true });
      const svg = alvo.querySelector('svg');
      if (svg) {
        svg.setAttribute('width', '220');
        svg.setAttribute('height', '220');
      }
    } catch (erro) {
      // Se a geração falhar, ainda mostramos o endereço para acesso manual.
      alvo.innerHTML = '<p class="qr-aviso">Não foi possível gerar o QR.<br>'
        + `Acesse no celular: <code>${esc(urlQr)}</code></p>`;
    }
  }

  const startPartyBtn = $('#startPartyBtn');
  if (startPartyBtn) {
    startPartyBtn.addEventListener('click', () => abrirAba('search'));
  }

  // Limpa os timers de polling ao trocar de aba (evita vazamento).
  window.addEventListener('beforeunload', () => timers.forEach(clearTimeout));

  aplicarModoAcesso();
  initQrCode();
  abrirAba('welcome');
  window.KaraokeStudio = {
    toast, tracks: () => tracks, audio, current: () => current,
    tocar, proxima: () => mover(1), anterior: () => mover(-1),
    definirVolume, aplicarControle, adicionarFesta, tocarProxima,
  };
}());
