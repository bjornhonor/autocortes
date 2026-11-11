"""Publicação simultânea nas três plataformas (fase 4). Regras em docs/PLATAFORMAS.md.

Planejado:
- youtube.py    YouTube Data API v3: videos.insert com upload resumível (OAuth).
- instagram.py  Instagram Graph API: container REELS -> status FINISHED -> media_publish.
- tiktok.py     Content Posting API: creator_info -> video/init -> upload em pedaços -> status.
- runner.py     dispara as três em paralelo, respeitando o teto diário de cada uma.
"""
