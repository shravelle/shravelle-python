"""Shravelle APM for Python.

    import shravelle
    shravelle.configure(service_name="checkout", api_key="shv_ing_...")   # before importing your framework

or with zero code changes:

    pip install "shravelle[auto]" && opentelemetry-bootstrap -a install
    shravelle-run gunicorn myproject.wsgi

Everything is exported as OTLP/protobuf to the Shravelle ingest gateway.
"""
from __future__ import annotations

import atexit
import contextlib
import functools
import json
import logging
import os
import socket
import sys
import threading
import urllib.request
from typing import Any, Callable, Dict, Optional

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Status, StatusCode

__all__ = ["configure", "shutdown", "notice_error", "set_user", "add_attributes", "record_event", "segment",
           "counter", "histogram", "record_deployment"]
__version__ = "1.0.0"

_state: Dict[str, Any] = {}
_log = logging.getLogger("shravelle")


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def configure(service_name: Optional[str] = None, api_key: Optional[str] = None, endpoint: Optional[str] = None,
              environment: Optional[str] = None, service_version: Optional[str] = None, sample_rate: Optional[float] = None,
              capture_logs: bool = True, log_level: int = logging.INFO, auto_instrument: bool = True,
              metric_interval_ms: int = 30000, resource_attributes: Optional[Dict[str, str]] = None,
              profiling: Optional[bool] = None, profiling_memory: Optional[bool] = None,
              profile_interval: float = 60.0, profile_cpu_seconds: float = 10.0, runtime_metrics: bool = True) -> None:
    """Start exporting traces, metrics and logs. Safe to call more than once."""
    if _state.get("configured"):
        return
    endpoint = (endpoint or _env("SHRAVELLE_ENDPOINT", "http://localhost:4318")).rstrip("/")
    api_key = api_key or _env("SHRAVELLE_API_KEY")
    if not api_key:
        _log.warning("shravelle: SHRAVELLE_API_KEY is not set; telemetry will be rejected")
    service_name = service_name or _env("SHRAVELLE_SERVICE_NAME", _env("OTEL_SERVICE_NAME", os.path.basename(sys.argv[0] or "python-app")))
    rate = float(sample_rate if sample_rate is not None else _env("SHRAVELLE_SAMPLE_RATE", "1"))
    headers = {"x-api-key": api_key}
    attrs = {
        "service.name": service_name,
        "service.version": service_version or _env("SHRAVELLE_SERVICE_VERSION"),
        "deployment.environment.name": environment or _env("SHRAVELLE_ENV", "production"),
        "host.name": _env("SHRAVELLE_HOSTNAME", socket.gethostname()),
        "process.runtime.name": sys.implementation.name,
        "process.runtime.version": ".".join(map(str, sys.version_info[:3])),
        "telemetry.distro.name": "shravelle-python",
    }
    attrs.update(resource_attributes or {})
    resource = Resource.create(attrs)

    tp = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(rate)))
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint + "/v1/traces", headers=headers)))
    trace.set_tracer_provider(tp)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint + "/v1/metrics", headers=headers),
                                           export_interval_millis=metric_interval_ms)
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint + "/v1/logs", headers=headers)))
    set_logger_provider(lp)
    if capture_logs:
        handler = LoggingHandler(level=log_level, logger_provider=lp)
        logging.getLogger().addHandler(handler)
        if logging.getLogger().level > log_level or logging.getLogger().level == logging.NOTSET:
            logging.getLogger().setLevel(log_level)

    _state.update(configured=True, tp=tp, mp=mp, lp=lp, endpoint=endpoint, api_key=api_key,
                  service=service_name, version=attrs["service.version"])
    from . import _profiler
    if runtime_metrics:
        with contextlib.suppress(Exception):
            _profiler.register_runtime_metrics(mp.get_meter("shravelle.runtime"))
    if profiling is None:
        profiling = _env("SHRAVELLE_PROFILING", "true").lower() != "false"
    if profiling_memory is None:
        profiling_memory = _env("SHRAVELLE_PROFILING_MEMORY", "false").lower() == "true"
    if profiling:
        prof = _profiler.Profiler(endpoint, api_key, service_name, attrs["service.version"],
                                  attrs["deployment.environment.name"], attrs["host.name"],
                                  interval=profile_interval, cpu_seconds=profile_cpu_seconds, memory=profiling_memory)
        prof.start()
        _state["profiler"] = prof
    if auto_instrument:
        _auto_instrument()
    _install_hooks()
    atexit.register(shutdown)


def _auto_instrument() -> None:
    """Activate every installed OpenTelemetry instrumentation (Django, Flask,
    FastAPI, requests, httpx, SQLAlchemy, psycopg, redis, celery, ...)."""
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return
    try:
        eps = entry_points(group="opentelemetry_instrumentor")
    except TypeError:  # Python < 3.10
        eps = entry_points().get("opentelemetry_instrumentor", [])
    skip = set(filter(None, _env("SHRAVELLE_DISABLED_INSTRUMENTATIONS").split(",")))
    for ep in eps:
        if ep.name in skip:
            continue
        try:
            ep.load()().instrument()
            _log.debug("shravelle: instrumented %s", ep.name)
        except Exception as e:  # missing library, version mismatch: skip quietly
            _log.debug("shravelle: skip %s: %s", ep.name, e)


def _install_hooks() -> None:
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        notice_error(exc, {"exception.escaped": "true", "fatal": "true"})
        shutdown()
        prev(exc_type, exc, tb)

    sys.excepthook = hook
    if hasattr(threading, "excepthook"):
        prev_t = threading.excepthook

        def thook(args):
            notice_error(args.exc_value, {"exception.escaped": "true", "thread": getattr(args.thread, "name", "")})
            prev_t(args)

        threading.excepthook = thook


def shutdown() -> None:
    """Flush everything (called automatically at exit)."""
    prof = _state.pop("profiler", None)
    if prof is not None:
        with contextlib.suppress(Exception):
            prof.stop()
    for key in ("tp", "mp", "lp"):
        p = _state.pop(key, None)
        if p is not None:
            with contextlib.suppress(Exception):
                p.shutdown()


def notice_error(exc: BaseException, attributes: Optional[Dict[str, Any]] = None) -> None:
    """Record an exception on the current span and as an ERROR log."""
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.record_exception(exc, attributes=attributes or {})
        span.set_status(Status(StatusCode.ERROR, str(exc)))
    logging.getLogger("shravelle.errors").error("%s: %s", type(exc).__name__, exc, exc_info=exc, extra=attributes or {})


def add_attributes(**attrs: Any) -> None:
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        for k, v in attrs.items():
            span.set_attribute(k, v)


def set_user(user_id: Any, **attrs: Any) -> None:
    add_attributes(**{"enduser.id": str(user_id)}, **attrs)


def record_event(name: str, **attributes: Any) -> None:
    """Business event, e.g. record_event("order.placed", amount=499, country="IN")."""
    logging.getLogger("shravelle.events").info(name, extra={"event.name": name, **attributes})


def segment(name: str, **attributes: Any):
    """Custom span as a context manager or decorator:

        with shravelle.segment("pricing.calculate"): ...
        @shravelle.segment("load_user")
        def load_user(...): ...
    """
    tracer = trace.get_tracer("shravelle")

    class _Seg(contextlib.ContextDecorator):
        def __enter__(self):
            self._cm = tracer.start_as_current_span(name, attributes=attributes)
            return self._cm.__enter__()

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    return _Seg()


def counter(name: str, unit: str = "", description: str = ""):
    return metrics.get_meter("shravelle").create_counter(name, unit=unit, description=description)


def histogram(name: str, unit: str = "", description: str = ""):
    return metrics.get_meter("shravelle").create_histogram(name, unit=unit, description=description)


def record_deployment(version: str, commit: str = "", description: str = "", user: str = "",
                      service: Optional[str] = None) -> None:
    """Send a deployment marker (call from CI after a release)."""
    endpoint = _state.get("endpoint") or _env("SHRAVELLE_ENDPOINT", "http://localhost:4318").rstrip("/")
    key = _state.get("api_key") or _env("SHRAVELLE_API_KEY")
    body = json.dumps({"service": service or _state.get("service") or _env("SHRAVELLE_SERVICE_NAME", "python-app"),
                       "version": version, "commit": commit, "description": description, "user": user}).encode()
    req = urllib.request.Request(endpoint + "/v1/deployments", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (endpoint is configured by the user)
        if r.status >= 300:
            raise RuntimeError("deployment marker failed: HTTP %d" % r.status)


def wsgi_middleware(app: Callable) -> Callable:
    """Minimal fallback when opentelemetry-instrumentation-wsgi is unavailable."""
    try:
        from opentelemetry.instrumentation.wsgi import OpenTelemetryMiddleware
        return OpenTelemetryMiddleware(app)
    except ImportError:
        return app
