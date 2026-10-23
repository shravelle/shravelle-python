"""Continuous profiling and runtime metrics for Python (standard library only).

CPU profile (on by default): for ``cpu_seconds`` (10 s) of every ``interval``
(60 s) a background thread samples every thread's stack every 10 ms and charges
the CPU time each thread used since the previous sample (per-thread CPU clocks,
Linux) to the stack it is running. Where per-thread CPU clocks are not
available (macOS, Windows) it records wall-clock samples instead (type
``wall``), which includes time spent waiting.

Memory profile (opt-in, ``SHRAVELLE_PROFILING_MEMORY=true``): tracemalloc
snapshots of live memory per allocation traceback (``inuse_space`` and
``inuse_objects``). tracemalloc slows allocations down, so it is off by default.

Runtime metrics: process.cpu.utilization, process.memory.usage,
process.thread.count, cpython.gc.collections.
"""
from __future__ import annotations

import gc
import gzip
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

_log = logging.getLogger("shravelle")


def _frame_name(code) -> str:
    name = getattr(code, "co_qualname", None) or code.co_name
    parts = code.co_filename.replace("\\", "/").split("/")
    path = "/".join(parts[-2:]) if len(parts) > 1 else parts[0]
    return f"{path}:{name}".replace(";", ":")


def _stack(frame) -> List[str]:
    out = []
    while frame is not None:
        out.append(_frame_name(frame.f_code))
        frame = frame.f_back
    out.reverse()  # root first
    return out


class Profiler:
    def __init__(self, endpoint: str, api_key: str, service: str, version: str = "", env: str = "",
                 host: str = "", interval: float = 60.0, cpu_seconds: float = 10.0, sample_ms: float = 10.0,
                 memory: bool = False):
        self.endpoint, self.api_key, self.service = endpoint.rstrip("/"), api_key, service
        self.version, self.env, self.host = version, env, host
        self.interval, self.cpu_seconds, self.sample_ms, self.memory = interval, cpu_seconds, sample_ms, memory
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cpu_clock = hasattr(time, "pthread_getcpuclockid") and sys.platform.startswith("linux")

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self.memory:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start(int(os.environ.get("SHRAVELLE_PROFILING_MEMORY_FRAMES", "30")))
        self._thread = threading.Thread(target=self._run, name="shravelle-profiler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._cpu()
                if self.memory:
                    self._heap()
            except Exception as e:  # never break the application
                _log.debug("shravelle profiler: %s", e)
            self._stop.wait(max(1.0, self.interval - (time.monotonic() - started)))

    # ------------------------------------------------------------------ upload
    def _upload(self, params: Dict[str, str], lines: Dict[str, int]) -> None:
        if not lines:
            return
        body = gzip.compress("".join(f"{k} {v}\n" for k, v in lines.items() if v > 0).encode())
        params.update(service=self.service, version=self.version, env=self.env, host=self.host, language="python")
        url = f"{self.endpoint}/v1/profiles?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "text/plain", "Content-Encoding": "gzip", "X-API-Key": self.api_key})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:  # best effort; the next interval sends fresh data
            _log.debug("shravelle profile upload failed: %s", e)

    # ------------------------------------------------------------------ CPU
    def _cpu(self) -> None:
        me = threading.get_ident()
        counts: Dict[str, int] = {}
        last: Dict[int, int] = {}
        clocks: Dict[int, int] = {}
        end = time.monotonic() + self.cpu_seconds
        step = self.sample_ms / 1000.0
        native = {t.ident: getattr(t, "native_id", None) for t in threading.enumerate()}
        while time.monotonic() < end and not self._stop.is_set():
            frames = sys._current_frames()
            for ident, frame in frames.items():
                if ident == me:
                    continue
                if self._cpu_clock:
                    nid = native.get(ident)
                    if nid is None:
                        native = {t.ident: getattr(t, "native_id", None) for t in threading.enumerate()}
                        nid = native.get(ident)
                    if nid is None:
                        continue
                    try:
                        clk = clocks.get(ident) or time.pthread_getcpuclockid(ident)
                        clocks[ident] = clk
                        now = time.clock_gettime_ns(clk)
                    except (OSError, OverflowError):
                        continue
                    used = now - last.get(ident, now)
                    last[ident] = now
                    if used <= 0:
                        continue
                    key = ";".join(_stack(frame))
                    counts[key] = counts.get(key, 0) + used
                else:
                    key = ";".join(_stack(frame))
                    counts[key] = counts.get(key, 0) + 1
            del frames
            time.sleep(step)
        if self._cpu_clock:
            self._upload({"type": "cpu", "unit": "nanoseconds", "duration": str(int(self.cpu_seconds))}, counts)
        else:
            self._upload({"type": "wall", "unit": "samples", "interval_ms": str(self.sample_ms),
                          "duration": str(int(self.cpu_seconds))}, counts)

    # ------------------------------------------------------------------ memory
    def _heap(self) -> None:
        import tracemalloc
        snap = tracemalloc.take_snapshot().filter_traces((
            tracemalloc.Filter(False, tracemalloc.__file__),
            tracemalloc.Filter(False, __file__),
        ))
        size: Dict[str, int] = {}
        count: Dict[str, int] = {}
        for st in snap.statistics("traceback"):
            key = ";".join(f"{'/'.join(f.filename.replace(chr(92), '/').split('/')[-2:])}:{f.lineno}" for f in st.traceback)
            size[key] = size.get(key, 0) + st.size
            count[key] = count.get(key, 0) + st.count
        self._upload({"type": "inuse_space", "unit": "bytes"}, size)
        self._upload({"type": "inuse_objects", "unit": "count"}, count)


# ---------------------------------------------------------------------- runtime metrics
def register_runtime_metrics(meter) -> None:
    """Observable gauges for the Runtime tab (CPU, memory, threads, GC)."""
    from opentelemetry.metrics import Observation

    state = {"cpu": None, "at": None}
    ncpu = os.cpu_count() or 1

    def cpu_cb(_options):
        t = os.times()
        used, now = t.user + t.system, time.monotonic()
        out = []
        if state["cpu"] is not None and now > state["at"]:
            out.append(Observation(max(0.0, (used - state["cpu"]) / (now - state["at"]) / ncpu)))
        state["cpu"], state["at"] = used, now
        return out

    def rss_cb(_options):
        try:
            with open("/proc/self/statm") as f:
                return [Observation(int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE"))]
        except Exception:
            try:
                import resource
                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                return [Observation(peak if sys.platform == "darwin" else peak * 1024)]  # peak RSS fallback
            except Exception:
                return []

    meter.create_observable_gauge("process.cpu.utilization", [cpu_cb], unit="1",
                                  description="CPU used by the process, fraction of all CPUs")
    meter.create_observable_gauge("process.memory.usage", [rss_cb], unit="By")
    meter.create_observable_gauge("process.thread.count", [lambda _o: [Observation(threading.active_count())]], unit="{thread}")
    meter.create_observable_gauge("cpython.gc.collections",
                                  [lambda _o: [Observation(sum(s.get("collections", 0) for s in gc.get_stats()))]],
                                  unit="{collection}")

