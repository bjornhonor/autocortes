"""Detecção de momentos-chave.

- signals.py  séries por janela: peso das mensagens do chat e volume do áudio, z-score robusto.
- scorer.py   soma ponderada dos sinais; picos acima do limite viram Peak (com intervalo e teto).
- judge.py    o Claude, via `claude -p` na assinatura, decide se vale, acerta início/fim e
              escreve título, descrição e hashtags.
"""
