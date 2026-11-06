"""Release diagnostic for the packaged llama.cpp summary runtime."""


def probe_summary_runtime():
    from llama_runtime import verify_llama_runtime
    verify_llama_runtime()
    from llama_cpp import Llama
    if not callable(Llama):
        raise RuntimeError("llama_cpp.Llama is unavailable")
    from llama_runtime import load_backends
    devices = load_backends()
    if not any(device["type"] == "cpu" for device in devices):
        raise RuntimeError("The llama.cpp runtime registered no CPU backend")
    # Device types only: names identify the host's hardware.
    return sorted(device["type"] for device in devices)


def main():
    try:
        devices = probe_summary_runtime()
    except Exception as exc:
        print(f"SUMMARY_RUNTIME_PROBE fail: {exc}")
        return 1
    print("SUMMARY_RUNTIME_PROBE pass: devices=" + ",".join(devices))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
