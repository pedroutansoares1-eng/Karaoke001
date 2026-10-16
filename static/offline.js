/* Armazenamento offline via IndexedDB.
 *
 * Guarda os dados reutilizáveis do app (lista de faixas e payload do karaokê:
 * letra + afinação) para o app continuar útil sem internet. O service worker
 * cuida do "casco" da página e dos áudios; este módulo cuida dos dados. */
(function () {
  'use strict';

  const NOME_BD = 'vocalis-offline';
  const LOJA = 'dados';
  const PREFIXO_KARAOKE = 'karaoke-';
  let abertura = null;

  function abrir() {
    if (abertura) return abertura;
    if (!('indexedDB' in window)) {
      abertura = Promise.reject(new Error('IndexedDB indisponível.'));
      return abertura;
    }
    abertura = new Promise((resolve, reject) => {
      const pedido = window.indexedDB.open(NOME_BD, 1);
      pedido.onupgradeneeded = () => {
        if (!pedido.result.objectStoreNames.contains(LOJA)) {
          pedido.result.createObjectStore(LOJA);
        }
      };
      pedido.onsuccess = () => resolve(pedido.result);
      pedido.onerror = () => { abertura = null; reject(pedido.error); };
    });
    return abertura;
  }

  function transacao(modo, operacao) {
    return abrir().then((bd) => new Promise((resolve, reject) => {
      const tx = bd.transaction(LOJA, modo);
      const loja = tx.objectStore(LOJA);
      const pedido = operacao(loja);
      pedido.onsuccess = () => resolve(pedido.result);
      pedido.onerror = () => reject(pedido.error);
    }));
  }

  const VocalisOffline = {
    salvar(chave, valor) { return transacao('readwrite', (loja) => loja.put(valor, chave)); },
    ler(chave) { return transacao('readonly', (loja) => loja.get(chave)); },
    remover(chave) { return transacao('readwrite', (loja) => loja.delete(chave)); },
  };

  Object.defineProperty(VocalisOffline, 'chaveKaraoke', {
    get() { return (id) => `${PREFIXO_KARAOKE}${String(id || '').slice(0, 64)}`; },
  });

  window.VocalisOffline = VocalisOffline;
}());