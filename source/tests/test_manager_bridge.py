"""The engine side of the Tauri manager bridge (``manager_bridge``).

The endpoint runs over real OS pipes, the way the manager process talks to
it; controllers are fakes, so every refusal path is reachable without audio
devices. Reads wait with timeouts, never bare sleeps, and every pipe and pool
is closed in ``tearDown``.
"""

import json
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
import manager_bridge
from manager_bridge import (
    APP_ERROR,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    ManagerApi,
    ManagerHost,
    RpcEndpoint,
    RpcFailure,
)

WAIT = 5.0


class Peer:
    """The manager's side of an endpoint: write requests, read replies."""

    def __init__(self, methods, on_close=None):
        engine_in, self._to_engine = os.pipe()
        self._from_engine, engine_out = os.pipe()
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.endpoint = RpcEndpoint(os.fdopen(engine_in, "rb", buffering=0), os.fdopen(engine_out, "wb", buffering=0),
                                    methods, executor=self.executor, on_close=on_close)
        self.writer = os.fdopen(self._to_engine, "wb", buffering=0)
        self.reader = os.fdopen(self._from_engine, "rb")
        self.endpoint.start()

    def send(self, message):
        raw = message if isinstance(message, bytes) else json.dumps(message).encode("utf-8")
        self.writer.write(raw + b"\n")

    def receive(self):
        box = []
        thread = threading.Thread(target=lambda: box.append(self.reader.readline()), daemon=True)
        thread.start()
        thread.join(WAIT)
        if not box:
            raise AssertionError("no message from the engine")
        return json.loads(box[0])

    def close(self):
        for stream in (self.writer, self.reader):
            try:
                stream.close()
            except OSError:
                pass
        self.endpoint.close()
        self.endpoint.join(WAIT)
        self.executor.shutdown(wait=True)


class RpcEndpointTests(unittest.TestCase):
    def setUp(self):
        self.closed = threading.Event()
        self.calls = []

        def echo(params):
            return {"echo": params.get("value")}

        def refuse(_params):
            raise RpcFailure("Aguardando o resumo.", detail="job=summary")

        def crash(_params):
            raise OSError(r"cannot open C:\Users\someone\secret\file.txt")

        self.peer = Peer({"echo": echo, "refuse": refuse, "crash": crash,
                          "record": lambda params: self.calls.append(params)},
                         on_close=self.closed.set)

    def tearDown(self):
        self.peer.close()

    def test_request_gets_its_result_by_id(self):
        self.peer.send({"jsonrpc": "2.0", "id": 7, "method": "echo", "params": {"value": "olá"}})
        self.assertEqual(self.peer.receive(), {"jsonrpc": "2.0", "id": 7, "result": {"echo": "olá"}})

    def test_unknown_method_is_refused(self):
        self.peer.send({"jsonrpc": "2.0", "id": 1, "method": "nope", "params": {}})
        self.assertEqual(self.peer.receive()["error"]["code"], METHOD_NOT_FOUND)

    def test_written_refusal_keeps_its_message_and_detail(self):
        self.peer.send({"jsonrpc": "2.0", "id": 2, "method": "refuse", "params": {}})
        error = self.peer.receive()["error"]
        self.assertEqual(error["code"], APP_ERROR)
        self.assertEqual(error["message"], "Aguardando o resumo.")
        self.assertEqual(error["data"], {"detail": "job=summary"})

    def test_unexpected_failure_answers_with_a_redacted_detail(self):
        self.peer.send({"jsonrpc": "2.0", "id": 3, "method": "crash", "params": {}})
        error = self.peer.receive()["error"]
        self.assertEqual(error["code"], INTERNAL_ERROR)
        self.assertIn("OSError", error["data"]["detail"])
        self.assertNotIn("someone", error["data"]["detail"])

    def test_params_must_be_an_object(self):
        self.peer.send({"jsonrpc": "2.0", "id": 4, "method": "echo", "params": [1]})
        self.assertEqual(self.peer.receive()["error"]["code"], INVALID_PARAMS)

    def test_a_bad_line_is_dropped_and_the_bridge_keeps_serving(self):
        self.peer.send(b"not json")
        self.peer.send({"jsonrpc": "2.0", "id": 5, "method": "echo", "params": {"value": 1}})
        self.assertEqual(self.peer.receive()["result"], {"echo": 1})

    def test_a_notification_runs_without_a_reply(self):
        self.peer.send({"jsonrpc": "2.0", "method": "record", "params": {"n": 1}})
        self.peer.send({"jsonrpc": "2.0", "id": 6, "method": "echo", "params": {"value": 2}})
        self.assertEqual(self.peer.receive()["id"], 6)
        deadline = time.monotonic() + WAIT
        while not self.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.calls, [{"n": 1}])

    def test_engine_notifications_reach_the_window(self):
        self.assertTrue(self.peer.endpoint.notify("window.show", {"destination": "voice"}))
        self.assertEqual(self.peer.receive(),
                         {"jsonrpc": "2.0", "method": "window.show", "params": {"destination": "voice"}})

    def test_end_of_input_closes_once(self):
        self.peer.writer.close()
        self.assertTrue(self.closed.wait(WAIT))
        self.assertTrue(self.peer.endpoint.closed)
        self.assertFalse(self.peer.endpoint.notify("late", {}))


class FakeMeetings:
    def __init__(self, *, notice=False, accept=True):
        self.notice = notice
        self.accept = accept
        self.granted = False
        self.started = []
        self.revoked = 0
        self.state = {"state": "idle"}

    def snapshot(self):
        return dict(self.state)

    def devices(self):
        return [{"id": "mic-1", "name": "USB  Mic", "kind": "microphone", "default": True},
                {"id": "out-1", "name": "Speakers", "kind": "system"}]

    def refresh_privacy_defaults(self):
        return {}

    def recording_notice_required(self):
        return self.notice and not self.granted

    def recording_notice(self, language=None):
        return "Esta reunião será gravada."

    def grant_recording_consent(self):
        self.granted = True

    def revoke_recording_consent(self):
        self.granted = False
        self.revoked += 1

    def start(self, settings, title=""):
        self.started.append((settings, title))
        return self.accept

    def list_sessions_page(self, **kwargs):
        self.page_args = kwargs
        return {"items": [{"id": "a", "title": "  Weekly   sync ", "status": "completed",
                           "created_at": "2026-10-01T12:00:00Z", "duration": 125},
                          {"id": "b", "title": "", "status": "failed", "created_at": None, "duration": 0}],
                "next_cursor": "c2"}


class FakeVoice:
    def status_snapshot(self):
        return {"state": "idle"}

    def is_enabled(self):
        return True


def default_sources(**overrides):
    sources = {"microphone": {"enabled": True, "selection": {"mode": "default", "default_role": "multimedia"}},
               "system": {"enabled": True, "selection": {"mode": "default", "default_role": "communications"}}}
    sources.update(overrides)
    return sources


def make_api(meetings=None, *, ready=True, settings=None, persisted=None, languages=None):
    persisted = persisted if persisted is not None else []
    return ManagerApi(
        meetings=meetings or FakeMeetings(), voice=FakeVoice(),
        settings_getter=lambda: dict(settings or {}),
        persist_settings=lambda payload: persisted.append(payload) or True,
        startup_status=lambda: (ready, "" if ready else "Preparando privacidade e recuperação local…"),
        voice_presentation=lambda: ("Ditado pronto\nEquilibrado · auto · Ctrl+Alt+Space · segurar", "success"),
        history_status=lambda status: ("Concluído", "text"),
        set_voice_enabled=lambda enabled, confirmed: {"status": "accepted"},
        on_language_changed=(languages.append if languages is not None else lambda _code: None),
        display_name="SnipVoice v1.3.0", version="1.3.0",
    )


class ManagerApiTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_start_waits_for_startup_recovery(self):
        meetings = FakeMeetings()
        with self.assertRaises(RpcFailure) as caught:
            make_api(meetings, ready=False).start_recording({"title": "", "sources": default_sources()})
        self.assertIn("Preparando", caught.exception.message)
        self.assertEqual(meetings.started, [])

    def test_notice_is_returned_before_anything_records(self):
        meetings = FakeMeetings(notice=True)
        api = make_api(meetings)
        result = api.start_recording({"title": "Daily", "sources": default_sources()})
        self.assertEqual(result, {"status": "consent", "notice": "Esta reunião será gravada."})
        self.assertEqual(meetings.started, [])

        self.assertEqual(api.start_recording({"title": "Daily", "sources": default_sources(), "consent": True}),
                         {"status": "started"})
        settings, title = meetings.started[0]
        self.assertEqual(title, "Daily")
        self.assertEqual(settings.sources, "both")
        self.assertEqual(settings.system.default_role, "communications")

    def test_a_refused_start_revokes_consent_and_says_why(self):
        meetings = FakeMeetings(accept=False)
        meetings.state = {"state": "idle", "processing": True, "processing_kind": "summary"}
        with self.assertRaises(RpcFailure) as caught:
            make_api(meetings).start_recording({"title": "", "sources": default_sources()})
        self.assertIn("Interromper e gravar", caught.exception.message)
        self.assertEqual(meetings.revoked, 1)

    def test_a_manual_device_must_still_exist(self):
        api = make_api()
        sources = default_sources(microphone={"enabled": True, "selection": {"mode": "manual", "endpoint_id": "gone"}})
        api.devices({})
        with self.assertRaises(RpcFailure) as caught:
            api.start_recording({"title": "", "sources": sources})
        self.assertEqual(caught.exception.code, INVALID_PARAMS)

        sources["microphone"]["selection"]["endpoint_id"] = "mic-1"
        self.assertEqual(api.start_recording({"title": "", "sources": sources}), {"status": "started"})

    def test_title_and_selection_are_validated(self):
        api = make_api()
        for params in ({"title": "x" * 401, "sources": default_sources()},
                       {"title": 3, "sources": default_sources()},
                       {"title": "", "sources": default_sources(system={"enabled": True, "selection": {"mode": "?"}})},
                       {"title": "", "sources": "both"}):
            with self.subTest(params=str(params)[:60]), self.assertRaises(RpcFailure) as caught:
                api.start_recording(params)
            self.assertEqual(caught.exception.code, INVALID_PARAMS)

    def test_devices_are_named_without_extra_spaces(self):
        devices = make_api().devices({})["devices"]
        self.assertEqual(devices[0], {"id": "mic-1", "name": "USB Mic", "kind": "microphone",
                                      "default": True, "communicationsDefault": None})

    def test_idle_ready_recorder_can_start(self):
        view = make_api().recording_view({"state": "idle"})
        self.assertTrue(view["canStart"])
        self.assertEqual(view["tone"], "ok")
        self.assertFalse(view["capturing"])
        self.assertEqual(view["tracks"]["microphone"]["reading"], "sem sinal")

    def test_a_held_recorder_names_the_hold(self):
        view = make_api(ready=False).recording_view({"state": "idle"})
        self.assertFalse(view["canStart"])
        self.assertIn("Preparando", view["hold"])
        self.assertTrue(view["status"].startswith("Aguardando"))

    def test_a_lost_source_is_reported_only_after_the_grace_period(self):
        snapshot = {"state": "recording", "elapsed": 61,
                    "capture_health": {"microphone": {"state": "receiving"}, "system": {"state": "disconnected"}}}
        now = time.monotonic()
        fresh = make_api().recording_view(snapshot, {"system": now})
        self.assertEqual(fresh["lost"], [])
        lost = make_api().recording_view(snapshot, {"system": now - 65})["lost"]
        self.assertEqual([item["track"] for item in lost], ["system"])
        self.assertGreaterEqual(lost[0]["silentFor"], 65)
        view = make_api().recording_view(snapshot, {"system": now - 65})
        self.assertEqual(view["tone"], "warning")
        self.assertTrue(view["canStop"])

    def test_library_rows_are_written_words(self):
        meetings = FakeMeetings()
        meetings.state = {"state": "idle", "processing": True, "processing_kind": "summary",
                          "processing_session": "b"}
        page = make_api(meetings).library_list({"query": " weekly "})
        self.assertEqual(meetings.page_args["query"], "weekly")
        self.assertEqual(page["nextCursor"], "c2")
        first, second = page["items"]
        self.assertEqual((first["title"], first["status"], first["duration"]), ("Weekly sync", "Concluída", "00:02:05"))
        self.assertEqual((second["status"], second["tone"]), ("Processando", "busy"))
        self.assertEqual(second["date"], "Sem data")

    def test_language_change_persists_and_returns_the_catalog(self):
        persisted, languages = [], []
        api = make_api(persisted=persisted, languages=languages)
        result = api.set_language({"language": "en-US"})
        self.assertEqual(persisted, [{"language": "en-US"}])
        self.assertEqual(languages, ["en-US"])
        self.assertEqual(result["catalog"]["Geral"], "General")
        with self.assertRaises(RpcFailure):
            api.set_language({"language": "fr-FR"})

    def test_appearance_accepts_only_known_preferences(self):
        persisted = []
        api = make_api(persisted=persisted)
        self.assertEqual(api.set_appearance({"appearance": "light"}), {"appearance": "light"})
        with self.assertRaises(RpcFailure):
            api.set_appearance({"appearance": "sepia"})
        self.assertEqual(persisted, [{"appearance": "light"}])

    def test_hello_describes_the_engine(self):
        hello = make_api(settings={"appearance": "nope"}).hello({})
        self.assertEqual((hello["protocol"], hello["language"], hello["appearance"]), (1, "pt-BR", "dark"))
        self.assertEqual(hello["catalog"], {})
        self.assertEqual(hello["startup"], {"ready": True, "message": ""})


class FakeProcess:
    """A manager process whose pipes the test drives."""

    def __init__(self):
        child_in, self._stdin_w = os.pipe()
        self._stdout_r, child_out = os.pipe()
        self.stdin = os.fdopen(self._stdin_w, "wb", buffering=0)
        self.stdout = os.fdopen(self._stdout_r, "rb", buffering=0)
        self.child_reader = os.fdopen(child_in, "rb")
        self.child_writer = os.fdopen(child_out, "wb", buffering=0)
        self.waited = False

    def read_message(self):
        box = []
        thread = threading.Thread(target=lambda: box.append(self.child_reader.readline()), daemon=True)
        thread.start()
        thread.join(WAIT)
        if not box or not box[0]:
            raise AssertionError("no message for the manager")
        return json.loads(box[0])

    def exit(self):
        self.child_writer.close()

    def wait(self, _timeout=None):
        self.waited = True
        return 0

    def kill(self):
        pass

    def close(self):
        for stream in (self.child_reader, self.child_writer, self.stdin, self.stdout):
            try:
                stream.close()
            except OSError:
                pass


class ManagerHostTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.closed = threading.Event()

        def popen(args, **kwargs):
            self.assertEqual(args, ["manager.exe"])
            process = FakeProcess()
            self.processes.append(process)
            return process

        self.host = ManagerHost(make_api(), "manager.exe", on_closed=self.closed.set, popen=popen)

    def tearDown(self):
        self.host.shutdown()
        for process in self.processes:
            process.close()

    def _next(self, process, method):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            message = process.read_message()
            if message.get("method") == method:
                return message
        raise AssertionError(f"{method} never arrived")

    def test_show_starts_once_then_focuses(self):
        self.assertTrue(self.host.show())
        self.assertTrue(self.host.show("voice"))
        self.assertEqual(len(self.processes), 1)
        process = self.processes[0]
        self.assertEqual(self._next(process, "window.show")["params"], {})
        self.assertEqual(self._next(process, "window.show")["params"], {"destination": "voice"})

    def test_recording_state_is_pushed_on_start(self):
        self.host.show()
        view = self._next(self.processes[0], "recording.state")["params"]
        self.assertTrue(view["canStart"])

    def test_a_closed_window_reports_once_and_respawns_on_show(self):
        self.host.show()
        self.processes[0].exit()
        self.assertTrue(self.closed.wait(WAIT))
        self.assertFalse(self.host.running)
        self.host.show("library")
        self.assertEqual(len(self.processes), 2)

    def test_a_window_that_never_greets_falls_back_instead_of_closing(self):
        unusable = threading.Event()
        closed = threading.Event()
        processes = []

        def popen(_args, **_kwargs):
            processes.append(FakeProcess())
            return processes[-1]

        host = ManagerHost(make_api(), "manager.exe", on_closed=closed.set, on_unusable=unusable.set, popen=popen)
        try:
            host.show()
            processes[0].exit()
            self.assertTrue(unusable.wait(WAIT))
            self.assertFalse(closed.is_set())
        finally:
            host.shutdown()
            for process in processes:
                process.close()

    def test_a_window_that_greeted_then_closes_is_an_ordinary_close(self):
        unusable = threading.Event()
        closed = threading.Event()
        processes = []

        def popen(_args, **_kwargs):
            processes.append(FakeProcess())
            return processes[-1]

        host = ManagerHost(make_api(), "manager.exe", on_closed=closed.set, on_unusable=unusable.set, popen=popen)
        try:
            host.show()
            process = processes[0]
            hello = {"jsonrpc": "2.0", "id": 1, "method": "app.hello", "params": {}}
            process.child_writer.write(json.dumps(hello).encode() + bytes([10]))
            reply = self._next_reply(process, 1)
            self.assertEqual(reply["result"]["protocol"], manager_bridge.PROTOCOL_VERSION)
            process.exit()
            self.assertTrue(closed.wait(WAIT))
            self.assertFalse(unusable.is_set())
        finally:
            host.shutdown()
            for process in processes:
                process.close()

    def _next_reply(self, process, request_id):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            message = process.read_message()
            if message.get("id") == request_id:
                return message
        raise AssertionError(f"no reply to request {request_id}")

    def test_a_window_that_cannot_start_says_so(self):
        failed = []

        def missing(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file")

        host = ManagerHost(make_api(), "manager.exe", on_spawn_failed=lambda: failed.append(True), popen=missing)
        self.assertFalse(host.show())
        self.assertEqual(failed, [True])
        self.assertFalse(host.running)

    def test_quit_closes_the_window_without_the_tray_notice(self):
        self.host.show()
        self.host.shutdown()
        self.assertTrue(self.processes[0].waited)
        self.assertFalse(self.closed.is_set())
        self.assertFalse(self.host.show())


class ExecutableLookupTests(unittest.TestCase):
    def test_override_must_exist(self):
        previous = os.environ.get(manager_bridge.MANAGER_EXE_ENV)
        try:
            os.environ[manager_bridge.MANAGER_EXE_ENV] = os.path.join(SOURCE_DIR, "missing-manager.exe")
            self.assertIsNone(manager_bridge.manager_executable(SOURCE_DIR))
            os.environ[manager_bridge.MANAGER_EXE_ENV] = os.path.abspath(__file__)
            self.assertEqual(manager_bridge.manager_executable(SOURCE_DIR), os.path.abspath(__file__))
        finally:
            if previous is None:
                os.environ.pop(manager_bridge.MANAGER_EXE_ENV, None)
            else:
                os.environ[manager_bridge.MANAGER_EXE_ENV] = previous

    def test_tauri_manager_is_the_default_and_tk_opts_out(self):
        previous = os.environ.pop(manager_bridge.MANAGER_KIND_ENV, None)
        try:
            self.assertTrue(manager_bridge.tauri_manager_requested())
            os.environ[manager_bridge.MANAGER_KIND_ENV] = " Tk "
            self.assertFalse(manager_bridge.tauri_manager_requested())
            os.environ[manager_bridge.MANAGER_KIND_ENV] = "tauri"
            self.assertTrue(manager_bridge.tauri_manager_requested())
        finally:
            os.environ.pop(manager_bridge.MANAGER_KIND_ENV, None)
            if previous is not None:
                os.environ[manager_bridge.MANAGER_KIND_ENV] = previous


if __name__ == "__main__":
    unittest.main()
