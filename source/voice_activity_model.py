"""Locate and verify the bundled Silero VAD model without downloading it."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys


MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
MODEL_RELATIVE_PATH = Path("voice-vad") / "silero-vad.onnx"
MAX_MODEL_BYTES = 20 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verified_model_path() -> Path:
    """Return the verified frozen asset or repository build asset path."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False):
        if not frozen_root:
            raise FileNotFoundError("Frozen VAD runtime has no resource directory")
        candidate = Path(frozen_root) / MODEL_RELATIVE_PATH
        if not candidate.is_file():
            raise FileNotFoundError("The bundled Silero VAD model is missing")
        if candidate.stat().st_size > MAX_MODEL_BYTES:
            raise ValueError("The bundled Silero VAD model exceeds the size ceiling")
        if _sha256(candidate) != MODEL_SHA256:
            raise ValueError("The bundled Silero VAD model failed hash verification")
        return candidate

    candidate = Path(__file__).resolve().parents[1] / "build" / MODEL_RELATIVE_PATH
    if not candidate.is_file():
        raise FileNotFoundError("The development Silero VAD model is missing")
    if candidate.stat().st_size > MAX_MODEL_BYTES:
        raise ValueError("The development Silero VAD model exceeds the size ceiling")
    if _sha256(candidate) != MODEL_SHA256:
        raise ValueError("The development Silero VAD model failed hash verification")
    return candidate
