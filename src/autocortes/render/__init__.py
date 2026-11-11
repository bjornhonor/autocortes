"""Do trecho bruto ao short 9:16 legendado (fase 1).

- pipeline.py   render_short(): uma passada de ffmpeg com corte, layout, legenda e loudnorm.
- reframe.py    filtros 9:16 por layout: blur, center, split, face.
- facetrack.py  layout face: acha o rosto (YuNet) e planeja o movimento do recorte.
- captions.py   faster-whisper com timestamps por palavra -> legenda .ass estilo shorts.
"""
