"""Packaged check that the bundled manager window starts and reaches the engine.

``Snipvoice.exe --manager-window-probe`` runs before desktop imports or the
instance lock. It starts the bundled ``snipvoice-manager`` with no visible
window (``headless`` in ``app.hello``), waits for the page to ask for
``app.hello`` over the stdio bridge, then closes stdin so the window exits.
That proves the bundled binary, its embedded page and the bridge together.
"""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from manager_bridge import PROTOCOL_VERSION, RpcEndpoint, manager_executable

TIMEOUT_SECONDS = 60.0
EXIT_GRACE_SECONDS = 10.0


def _resource_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def main(executable=None, timeout=TIMEOUT_SECONDS):
    executable = executable or manager_executable(_resource_dir())
    if executable is None:
        print("manager window probe: snipvoice-manager is not bundled", file=sys.stderr)
        return 1
    greeted = threading.Event()

    def hello(_params):
        greeted.set()
        return {"protocol": PROTOCOL_VERSION, "displayName": "Snipvoice", "version": "probe", "language": "pt-BR",
                "languages": {"pt-BR": "Português (Brasil)"}, "catalog": {}, "appearance": "dark",
                "startup": {"ready": True, "message": ""}, "enginePid": os.getpid(), "headless": True}

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        process = subprocess.Popen([executable], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, creationflags=flags, bufsize=0)
    except OSError as error:
        print(f"manager window probe: could not start {os.path.basename(executable)}: {error}", file=sys.stderr)
        return 1
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="manager-probe")
    endpoint = RpcEndpoint(process.stdout, process.stdin, {"app.hello": hello}, executor=executor)
    endpoint.start()
    try:
        answered = greeted.wait(timeout)
    finally:
        endpoint.close()
        try:
            process.wait(EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(EXIT_GRACE_SECONDS)
            print("manager window probe: the window did not exit after its input closed", file=sys.stderr)
            answered = False
        executor.shutdown(wait=False, cancel_futures=True)
    if not answered:
        print("manager window probe: the window never asked for app.hello", file=sys.stderr)
        return 1
    print("manager window probe: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
