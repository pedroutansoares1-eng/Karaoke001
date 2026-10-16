# Karaokê de Casa 🎤

Um karaokê caseiro que roda no seu PC. Ele baixa a música, separa a voz do
instrumental, busca a letra sincronizada e monta a afinação de referência —
deixando tudo pronto **antes** da festa começar. Os convidados entram pelo
celular (QR Code) só para escolher as músicas na fila; **o microfone fica no
PC**.

<!-- Se você tiver prints, coloque-os aqui: docs/tela-karaoke.png etc. -->

## O que ele faz

- **Busca e baixa** músicas do YouTube / YouTube Music (via yt-dlp), com filtros
  para versão de estúdio, ao vivo, instrumental ou karaokê.
- **Separa a voz do instrumental** com Demucs (`htdemucs`), em três qualidades
  (rápida, equilibrada e alta), usando GPU quando disponível e caindo para a CPU
  se não estiver.
- **Busca a letra sincronizada** e, quando a letra encontrada não cobre a música
  inteira, completa o final com Whisper.
- **Monta a afinação de referência** (librosa) para dar nota enquanto se canta.
- **Player de karaokê** em tela cheia, com pontuação, ranking e gravação de voz
  mixada no instrumental (renderizada pelo FFmpeg).
- **Fila da festa** com WebSocket: o anfitrião controla, os convidados só
  escolhem músicas. QR Code na tela para o pessoal entrar.
- **Baixar playlist inteira** de uma vez, e o **"modo festa"**: baixar →
  separar → letra → afinação, deixando cada música pronta pra cantar.
- **"Preparar tudo pra cantar"**: um clique para preparar toda a sua biblioteca.
- **Funciona offline** como PWA depois de aberto (service worker + cache).
- **Proteção de anfitrião**: baixar, separar, preparar e apagar só funcionam a
  partir do próprio PC — um convidado na rede não consegue disparar nada disso.

## Como funciona

### O preparo de uma música

```
baixar (yt-dlp)  →  separar a voz (Demucs)  →  letra (sincronizada)  →  afinação
```

Cada etapa tem estado próprio e é retomável: nada é refeito se já existe. As
tarefas rodam numa fila com no máximo **2 trabalhos simultâneos**, para não
afogar a máquina enquanto a festa acontece.

### A ordem em que a letra é procurada

1. `.lrc` local que já esteja no disco
2. `syncedlyrics`
3. **LRCLIB** (escolhe o candidato sincronizado mais completo)
4. **Whisper** (só para completar o *final*, quando a letra humana não cobre a música)
5. Vagalume
6. lyrics.ovh

Encontrando uma letra feita por humano, ela é mantida — o Whisper nunca
sobrescreve o que já existe, apenas completa o final que faltou.

### Modo festa

A caixa de playlist aceita uma URL de playlist/álbum. Ela é expandida sem
baixar nada (`extract_flat`), e então **uma tarefa de download por faixa** entra
na fila — reaproveitando o progresso por música e pulando o que você já tem.
Com a opção de preparar marcada, cada faixa segue automaticamente para separação,
letra e afinação.

## Requisitos

- **Python 3.12**
- **FFmpeg** no PATH (conversão de áudio)
- Sistema com **4 GB+ de RAM** por trabalho de separação
- **GPU NVIDIA é opcional** — acelera bem a separação e o Whisper; sem ela tudo
  roda na CPU (mais lento)

Os modelos (Demucs e Whisper) são baixados na primeira execução.

## Instalação

```bash
git clone https://github.com/pedroutansoares1-eng/Karaoke001.git karaoke-studio
cd karaoke-studio

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

./start.sh
```

Abra **http://localhost:8000** no PC.

> O servidor roda **sem `--reload`** de propósito: recarregar derruba as conexões
> WebSocket da festa e a música para no meio. Para desenvolver, use
> `RECARGA=1 ./start.sh`.

## Como usar

1. Abra o app no PC e vá em **Buscar**.
2. Procure a música, escolha a qualidade da separação e clique em **Baixar**
   (ou cole uma playlist inteira para baixar tudo de uma vez).
3. Em **Separar**, acompanhe o andamento — ou use **Preparar tudo pra cantar**
   para deixar a biblioteca inteira pronta.
4. Na **Festa**, o QR Code aparece para os convidados entrarem.
5. Escolha a música na fila e cante. 🎤

### Fila da festa e convidados

- O **anfitrião** é quem abre o app por `localhost` no PC: controla a reprodução,
  o volume e a ordem da fila.
- Os **convidados** entram pelo endereço da rede (o do QR Code), caem no modo
  convidado e só **adicionam músicas** à fila. Eles não têm microfone, não abrem
  o player nem disparam download/separação.
- O microfone é capturado **apenas no PC**.

## Segurança

Este app foi feito para **rede local doméstica** e **não tem autenticação**.
Qualquer aparelho na sua rede alcança a API. Por isso:

- **Não exponha este servidor na internet** (sem port forwarding, sem túnel).
- As ações pesadas ou destrutivas — baixar, separar, preparar, apagar música,
  limpar ranking e controlar a festa — são **recusadas para quem não vem do
  próprio PC** (veja `app/anfitriao.py`). Os convidados mantêm só o que a festa
  precisa: buscar, ouvir prévia e mexer na fila.
- O arquivo `data/` e os `cookies*.txt`/`cookies*.json` (sessão do yt-dlp) ficam
  fora do controle de versão pelo `.gitignore`. **Não os publique.**

## Antes de publicar

O `.gitignore` mantém fora do repositório o `data/`, os cookies de sessão, o
banco e os ambientes virtuais. Mas ignore não protege contra um `git add -f`.
Por isso existe uma trava em `.githooks/pre-commit`, que roda a cada commit e
**recusa** o que não pode ser publicado: cookies, `.env`, banco, `data/`,
`venv`, arquivo acima de 5 MB ou qualquer informação pessoal (caminho da
máquina, IP da rede local, nome do dono).

Ela se ativa uma vez por clone:

```bash
git config core.hooksPath .githooks
```

Para ver a trava em ação, prepare algo proibido e tente commitar:

```bash
git add -f cookies.txt
git commit -m "teste"     # deve ser bloqueado
```

E, se for um caso que você conhece e quer mesmo assim:
`git commit --no-verify`.

## API

Tudo em JSON, servido pelo FastAPI. Os principais:

| Método | Rota | Para quê |
|---|---|---|
| `POST` | `/api/search` | buscar músicas |
| `POST` | `/api/preview` | ouvir uma prévia |
| `POST` | `/api/download` | baixar uma música (preparo opcional) |
| `POST` | `/api/playlist` | baixar/expandir uma playlist |
| `POST` | `/api/playlist/status` | estado de várias faixas num pedido só |
| `POST` | `/api/library/prepare` | preparar a biblioteca inteira |
| `POST` | `/api/separation/{id}` | separar a voz |
| `POST` | `/api/karaoke/{id}/prepare` | preparar letra + afinação |
| `GET` | `/api/karaoke/{id}` | letra sincronizada e afinação |
| `GET` | `/api/tracks` | biblioteca |
| `GET` | `/api/jobs` | fila de tarefas |
| `POST` | `/api/queue` | adicionar à fila da festa |
| `POST` | `/api/party/control` | controlar a reprodução (anfitrião) |
| `WS` | `/ws/party` | sincronizar a festa em tempo real |
| `GET` | `/api/network` | endereço da rede para o QR Code |

## Testes

A suíte cobre a API inteira **sem tocar na rede** (o yt-dlp é dublado) e sem a
stack de ML (`conftest.py` injeta stubs leves quando torch/librosa não existem).

```bash
pip install pytest httpx
python -m pytest -q
```

## Estrutura

```
main.py              API, orquestração do preparo e da biblioteca
app/
  lyrics.py          letra sincronizada, LRCLIB e completação com Whisper
  pitch.py           afinação de referência (librosa)
  party.py           fila da festa, papéis de anfitrião/convidado e WebSocket
  anfitriao.py       quem pode disparar ações pesadas (o PC do karaokê)
  tasks.py           fila de tarefas com limite de trabalhos simultâneos
static/              frontend sem build: app.js, karaoke.js, offline.js, CSS
test_api.py          testes da API
start.sh             inicialização
.githooks/           trava que impede publicar segredo ou dado pessoal
LICENSE              licença MIT
```

O frontend é JavaScript puro, sem passo de build: editar um arquivo e recarregar
já basta.

## Créditos

Em pé sobre ombros de gigantes: [FastAPI](https://fastapi.tiangolo.com/),
[yt-dlp](https://github.com/yt-dlp/yt-dlp),
[Demucs](https://github.com/facebookresearch/demucs),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper),
[librosa](https://librosa.org/), [syncedlyrics](https://github.com/rtcq/syncedlyrics)
e [LRCLIB](https://lrclib.net/). No navegador:
[pitchy](https://github.com/ianprime0509/pitchy) (MIT) e
[qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) (MIT).

## Licença

[MIT](LICENSE).

## Aviso

Baixe apenas conteúdo que você tem o direito de usar, e respeite os termos de
serviço das plataformas de origem. Este projeto é para uso pessoal, na sua casa.
