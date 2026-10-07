"""Reproducible synthetic holdout; prints metrics only, never query text."""

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import time

from meeting_index import MeetingIndex


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def wilson_interval(successes, count):
    proportion, z = successes / count, 1.96
    center = proportion + z * z / (2 * count)
    radius = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * count)) / count)
    denominator = 1 + z * z / count
    return [(center - radius) / denominator, (center + radius) / denominator]


def peak_rss(pid):
    """Return per-process peak working set; never claim a combined tree peak."""
    if os.name != "nt":
        if pid != os.getpid():
            return None
        import resource
        import sys
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (field, ctypes.c_size_t) for field in (
                "peak", "current", "paged_peak", "paged", "nonpaged_peak", "nonpaged",
                "pagefile", "pagefile_peak")]
    kernel, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        return None
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return counters.peak
        return None
    finally:
        kernel.CloseHandle(handle)


def run(semantic=False, *, embedding_cache=None):
    fixture = json.loads((Path(__file__).parent / "tests/fixtures/semantic_queries.json").read_text("utf-8"))
    temporary_root = Path(__file__).parent / "tests/tmp"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        index = MeetingIndex(Path(directory) / "library.sqlite")
        segments = [{"id": str(i), "text": text, "start": i * 10, "end": i * 10 + 8}
                    for i, text in enumerate(fixture["passages"])]
        metadata = {"id": "synthetic", "title": "Synthetic holdout", "status": "completed",
                    "revisions": [{"id": "revision-1", "status": "completed"}]}
        index.index_session(metadata, transcripts={"revision-1": segments})
        semantic_index = runtime = None
        build_ms = None
        worker_peaks = []
        try:
            if semantic:
                from embedding_runtime import EmbeddingRuntime
                from meeting_semantic import SemanticIndex, source_record
                start = time.perf_counter()
                runtime = EmbeddingRuntime(cache_dir=embedding_cache)
                semantic_index = SemanticIndex(Path(directory) / "semantic.sqlite")
                records = [source_record("synthetic", "revision-1", segment, metadata["title"])
                           for segment in segments]
                semantic_index.rebuild(records, runtime)
                build_ms = (time.perf_counter() - start) * 1000
                worker_peaks.append(peak_rss(runtime._worker.process.pid) or 0)
                runtime.close()
                runtime = None
            scores, times = {}, []
            for group, query, relevant in fixture["queries"]:
                start = time.perf_counter()
                hits = index.search(query, limit=50)
                if semantic_index:
                    from meeting_semantic import fuse_rankings
                    if runtime is None:
                        runtime = EmbeddingRuntime(cache_dir=embedding_cache)
                    semantic_hits = semantic_index.rank(runtime.embed_query(query), records)
                    hits = fuse_rankings(hits, semantic_hits)
                times.append((time.perf_counter() - start) * 1000)
                ranks = [i + 1 for i, hit in enumerate(hits[:10]) if int(hit["segment_id"]) in relevant]
                values = scores.setdefault(group, {"n": 0, "recall_at_5": 0, "mrr_at_10": 0,
                                                   "nonempty": 0})
                values["n"] += 1
                values["recall_at_5"] += int(any(rank <= 5 for rank in ranks))
                values["mrr_at_10"] += 1 / min(ranks) if ranks else 0
                values["nonempty"] += bool(hits)
            for values in scores.values():
                if values is not scores.get("unrelated"):
                    values["recall_at_5_95pct_interval"] = wilson_interval(values["recall_at_5"], values["n"])
                for key in ("recall_at_5", "mrr_at_10", "nonempty"):
                    values[key] /= values["n"]
            if runtime:
                worker_peaks.append(peak_rss(runtime._worker.process.pid) or 0)
            return {"mode": "hybrid" if semantic else "bm25", "groups": scores,
                    "latency_ms": {"cold": times[0], "warm_p50": statistics.median(times[1:]),
                                   "warm_p95": percentile(times[1:], .95)},
                    "index_bytes": sum(path.stat().st_size for path in Path(directory).glob("*.sqlite")),
                    "build_ms": build_ms,
                    "parent_peak_rss_bytes": peak_rss(os.getpid()),
                    "worker_peak_rss_bytes": max(worker_peaks, default=0) or None,
                    "limits": "Small synthetic holdout; unrelated recall is undefined (use nonempty for false positives). "
                              "Cold hybrid query reloads the model; OS file caches are not controlled. "
                              "RSS peaks are per-process, not simultaneous tree totals. "
                              "No private sample or concurrent recording/background impact acceptance."}
        finally:
            if runtime:
                runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic", action="store_true", help="Use the installed verified model; never download")
    parser.add_argument("--embedding-cache", type=Path, help="Explicit isolated model cache; does not change app settings")
    arguments = parser.parse_args()
    baseline = run()
    if arguments.semantic:
        hybrid = run(semantic=True, embedding_cache=arguments.embedding_cache)
        paraphrase_delta = hybrid["groups"]["paraphrase"]["recall_at_5"] - baseline["groups"]["paraphrase"]["recall_at_5"]
        exact_delta = hybrid["groups"]["exact"]["recall_at_5"] - baseline["groups"]["exact"]["recall_at_5"]
        print(json.dumps({"baseline": baseline, "hybrid": hybrid, "quality_gate": {
            "paraphrase_recall_delta": paraphrase_delta, "exact_recall_delta": exact_delta,
            "passed": paraphrase_delta >= .10 and exact_delta >= -.02,
            "limits": "Synthetic quality only; these samples cannot establish a 2-point noninferiority margin.",
        }}, indent=2))
    else:
        print(json.dumps(baseline, indent=2))
