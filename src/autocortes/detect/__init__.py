"""Detecção de momentos-chave (fase 2).

Planejado:
- signals.py     séries por janela: velocidade do chat, emotes/palavras, volume do áudio.
- transcribe.py  transcrição contínua dos segmentos com faster-whisper.
- scorer.py      z-score contra a média móvel, soma ponderada, picos viram Moment.
- judge.py       Claude decide se o candidato vale, ajusta início/fim e escreve o post.
"""
