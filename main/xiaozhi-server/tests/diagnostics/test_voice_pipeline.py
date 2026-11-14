"""Exercise real ASR/LLM/TTS code, queues, threads and codec with simulated cloud I/O."""
import asyncio
import base64
import copy
import json
import math
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import openai

# Project modules initialize logging on import; initialize config outside an event loop.
from config.config_loader import load_config
from config.settings import check_config_file
CONFIG = asyncio.run(load_config())
check_config_file()
from config.logger import setup_logging
setup_logging(CONFIG)

from core.connection import ConnectionHandler
from core.handle.abortHandle import handleAbortMessage
from core.handle.sendAudioHandle import _do_send_audio, sendAudioMessage, send_tts_message
from core.utils.latency import new_trace, trace_for
from scripts import diagnose_voice_live as live


class CloudSocket:
    def __init__(self, asr, auto_final=True):
        self.asr = asr
        self.auto_final = auto_final
        self.incoming = asyncio.Queue()
        self.audio_sent = False
        self.frame_count = 0
        self.closed = False

    async def send(self, message):
        data = json.loads(message)
        status = data["header"]["status"]
        if self.asr:
            self.frame_count += 1
            if status == 2 or (self.auto_final and self.frame_count == 25):
                text = {"ws": [{"cw": [{"w": "你好，请做一次语音测试"}]}]}
                payload = base64.b64encode(json.dumps(text).encode()).decode()
                await self.incoming.put(json.dumps({"header": {"code": 0, "status": 2},
                                                     "payload": {"result": {"text": payload}}}))
        else:
            if status == 0:
                response = {"header": {"code": 0}, "payload": {"audio": {"status": 0}}}
            elif status == 1 and not self.audio_sent:
                self.audio_sent = True
                # 240 ms of actual PCM, encoded by the production Opus encoder.
                pcm = struct.pack('<' + 'h' * 5760, *[int(8000 * math.sin(i * .12)) for i in range(5760)])
                response = {"header": {"code": 0}, "payload": {"audio": {
                    "status": 1, "audio": base64.b64encode(pcm).decode()}}}
            elif status == 2:
                response = {"header": {"code": 0}, "payload": {"audio": {"status": 2}}}
            else:
                return
            await self.incoming.put(json.dumps(response))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, use_tool, auto_final=True):
        config = copy.deepcopy(CONFIG)
        config['diagnostics'] = {'enabled': True, 'model_io': True}
        original_provider = live.provider
        requests = []
        sockets = []
        def response(request):
            body = json.loads(request.content)
            requests.append(body)
            followup = any(m['role'] == 'tool' for m in body['messages'])
            if use_tool and not followup:
                delta = {'tool_calls': [{'index': 0, 'id': 'call_probe', 'type': 'function',
                                        'function': {'name': 'diagnostic_echo', 'arguments': '{}'}}]}
            elif not use_tool:
                delta = {'tool_calls': [{'index': 0, 'id': 'call_answer', 'type': 'function',
                                        'function': {'name': 'direct_answer', 'arguments': json.dumps({'response': '语音链路测试成功。'})}}]}
            else:
                delta = {'content': '语音链路测试成功。'}
            # An initial empty event must not count as effective content.
            chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]},
                      {'choices': [{'index': 0, 'delta': delta}]},
                      {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
            sse = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse)
        def mocked_provider(cfg, module):
            if module == 'VAD':
                # Deterministic voice detector: source VAD was exercised by the live DNS attempt.
                import audioop
                return SimpleNamespace(is_vad=lambda conn, pcm: audioop.rms(pcm, 2) > 300)
            instance = original_provider(cfg, module)
            if module == 'LLM':
                instance.client.close()
                instance.client = openai.OpenAI(api_key='offline-test', base_url='https://offline.invalid/v1',
                                               http_client=httpx.Client(transport=httpx.MockTransport(response)))
            return instance
        async def connect(url, **kwargs):
            socket = CloudSocket('iat.cn-' in url, auto_final)
            sockets.append(socket)
            return socket
        pcm = struct.pack('<' + 'h' * 6400, *[int(8000 * math.sin(i * .1)) for i in range(6400)])
        records = []
        logger = setup_logging(CONFIG)
        sink = logger.add(lambda msg: records.append(str(msg)), level='INFO')
        try:
            with patch.object(live, 'provider', side_effect=mocked_provider), patch('websockets.connect', side_effect=connect):
                result = asyncio.run(live.probe(config, pcm, use_tool))
        finally:
            logger.remove(sink)
        self.assertEqual(len(requests), 2 if use_tool else 1)
        self.assertTrue(all(s.closed for s in sockets))
        summaries = [line for line in records if '[对话耗时汇总]' in line]
        self.assertEqual(len(summaries), 1)
        self.assertIn('"status": "completed"', summaries[0])
        self.assertIn('last_voice_to_first_audio_ms', summaries[0])
        if auto_final:
            self.assertIn('asr.fixed_wait', summaries[0])
        else:
            self.assertNotIn('asr.fixed_wait', summaries[0])
            self.assertIn('asr.client_stop_frame_sent', summaries[0])
        self.assertIn('tts.input', ''.join(records))
        self.assertIn('llm.sdk_request', ''.join(records))
        timeline = result['timeline_ms']
        self.assertLessEqual(timeline['llm.1.first_event'], timeline['llm.1.first_effective'])
        if use_tool:
            self.assertIn('tool.diagnostic_echo.call_probe', result['stages'])
            self.assertTrue(any(m['role'] == 'tool' for m in requests[-1]['messages']))

    def test_direct_answer_to_decodable_audio_and_single_summary(self):
        self.run_pipeline(False)

    def test_tool_followup_retains_one_turn_and_separate_llm_timings(self):
        self.run_pipeline(True)

    def test_client_stop_path_has_no_fixed_wait(self):
        self.run_pipeline(False, auto_final=False)

    def test_asr_connect_timeout_records_duration_and_terminates(self):
        from core.providers.asr.xunfei_stream import ASRProvider
        async def run():
            asr = ASRProvider({'app_id': 'offline', 'api_key': 'offline', 'api_secret': 'offline'}, True)
            conn = SimpleNamespace(config={}, logger=setup_logging(CONFIG), client_listen_mode='auto',
                                   asr_audio=[], client_have_voice=False, client_voice_stop=False, asr=asr)
            trace = new_trace(conn, 'audio')
            conn._input_trace = trace
            async def timeout(*args, **kwargs):
                raise TimeoutError('offline expected failure')
            with patch('websockets.connect', side_effect=timeout):
                await asr.receive_audio(conn, b'\0' * 1920, True)
            self.assertTrue(trace.finished)
            self.assertIn('asr.connect', trace.stats)
            self.assertEqual(trace.errors[0]['type'], 'TimeoutError')
        asyncio.run(run())

    def test_tts_provider_error_finishes_trace_and_closes_socket(self):
        from core.providers.tts.xunfei_stream import TTSProvider
        async def run():
            tts = object.__new__(TTSProvider)
            conn = SimpleNamespace(config={}, logger=setup_logging(CONFIG), stop_event=threading.Event(), client_abort=False)
            trace = new_trace(conn)
            conn.sentence_id = trace.id
            tts.conn = conn
            tts.current_sentence_id = trace.id
            ws = CloudSocket(False)
            await ws.incoming.put(json.dumps({'header': {'code': 11200, 'message': 'offline expected failure'}}))
            tts.ws = ws
            await tts._start_monitor_tts_response(trace)
            self.assertTrue(trace.finished)
            self.assertEqual(trace.errors[0]['stage'], 'tts.provider')
            self.assertTrue(ws.closed)
        asyncio.run(run())

    def test_legacy_intent_sentence_id_binds_to_original_trace(self):
        from core.providers.tts.xunfei_stream import TTSProvider
        from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
        from core.utils.latency import current_trace
        tts = TTSProvider({'app_id': 'offline', 'api_key': 'offline', 'api_secret': 'offline'}, True)
        conn = SimpleNamespace(config={}, logger=setup_logging(CONFIG))
        tts.conn = conn
        trace = new_trace(conn)
        token = current_trace.set(trace)
        try:
            tts.tts_text_queue.put(TTSMessageDTO(sentence_id='legacy-intent', sentence_type=SentenceType.FIRST,
                                               content_type=ContentType.ACTION))
        finally:
            current_trace.reset(token)
        self.assertIs(trace_for(conn, 'legacy-intent'), trace)
        self.assertIn('tts.enqueued', trace.events)

    def test_abort_only_finishes_output_not_incoming_turn(self):
        async def run():
            async def send(msg):
                pass
            conn = SimpleNamespace(config={'diagnostics': {'enabled': True}}, logger=setup_logging(CONFIG),
                                   client_abort=False, close_after_chat=False,
                                   websocket=SimpleNamespace(send=send), session_id='offline',
                                   clear_queues=lambda: None, clearSpeakStatus=lambda: None)
            output, incoming = new_trace(conn), new_trace(conn, 'audio')
            conn.sentence_id = output.id
            conn._input_trace = incoming
            await handleAbortMessage(conn)
            self.assertTrue(output.finished)
            self.assertFalse(incoming.finished)
        asyncio.run(run())

    def test_send_failure_finishes_turn(self):
        async def run():
            async def send(msg):
                raise ConnectionError('offline expected failure')
            conn = SimpleNamespace(config={}, logger=setup_logging(CONFIG),
                                   conn_from_mqtt_gateway=False, websocket=SimpleNamespace(send=send))
            trace = new_trace(conn)
            conn.sentence_id = trace.id
            with self.assertRaises(ConnectionError):
                await _do_send_audio(conn, b'packet', {'sentence_id': trace.id})
            self.assertTrue(trace.finished)
            self.assertEqual(trace.errors[0]['stage'], 'audio.send')
        asyncio.run(run())

    def test_stale_audio_does_not_enter_new_turn(self):
        async def run():
            conn = SimpleNamespace(sentence_id='new')
            await sendAudioMessage(conn, None, b'old', None, 'old')
        asyncio.run(run())

    def test_llm_stream_error_records_failure_and_enqueues_audio_end(self):
        import queue
        from core.providers.tts.dto.dto import SentenceType
        class FailingLLM:
            def response_with_functions(self, *args, **kwargs):
                raise RuntimeError('offline expected stream failure')
                yield
        conn = ConnectionHandler(copy.deepcopy(CONFIG), None, None, FailingLLM(), None, None)
        conn.intent_type = 'function_call'
        conn.func_handler = SimpleNamespace(get_functions=lambda: [])
        conn.tts = SimpleNamespace(tts_text_queue=queue.Queue())
        conn.features = {'emoji': False}
        try:
            trace = new_trace(conn)
            conn.chat('test', trace=trace)
            items = []
            while not conn.tts.tts_text_queue.empty():
                items.append(conn.tts.tts_text_queue.get_nowait())
            self.assertEqual(items[-1].sentence_type, SentenceType.LAST)
            self.assertIn('llm.1', [e['stage'] for e in trace.errors])
            self.assertIn('llm.1.stream', trace.stats)
        finally:
            conn.executor.shutdown(wait=False)

    def test_parallel_tool_durations_are_actual_execution_not_result_collection(self):
        async def run():
            async def tool(conn, data):
                from plugins_func.register import Action, ActionResponse
                await asyncio.sleep(.02 if data['name'] == 'slow' else .005)
                return ActionResponse(action=Action.REQLLM, result='ok')
            conn = SimpleNamespace(config={}, logger=setup_logging(CONFIG),
                                   func_handler=SimpleNamespace(handle_llm_function_call=tool))
            trace = new_trace(conn)
            results = await asyncio.gather(*[
                ConnectionHandler._call_tool_traced(conn, {'name': name, 'id': name}, trace)
                for name in ('slow', 'fast')])
            self.assertEqual(len(results), 2)
            self.assertLess(trace.stats['tool.fast.fast']['total_ms'], trace.stats['tool.slow.slow']['total_ms'])
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
