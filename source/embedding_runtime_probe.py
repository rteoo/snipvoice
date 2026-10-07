"""Model-free packaged proof of the pinned native embedding runtime."""


def probe_embedding_runtime():
    from llama_runtime import verify_llama_runtime
    verify_llama_runtime()
    return True


def main():
    try:
        probe_embedding_runtime()
    except Exception:
        print("EMBEDDING_RUNTIME_PROBE fail: install the approved custom llama.cpp runtime")
        return 1
    print("EMBEDDING_RUNTIME_PROBE pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
