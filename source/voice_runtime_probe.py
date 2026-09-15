"""Small release diagnostic for the packaged optional voice runtime."""

from voice_runtime import create_backend


def probe_voice_runtime():
    """Prove the packaged desktop and native voice imports are loadable."""
    import tkinter  # noqa: F401 - catch a package built without Tcl/Tk
    import sounddevice  # noqa: F401 - import itself exercises PortAudio loading
    import soxr  # noqa: F401 - import exercises the bundled libsoxr extension
    import transcribe_cpp_native  # noqa: F401 - exercise bundled native package

    backend = create_backend()
    if not backend.available():
        raise RuntimeError("transcribe.cpp backend is unavailable")
    return True


def main():
    try:
        probe_voice_runtime()
    except Exception as exc:
        print(f"VOICE_RUNTIME_PROBE fail: {exc}")
        return 1
    print("VOICE_RUNTIME_PROBE pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
