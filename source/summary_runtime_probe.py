"""Release diagnostic for the packaged llama.cpp summary runtime."""


def probe_summary_runtime():
    from llama_cpp import Llama
    if not callable(Llama):
        raise RuntimeError("llama_cpp.Llama is unavailable")
    return True


def main():
    try:
        probe_summary_runtime()
    except Exception as exc:
        print(f"SUMMARY_RUNTIME_PROBE fail: {exc}")
        return 1
    print("SUMMARY_RUNTIME_PROBE pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
