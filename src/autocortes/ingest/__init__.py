"""Captura de vídeo e chat (VOD na fase 2, live na fase 3).

Planejado:
- vod.py       baixa VOD e chat gravado (yt-dlp, chat-downloader) para desenvolver sem live.
- recorder.py  streamlink -> ffmpeg em segmentos .ts + segments.csv com os tempos.
- chat.py      chat ao vivo com horário: IRC anônimo da Twitch, chat-downloader no YouTube.
"""
