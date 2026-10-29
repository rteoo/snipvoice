"""Ditado settings for the Tauri manager: model, shortcuts, commands, corrections.

``DictationActions`` adds these JSON-RPC methods to ``ManagerApi`` with the
Tk form's rules: both shortcuts parse, differ and leave the recording
shortcut free; a model that is not downloaded is confirmed before saving;
and while a shortcut field has focus the global hotkeys are paused through
``begin_hotkey_edit``, so pressing the old chord types it instead of dictating.
"""

import threading

import voice_models
from i18n import tr
from manager_bridge import INVALID_PARAMS, RpcFailure, _text, technical_detail
from meeting_gui import LANGUAGE_LABELS, PROFILE_LABELS
from meeting_settings import validate_hotkey_conflicts
from voice_catalog import available_languages, format_size, selectable_catalog, third_party_notices
from voice_commands import CommandConflictError, validate_command
from voice_hotkey import parse_chord
from voice_settings import ACTIVATION_HOLD, ACTIVATION_TOGGLE
from voice_text_support import validate_replacements

MAX_COMMANDS = 2000
MAX_REPLACEMENTS = 500


class DictationActions:
    """The Ditado form and its editors over the voice controller.

    ``voice`` (VoiceController); ``settings_getter``/``persist_settings``
    (settings.json); ``commands_getter() -> dict`` and
    ``save_commands(commands, expected)`` (commands.json, raising
    ``CommandConflictError`` when the file changed); ``reload_commands() -> bool``
    reads commands.json again;
    ``on_changed()`` refreshes the tray after a save or removal.
    """

    def __init__(self, *, voice, settings_getter, persist_settings, commands_getter, save_commands,
                 reload_commands, on_changed):
        self.voice = voice
        self.settings_getter = settings_getter
        self.persist_settings = persist_settings
        self.commands_getter = commands_getter
        self.save_commands = save_commands
        self.reload_commands = reload_commands
        self.on_changed = on_changed
        self._lock = threading.Lock()
        self._edit_token = None

    def methods(self):
        return {
            "dictation.settings": self.settings,
            "dictation.save": self.save,
            "dictation.beginShortcutEdit": self.begin_shortcut_edit,
            "dictation.endShortcutEdit": self.end_shortcut_edit,
            "dictation.commands": self.commands,
            "dictation.saveCommands": self.save_command_list,
            "dictation.reloadCommands": self.reload,
            "dictation.corrections": self.corrections,
            "dictation.saveCorrections": self.save_corrections,
            "dictation.licenses": lambda _params: {"lines": list(third_party_notices())},
            "dictation.removeModel": self.remove_model,
        }

    def _require_voice(self):
        if self.voice is None:
            raise RpcFailure(tr("A entrada por voz está indisponível nesta instalação."))
        return self.voice

    # -- Form -----------------------------------------------------------

    def settings(self, _params):
        voice = self._require_voice()
        current = voice.settings
        mode = getattr(current, "activation_mode", ACTIVATION_HOLD)
        return {
            "profiles": [{"id": entry["profile"], "label": tr(PROFILE_LABELS[entry["profile"]]),
                          "installed": bool(voice_models.model_is_installed(entry, voice.cache_dir))}
                         for entry in selectable_catalog()],
            "languages": {entry["profile"]: [{"id": language, "label": tr(LANGUAGE_LABELS[language])}
                                             for language in available_languages(entry["profile"])]
                          for entry in selectable_catalog()},
            "values": {
                "profile": current.profile,
                "language": current.language,
                "hotkey": current.hotkey,
                "commandHotkey": current.command_hotkey,
                "activationMode": mode if mode in (ACTIVATION_HOLD, ACTIVATION_TOGGLE) else ACTIVATION_HOLD,
            },
            "commands": len(self.commands_getter()),
            "corrections": len(getattr(current, "voice_replacements", {}) or {}),
        }

    def save(self, params):
        voice = self._require_voice()
        values = params.get("values")
        if not isinstance(values, dict):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        entry = next((item for item in selectable_catalog() if item["profile"] == values.get("profile")), None)
        language = values.get("language")
        mode = values.get("activationMode")
        if entry is None or mode not in (ACTIVATION_HOLD, ACTIVATION_TOGGLE):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if language not in available_languages(entry["profile"]):
            raise RpcFailure(tr("Este modelo usa detecção automática de idioma. Selecione auto."), code=INVALID_PARAMS)
        try:
            dictation = parse_chord(_text(values, "hotkey", limit=80))
        except ValueError as exc:
            raise RpcFailure(tr("O atalho de ditado precisa de um modificador e uma tecla.\n\nExemplo: ctrl+alt+space"),
                             code=INVALID_PARAMS) from exc
        try:
            command = parse_chord(_text(values, "commandHotkey", limit=80))
        except ValueError as exc:
            raise RpcFailure(tr("O atalho de comando precisa de um modificador e uma tecla.\n\n"
                                "Exemplo: ctrl+alt+shift+space"), code=INVALID_PARAMS) from exc
        if dictation == command:
            raise RpcFailure(tr("Escolha atalhos diferentes para ditado e comando por voz."), code=INVALID_PARAMS)
        try:
            validate_hotkey_conflicts(dict(self.settings_getter(), voice_hotkey=dictation.spec,
                                           voice_command_hotkey=command.spec))
        except ValueError as exc:
            raise RpcFailure(str(exc), code=INVALID_PARAMS) from exc
        if params.get("confirmed") is not True and not voice_models.model_is_installed(entry, voice.cache_dir):
            return {"status": "confirm", "title": tr("Baixar modelo de voz"),
                    "message": tr("Isso vai baixar {size} ({license}).\n\n{attribution}",
                                  size=format_size(entry["size_bytes"]), license=entry["license_id"],
                                  attribution=entry["attribution"])}
        if voice.apply_options(profile=entry["profile"], language=language, hotkey=dictation.spec,
                               command_hotkey=command.spec, activation_mode=mode) is not True:
            raise RpcFailure(tr("Não foi possível salvar. Tente novamente."))
        self.on_changed()
        return {"status": "saved", "settings": self.settings({})}

    # -- Shortcut capture ----------------------------------------------

    def begin_shortcut_edit(self, _params):
        voice = self._require_voice()
        token = threading.Event()
        token.set()
        with self._lock:
            previous, self._edit_token = self._edit_token, token
        if previous is not None:
            previous.clear()
            voice.end_hotkey_edit(previous)
        ready = voice.begin_hotkey_edit(token) is True
        return {"ready": ready, "message": tr("Atalhos globais pausados. Pressione o novo atalho.") if ready
                else tr("Aguarde o ditado terminar antes de capturar um atalho.")}

    def end_shortcut_edit(self, _params):
        with self._lock:
            token, self._edit_token = self._edit_token, None
        if token is not None and self.voice is not None:
            token.clear()
            self.voice.end_hotkey_edit(token)
        return {"ended": token is not None}

    # -- Commands -------------------------------------------------------

    @staticmethod
    def _command_pairs(value):
        if not isinstance(value, list) or len(value) > MAX_COMMANDS:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        pairs = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("phrase"), str) or not isinstance(item.get("text"), str):
                raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
            pairs.append((item["phrase"], item["text"]))
        return pairs

    def commands(self, _params):
        return {"commands": [{"phrase": phrase, "text": text} for phrase, text in self.commands_getter().items()]}

    def save_command_list(self, params):
        baseline = dict(self._command_pairs(params.get("baseline")))
        commands = {}
        for phrase, text in self._command_pairs(params.get("commands")):
            if baseline.get(phrase) == text and phrase not in commands:
                # Untouched entries stay as stored, as the Tk editor keeps hand-edited ones.
                commands[phrase] = text
                continue
            try:
                key, value = validate_command(phrase, text, commands, None)
            except ValueError as exc:
                raise RpcFailure(str(exc), code=INVALID_PARAMS) from exc
            commands[key] = value
        try:
            self.save_commands(commands, baseline)
        except CommandConflictError as exc:
            raise RpcFailure(str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise RpcFailure(tr("Não foi possível salvar os comandos. A biblioteca anterior foi preservada; "
                                "revise o arquivo e tente novamente."), detail=technical_detail(exc)) from exc
        return self.commands({})

    def reload(self, _params):
        if self.reload_commands() is False:
            raise RpcFailure(tr("Não foi possível ler commands.json. Os comandos anteriores foram preservados."))
        return {"commands": len(self.commands_getter())}

    # -- Corrections ----------------------------------------------------

    def corrections(self, _params):
        voice = self._require_voice()
        current = getattr(voice.settings, "voice_replacements", {}) or {}
        return {"items": [{"source": source, "replacement": replacement} for source, replacement in current.items()]}

    def save_corrections(self, params):
        voice = self._require_voice()
        items = params.get("items")
        if not isinstance(items, list) or len(items) > MAX_REPLACEMENTS:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        candidate = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("source"), str) \
                    or not isinstance(item.get("replacement"), str):
                raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
            candidate[item["source"]] = item["replacement"]
        checked = validate_replacements(candidate)
        if checked != candidate or len(checked) != len(items):
            raise RpcFailure(tr("Revise os termos informados."), code=INVALID_PARAMS)
        if not self.persist_settings({"voice_replacements": checked}):
            raise RpcFailure(tr("Não foi possível salvar as correções da transcrição."))
        voice.settings.voice_replacements = checked
        return self.corrections({})

    # -- Model ----------------------------------------------------------

    def remove_model(self, params):
        voice = self._require_voice()
        if params.get("confirmed") is not True:
            return {"status": "confirm", "title": tr("Remover modelo"),
                    "message": tr("A entrada por voz será desligada e só o modelo deste perfil será apagado.")}
        if voice.delete_active_model() is False:
            raise RpcFailure(tr("Aguarde a gravação terminar antes de remover o modelo."))
        self.on_changed()
        return {"status": "removed"}
