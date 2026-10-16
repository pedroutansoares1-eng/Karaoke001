"""Stubs das dependências pesadas.

`main.py` importa torch e app/pitch.py importa librosa. Em CI (ou em uma
máquina sem a stack de ML) isso exige instalar centenas de MB só para rodar
os testes de API. Este conftest injeta dublês leves quando os módulos reais
não estão disponíveis, sem sobrescrever instalações verdadeiras.
"""
import sys
import types

try:  # pragma: no cover - depende do ambiente
    import torch  # noqa: F401
except ImportError:  # pragma: no cover
    stub = types.ModuleType('torch')
    stub.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules['torch'] = stub

try:  # pragma: no cover - depende do ambiente
    import librosa  # noqa: F401
except ImportError:  # pragma: no cover
    stub = types.ModuleType('librosa')
    stub.load = lambda *a, **k: (None, 44100)
    stub.pyin = lambda *a, **k: (None, None, None)
    stub.times_like = lambda *a, **k: []
    stub.note_to_hz = lambda nota: 440.0
    sys.modules['librosa'] = stub
