"""Operator-visible probe for the voice behavior contract.

Prints one line per clause. Does not open a microphone or download a model.
"""

import os
import sys
import tempfile

from trigger_index import compile_trigger_index
from voice_dispatch import MODE_COMMAND, MODE_DICTATION, VoiceTarget
from voice_runtime import FakeAsrBackend
from voice_support import STATE_IDLE, STATE_UNAVAILABLE, VoiceController


class InlineRunner:
    def start(self, fn, *args, name=None):
        fn(*args)


class FakeCapture:
    def __init__(self, samples=None):
        self.samples = list(samples or [0.1, 0.2])
        self._queue = self

    def start(self):
        return None

    def stop(self):
        return self.samples, False

    def get(self, timeout=0.1):
        raise Exception("empty")


def _controller(transcript, tmp):
    inserted = []
    expanded = []
    forms = []
    backend = FakeAsrBackend(transcript=transcript)
    controller = VoiceController(
        {},
        task_runner=InlineRunner(),
        insert_text=lambda text: inserted.append(text) or True,
        expand_trigger=lambda trigger: expanded.append(trigger) or True,
        notify=lambda *args, **kwargs: None,
        logger=None,
        capture_target=lambda: VoiceTarget("window", handle=1),
        restore_target=lambda target: True,
        secure_input_blocks=lambda: False,
        backend=backend,
        capture_factory=lambda: FakeCapture(),
        cache_dir=tmp,
        download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
            tmp, "m.gguf"
        ),
    )
    controller.bind_library(
        lambda: {"xadds": "hi"},
        lambda: compile_trigger_index({"xadds": "hi"}, set()),
    )
    return controller, inserted, expanded, forms, backend


def main():
    results = []
    tmp_parent = os.path.join(os.path.dirname(__file__), "tests", "tmp")
    os.makedirs(tmp_parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="voice-probe-", dir=tmp_parent)

    def clause(name, ok, detail=""):
        results.append(ok)
        suffix = f" {detail}" if detail else ""
        print(f"CLAUSE {name} {'PASS' if ok else 'FAIL'}{suffix}")

    controller, inserted, expanded, forms, backend = _controller("hello", tmp)
    clause(
        "defaults_off",
        (not controller.enabled) and controller.state == STATE_UNAVAILABLE,
    )

    import unittest.mock as mock
    with mock.patch("voice_support.model_is_installed", return_value=True), \
            mock.patch("voice_support.installed_model_path", return_value="m.gguf"), \
            mock.patch.object(controller, "_start_monitor"):
        controller.enable()

    clause("enable_ready", controller.state == STATE_IDLE)

    backend.transcript = "xadds"
    controller.handle_hotkey_press(MODE_DICTATION)
    controller.handle_hotkey_release(MODE_DICTATION)
    clause("dictation_literal", inserted == ["xadds"] and expanded == [])

    inserted.clear()
    expanded.clear()
    backend.transcript = "please xadds now"
    controller.handle_hotkey_press(MODE_COMMAND)
    controller.handle_hotkey_release(MODE_COMMAND)
    clause("command_requires_exact_match", expanded == [] and inserted == [])

    backend.transcript = "xadds"
    controller.handle_hotkey_press(MODE_COMMAND)
    controller.handle_hotkey_release(MODE_COMMAND)
    clause("command_expands_exact_trigger", expanded == ["xadds"] and inserted == [])

    seen = []
    controller.register_form_target(lambda text: seen.append(text))
    backend.transcript = "Joao"
    controller.handle_hotkey_press(MODE_DICTATION)
    controller.handle_hotkey_release(MODE_DICTATION)
    clause("form_updates_widget_only", seen == ["Joao"] and "Joao" not in inserted)

    controller.unregister_form_target()
    inserted.clear()
    backend.transcript = ""
    controller.handle_hotkey_press(MODE_DICTATION)
    controller.handle_hotkey_release(MODE_DICTATION)
    clause("empty_does_not_insert", inserted == [])

    backend.transcript = "later"
    controller.handle_hotkey_press(MODE_DICTATION)
    ignored = not controller.handle_hotkey_press(MODE_DICTATION)
    controller.handle_hotkey_release(MODE_DICTATION)
    clause("second_press_ignored", ignored)

    inserted.clear()
    backend.transcript = "after-shutdown"
    controller.handle_hotkey_press(MODE_DICTATION)
    controller.shutdown()
    clause("shutdown_does_not_insert", inserted == [])

    ok = all(results)
    print("RESULT pass" if ok else "RESULT fail")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
