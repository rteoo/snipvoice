"""Identity and preflight verification for the approved custom llama.cpp wheel."""

import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import sys


WRAPPER_VERSION = "0.3.36"
WRAPPER_REVISION = "1652066e0af45f2313b339670ef9555e8a54e545"
NATIVE_REVISION = "4fbc76dec51d0add466f0210855c0596589b60d4"
MANIFEST_NAME = "snipvoice-native.json"


def verify_manifest(root, *, check_hashes=True, manifest_root=None):
    root = Path(root)
    manifest_path = Path(manifest_root or root) / MANIFEST_NAME
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 64000:
        raise RuntimeError("Invalid custom llama.cpp runtime manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {"schema": 1, "wrapper_version": WRAPPER_VERSION,
                "wrapper_revision": WRAPPER_REVISION, "native_revision": NATIVE_REVISION}
    if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Install the approved custom llama.cpp wheel; upstream wheels are incompatible")
    libraries = manifest.get("libraries")
    if not isinstance(libraries, dict) or not 1 <= len(libraries) <= 128:
        raise RuntimeError("Invalid custom llama.cpp library inventory")
    for name, digest in libraries.items():
        if not isinstance(name, str):
            raise RuntimeError("Invalid custom llama.cpp library identity")
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts
                or "\\" in name or len(relative.parts) < 2 or relative.parts[0] != "lib"
                or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise RuntimeError("Invalid custom llama.cpp library identity")
        path = root.joinpath(*relative.parts)
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("Custom llama.cpp native library is missing or unsafe")
        if check_hashes:
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != digest:
                raise RuntimeError("Custom llama.cpp native library hash mismatch")
    return manifest


def _macos_manifest_root(root):
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return None
    executable = Path(sys.executable).resolve()
    contents = executable.parent.parent
    framework = contents / "Frameworks/llama_cpp"
    resources = contents / "Resources/llama_cpp"
    if (executable.parent.name != "MacOS" or contents.name != "Contents"
            or contents.parent.suffix != ".app" or root.resolve() != framework.resolve()
            or not framework.resolve().is_relative_to(contents)
            or not resources.resolve().is_relative_to(contents)):
        raise RuntimeError("Invalid custom llama.cpp application bundle layout")
    # PyInstaller cross-links package data from Frameworks to Resources. Read
    # the sealed resource directly; keep rejecting symlinks in source installs
    # and in the actual resource/native library files.
    return resources


def verify_llama_runtime():
    specification = importlib.util.find_spec("llama_cpp")
    if specification is None or not specification.origin:
        raise RuntimeError("Install the approved custom llama.cpp wheel")
    # ceiling: frozen probes check identity/inventory/ABI, not post-signing file
    # hashes. Add signature-aware hashing if an independent binary-integrity
    # boundary is required. Signing changes native bytes; the source preflight
    # verifies hashes before packaging; frozen probes verify inventory/ABI and
    # release scripts independently verify the final bundle signature.
    root = Path(specification.origin).parent
    manifest = verify_manifest(root, check_hashes=not getattr(sys, "frozen", False),
                               manifest_root=_macos_manifest_root(root))
    import llama_cpp
    if llama_cpp.__version__ != WRAPPER_VERSION or not callable(llama_cpp.llama_model_n_embd_out):
        raise RuntimeError("Custom llama.cpp wrapper API is incompatible")
    if "lazy_mode" not in dict(llama_cpp.llama_model_params._fields_):
        raise RuntimeError("Custom llama.cpp model parameter ABI is incompatible")
    parameters = llama_cpp.llama_model_default_params()
    if parameters.vocab_only or parameters.no_alloc:
        raise RuntimeError("Custom llama.cpp native parameter ABI is incompatible")
    return manifest
