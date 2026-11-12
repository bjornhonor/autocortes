# autocortes

Ferramenta que assiste lives, detecta momentos-chave, gera shorts 9:16 legendados e publica no YouTube Shorts, Instagram Reels e TikTok ao mesmo tempo. Visão e roadmap: README.md. Desenho técnico: docs/ARQUITETURA.md. Regras das APIs de postagem: docs/PLATAFORMAS.md.

Meta atual: lançamento do GTA 6 em 19/11/2026, com várias lives ao mesmo tempo e shorts quase em tempo real (`autocortes watch`, em `live.py`). Prioridade é robustez do modo live nesta máquina (CPU i5-11400, ~36 GB livres): nenhum momento pode se perder por falha do juiz ou de um canal.

## Convenções

- Docs, mensagens da CLI e comentários em pt-BR; nomes no código (variáveis, funções, chaves do config) em inglês.
- Python 3.11+, layout `src/`, CLI em Typer (`autocortes`). Parâmetros em `config.yaml` (modelos pydantic; chave desconhecida dá erro), segredos em `.env`.
- Todo processamento de vídeo/áudio passa pelo ffmpeg via `subprocess` com lista de argumentos, nunca `shell=True`.
- Tempo sempre em segundos desde o início da gravação (`t`); ver "Linha do tempo única" em docs/ARQUITETURA.md.
- Plataforma principal é Windows: cuidado com caminhos dentro do filtro `ass` do ffmpeg. A CLI força UTF-8 em stdout/stderr (`cli.run`) para os acentos não quebrarem fora do console.
- O render roda o ffmpeg com cwd numa pasta temporária; legenda, fontes e `face.cmd` entram por caminho relativo (ver "Pasta de trabalho" em docs/ARQUITETURA.md). Use `ffmpeg.find_tool`/`run_ffmpeg`, não `"ffmpeg"` direto: logo após instalar pelo winget o PATH dos processos abertos ainda é o velho.
- Bibliotecas pesadas (faster-whisper, OpenCV) são importadas dentro das funções, para a CLI e os testes rodarem sem elas.
- O juiz chama o Claude pela CLI do Claude Code (`claude -p`, em `detect/judge.py`) na assinatura pessoal do usuário (conta Pro), não pelo SDK. O subprocesso roda sem `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`: com elas, o `claude -p` cobraria pela API. O Opus 5.5 exige a CLI 2.1.280+ (`claude update`).
- Nunca leia o terminal do usuário por inteiro se ele pode ter rodado `claude setup-token`: o token fica impresso na tela.
- Desenvolver e testar cada etapa em VODs antes de ligar na live.

## Comandos

- Ambiente: `.venv\Scripts\autocortes doctor`
- Testes: `.venv\Scripts\python -m pytest` (os de `tests/test_render_ffmpeg.py` renderizam de verdade e pulam sem ffmpeg)
- Short de teste: `.venv\Scripts\autocortes render video.mp4 -s 0:02 -e 0:20 --layout face`
- Ensaio do modo live: `.venv\Scripts\autocortes -c teste.yaml watch twitch.tv/canal --no-judge --minutes 5` (um `teste.yaml` com `trigger_zscore` e `baseline_minutes` baixos faz os picos saírem em minutos; aponte `data_dir` para fora do projeto)
- Lint: `.venv\Scripts\ruff check src tests`
