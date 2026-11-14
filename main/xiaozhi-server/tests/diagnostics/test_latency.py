"""Run with python -m unittest discover -s tests/diagnostics -v (no cloud calls)."""
import asyncio
import json
import queue
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from core.utils.latency import (
    TurnTrace, TimedQueue, current_trace, new_trace, trace_for, finish_all,
)


class Recorder:
    def __init__(self):
        self.lines = []

    def bind(self, **kwargs):
        return self

    def info(self, fmt, value):
        self.lines.append((fmt, json.loads(value)))


def connection(**settings):
    return SimpleNamespace(config={"diagnostics": settings}, logger=Recorder())


class LatencyTests(unittest.TestCase):
    def test_summary_uses_wall_intervals_not_sum_of_overlapping_spans(self):
        conn = connection()
        with patch("core.utils.latency.time.monotonic", return_value=10):
            trace = new_trace(conn, "audio")
        trace.mark("voice.first", at=10)
        trace.mark("voice.last", at=11)
        trace.mark("asr.final", at=11.7)
        trace.mark("tts.first_text_sent", at=12)
        trace.mark("tts.first_audio", at=12.3)
        trace.mark("audio.first_sent", at=12.4)
        trace.observe("llm.1.stream", 1.5)
        trace.observe("tts.synthesis", 1.2)
        with patch("core.utils.latency.time.monotonic", return_value=13):
            trace.finish("completed")
        summary = conn.logger.lines[-1][1]
        self.assertEqual(summary["total_ms"], 3000)
        self.assertEqual(summary["intervals"]["last_voice_to_first_audio_ms"], 1400)
        self.assertEqual(summary["intervals"]["tts_first_audio_ms"], 300)

    def test_finish_once_and_ignore_late_callbacks(self):
        conn = connection(model_io=True)
        trace = new_trace(conn)
        trace.mark("first", once=True, at=trace.started)
        trace.mark("first", once=True, at=trace.started + 1)
        self.assertEqual(trace.events["first"], trace.started)
        trace.finish("aborted")
        trace.finish("completed")
        trace.mark("late")
        trace.observe("late", 1)
        summaries = [data for label, data in conn.logger.lines if "汇总" in label]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["status"], "aborted")
        self.assertNotIn("late", trace.events)
        self.assertNotIn("late", trace.stats)

    def test_payload_opt_in_preserves_unicode_newlines_and_long_text(self):
        conn = connection()
        trace = new_trace(conn)
        trace.io("llm.input", "secret")
        self.assertFalse(any("模型IO" in label for label, _ in conn.logger.lines))
        conn = connection(model_io=True)
        trace = new_trace(conn)
        text = "中文\n" + "x" * 20000
        trace.io("llm.input", {"text": text})
        self.assertEqual(conn.logger.lines[-1][1]["payload"]["text"], text)

    def test_concurrent_stats_and_context_do_not_mix_turns(self):
        first, second = new_trace(connection()), new_trace(connection())
        def work(trace):
            token = current_trace.set(trace)
            try:
                for _ in range(500):
                    current_trace.get().observe("work", .001)
            finally:
                current_trace.reset(token)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, [first, second, first, second]))
        self.assertEqual(first.stats["work"]["count"], 1000)
        self.assertEqual(second.stats["work"]["count"], 1000)
        self.assertIsNone(current_trace.get())

    def test_registry_keeps_pending_input_when_output_turn_finishes(self):
        conn = connection()
        output, incoming = new_trace(conn), new_trace(conn)
        conn.sentence_id = output.id
        trace_for(conn).finish("aborted")
        self.assertFalse(incoming.finished)
        newer = new_trace(conn)
        self.assertIsNone(trace_for(conn, output.id))
        self.assertIs(trace_for(conn, incoming.id), incoming)
        finish_all(conn, "disconnected")
        self.assertTrue(incoming.finished and newer.finished)

    def test_span_records_failure_and_elapsed(self):
        conn = connection()
        trace = new_trace(conn)
        with self.assertRaises(TimeoutError):
            with trace.span("tool.test"):
                raise TimeoutError("should not be printed")
        self.assertEqual(trace.errors, [{"stage": "tool.test", "type": "TimeoutError"}])
        self.assertEqual(trace.stats["tool.test"]["count"], 1)
        self.assertNotIn("should not be printed", str(conn.logger.lines))

    def test_disabled_diagnostics_produce_no_events_or_payloads(self):
        conn = connection(enabled=False, model_io=True)
        trace = new_trace(conn)
        with trace.span("test"):
            trace.io("input", "secret")
        trace.finish("completed")
        self.assertFalse(conn.logger.lines)

    def test_timed_queue_preserves_identity_fifo_and_task_accounting(self):
        observations = []
        q = TimedQueue(lambda item, elapsed: observations.append((item, elapsed)))
        first, second = object(), object()
        q.put(first)
        q.put(second)
        self.assertIs(q.get_nowait(), first)
        q.task_done()
        self.assertIs(q.get_nowait(), second)
        q.task_done()
        q.join()
        self.assertEqual([item for item, _ in observations], [first, second])
        self.assertTrue(all(elapsed >= 0 for _, elapsed in observations))
        with self.assertRaises(queue.Empty):
            q.get_nowait()


if __name__ == "__main__":
    unittest.main()
