# Arquitetura

## Linha do tempo única

Tudo se alinha num relógio só: **`t` = segundos desde o início da gravação** (da sessão). O gravador anota o horário em que o primeiro quadro foi gravado, e cada mensagem de chat, palavra transcrita e janela de áudio é convertida para `t`. Um momento é sempre um par `(início, fim)` nessa escala.

O chat reage alguns segundos *depois* do que aconteceu (tempo de ver + digitar). Por isso o pico do chat é recuado `detect.reaction_delay_seconds` (padrão 8 s), e o juiz lê `context_before_seconds` antes e `context_after_seconds` depois do pico para achar o começo certo.

## Modo live — `live.py`

`autocortes watch <url> [<url> ...]` acompanha várias lives ao mesmo tempo:

```
por canal:   gravador (streamlink -> ffmpeg)      chat (IRC da Twitch)
                         \                            /
                     analisador: sinais por janela -> picos
                                       |
para todos:        juiz (Whisper + Claude) -> renderizador -> data/fila/
```

- Cada canal tem gravador, chat e analisador próprios; os analisadores rodam no laço principal a cada 2 s.
- Um **juiz** e um **renderizador** atendem todos os canais, um trabalho por vez cada, para não disputarem a CPU. O juiz pega primeiro o pico mais forte.
- Se o Claude falhar (sem login, limite da assinatura), o juiz deixa o Claude de lado por 5 min e os picos viram short do mesmo jeito, só pelos sinais: corte fixo em volta do pico (`clip.pre_roll_seconds` / `post_roll_seconds`), marcado como "sem juiz". Nenhum momento se perde por falta de cota.
- A fila de revisão (`data/fila/`) recebe, por short: o `.mp4`, um `.txt` com título, descrição e hashtags prontos para colar e um `.json` com tudo (canal, nota, motivo, tempos). `autocortes queue` lista a fila. Cada veredito do Claude, com o consumo, fica em `data/fila/juiz.jsonl`.

## 1. Captura — `ingest/`

```
python -m streamlink --stdout --twitch-low-latency --retry-streams 30 <url> best
  | ffmpeg -i pipe:0 -c copy -f segment -segment_time 10
           -segment_list segments.csv -segment_list_type csv seg_%06d.ts
```

- **Sem reencode** (`-c copy`): gravar custa quase nada de CPU.
- **Segmentos `.ts` de 10 s**: o corte sai pouco depois do momento, e se algo cair o que já foi gravado continua íntegro. Os timestamps não são zerados a cada segmento, então um trecho que atravessa segmentos é só emendar os arquivos (MPEG-TS aceita emenda byte a byte).
- **Retenção curta** (`capture.retention_hours`, padrão 2 h): uma live em 1080p grava ~3,5 GB por hora, e o corte sai em minutos.
- **Esperando a live**: com `--retry-streams 30`, o streamlink tenta de 30 em 30 s até o canal entrar no ar. Se a live cair e voltar, começa uma sessão nova (pasta nova, relógio novo).
- **Chat da Twitch**: IRC anônimo (`irc.chat.twitch.tv`, nick `justinfan<número>`, sem login). YouTube e Kick ficam para depois; nesses canais os picos saem só pelo áudio.

## 2. Detecção — `detect/`

### Sinais baratos, o tempo todo

Séries em janelas de `detect.window_seconds` (5 s), em z-score robusto (mediana e MAD) contra os últimos `baseline_minutes`:

| Sinal | Cálculo | Observação |
|---|---|---|
| `chat` | peso das mensagens na janela | cada autor conta no máximo 2 vezes por janela (spam não pesa); risada e hype (`KEKW`, `kkkk`, `Pog`, "clipa", "NAOOO") valem o dobro |
| `audio` | volume (dB) de cada segundo, médio na janela | grito, risada, susto; depois dá para trocar por um classificador de som |

O score é a média ponderada dos sinais disponíveis (`detect.weights`: chat 0,6, áudio 0,4). Janela acima de `trigger_zscore` vira pico, com distância mínima entre picos (`min_gap_seconds`) e teto por hora (`max_clips_per_hour`), por canal. Depois: clipes criados pelos viewers (API da Twitch) como terceiro sinal.

### Claude, só nos picos

Para cada pico, o juiz transcreve só o trecho em volta (`context_before_seconds` + `context_after_seconds`, padrão 90 + 30 s) com o Whisper, que fica carregado na memória, e tira `llm.frames` quadros (padrão 5, JPEG 640×360) de 10 s antes a 6 s depois do pico. Manda ao Claude a transcrição com início e fim de cada frase, uma amostra do chat perto do pico e os quadros, numa mensagem em stream-json. Assim ele enxerga o lance mesmo sem ninguém falar: o chat e o áudio dizem *quando*, os quadros mostram *o quê*. Os critérios do canal entram no prompt por `llm.focus` (no `config.yaml`, os de GTA). A resposta é estruturada (`--json-schema`):

- `is_clip`, `score` (0 a 10) e o motivo; abaixo de `min_judge_score` (padrão 8), o pico é descartado
- `start`/`end` em frases completas, entre `clip.min_seconds` e `clip.max_seconds`
- `title`, `description` e `hashtags` em pt-BR
- `headline`: a manchete sensacionalista do topo do vídeo, com a palavra mais forte entre asteriscos

O Claude roda pela **assinatura**, via CLI do Claude Code (`claude -p`, versão 2.1.280 ou mais nova para o Opus 5.5), com o modelo de `llm.model` (padrão `claude-opus-5-5`, esforço `medium`). Medido em 11/11/2026: ~2,8 mil tokens de entrada e ~0,9 mil de saída por chamada, US$ 0,04 em preço de API, ~10 s de resposta. A chamada é enxuta, para gastar pouco da cota: prompt de sistema próprio, sem ferramentas (`--tools ""`), sem MCP e sem salvar sessão. O ambiente do subprocesso vai sem `ANTHROPIC_API_KEY`, porque com ela o `claude -p` cobraria pela API. O login vem de `CLAUDE_CODE_OAUTH_TOKEN` (gerado com `claude setup-token`; espaços colados junto são removidos) ou do login do Claude Code. As mensagens do chat entram no prompt como dados, e o prompt manda ignorar pedidos escritos nelas.

Por que transcrever só em volta dos picos: a live inteira ocuparia ~25% desta CPU por canal; só os trechos dos picos, uns 10% (ver "Esta máquina").

## 3. Render — `render/`

Uma única passada de `ffmpeg` por short: corta → reenquadra → legenda → normaliza o áudio → codifica.

**Corte.** No `autocortes render`, é `-ss`/`-t` direto no arquivo. No modo live, os segmentos que cobrem o trecho são emendados num `.ts` temporário e cortados com `-ss`/`-t` relativos ao primeiro segmento. Como o vídeo é reencodado de qualquer jeito, o corte sai exato no quadro.

**Pasta de trabalho.** O ffmpeg roda com uma pasta temporária como diretório atual, e a legenda (`captions.ass`), as fontes (`fonts/`) e o roteiro do recorte (`face.cmd`) entram por caminho relativo. Caminho do Windows (`C:\...`) dentro de um filtergraph exige escapes frágeis.

**Layouts 9:16 (1080×1920)**, em `render.layout`:

| Layout | Ideal para | Como |
|---|---|---|
| `gta` (padrão) | GTA e jogos em geral | faixa de 440 px em cima para a manchete, o jogo recortado no centro em 4:5 (`render.gta_crop`) e ampliado para 1080×1350, fundo desfocado e escurecido, webcam opcional no canto de cima do jogo |
| `blur` | qualquer coisa | vídeo inteiro no centro; o fundo é o próprio vídeo ampliado e desfocado |
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

Filtro do `blur` (o fundo é desfocado em 1/4 da resolução, que é bem mais rápido):

```
[src]split=2[bgsrc][fgsrc];
[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,scale=1080:1920[bg];
[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];
[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]
```

**Legendas** (`render/captions.py`).
- `faster-whisper` com `word_timestamps=True` e filtro de voz (VAD), no modelo `captions.whisper_model`, idioma `pt`. Com `captions.device: auto` tenta a GPU e, se faltarem as DLLs do CUDA, cai para a CPU e avisa. No modo live, a transcrição feita pelo juiz é reaproveitada na legenda.
- Arquivo `.ass` estilo shorts: blocos de até `words_per_line` palavras (pausa > 0,6 s ou fim de frase começam outro bloco), em maiúsculas, fonte grossa com contorno, a palavra falada em destaque (`highlight_color`) com um "pop" de 112%.
- Posição: `margin_bottom` px acima da borda, fora da área coberta pela interface do TikTok/Reels (legenda do post, botões).
- Manchete: a `headline` do juiz fica no topo durante o vídeo todo, em maiúsculas, com contorno grosso, e a palavra entre asteriscos vai na cor de destaque. Ela sai mesmo quando o trecho não tem fala.
- Queimada com o filtro `ass`; a fonte (Montserrat ExtraBold, licença OFL) vem dentro do pacote, em `src/autocortes/assets/fonts`, e é copiada para a pasta de trabalho.

**Codificação.** H.264, `yuv420p`, 30 fps, AAC 48 kHz, `-movflags +faststart`, áudio normalizado com `loudnorm` para -14 LUFS. É um perfil aceito pelas três plataformas. Com `render.encoder: auto`, um teste de um quadro decide entre `h264_nvenc` (GPU) e `libx264`.

## 4. Revisão

Hoje a fila é a pasta `data/fila/`: você assiste ao `.mp4`, copia o texto do `.txt` e posta. `autocortes queue` lista o que está pronto, com nota e título. Depois vira um painel web (fase 5) e, com `publish.manual_review: false`, o que o juiz aprova é publicado direto.

## 5. Publicação — `publish/`

Um publicador por plataforma, com a mesma interface (`publish(short) -> PostResult`, tipos em `models.py`). Os três rodam em paralelo (`asyncio`), cada um com retry e backoff próprios: falha em um não segura os outros. Instagram e TikTok processam o vídeo de forma assíncrona, então o publicador acompanha o status até o fim.

Cada plataforma tem teto diário (o TikTok, em geral ~15 posts por conta), menor do que uma live longa pode render. O publicador respeita o teto de cada uma e agenda o excedente para o dia seguinte. Regras e limites de cada API: [PLATAFORMAS.md](PLATAFORMAS.md).

## Estado

Por enquanto, arquivos: `data/live/<canal>/<sessão>/` (segmentos, `segments.csv`, `gravacao.log`) e `data/fila/` (shorts, `.txt`, `.json`, `juiz.jsonl`). Um banco SQLite entra quando a publicação automática e o painel precisarem guardar status de post.

## Esta máquina

Medido em 11/11/2026 (i5-11400, 16 GB, GTX 1650 4 GB, driver NVIDIA 581.80):

- **Whisper** `large-v3-turbo` na CPU (int8): 4× o tempo real, depois de 4 s para carregar o modelo.
- **Render** com `libx264`: um short de 18 s em ~5 s.
- **Várias lives**: transcrevendo só em volta dos picos, cada live ocupa ~10% da CPU, então 3 lives simultâneas ficam em ~30–40%. Para mais lives, ou para transcrever lives inteiras:
  - **Whisper na GPU**: precisa das DLLs cuBLAS 12 e cuDNN 9 do CUDA, que não estão instaladas. Com `captions.device: auto` ele tenta a GPU e cai para a CPU sozinho.
  - **NVENC (render na GPU)**: o ffmpeg 9.0.2 exige driver NVIDIA 610+. Com o driver atual, o render fica no `libx264`.
- **Disco**: só ~36 GB livres no C:, e três lives em 1080p gravam ~10 GB por hora. Por isso a retenção padrão é de 2 h.
