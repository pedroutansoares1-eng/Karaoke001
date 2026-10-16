"""Extração do pitch de referência do vocal isolado."""
import json
import os
from pathlib import Path

import librosa
import numpy as np

F_MIN = librosa.note_to_hz('C2')
F_MAX = librosa.note_to_hz('C6')
FRAME_LENGTH = 2048
HOP_LENGTH = 1024


def hz_to_midi(hz: float) -> float:
    """Converte Hz para MIDI: 69 + 12 * log2(f / 440)."""
    return 69 + 12 * float(np.log2(hz / 440))


def extract_pitch_reference(audio_path: Path, output_path: Path) -> list[dict[str, float]]:
    """Extrai o f0 do vocal e grava [{'t','midi'}, ...] em JSON.

    Raises FileNotFoundError / RuntimeError para que o chamador reporte o erro.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f'Vocal não encontrado: {audio_path}')

    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    if y is None or len(y) == 0:
        raise RuntimeError('O vocal está vazio.')

    # hop_length maior reduz o custo (o pyin é o passo mais caro) e
    # max_transition_rate evita saltos irreais entre frames.
    f0, _, _ = librosa.pyin(
        y, fmin=F_MIN, fmax=F_MAX, sr=sr,
        frame_length=FRAME_LENGTH, hop_length=HOP_LENGTH,
        resolution=0.1, max_transition_rate=35.92,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=HOP_LENGTH)

    result = [
        {'t': round(float(t), 4), 'midi': round(hz_to_midi(hz), 3)}
        for t, hz in zip(times, f0)
        if hz is not None and np.isfinite(hz) and hz > 0
    ]
    if not result:
        raise RuntimeError('Nenhuma nota foi detectada no vocal.')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Escrita atômica: um .json truncado faria o /api/karaoke devolver 500.
    temporario = output_path.with_suffix('.json.tmp')
    temporario.write_text(json.dumps(result), encoding='utf-8')
    os.replace(temporario, output_path)
    return result
