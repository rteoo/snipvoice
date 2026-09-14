"""On-demand voice-model cache: download, verify, install, delete.

Models never live in the snippet data directory or the installer. The cache
is a non-roaming per-user location, overridable for tests and power users.

A download is streamed to a deterministic sibling ``.partial`` file and
re-hashed before a verified HTTP Range resume. It is promoted with
``os.replace`` only after size and SHA-256 match the catalog. Corrupt
downloads are discarded; cancelled or interrupted transfers remain resumable
without touching the last valid install.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request

from voice_catalog import catalog_entry, catalog_entry_by_id


ENV_VOICE_CACHE = "SNIPVOICE_VOICE_CACHE"
CACHE_DIR_NAME = "voice-models"
MANIFEST_NAME = "manifest.json"
PARTIAL_SUFFIX = ".partial"
MAX_REDIRECTS = 5
CHUNK_SIZE = 1024 * 1024
# ceiling: single-file GGUF downloads only. A catalog entry that points at an
# archive needs a path-safe extractor before it can be added.
_MANIFEST_IDENTITY_FIELDS = (
    "id",
    "profile",
    "filename",
    "sha256",
    "size_bytes",
    "license_id",
    "upstream_model",
)


class VoiceModelError(Exception):
    """User-visible failure while installing or removing a model."""


def default_voice_cache_dir(system=None):
    """Non-roaming cache for regenerable model files.

    Windows: ``%LOCALAPPDATA%\\Snipvoice\\voice-models``
    macOS: ``~/Library/Caches/Snipvoice/voice-models``
    Linux: ``~/.cache/snipvoice/voice-models``
    """
    override = os.environ.get(ENV_VOICE_CACHE)
    if override:
        return os.path.abspath(os.path.expanduser(override))

    from platform_support import current_os

    os_name = system or current_os()
    home = os.path.expanduser("~")
    if os_name == "windows":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(root, "Snipvoice", CACHE_DIR_NAME)
    if os_name == "darwin":
        return os.path.join(home, "Library", "Caches", "Snipvoice", CACHE_DIR_NAME)
    return os.path.join(home, ".cache", "snipvoice", CACHE_DIR_NAME)


def model_dir(cache_dir, entry):
    return os.path.join(cache_dir, _safe_entry_id(entry["id"]))


def model_path(cache_dir, entry):
    return os.path.join(model_dir(cache_dir, entry), entry["filename"])


def manifest_path(cache_dir, entry):
    return os.path.join(model_dir(cache_dir, entry), MANIFEST_NAME)


def partial_path(cache_dir, entry):
    """Stable, catalog-owned recovery path for an interrupted download."""
    return model_path(cache_dir, entry) + PARTIAL_SUFFIX


def _read_manifest(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def model_is_installed(entry, cache_dir):
    """True when the pinned file exists and its verified manifest is current.

    A matching manifest and file stat avoid re-reading multi-gigabyte models on
    every tray/menu check. Older manifests, or files whose stat changed, are
    hashed once and receive an atomic manifest upgrade when valid.
    """
    path = model_path(cache_dir, entry)
    manifest = _read_manifest(manifest_path(cache_dir, entry))
    if manifest is None or not os.path.isfile(path):
        return False
    try:
        stat = os.stat(path)
    except OSError:
        return False
    if stat.st_size != entry["size_bytes"]:
        return False
    identity_matches = all(
        manifest.get(key) == entry.get(key) for key in _MANIFEST_IDENTITY_FIELDS
    )
    verified_stat = _stat_metadata(stat)
    if identity_matches and manifest.get("verified_stat") == verified_stat:
        return True
    try:
        digest = _hash_file(path)
    except VoiceModelError:
        return False
    if digest != entry["sha256"]:
        return False
    try:
        _write_manifest(entry, cache_dir, digest, verified_stat=verified_stat)
    except OSError:
        # The bytes are verified even if a read-only cache prevents upgrading
        # the optimization metadata; the next check will hash again.
        pass
    return True


def installed_model_path(entry, cache_dir):
    if model_is_installed(entry, cache_dir):
        return model_path(cache_dir, entry)
    return None


def _ensure_parent(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _free_bytes(path):
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if not probe:
        probe = os.path.abspath(os.sep)
    return shutil.disk_usage(probe).free


def _safe_entry_id(model_id):
    if not isinstance(model_id, str) or not model_id:
        raise VoiceModelError("Identificador de modelo inválido.")
    if model_id in (".", "..") or "/" in model_id or "\\" in model_id or os.path.sep in model_id:
        raise VoiceModelError("Identificador de modelo inválido.")
    return model_id


def _safe_url(url):
    if not isinstance(url, str):
        raise VoiceModelError("URL do modelo inválida.")
    if not url.lower().startswith("https://"):
        raise VoiceModelError("O download do modelo só aceita HTTPS.")
    return url


class _LimitedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise VoiceModelError("Redirecionamento inseguro recusado.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_LimitedRedirect)


def _response_status(response):
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return int(status)


def _response_url_is_https(response):
    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return True
    final_url = geturl()
    return isinstance(final_url, str) and final_url.lower().startswith("https://")


def _matching_content_range(value, offset, total):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value.strip(), re.IGNORECASE)
    if match is None:
        return False
    start, end, declared_total = (int(item) for item in match.groups())
    return (
        start == offset
        and declared_total == total
        and offset <= end < total
    )


def _remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _rehash_partial(path, expected_size):
    """Return the digest state for retained bytes, rejecting unsafe leftovers."""
    hasher = hashlib.sha256()
    written = 0
    oversized = False
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_size:
                    oversized = True
                    break
                hasher.update(chunk)
    except FileNotFoundError:
        return hashlib.sha256(), 0
    except OSError as exc:
        raise VoiceModelError(f"Falha ao ler o download parcial: {exc}") from exc
    if oversized:
        _remove_file(path)
        raise VoiceModelError("O arquivo parcial é maior que o tamanho pinado.")
    return hasher, written


def _stat_metadata(stat):
    return {
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
    }


def _hash_file(path):
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError as exc:
        raise VoiceModelError(f"Falha ao verificar o modelo de voz: {exc}") from exc
    return hasher.hexdigest()


def download_model(entry, cache_dir, progress=None, cancel_event=None, opener=None):
    """Download and verify ``entry`` into ``cache_dir``.

    ``progress`` is ``callable(bytes_done, bytes_total)``. ``cancel_event`` is
    a ``threading.Event``. ``opener`` is a test hook matching
    ``urllib.request.OpenerDirector.open``. Returns the installed model path.
    """
    if model_is_installed(entry, cache_dir):
        return model_path(cache_dir, entry)

    dest_dir = model_dir(cache_dir, entry)
    dest_file = model_path(cache_dir, entry)
    os.makedirs(dest_dir, exist_ok=True)
    retained_path = partial_path(cache_dir, entry)
    expected_size = entry["size_bytes"]

    url = _safe_url(entry["url"])

    hasher, written = _rehash_partial(retained_path, expected_size)
    needed = int((expected_size - written) * 1.1) + CHUNK_SIZE
    free = _free_bytes(dest_dir)
    if free < needed:
        raise VoiceModelError(
            "Espaço em disco insuficiente para baixar o modelo de voz."
        )
    if written == expected_size:
        if hasher.hexdigest() != entry["sha256"]:
            _remove_file(retained_path)
            raise VoiceModelError("A verificação SHA-256 do modelo de voz falhou.")
        os.replace(retained_path, dest_file)
        _write_manifest(entry, cache_dir, entry["sha256"])
        return dest_file
    if progress is not None and written:
        progress(written, expected_size)

    open_url = opener or _opener().open
    resume = bool(written)
    oversized = False
    try:
        while True:
            headers = {"Range": f"bytes={written}-"} if resume else {}
            request = urllib.request.Request(url, headers=headers, method="GET")
            restart_without_range = False
            try:
                response_context = open_url(request, timeout=30)
            except urllib.error.HTTPError as exc:
                if resume and exc.code == 416:
                    exc.close()
                    _remove_file(retained_path)
                    hasher = hashlib.sha256()
                    written = 0
                    resume = False
                    if progress is not None:
                        progress(0, expected_size)
                    continue
                raise
            with response_context as response:
                if not _response_url_is_https(response):
                    raise VoiceModelError("Redirecionamento inseguro recusado.")
                status = _response_status(response)
                if resume:
                    content_range = response.headers.get("Content-Range")
                    if status == 206 and _matching_content_range(
                            content_range, written, expected_size):
                        mode = "ab"
                    elif status == 200:
                        hasher = hashlib.sha256()
                        written = 0
                        mode = "wb"
                        if progress is not None:
                            progress(0, expected_size)
                    else:
                        _remove_file(retained_path)
                        hasher = hashlib.sha256()
                        written = 0
                        resume = False
                        if progress is not None:
                            progress(0, expected_size)
                        restart_without_range = True
                        mode = None
                elif status == 200:
                    mode = "wb"
                else:
                    raise VoiceModelError(
                        "Resposta de download parcial inesperada do servidor."
                    )

                if not restart_without_range:
                    with open(retained_path, mode) as handle:
                        while True:
                            if cancel_event is not None and cancel_event.is_set():
                                raise VoiceModelError("Download do modelo cancelado.")
                            chunk = response.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            if written + len(chunk) > expected_size:
                                oversized = True
                                break
                            handle.write(chunk)
                            hasher.update(chunk)
                            written += len(chunk)
                            if progress is not None:
                                progress(written, expected_size)
            if restart_without_range:
                continue
            break
        if oversized:
            _remove_file(retained_path)
            raise VoiceModelError("O arquivo baixado é maior que o tamanho pinado.")
        digest = hasher.hexdigest()
        if written != expected_size:
            _remove_file(retained_path)
            raise VoiceModelError(
                "Tamanho do modelo baixado não confere com o catálogo."
            )
        if digest != entry["sha256"]:
            _remove_file(retained_path)
            raise VoiceModelError(
                "A verificação SHA-256 do modelo de voz falhou."
            )
        os.replace(retained_path, dest_file)
        _write_manifest(entry, cache_dir, digest)
        return dest_file
    except VoiceModelError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise VoiceModelError(f"Falha ao baixar o modelo de voz: {exc}") from exc


def _write_manifest(entry, cache_dir, digest, verified_stat=None):
    if verified_stat is None:
        try:
            verified_stat = _stat_metadata(os.stat(model_path(cache_dir, entry)))
        except OSError:
            verified_stat = None
    payload = {
        "id": entry["id"],
        "profile": entry["profile"],
        "filename": entry["filename"],
        "sha256": digest,
        "size_bytes": entry["size_bytes"],
        "license_id": entry["license_id"],
        "upstream_model": entry["upstream_model"],
        "verified_stat": verified_stat,
    }
    path = manifest_path(cache_dir, entry)
    _ensure_parent(path)
    fd, tmp = tempfile.mkstemp(prefix="manifest.", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def delete_model(entry, cache_dir):
    """Remove only the catalog-owned directory for ``entry``."""
    target = os.path.abspath(model_dir(cache_dir, entry))
    cache_root = os.path.abspath(cache_dir)
    try:
        inside = os.path.commonpath([target, cache_root]) == cache_root
    except ValueError:
        inside = False
    if not inside:
        raise VoiceModelError("Recusa em apagar fora do cache de modelos.")
    if os.path.basename(target) != entry["id"]:
        raise VoiceModelError("Recusa em apagar um diretório que não é do catálogo.")
    if not os.path.exists(target):
        return True
    shutil.rmtree(target)
    return True


def delete_profile_model(profile, cache_dir):
    entry = catalog_entry(profile)
    if entry is None:
        raise VoiceModelError("Perfil de voz desconhecido.")
    return delete_model(entry, cache_dir)


def resolve_entry(profile_or_id):
    entry = catalog_entry(profile_or_id)
    if entry is None:
        entry = catalog_entry_by_id(profile_or_id)
    if entry is None:
        raise VoiceModelError("Perfil de voz desconhecido.")
    return entry
