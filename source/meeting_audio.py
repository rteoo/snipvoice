"""Bounded, versioned transport to native microphone/render capture helpers."""

import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading

from i18n import tr

VERSION = 1
MAX_HEADER = 65536
MAX_PAYLOAD = 4 * 1024 * 1024
# ceiling: four maximum-sized native blocks (~16 MiB); disk stalls stop capture.
QUEUE_BLOCKS = 4


class MeetingAudioError(RuntimeError):
    def __init__(self, message, resource_live=False):
        super().__init__(message)
        self.resource_live = resource_live


def _read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            if not result:
                raise EOFError(tr("O capturador de áudio encerrou o fluxo."))
            raise MeetingAudioError(tr("O capturador enviou um bloco de áudio incompleto."))
        result.extend(chunk)
    return bytes(result)


def read_frame(stream):
    size = struct.unpack("<I", _read_exact(stream, 4))[0]
    if not 0 < size <= MAX_HEADER:
        raise MeetingAudioError(tr("Cabeçalho do capturador inválido."))
    try:
        event = json.loads(_read_exact(stream, size))
    except (ValueError, UnicodeError) as exc:
        raise MeetingAudioError(tr("Resposta do capturador inválida.")) from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise MeetingAudioError(tr("Evento do capturador inválido."))
    length = struct.unpack("<I", _read_exact(stream, 4))[0]
    if length > MAX_PAYLOAD:
        raise MeetingAudioError(tr("Bloco do capturador excedeu o limite seguro."))
    payload = _read_exact(stream, length) if length else b""
    if event["type"] == "audio":
        rate, channels, frames = (event.get(k) for k in ("rate", "channels", "frames"))
        stamp = event.get("timestamp")
        if (any(isinstance(v, bool) or not isinstance(v, int) for v in (rate, channels, frames))
                or not 8000 <= rate <= 192000 or not 1 <= channels <= 8
                or frames <= 0 or frames * channels * 4 != length
                or not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not math.isfinite(stamp) or stamp < 0
                or event.get("track") not in ("microphone", "system")
                or not isinstance(event.get("sequence"), int)
                or isinstance(event["sequence"], bool)
                or event["sequence"] < 0):
            raise MeetingAudioError(tr("Formato ou relógio de captura inválido."))
    elif payload:
        raise MeetingAudioError(tr("Evento de controle contém áudio inesperado."))
    return event, payload


def encode_frame(event, payload=b""):
    header = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not 0 < len(header) <= MAX_HEADER or len(payload) > MAX_PAYLOAD:
        raise MeetingAudioError(tr("Evento excedeu o limite de transporte."))
    return struct.pack("<I", len(header)) + header + struct.pack("<I", len(payload)) + payload


def default_helper_path():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    name = "snipvoice-capture.exe" if os.name == "nt" else "snipvoice-capture"
    return root / "native" / "bin" / name


class NativeCapture:
    def __init__(self, helper_path=None, popen=None):
        self.path = Path(helper_path) if helper_path else default_helper_path()
        self._popen = popen or subprocess.Popen
        self._process = None
        self._queue = queue.Queue(QUEUE_BLOCKS)
        self._ready = threading.Event()
        self._done = threading.Event()
        self._failure = None
        self._reader = None
        self._diagnostics = None
        self._generation = 0

    def _spawn(self, arguments):
        if sys.platform not in ("win32", "darwin"):
            raise MeetingAudioError(tr("A captura de reuniões suporta Windows e macOS."))
        if not self.path.is_file():
            raise MeetingAudioError(tr("Capturador nativo ausente. Compile ou instale o pacote do SnipVoice."))
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                   "stderr": subprocess.PIPE, "bufsize": 0}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        return self._popen([str(self.path), *arguments], **options)

    def _drain_diagnostics(self):
        # Native diagnostics never contain transcripts. Do not retain unbounded output.
        try:
            while self._process.stderr.read(4096):
                pass
        except (OSError, ValueError):
            return

    def _run_reader(self):
        try:
            while True:
                event, payload = read_frame(self._process.stdout)
                generation = event.get("generation")
                if (not isinstance(generation, int) or isinstance(generation, bool)
                        or generation < 0 or generation != self._generation):
                    raise MeetingAudioError(tr("O capturador enviou uma sessão antiga."))
                if not self._ready.is_set():
                    if event["type"] != "ready" or event.get("version") != VERSION:
                        raise MeetingAudioError(tr("Versão ou inicialização do capturador incompatível."))
                    self._ready.set()
                    continue
                try:
                    self._queue.put((event, payload), timeout=0.25)
                except queue.Full as exc:
                    raise MeetingAudioError(tr("O disco não acompanhou a captura; o áudio parcial foi preservado.")) from exc
                if event["type"] == "stopped":
                    return
        except EOFError:
            self._failure = MeetingAudioError(tr("O capturador encerrou antes de confirmar a parada."))
        except (OSError, ValueError, MeetingAudioError) as exc:
            self._failure = exc
        finally:
            self._done.set()
            self._ready.set()

    def start(self, settings, generation):
        if self._process is not None:
            raise MeetingAudioError(tr("Já existe uma captura em andamento."))
        self._generation = generation
        self._process = self._spawn(["--capture", "--sources", settings.sources,
                                    "--microphone", settings.microphone.argument(),
                                    "--system", settings.system.argument(),
                                    "--generation", str(generation)])
        self._diagnostics = threading.Thread(target=self._drain_diagnostics, daemon=True)
        self._reader = threading.Thread(target=self._run_reader, daemon=True)
        self._diagnostics.start()
        self._reader.start()
        if not self._ready.wait(8) or self._failure is not None:
            self.stop(force=True)
            raise MeetingAudioError(tr("Não foi possível iniciar a captura nativa.")) from self._failure

    def read_event(self, timeout=0.1):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._failure is not None:
                raise MeetingAudioError(str(self._failure)) from self._failure
            return None

    def command(self, command):
        if self._process is None or self._process.poll() is not None:
            raise MeetingAudioError(tr("O capturador de áudio não está ativo."))
        try:
            self._process.stdin.write((json.dumps({"command": command}) + "\n").encode())
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MeetingAudioError(tr("Não foi possível controlar a captura.")) from exc

    def pause(self):
        self.command("pause")

    def resume(self):
        self.command("resume")

    def stop(self, force=False):
        process = self._process
        if process is None:
            return
        if not force and process.poll() is None:
            try:
                self.command("stop")
            except MeetingAudioError:
                force = True
        try:
            process.wait(timeout=3 if not force else 0.01)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(2)
            if self._reader.is_alive():
                raise MeetingAudioError(tr("O capturador ainda está encerrando; aguarde antes de gravar novamente."),
                                        resource_live=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if process.returncode and not force:
            raise MeetingAudioError(tr("O capturador encerrou com erro; o áudio parcial foi preservado."))

    def _query(self, argument):
        process = self._spawn([argument])
        values, errors = [], []

        def reader():
            try:
                # ceiling: a probe emits at most two bounded control frames.
                for _ in range(2):
                    try:
                        event, _ = read_frame(process.stdout)
                    except EOFError:
                        break
                    values.append(event)
            except (OSError, ValueError, MeetingAudioError) as exc:
                errors.append(exc)

        def stderr_reader():
            try:
                while process.stderr.read(4096):
                    pass
            except (OSError, ValueError):
                return

        thread = threading.Thread(target=reader, daemon=True)
        diagnostics = threading.Thread(target=stderr_reader, daemon=True)
        thread.start()
        diagnostics.start()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=3)
            raise MeetingAudioError(tr("O capturador não respondeu à consulta.")) from exc
        finally:
            thread.join(2)
            diagnostics.join(2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        if process.returncode or errors or thread.is_alive():
            raise MeetingAudioError(tr("Falha na consulta ao capturador nativo."))
        return values

    def list_devices(self):
        frames = self._query("--list")
        if (not frames or frames[0].get("type") != "devices"
                or frames[0].get("version") != VERSION):
            raise MeetingAudioError(tr("Lista de dispositivos incompatível."))
        devices = frames[0].get("devices")
        if not isinstance(devices, list) or len(devices) > 256:
            raise MeetingAudioError(tr("Lista de dispositivos inválida."))
        for device in devices:
            if (not isinstance(device, dict) or device.get("kind") not in ("microphone", "system")
                    or not isinstance(device.get("id"), str)
                    or not isinstance(device.get("name"), str)):
                raise MeetingAudioError(tr("Dispositivo de áudio inválido."))
        return devices

    def self_test(self):
        frames = self._query("--self-test")
        if not frames or frames[0].get("type") != "ready" or frames[0].get("version") != VERSION:
            raise MeetingAudioError(tr("Autoteste do capturador falhou."))
        return True
