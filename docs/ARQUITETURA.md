# Arquitetura

## Linha do tempo única

Tudo se alinha num relógio só: **`t` = segundos desde o início da gravação**. O gravador anota o horário (UTC) em que começou, e cada mensagem de chat, palavra transcrita e janela de áudio é convertida para `t`. Um momento é sempre um par `(início, fim)` nessa escala.

O chat reage alguns segundos *depois* do que aconteceu (tempo de ver + digitar). Por isso o pico do chat é recuado `detect.reaction_delay_seconds` (padrão 8 s) e o candidato ganha `clip.pre_roll_seconds` de contexto antes. Esse atraso é calibrado na fase 2, comparando os picos com momentos marcados na mão.

## 1. Captura — `ingest/`

```
streamlink --stdout --twitch-disable-ads <url> best
  | ffmpeg -i pipe:0 -c copy -f segment -segment_time 60 -reset_timestamps 1
           -segment_list segments.csv seg_%05d.ts
```

- **Sem reencode** (`-c copy`): gravar custa quase nada de CPU.
- **Segmentos `.ts` de 60 s**: se algo cair, o que já foi gravado continua íntegro; o que passou de `capture.retention_hours` e não virou corte é apagado; o `segments.csv` guarda início e fim de cada segmento.
- **Reconexão**: se a live cair e voltar, começa um novo grupo de segmentos com novo horário de início.
- **Chat**: Twitch por IRC anônimo (`irc.chat.twitch.tv`, nick `justinfan<número>`, sem login); YouTube via `chat-downloader`; Kick (websocket) fica para depois. Cada mensagem vira `(t, autor, texto)`.
- **VOD** (fase 2): `yt-dlp` baixa o vídeo; o chat vem do `live_chat` do YouTube ou do `chat-downloader` (Twitch). Assim dá para desenvolver e calibrar sem esperar live.

## 2. Detecção — `detect/`

### Sinais baratos, o tempo todo

Séries em janelas de `detect.window_seconds` (5 s):

| Sinal | Cálculo | Observação |
|---|---|---|
| `chat` | mensagens por janela, em z-score contra a média móvel de `baseline_minutes` | conta autores únicos (spam de uma pessoa não pesa); mensagens de risada/hype (`KEKW`, `LUL`, `kkkk`, `Pog`, "clipa") valem mais |
| `audio` | volume (RMS) por janela, em z-score | grito, risada, susto; depois dá para trocar por um classificador de som (risada, aplauso) |
| `viewer_clips` | clipes criados pelos viewers (Twitch API `GET /helix/clips`) | sinal raro e forte: a audiência já validou |

O score é a soma ponderada (`detect.weights`). Picos acima de `trigger_zscore` viram candidatos; candidatos que se sobrepõem se fundem, e há distância mínima entre cortes (`min_gap_seconds`) e teto por hora (`max_clips_per_hour`).

Em paralelo, o `faster-whisper` transcreve cada segmento assim que ele fecha. A transcrição alimenta o Claude e é reaproveitada na legenda.

### Claude, só nos picos

Para cada candidato, o Claude recebe ~2 min de transcrição em volta do pico, com a marcação de onde o chat explodiu, e devolve uma saída estruturada:

- `is_clip`, `score` (0–10) e o motivo
- `start`/`end` ajustados para começar e terminar em frases completas, entre `clip.min_seconds` e `clip.max_seconds`
- `title`, `description` e `hashtags` em pt-BR

Modelo em `llm.model` (padrão `claude-opus-5-5`), via SDK oficial `anthropic`. Os critérios do que é um bom corte ficam num prompt de sistema estável; cada chamada muda só a transcrição. Para VODs, dá para mandar tudo pela Batch API (metade do preço, sem pressa).

Por que não mandar a live inteira para o LLM: custo e latência. Os sinais baratos descartam a maior parte do tempo e o Claude julga só o que já parece interessante. Estimativa grosseira: poucos dólares por live de 4 h com os parâmetros padrão.

## 3. Render — `render/`

Uma única passada de `ffmpeg` por short: corta → reenquadra → legenda → normaliza o áudio → codifica.

**Corte.** Hoje (fase 1) é `-ss`/`-t` direto no arquivo de entrada. Na live (fase 3) o trecho pode atravessar segmentos: monta-se uma lista `concat` com os segmentos envolvidos e aplica-se `-ss`/`-t` relativos ao primeiro. Como o vídeo é reencodado de qualquer jeito, o corte sai exato no frame.

**Pasta de trabalho.** O ffmpeg roda com uma pasta temporária como diretório atual, e a legenda (`captions.ass`), as fontes (`fonts/`) e o roteiro do recorte (`face.cmd`) entram por caminho relativo. Caminho do Windows (`C:\...`) dentro de um filtergraph exige escapes frágeis.

**Layouts 9:16 (1080×1920)** — `render.layout`:

| Layout | Ideal para | Como |
|---|---|---|
| `blur` (padrão) | qualquer coisa | vídeo inteiro no centro; o fundo é o próprio vídeo ampliado e desfocado |
| `center` | ação no meio da tela | recorte 9:16 central |
| `split` | gameplay com facecam | facecam (`render.facecam` = x, y, w, h no original) em cima, gameplay embaixo |
| `face` | podcast, just chatting | segue o rosto (detalhes abaixo) |

**Layout `face`** (`render/facetrack.py`):
1. O ffmpeg entrega 4 quadros por segundo, reduzidos para 640 px, e o detector YuNet (OpenCV Zoo, baixado na primeira vez e conferido por SHA-256) acha os rostos.
2. Segue o maior rosto; entre rostos de tamanho parecido, fica com o mais perto do anterior, para não pular entre duas pessoas.
3. Instantes sem rosto herdam a posição vizinha; uma mediana móvel de 5 amostras descarta detecções erradas isoladas.
4. Zona morta: o recorte só se mexe quando o rosto se afasta mais de 12% da largura do recorte. Aí ele vai atrás, numa panorâmica com velocidade limitada.
5. Salto maior que 25% da largura do quadro, se continuar na amostra seguinte (troca de plano ou de pessoa), vira corte seco.
6. A posição de cada quadro vai para um roteiro do filtro `sendcmd`, que move o `crop@face` durante o encode.

Filtro do `blur`:

```
[0:v]split[a][b];
[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20[bg];
[b]scale=1080:-2[fg];
[bg][fg]overlay=(W-w)/2:(H-h)/2
```

**Legendas** (`render/captions.py`).
- `faster-whisper` com `word_timestamps=True` e filtro de voz (VAD), no modelo `captions.whisper_model`, idioma `pt`. Com `captions.device: auto` tenta a GPU e, se faltarem as DLLs do CUDA, cai para a CPU e avisa.
- Arquivo `.ass` estilo shorts: blocos de até `words_per_line` palavras (pausa > 0,6 s ou fim de frase começam outro bloco), em maiúsculas, fonte grossa com contorno, a palavra falada em destaque (`highlight_color`) com um "pop" de 112%.
- Posição: `margin_bottom` px acima da borda, fora da área coberta pela interface do TikTok/Reels (legenda do post, botões).
- Queimada com o filtro `ass`; a fonte (Montserrat ExtraBold, licença OFL) vem dentro do pacote, em `src/autocortes/assets/fonts`, e é copiada para a pasta de trabalho.

**Codificação.** H.264, `yuv420p`, 30 fps, AAC 48 kHz, `-movflags +faststart`, áudio normalizado com `loudnorm` para -14 LUFS. É um perfil aceito pelas três plataformas. Com `render.encoder: auto`, um teste de um quadro decide entre `h264_nvenc` (GPU) e `libx264`.

## 4. Revisão

Cada short renderizado entra numa fila. Na CLI: listar, assistir, aprovar, editar o texto, descartar; na fase 5 vira um painel web. Com `publish.manual_review: false`, o que o detector aprova é publicado direto.

## 5. Publicação — `publish/`

Um publicador por plataforma, com a mesma interface (`publish(short) -> PostResult`, tipos em `models.py`). Os três rodam em paralelo (`asyncio`), cada um com retry e backoff próprios: falha em um não segura os outros. Instagram e TikTok processam o vídeo de forma assíncrona, então o publicador acompanha o status até o fim.

Cada plataforma tem teto diário (o TikTok, em geral ~15 posts por conta), menor do que uma live longa pode render. O publicador respeita o teto de cada uma e agenda o excedente para o dia seguinte. Regras e limites de cada API: [PLATAFORMAS.md](PLATAFORMAS.md).

## Estado

SQLite em `data/autocortes.db`:

```
streams   id, url, platform, started_at_utc, status
segments  stream_id, idx, path, t_start, t_end
moments   id, stream_id, t_start, t_end, score, signals, reason, status
shorts    id, moment_id, path, layout, title, description, hashtags, status
posts     short_id, platform, status, post_id, url, error, published_at
```

Ciclo de vida: `detected → rendered → approved → published` (ou `rejected` / `failed`). Cada etapa só consome o que a anterior deixou com o status certo, então o processo pode parar e retomar sem perder nada.

## Esta máquina

Medido na fase 1 (i5-11400, 16 GB, GTX 1650 4 GB, driver NVIDIA 581.80):

- **Short de 18 s em 22 s**, contando carregar o Whisper, transcrever na CPU (`large-v3-turbo` em int8) e renderizar com `libx264`.
- **Whisper na GPU**: precisa das DLLs cuBLAS 12 e cuDNN 9 do CUDA, que não estão instaladas. Com `captions.device: auto` ele tenta a GPU e cai para a CPU sozinho. Vale instalar quando a transcrição contínua da live (fase 3) pedir velocidade.
- **NVENC (render na GPU)**: o ffmpeg 9.0.2 exige driver NVIDIA 610+. Com o driver atual o render fica no `libx264`, que dá conta; atualizar o driver libera o NVENC sem mudar o código.
- **Ainda não instalados**: streamlink e yt-dlp (extra `ingest`, fase 2). O `autocortes doctor` mostra o que falta.
