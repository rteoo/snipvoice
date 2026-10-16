"""Fetch and verify the pinned Silero VAD model for release builds."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import os
import tempfile
from urllib.request import Request, urlopen


MODEL_VERSION = "6.2.2"
MODEL_COMMIT = "60b7ffa243625ebdc1070275a29f18c87843786a"
MODEL_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    f"{MODEL_COMMIT}/src/silero_vad/data/silero_vad.onnx"
)
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
MODEL_NAME = "silero-vad.onnx"
LICENSE_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    f"{MODEL_COMMIT}/LICENSE"
)
LICENSE_SHA256 = "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"
MAX_MODEL_BYTES = 20 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(path: Path) -> Path:
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_MODEL_BYTES:
        raise RuntimeError("Silero VAD model is missing or exceeds the size ceiling")
    if _sha256(path) != MODEL_SHA256:
        raise RuntimeError("Silero VAD model hash does not match the pinned release")
    return path


def fetch_asset(url: str, destination: Path, *, sha256: str | None = None,
                max_bytes: int = MAX_MODEL_BYTES) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with urlopen(Request(url, headers={"User-Agent": "Snipvoice-release-fetch"}), timeout=60) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise RuntimeError("download exceeds the configured size ceiling")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.",
                dir=destination.parent, delete=False,
            ) as target:
                temporary = Path(target.name)
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise RuntimeError("download exceeds the configured size ceiling")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        if sha256 and _sha256(temporary) != sha256.lower():
            raise RuntimeError("download hash does not match the pinned asset")
        os.replace(temporary, destination)
        temporary = None
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_voice_assets(output_dir: Path) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    model = fetch_asset(MODEL_URL, output_dir / MODEL_NAME, sha256=MODEL_SHA256)
    license_path = fetch_asset(
        LICENSE_URL, output_dir / "LICENSE", sha256=LICENSE_SHA256,
        max_bytes=128 * 1024,
    )
    return verify_model(model), license_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("build/voice-vad"))
    args = parser.parse_args()
    model, license_path = fetch_voice_assets(args.output_dir)
    print(f"{model} sha256={_sha256(model)} bytes={model.stat().st_size}")
    print(f"{license_path} bytes={license_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
