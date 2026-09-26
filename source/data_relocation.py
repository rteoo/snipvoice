"""Move the whole Snipvoice data directory to a user-chosen folder.

A move is requested while the app runs and performed at the next start, before
logging, settings, history, or the meeting store open any file. The pointer
records the pending move, so a crash mid-copy resumes on the following start
instead of stranding data between two folders.
"""

import json
import os
import shutil

import app_paths
from i18n import tr
from snippet_utils import write_json_atomic

DEFAULT_FOLDER_NAME = "snipvoice"
_INCOMPLETE_MARKER = ".snipvoice-move-incomplete"


class RelocationError(Exception):
    """User-visible reason a move cannot start or did not complete."""


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _inside(child, parent):
    child = os.path.normcase(os.path.realpath(child))
    parent = os.path.normcase(os.path.realpath(parent))
    try:
        return os.path.commonpath((child, parent)) == parent
    except ValueError:
        return False


def _is_root(path):
    absolute = os.path.abspath(path)
    return os.path.dirname(absolute) == absolute


def _non_empty(path):
    with os.scandir(path) as entries:
        return any(True for _ in entries)


def target_for_choice(chosen):
    """Map a folder picked in a dialog to the data folder that will be used.

    Picking an empty folder uses it directly. Picking a drive root or a folder
    that already has content (``D:\\``) nests a ``snipvoice`` folder inside it.
    """
    chosen = os.path.abspath(chosen)
    if _is_root(chosen) or (os.path.isdir(chosen) and _non_empty(chosen)):
        return os.path.join(chosen, DEFAULT_FOLDER_NAME)
    return chosen


def validate_target(current, target):
    """Return the normalized target, or raise RelocationError."""
    if app_paths.env_data_dir():
        raise RelocationError(
            tr("A pasta de dados está definida pela variável {name}.", name=app_paths.ENV_HOME)
        )
    if not isinstance(target, str) or not target.strip() or not os.path.isabs(target):
        raise RelocationError(tr("Escolha uma pasta com caminho completo."))
    target = os.path.abspath(target.strip())
    if _same(current, target):
        raise RelocationError(tr("Essa já é a pasta de dados atual."))
    if _is_root(target) or _same(target, os.path.expanduser("~")):
        raise RelocationError(tr("Escolha uma pasta própria, não a raiz do disco ou a pasta pessoal."))
    if _inside(target, current) or _inside(current, target):
        raise RelocationError(tr("A nova pasta não pode ficar dentro da atual, nem conter a atual."))
    if not os.path.isdir(os.path.dirname(target)):
        raise RelocationError(tr("A pasta onde a nova pasta seria criada não existe."))
    if os.path.lexists(target):
        if not os.path.isdir(target):
            raise RelocationError(tr("Já existe um arquivo com esse nome no destino."))
        if _non_empty(target):
            raise RelocationError(tr("A pasta de destino precisa estar vazia."))
    return target


def directory_size(path):
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass
    return total


def request_relocation(current, target):
    """Validate and record a move to perform at the next start."""
    target = validate_target(current, target)
    location = app_paths.read_location()
    location["data_dir"] = current
    location["pending_move"] = {"from": current, "to": target}
    app_paths.write_location(location)
    return target


def _manifest(root):
    files = {}
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            files[os.path.relpath(path, root)] = os.path.getsize(path)
    return files


def _copy_verified(src, dst):
    """Copy ``src`` into ``dst`` and return the verified relative file list."""
    manifest = _manifest(src)
    needed = sum(manifest.values())
    free = shutil.disk_usage(os.path.dirname(dst)).free
    if needed > free:
        raise RelocationError(tr("Não há espaço livre suficiente na pasta de destino."))
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, _INCOMPLETE_MARKER), "w", encoding="utf-8"):
        pass
    shutil.copytree(src, dst, dirs_exist_ok=True)
    for relative, size in manifest.items():
        copied = os.path.join(dst, relative)
        if not os.path.isfile(copied) or os.path.getsize(copied) != size:
            raise RelocationError(tr("A cópia de {path} não confere com o original.", path=relative))
    return manifest


def _remove_copied(root, manifest):
    """Delete exactly the files that were copied, then any emptied folders.

    Anything the copy did not cover stays in place, so a folder that gained
    unrelated files is never removed wholesale.
    """
    leftovers = False
    for relative in manifest:
        try:
            os.remove(os.path.join(root, relative))
        except FileNotFoundError:
            pass
        except OSError:
            leftovers = True
    for directory, _dirs, _files in os.walk(root, topdown=False):
        try:
            os.rmdir(directory)
        except OSError:
            leftovers = True
    return not leftovers


def _discard_incomplete(dst):
    if os.path.isfile(os.path.join(dst, _INCOMPLETE_MARKER)):
        shutil.rmtree(dst, ignore_errors=True)


def rebase_final_audio_paths(data_dir, old_home):
    """Point default-location final recordings at the moved data directory.

    Final WAVs default to ``<data>/recordings`` and meetings record that
    location as an absolute path. User-chosen export folders are untouched.
    """
    meetings = os.path.join(data_dir, "meetings")
    if not os.path.isdir(meetings):
        return
    old_prefix = os.path.normcase(os.path.abspath(old_home))
    for entry in os.scandir(meetings):
        metadata_path = os.path.join(entry.path, "metadata.json")
        if not entry.is_dir() or not os.path.isfile(metadata_path):
            continue
        try:
            with open(metadata_path, encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (OSError, ValueError):
            continue
        final_audio = metadata.get("final_audio") if isinstance(metadata, dict) else None
        path = final_audio.get("path") if isinstance(final_audio, dict) else None
        if not isinstance(path, str):
            continue
        absolute = os.path.abspath(path)
        try:
            inside = os.path.commonpath((os.path.normcase(absolute), old_prefix)) == old_prefix
        except ValueError:
            inside = False
        if not inside:
            continue
        final_audio["path"] = os.path.join(data_dir, os.path.relpath(absolute, old_home))
        write_json_atomic(metadata_path, metadata)


def _move(src, dst):
    """Move ``src`` to ``dst``. Return the copied manifest, or None after a rename.

    A returned manifest means the old files still exist and must be removed
    only after the pointer names ``dst``.
    """
    _discard_incomplete(dst)
    if os.path.isdir(dst) and not _non_empty(dst):
        os.rmdir(dst)
    try:
        os.rename(src, dst)
        return None
    except OSError:
        pass
    # Another volume, or a file held open elsewhere: copy, verify, then delete.
    try:
        manifest = _copy_verified(src, dst)
    except (OSError, RelocationError):
        _discard_incomplete(dst)
        raise
    os.remove(os.path.join(dst, _INCOMPLETE_MARKER))
    return manifest


def complete_pending_relocation():
    """Perform a recorded move. Return a user-facing message, or "" when idle.

    Must run while holding the single-instance lock and before anything opens
    files in the data directory.
    """
    location = app_paths.read_location()
    pending = location.get("pending_move")
    if not isinstance(pending, dict):
        return ""
    src, dst = pending.get("from"), pending.get("to")
    if not (isinstance(src, str) and isinstance(dst, str)) or app_paths.env_data_dir():
        location.pop("pending_move", None)
        app_paths.write_location(location)
        return ""
    incomplete = os.path.isfile(os.path.join(dst, _INCOMPLETE_MARKER))
    manifest = None
    # A rename that happened just before a crash leaves only the new folder.
    if os.path.exists(src) or not os.path.isdir(dst) or incomplete:
        try:
            if not incomplete:
                validate_target(src, dst)
            manifest = _move(src, dst)
        except (OSError, RelocationError) as exc:
            location.pop("pending_move", None)
            location["data_dir"] = src
            app_paths.write_location(location)
            return tr(
                "Não foi possível mover os dados para {path}: {error}. Nada foi alterado.",
                path=dst, error=exc,
            )
    rebase_final_audio_paths(dst, src)
    location.pop("pending_move", None)
    location["data_dir"] = dst
    app_paths.write_location(location)
    if manifest is not None and not _remove_copied(src, manifest):
        return tr(
            "Dados movidos para {path}. Alguns arquivos antigos em {old} não puderam "
            "ser apagados; remova essa pasta manualmente.",
            path=dst, old=src,
        )
    return tr("Dados movidos para {path}.", path=dst)


MODEL_FOLDERS = ("voice-models", "summary-models")
_MODEL_ENV_OVERRIDES = ("SNIPVOICE_VOICE_CACHE", "SNIPVOICE_SUMMARY_CACHE")


def models_env_locked():
    return any(os.environ.get(name) for name in _MODEL_ENV_OVERRIDES)


def validate_models_target(current, target):
    """Return the normalized model root, or raise RelocationError.

    Unlike the data folder, the model root may be a shared folder that already
    holds other files: only the ``voice-models`` and ``summary-models``
    subfolders are ever written there.
    """
    if models_env_locked():
        raise RelocationError(
            tr("A pasta dos modelos está definida por SNIPVOICE_VOICE_CACHE ou "
               "SNIPVOICE_SUMMARY_CACHE.")
        )
    if not isinstance(target, str) or not target.strip() or not os.path.isabs(target):
        raise RelocationError(tr("Escolha uma pasta com caminho completo."))
    target = os.path.abspath(target.strip())
    if _same(current, target):
        raise RelocationError(tr("Essa já é a pasta dos modelos atual."))
    for folder in MODEL_FOLDERS:
        if _inside(target, os.path.join(current, folder)):
            raise RelocationError(tr("A nova pasta não pode ficar dentro da pasta atual dos modelos."))
    if os.path.lexists(target) and not os.path.isdir(target):
        raise RelocationError(tr("Já existe um arquivo com esse nome no destino."))
    if not os.path.isdir(target) and not os.path.isdir(os.path.dirname(target)):
        raise RelocationError(tr("A pasta onde a nova pasta seria criada não existe."))
    return target


def models_size(root):
    return sum(directory_size(os.path.join(root, folder)) for folder in MODEL_FOLDERS)


def request_models_relocation(current, target):
    """Validate and record a model-folder move to perform at the next start."""
    target = validate_models_target(current, target)
    location = app_paths.read_location()
    location["models_dir"] = current
    location["pending_models_move"] = {"from": current, "to": target}
    app_paths.write_location(location)
    return target


def _model_items(root):
    for folder in MODEL_FOLDERS:
        parent = os.path.join(root, folder)
        if not os.path.isdir(parent):
            continue
        for entry in sorted(os.scandir(parent), key=lambda item: item.name):
            if entry.is_dir():
                yield folder, entry.name


def complete_pending_models_relocation():
    """Move each downloaded model into the new root. Return a message or "".

    Every model is renamed or copied and verified before the pointer switches;
    a failure puts renamed models back and discards copies, so the old root
    stays complete. A model already present at the destination is kept there
    and the old copy is left untouched.
    """
    location = app_paths.read_location()
    pending = location.get("pending_models_move")
    if not isinstance(pending, dict):
        return ""
    src, dst = pending.get("from"), pending.get("to")
    if not (isinstance(src, str) and isinstance(dst, str)) or models_env_locked():
        location.pop("pending_models_move", None)
        app_paths.write_location(location)
        return ""
    renamed, copied, kept = [], [], []
    try:
        for folder, name in _model_items(src):
            source = os.path.join(src, folder, name)
            target = os.path.join(dst, folder, name)
            _discard_incomplete(target)
            if os.path.exists(target):
                kept.append(name)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                os.rename(source, target)
                renamed.append((source, target))
                continue
            except OSError:
                pass
            try:
                copied.append((source, target, _copy_verified(source, target)))
            except (OSError, RelocationError):
                _discard_incomplete(target)
                raise
            os.remove(os.path.join(target, _INCOMPLETE_MARKER))
    except (OSError, RelocationError) as exc:
        for source, target in reversed(renamed):
            try:
                os.rename(target, source)
            except OSError:
                pass
        for _source, target, _manifest_files in copied:
            shutil.rmtree(target, ignore_errors=True)
        location.pop("pending_models_move", None)
        location["models_dir"] = src
        app_paths.write_location(location)
        return tr(
            "Não foi possível mover os modelos para {path}: {error}. Nada foi alterado.",
            path=dst, error=exc,
        )
    location.pop("pending_models_move", None)
    location["models_dir"] = dst
    app_paths.write_location(location)
    leftovers = False
    for source, _target, manifest in copied:
        leftovers = not _remove_copied(source, manifest) or leftovers
    for folder in MODEL_FOLDERS:
        try:
            os.rmdir(os.path.join(src, folder))
        except OSError:
            pass
    message = tr("Modelos movidos para {path}.", path=dst)
    if kept:
        message += tr(
            " Estes modelos já estavam no destino e foram mantidos lá: {names}. "
            "As cópias antigas continuam em {old}.",
            names=", ".join(kept), old=src,
        )
    if leftovers:
        message += tr(" Alguns arquivos antigos em {old} não puderam ser apagados.", old=src)
    return message
