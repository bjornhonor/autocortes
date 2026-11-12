"""Captura da live: vídeo em segmentos e chat com horário.

- recorder.py  streamlink -> ffmpeg em segmentos .ts curtos (+ segments.csv), sem reencode.
- segments.py  lê a lista de segmentos, acha os que cobrem um trecho e os emenda.
- chat.py      chat da Twitch por IRC anônimo. YouTube e Kick ficam para depois.
"""
