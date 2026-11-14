"""Opt-in real provider probe, with a local device sink and a harmless test tool.

From the server directory:
  python scripts/diagnose_voice_live.py --audio /tmp/probe.wav
WAV: mono, signed 16-bit PCM, 16 kHz. Makes real ASR/LLM/TTS requests.
"""
import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from config.config_loader import load_config


class DeviceSink:
    """Decode outgoing audio to verify valid Opus, without a physical device."""
    def __init__(self):
        import opuslib_next
        self.decoder = opuslib_next.Decoder(24000, 1)
        self.packets = 0
        self.nonzero_samples = False
        self.done = asyncio.Event()

    async def send(self, message):
        if isinstance(message, bytes):
            pcm = self.decoder.decode(message, 1440)
            self.packets += 1
            self.nonzero_samples |= any(pcm)
        elif json.loads(message).get("state") == "stop":
            self.done.set()

    async def close(self):
        pass


class ProbeTool:
    def __init__(self):
        self.calls = 0

    def get_functions(self):
        return [{"type": "function", "function": {
            "name": "diagnostic_echo", "description": "语音链路测试专用，返回固定测试结果。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }}]

    async def handle_llm_function_call(self, conn, data):
        from plugins_func.register import Action, ActionResponse
        assert data["name"] == "diagnostic_echo", data["name"]
        self.calls += 1
        return ActionResponse(action=Action.REQLLM, result="诊断工具执行成功")


def provider(config, module):
    name = config["selected_module"][module]
    settings = config[module][name]
    kind = settings.get("type", name)
    factory = importlib.import_module(f"core.utils.{module.lower()}").create_instance
    return factory(kind, settings, True) if module in ("ASR", "TTS") else factory(kind, settings)



async def probe(config, audio, use_tool):
    import threading
    import opuslib_next
    from core.connection import ConnectionHandler
    from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
    from core.utils.dialogue import Message
    from core.utils.latency import finish_all

    asr, tts, llm, vad = [provider(config, m) for m in ("ASR", "TTS", "LLM", "VAD")]
    conn = ConnectionHandler(config, vad, asr, llm, None, None)
    conn.loop = asyncio.get_running_loop()
    conn.asr, conn.tts, conn.vad = asr, tts, vad
    conn.intent_type = "function_call"
    conn.func_handler = ProbeTool()
    conn.features = {"emoji": False}
    conn.headers = {"device-id": "diagnostic-local-sink"}
    conn.bind_completed_event.set()
    conn.websocket = sink = DeviceSink()
    conn.dialogue.put(Message(role="system", content=(
        "这是语音链路测试。必须先调用 diagnostic_echo，收到结果后只说：语音链路测试成功。"
        if use_tool else "这是语音链路测试。不要调用工具，请使用 direct_answer 简短回复：语音链路测试成功。"
    )))
    try:
        await tts.open_audio_channels(conn)
        await asr.open_audio_channels(conn)
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
        # Lead-in and trailing silence exercise the actual VAD and audio queue.
        pcm = b'\0' * 9600 + audio + b'\0' * 64000
        for offset in range(0, len(pcm), 1920):
            frame = pcm[offset:offset + 1920].ljust(1920, b'\0')
            await conn._route_message(encoder.encode(frame, 960))
            await asyncio.sleep(.06)
        # Same stop control packet as the hardware; does not call ASR callbacks directly.
        await ListenTextMessageHandler().handle(conn, {"state": "stop"})
        await asyncio.wait_for(sink.done.wait(), timeout=50)
        await asyncio.sleep(.05)
        traces = list(conn._turn_traces.values())
        assert len(traces) == 1, f"expected one turn, got {len(traces)}"
        trace = traces[0]
        required = {"voice.first", "asr.connect.end", "asr.final", "llm.1.first_effective",
                    "tts.connect.end", "tts.first_audio", "audio.first_sent", "audio.stop_sent"}
        assert required <= trace.events.keys(), f"missing events: {required - trace.events.keys()}"
        assert trace.finished and not trace.errors, "turn failed"
        assert sink.packets > 0 and sink.nonzero_samples, "no valid audio"
        if use_tool:
            assert conn.func_handler.calls == 1, "test tool not called exactly once"
            assert "llm.2.first_effective" in trace.events, "missing tool follow-up LLM"
        result = {"scenario": "tool_followup" if use_tool else "direct_answer",
                  "trace_id": trace.id, "opus_packets": sink.packets, "valid_audio": sink.nonzero_samples,
                  "tool_calls": conn.func_handler.calls, "stages": trace.stats,
                  "timeline_ms": {k: round((v - trace.started) * 1000, 2) for k, v in trace.events.items()}}
        print("PROBE_PASS " + json.dumps(result, ensure_ascii=False))
        return result
    finally:
        finish_all(conn, "probe_cleanup")
        conn.stop_event.set()
        await asr.close()
        await tts.close()
        if hasattr(conn, "audio_rate_controller"):
            conn.audio_rate_controller.stop_sending()
        llm.client.close()
        conn.executor.shutdown(wait=False)
        for thread in (getattr(conn, "asr_priority_thread", None), getattr(tts, "tts_priority_thread", None),
                       getattr(tts, "audio_play_priority_thread", None)):
            if thread and thread is not threading.current_thread():
                await asyncio.to_thread(thread.join, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--scenario", choices=("direct", "tool", "both"), default="both")
    args = parser.parse_args()
    with wave.open(str(args.audio), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        audio = wav.readframes(wav.getnframes())
        assert 0 < wav.getnframes() <= 16000 * 15, "probe audio must be 0–15 seconds"
    config = asyncio.run(load_config())
    config["diagnostics"] = {"enabled": True, "model_io": True}
    from config.settings import check_config_file
    check_config_file()
    from config.logger import setup_logging
    setup_logging(config)
    async def run():
        results = []
        for use_tool in ([False, True] if args.scenario == "both" else [args.scenario == "tool"]):
            results.append(await probe(config, audio, use_tool))
        (ROOT / "tmp/voice-diagnostic-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    try:
        import socket
        from urllib.parse import urlparse
        endpoints = ["iat.cn-huabei-1.xf-yun.com", "cbm01.cn-huabei-1.xf-yun.com"]
        llm_settings = config["LLM"][config["selected_module"]["LLM"]]
        endpoints.append(urlparse(llm_settings.get("base_url") or llm_settings.get("url")).hostname)
        for host in endpoints:
            socket.getaddrinfo(host, 443)
        asyncio.run(run())
    except Exception as exc:
        # Avoid dumping credentials from third-party exception objects.
        import traceback
        print("PROBE_FAILED", type(exc).__name__, "attribute=", getattr(exc, "name", None), file=sys.stderr)
        for frame in traceback.extract_tb(exc.__traceback__):
            print(f"  {frame.filename}:{frame.lineno} {frame.name}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
