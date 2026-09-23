import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_paths
import data_relocation
from data_relocation import RelocationError

TMP = Path(__file__).resolve().parent / "tmp"
TMP.mkdir(exist_ok=True)


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class RelocationTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.config = os.path.join(self.root, "config")
        self.src = os.path.join(self.root, "home", ".snipvoice")
        self.dst = os.path.join(self.root, "D", "snipvoice")
        os.makedirs(os.path.dirname(self.dst))
        _write(os.path.join(self.src, "settings.json"), "{}")
        _write(os.path.join(self.src, "voice-history", "entry.json"), "history")
        _write(os.path.join(self.src, "recordings", "a.wav"), "wav")
        self.final_wav = os.path.join(self.src, "recordings", "a.wav")
        _write(
            os.path.join(self.src, "meetings", "m1", "metadata.json"),
            json.dumps({"final_audio": {"path": self.final_wav}}),
        )
        _write(
            os.path.join(self.src, "meetings", "m2", "metadata.json"),
            json.dumps({"final_audio": {"path": os.path.join(self.root, "exports", "b.wav")}}),
        )
        patches = [
            mock.patch.object(app_paths, "config_dir", return_value=self.config),
            mock.patch.object(app_paths, "default_data_dir", return_value=self.src),
            mock.patch.dict(os.environ, {app_paths.ENV_HOME: ""}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _metadata(self, home, meeting):
        with open(os.path.join(home, "meetings", meeting, "metadata.json"), encoding="utf-8") as handle:
            return json.load(handle)


class ValidationTests(RelocationTestCase):
    def test_rejects_nested_same_and_non_empty_targets(self):
        with self.assertRaisesRegex(RelocationError, "atual"):
            data_relocation.validate_target(self.src, self.src)
        with self.assertRaisesRegex(RelocationError, "dentro"):
            data_relocation.validate_target(self.src, os.path.join(self.src, "sub"))
        with self.assertRaisesRegex(RelocationError, "dentro"):
            data_relocation.validate_target(self.src, os.path.dirname(self.src))
        _write(os.path.join(self.dst, "other.txt"))
        with self.assertRaisesRegex(RelocationError, "vazia"):
            data_relocation.validate_target(self.src, self.dst)

    def test_rejects_relative_root_and_missing_parent(self):
        with self.assertRaisesRegex(RelocationError, "completo"):
            data_relocation.validate_target(self.src, "relative")
        with self.assertRaisesRegex(RelocationError, "raiz"):
            data_relocation.validate_target(self.src, os.path.abspath(os.sep))
        with self.assertRaisesRegex(RelocationError, "não existe"):
            data_relocation.validate_target(self.src, os.path.join(self.root, "nope", "x"))

    def test_env_override_disables_relocation(self):
        with mock.patch.dict(os.environ, {app_paths.ENV_HOME: self.src}):
            with self.assertRaisesRegex(RelocationError, app_paths.ENV_HOME):
                data_relocation.validate_target(self.src, self.dst)

    def test_choice_nests_inside_a_folder_with_content(self):
        parent = os.path.dirname(self.dst)
        _write(os.path.join(parent, "unrelated.txt"))
        self.assertEqual(
            data_relocation.target_for_choice(parent),
            os.path.join(parent, data_relocation.DEFAULT_FOLDER_NAME),
        )
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        self.assertEqual(data_relocation.target_for_choice(empty), empty)
        root = os.path.abspath(os.sep)
        self.assertEqual(
            data_relocation.target_for_choice(root),
            os.path.join(root, data_relocation.DEFAULT_FOLDER_NAME),
        )


class MoveTests(RelocationTestCase):
    def _assert_moved(self):
        self.assertFalse(os.path.exists(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "voice-history", "entry.json")))
        self.assertEqual(app_paths.read_location(), {"data_dir": self.dst})
        self.assertEqual(app_paths.configured_data_dir(), self.dst)
        self.assertEqual(
            self._metadata(self.dst, "m1")["final_audio"]["path"],
            os.path.join(self.dst, "recordings", "a.wav"),
        )
        self.assertEqual(
            self._metadata(self.dst, "m2")["final_audio"]["path"],
            os.path.join(self.root, "exports", "b.wav"),
        )

    def test_nothing_pending_is_a_no_op(self):
        self.assertEqual(data_relocation.complete_pending_relocation(), "")
        self.assertEqual(app_paths.configured_data_dir(), self.src)

    def test_request_then_start_moves_by_rename(self):
        data_relocation.request_relocation(self.src, self.dst)
        self.assertEqual(app_paths.configured_data_dir(), self.src)
        message = data_relocation.complete_pending_relocation()
        self.assertEqual(message, f"Dados movidos para {self.dst}.")
        self._assert_moved()

    def test_cross_volume_move_copies_verifies_and_deletes(self):
        data_relocation.request_relocation(self.src, self.dst)
        with mock.patch.object(data_relocation.os, "rename", side_effect=OSError(18, "EXDEV")):
            message = data_relocation.complete_pending_relocation()
        self.assertEqual(message, f"Dados movidos para {self.dst}.")
        self._assert_moved()
        self.assertFalse(os.path.exists(os.path.join(self.dst, ".snipvoice-move-incomplete")))

    def test_failed_copy_keeps_the_original_and_cleans_the_target(self):
        data_relocation.request_relocation(self.src, self.dst)
        with mock.patch.object(data_relocation.os, "rename", side_effect=OSError(18, "EXDEV")), \
                mock.patch.object(data_relocation.shutil, "copytree", side_effect=OSError("disk")):
            message = data_relocation.complete_pending_relocation()
        self.assertIn("Nada foi alterado", message)
        self.assertTrue(os.path.isfile(os.path.join(self.src, "settings.json")))
        self.assertFalse(os.path.exists(self.dst))
        self.assertEqual(app_paths.read_location(), {"data_dir": self.src})

    def test_insufficient_space_refuses_before_copying(self):
        data_relocation.request_relocation(self.src, self.dst)
        usage = mock.Mock(free=1)
        with mock.patch.object(data_relocation.os, "rename", side_effect=OSError(18, "EXDEV")), \
                mock.patch.object(data_relocation.shutil, "disk_usage", return_value=usage):
            message = data_relocation.complete_pending_relocation()
        self.assertIn("espaço livre", message)
        self.assertTrue(os.path.isfile(os.path.join(self.src, "settings.json")))
        self.assertFalse(os.path.exists(self.dst))

    def test_crash_after_rename_finishes_on_next_start(self):
        data_relocation.request_relocation(self.src, self.dst)
        os.rename(self.src, self.dst)
        self.assertEqual(
            data_relocation.complete_pending_relocation(), f"Dados movidos para {self.dst}."
        )
        self._assert_moved()

    def test_crash_mid_copy_restarts_the_copy(self):
        data_relocation.request_relocation(self.src, self.dst)
        _write(os.path.join(self.dst, ".snipvoice-move-incomplete"), "")
        _write(os.path.join(self.dst, "settings.json"), "partial")
        with mock.patch.object(data_relocation.os, "rename", side_effect=OSError(18, "EXDEV")):
            message = data_relocation.complete_pending_relocation()
        self.assertEqual(message, f"Dados movidos para {self.dst}.")
        self._assert_moved()
        with open(os.path.join(self.dst, "settings.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{}")

    def test_old_files_that_cannot_be_deleted_are_reported(self):
        data_relocation.request_relocation(self.src, self.dst)
        real_remove = os.remove

        def locked(path):
            if path.endswith("entry.json") and path.startswith(self.src):
                raise PermissionError("in use")
            return real_remove(path)

        with mock.patch.object(data_relocation.os, "rename", side_effect=OSError(18, "EXDEV")), \
                mock.patch.object(data_relocation.os, "remove", side_effect=locked):
            message = data_relocation.complete_pending_relocation()
        self.assertIn("remova essa pasta manualmente", message)
        self.assertEqual(app_paths.read_location(), {"data_dir": self.dst})
        self.assertTrue(os.path.isfile(os.path.join(self.src, "voice-history", "entry.json")))
        self.assertFalse(os.path.exists(os.path.join(self.src, "settings.json")))

    def test_unrelated_files_added_to_the_old_folder_survive_deletion(self):
        data_relocation.request_relocation(self.src, self.dst)
        manifest = data_relocation._manifest(self.src)
        data_relocation._copy_verified(self.src, self.dst)
        _write(os.path.join(self.src, "added-later.txt"))
        self.assertFalse(data_relocation._remove_copied(self.src, manifest))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "added-later.txt")))


class ResolveTests(RelocationTestCase):
    def test_unreachable_chosen_folder_falls_back_without_changing_the_pointer(self):
        missing = os.path.join(self.root, "gone", "snipvoice")
        app_paths.write_location({"data_dir": missing})
        with mock.patch.object(app_paths.os, "makedirs", side_effect=lambda path, exist_ok: (
                (_ for _ in ()).throw(OSError("no drive")) if path == missing else None)):
            path, warning = app_paths.resolve_data_dir()
        self.assertEqual(path, self.src)
        self.assertIn(missing, warning)
        self.assertEqual(app_paths.read_location(), {"data_dir": missing})

    def test_malformed_pointer_reads_as_default(self):
        os.makedirs(self.config)
        _write(os.path.join(self.config, app_paths.LOCATION_NAME), "not json")
        self.assertEqual(app_paths.configured_data_dir(), self.src)



class ModelsRelocationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.config = os.path.join(self.root, "config")
        self.src = os.path.join(self.root, "local", "Snipvoice")
        self.dst = os.path.join(self.root, "D", "models")
        _write(os.path.join(self.src, "voice-models", "parakeet", "parakeet.gguf"), "voice")
        _write(os.path.join(self.src, "voice-models", "parakeet", "manifest.json"), "{}")
        _write(os.path.join(self.src, "summary-models", "qwen", "qwen.gguf"), "summary")
        _write(os.path.join(self.src, app_paths.LOCATION_NAME), "{}")
        _write(os.path.join(self.dst, "lmstudio", "other.gguf"), "foreign")
        patches = [
            mock.patch.object(app_paths, "config_dir", return_value=self.config),
            mock.patch.object(app_paths, "default_models_dir", return_value=self.src),
            mock.patch.dict(os.environ, {"SNIPVOICE_VOICE_CACHE": "", "SNIPVOICE_SUMMARY_CACHE": ""}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _assert_moved(self):
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "voice-models", "parakeet", "parakeet.gguf")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "summary-models", "qwen", "qwen.gguf")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "lmstudio", "other.gguf")))
        self.assertFalse(os.path.exists(os.path.join(self.src, "voice-models")))
        self.assertFalse(os.path.exists(os.path.join(self.src, "summary-models")))
        self.assertTrue(os.path.isfile(os.path.join(self.src, app_paths.LOCATION_NAME)))
        self.assertEqual(app_paths.configured_models_dir(), self.dst)

    def test_shared_folder_with_other_files_is_accepted(self):
        self.assertEqual(data_relocation.validate_models_target(self.src, self.dst), self.dst)

    def test_rejects_same_nested_and_env_locked_targets(self):
        with self.assertRaisesRegex(RelocationError, "atual"):
            data_relocation.validate_models_target(self.src, self.src)
        with self.assertRaisesRegex(RelocationError, "dentro"):
            data_relocation.validate_models_target(
                self.src, os.path.join(self.src, "voice-models", "x"))
        with mock.patch.dict(os.environ, {"SNIPVOICE_SUMMARY_CACHE": self.src}):
            with self.assertRaisesRegex(RelocationError, "SNIPVOICE"):
                data_relocation.validate_models_target(self.src, self.dst)

    def test_request_then_start_moves_models_by_rename(self):
        data_relocation.request_models_relocation(self.src, self.dst)
        self.assertEqual(app_paths.configured_models_dir(), self.src)
        self.assertEqual(
            data_relocation.complete_pending_models_relocation(),
            f"Modelos movidos para {self.dst}.",
        )
        self._assert_moved()

    def test_cross_volume_models_are_copied_verified_and_deleted(self):
        data_relocation.request_models_relocation(self.src, self.dst)
        with mock.patch.object(data_relocation.os, "rename", side_effect=OSError(18, "EXDEV")):
            message = data_relocation.complete_pending_models_relocation()
        self.assertEqual(message, f"Modelos movidos para {self.dst}.")
        self._assert_moved()

    def test_model_already_at_destination_is_kept_and_old_copy_left(self):
        _write(os.path.join(self.dst, "voice-models", "parakeet", "parakeet.gguf"), "theirs")
        data_relocation.request_models_relocation(self.src, self.dst)
        message = data_relocation.complete_pending_models_relocation()
        self.assertIn("parakeet", message)
        with open(os.path.join(self.dst, "voice-models", "parakeet", "parakeet.gguf"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "theirs")
        self.assertTrue(os.path.isfile(os.path.join(self.src, "voice-models", "parakeet", "parakeet.gguf")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "summary-models", "qwen", "qwen.gguf")))
        self.assertEqual(app_paths.configured_models_dir(), self.dst)

    def test_failure_puts_renamed_models_back_and_keeps_the_old_root(self):
        data_relocation.request_models_relocation(self.src, self.dst)
        real_rename = os.rename

        def rename(source, target):
            if "qwen" in source and source.startswith(self.src):
                raise OSError(18, "EXDEV")
            return real_rename(source, target)

        with mock.patch.object(data_relocation.os, "rename", side_effect=rename), \
                mock.patch.object(data_relocation.shutil, "copytree", side_effect=OSError("disk")):
            message = data_relocation.complete_pending_models_relocation()
        self.assertIn("Nada foi alterado", message)
        self.assertTrue(os.path.isfile(os.path.join(self.src, "voice-models", "parakeet", "parakeet.gguf")))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "summary-models", "qwen", "qwen.gguf")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "voice-models", "parakeet")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "summary-models", "qwen")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "lmstudio", "other.gguf")))
        self.assertEqual(app_paths.configured_models_dir(), self.src)

    def test_active_caches_follow_the_chosen_root(self):
        import summary_models
        import voice_models

        app_paths.write_location({"models_dir": self.dst})
        self.assertEqual(voice_models.voice_cache_dir(), os.path.join(self.dst, "voice-models"))
        self.assertEqual(summary_models.summary_cache_dir(), os.path.join(self.dst, "summary-models"))
        with mock.patch.dict(os.environ, {"SNIPVOICE_VOICE_CACHE": self.root}):
            self.assertEqual(voice_models.voice_cache_dir(), self.root)


if __name__ == "__main__":
    unittest.main()
