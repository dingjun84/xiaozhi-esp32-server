"""Per-turn diagnostics. No provider credentials or audio payloads are logged."""
import json
import queue
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar


current_trace = ContextVar("voice_latency_trace", default=None)
current_request = ContextVar("voice_latency_request", default=None)


class TurnTrace:
    def __init__(self, conn, source="text", started=None):
        self.id = uuid.uuid4().hex
        self.logger = conn.logger.bind(tag="voice_trace")
        settings = conn.config.get("diagnostics", {})
        self.enabled = settings.get("enabled", True)
        self.model_io = settings.get("model_io", False)
        self.started = time.monotonic() if started is None else started
        self.events = {}
        self.stats = {}
        self.errors = []
        self.finished = False
        self.lock = threading.RLock()
        self.mark("turn.start", source=source)

    def mark(self, event, once=False, at=None, **fields):
        if not self.enabled:
            return
        with self.lock:
            if self.finished or (once and event in self.events):
                return
            timestamp = time.monotonic() if at is None else at
            self.events[event] = timestamp
        self.logger.info("[链路] {}", json.dumps(dict(
            trace_id=self.id, event=event,
            elapsed_ms=round((timestamp - self.started) * 1000, 2), **fields
        ), ensure_ascii=False, default=str))

    def observe(self, name, seconds):
        if not self.enabled:
            return
        with self.lock:
            if self.finished:
                return
            stat = self.stats.setdefault(name, {"count": 0, "total_ms": 0, "max_ms": 0})
            ms = seconds * 1000
            stat["count"] += 1
            stat["total_ms"] += ms
            stat["max_ms"] = max(stat["max_ms"], ms)

    @contextmanager
    def span(self, name):
        started = time.monotonic()
        self.mark(name + ".start")
        try:
            yield
        except BaseException as exc:
            # Only type here: SDK exception strings may contain signed URLs.
            self.error(name, exc)
            raise
        finally:
            self.observe(name, time.monotonic() - started)
            self.mark(name + ".end")

    def io(self, kind, payload):
        if self.enabled and self.model_io:
            self.logger.info("[模型IO] {}", json.dumps(dict(
                trace_id=self.id, kind=kind, payload=payload
            ), ensure_ascii=False, default=str))

    def error(self, stage, exc):
        with self.lock:
            if self.finished:
                return
            self.errors.append({"stage": stage, "type": type(exc).__name__})
        self.mark("error", stage=stage, error_type=type(exc).__name__)

    def finish(self, status):
        if not self.enabled:
            self.finished = True
            return
        with self.lock:
            if self.finished:
                return
            self.finished = True
            now = time.monotonic()
            intervals = {}
            for name, start, end in (
                ("speech_to_first_audio_ms", "voice.first", "audio.first_sent"),
                ("last_voice_to_first_audio_ms", "voice.last", "audio.first_sent"),
                ("asr_tail_ms", "voice.last", "asr.final"),
                ("client_stop_to_first_audio_ms", "client.listen_stop", "audio.first_sent"),
                ("asr_final_to_first_audio_ms", "asr.final", "audio.first_sent"),
                ("tts_first_audio_ms", "tts.first_text_sent", "tts.first_audio"),
                ("audio_dispatch_ms", "tts.first_audio", "audio.first_sent"),
            ):
                if start in self.events and end in self.events:
                    delta = self.events[end] - self.events[start]
                    if delta >= 0:
                        intervals[name] = round(delta * 1000, 2)
            summary = dict(trace_id=self.id, status=status, errors=list(self.errors),
                           total_ms=round((now - self.started) * 1000, 2),
                           intervals=intervals,
                           stages={k: {f: round(v, 2) for f, v in s.items()}
                                   for k, s in self.stats.items()},
                           timeline_ms={k: round((v - self.started) * 1000, 2)
                                        for k, v in self.events.items()})
        self.logger.info("[对话耗时汇总] {}", json.dumps(summary, ensure_ascii=False))


async def timed_await(trace, name, awaitable):
    with trace.span(name) if trace else nullcontext():
        return await awaitable


def new_trace(conn, source="text", started=None):
    trace = TurnTrace(conn, source, started)
    # Keep only unfinished turns. Completed tasks retain their own trace reference.
    traces = getattr(conn, "_turn_traces", {})
    traces = {key: value for key, value in traces.items() if not value.finished}
    traces[trace.id] = trace
    conn._turn_traces = traces
    return trace


def trace_for(conn, sentence_id=None):
    return getattr(conn, "_turn_traces", {}).get(
        sentence_id if sentence_id is not None else getattr(conn, "sentence_id", None)
    )


def finish_all(conn, status):
    for trace in list(getattr(conn, "_turn_traces", {}).values()):
        trace.finish(status)


class TimedQueue(queue.Queue):
    """Preserve the public Queue API and measure time until dequeue."""
    def __init__(self, observer, on_put=None):
        super().__init__()
        self.observer = observer
        self.on_put = on_put

    def _put(self, item):
        if self.on_put:
            self.on_put(item)
        super()._put((time.monotonic(), item))

    def _get(self):
        started, item = super()._get()
        self.observer(item, time.monotonic() - started)
        return item
