# 语音链路诊断

适用当前配置：Silero VAD、讯飞流式 ASR、OpenAI 兼容 LLM、讯飞双流式 TTS。

## 开启与查看

在 `data/.config.yaml` 中配置，重启服务后生效。本次已在本地配置开启这两个开关。

```yaml
diagnostics:
  enabled: true
  model_io: true
```

分发用的 `config.yaml` 默认开启阶段日志和汇总，默认关闭完整模型内容。日志以 INFO 输出，无须把所有模块调到 DEBUG。`enabled: false` 关闭新增日志和统计，`model_io: false` 只关闭新增模型内容日志；原有业务日志沿用原来的级别和行为。

```sh
rg '\[链路\]|\[模型IO\]|\[对话耗时汇总\]' tmp/server.log
```

每条新增日志后面都是一行 JSON；其中 `trace_id` 标识一轮对话。可用 `rg '具体trace_id' tmp/server.log` 筛选单轮记录。工具调用后再次请求模型使用同一轮 ID，以 `llm.1`、`llm.2` 区分请求；模型内容记录还包含 `request_id`。个别旧意图处理器自行创建的 sentence_id 会绑定到原 trace，不会拆成两轮。

`[模型IO]` 包括识别结果、完整对话上下文、工具定义、最终 SDK 请求参数（含模型和思考配置）、模型返回文本/工具参数、工具结果、清洗替换后实际发给 TTS 的文本。内容不截断；换行按 JSON 转义保留。不记录密钥、签名 URL 或音频二进制。

## 每轮结束汇总

`[对话耗时汇总]` 正常在音频队列排空、结束通知发出后打印。打断、发送失败、ASR/TTS 错误、连接断开也会结束统计。每轮仅汇总一次，旧任务后续回调不会追加到新轮。

| 字段 | 含义 |
|---|---|
| `status` | completed / completed_with_errors / aborted / disconnected / asr_error / asr_incomplete / tts_error / tts_incomplete / send_error 等 |
| `total_ms` | 本轮开始到结束的实际时间，含用户说话、生成、发送及收尾等待 |
| `intervals.last_voice_to_first_audio_ms` | 最后有声帧入队到首个音频包发出，优先关注的等待指标 |
| `intervals.speech_to_first_audio_ms` | 首个判定有声的帧入队到首个音频包发出，包含说话时长 |
| `intervals.client_stop_to_first_audio_ms` | 客户端 stop 控制消息到首包音频发出，手动拾音模式优先看此项 |
| `intervals.asr_final_to_first_audio_ms` | ASR 最终结果到首包音频发出 |
| `intervals.asr_tail_ms` | 最后有声帧到 ASR 最终结果，包含尾部静音/识别等待 |
| `intervals.tts_first_audio_ms` | 首次文本请求发送完成到收到有效 PCM 音频 |
| `intervals.audio_dispatch_ms` | 首次收到有效 PCM 到首包音频发出，含编码、排队和调度 |
| `stages` | 各步骤的次数、累计耗时和最大耗时，单位 ms |
| `timeline_ms` | 各里程碑相对本轮开始的时间，单位 ms |
| `errors` | 出错阶段和异常类型；不包含可能带认证信息的第三方异常正文 |

主要步骤包括：

- `audio.input_queue`、`audio.decode`、`vad.inference`：入口音频排队、解码、VAD；高频步骤只汇总，不逐帧打印。
- `asr.connect`、`asr.audio_send`、`asr.recognition_including_speech`、`asr.fixed_wait`、`asr.result_extract`、`asr.postprocess`。
- `voiceprint.identify`、`intent`、`memory.query`：仅在实际执行时记录。
- `chat.executor_queue`、`llm.N.context`、`llm.N.http_open`、`llm.N.first_effective`、`llm.N.stream`。
- `tool.工具名.调用ID` 及其 `.loop_dispatch`：分别测量工具真实执行和调度等待，避免并行工具的结果收集顺序影响耗时。
- `tts.text_queue`、`tts.loop_dispatch`、`tts.text_prepare`、`tts.connect`、`tts.text_send`、`tts.total`。
- `audio.encode`、`tts.audio_queue`、`audio.loop_dispatch`、`audio.pacing_queue`、`audio.websocket_send`、`audio.drain`。

解读时注意：

1. ASR/TTS/LLM 是并行流式流程，步骤耗时不能直接相加得到总耗时；应看里程碑的时间差。
2. `llm.N.http_open` 是 SDK 创建流、等待响应头的整体耗时，可能包含 SDK 自动重试，不等于纯 TCP/TLS 建连耗时。`first_event` 可为空事件；`first_effective` 才是有效文本或工具名称/参数的首次返回。
3. `asr.fixed_wait` 保留原有 250ms 等待，仅在该代码分支执行时出现。客户端先发送 stop、令识别状态停止后，通常不会再执行此等待。
4. `audio.pacing_queue` 包含为正常播放节奏安排的等待；`audio.drain` 包含既有的播放收尾估算等待。它们不直接代表模型慢。
5. 入口统计从首个判定有声的帧开始，前导缓存/闲置音频不计为本轮；`voice.last` 是服务端 VAD 对最后有声帧的估计，不是人说完的精确物理时刻。手动模式跳过 VAD，有声帧时间退化为输入帧时间，应优先使用 client_stop 指标。客户端网络和播放缓冲需客户端埋点才能继续拆分。
6. 缺失字段表示该步骤未执行或尚未到达，不能按 0ms 解读；本地配置 Memory 为 nomem、未启用声纹时，相应字段缺失是正常现象。

## 自动验证

不调用云服务的回归测试（无需 pytest）：

```sh
python -m unittest discover -s tests/diagnostics -v
```

本次 19 项测试通过。覆盖计时/关闭开关、长文本完整记录、线程隔离、并行工具、旧音频过滤、打断、错误路径，以及普通回复和工具后二次 LLM 的完整音频链路。端到端测试模拟云端 WebSocket/SSE 和 VAD 判断，其余走生产代码：Opus 输入解码、队列、线程、ASR 结果处理、LLM SDK 流解析、TTS 文本处理、Opus 输出编码和流控；输出会实际解码校验。

真实云调用验证（读取现有配置，产生实际 API 调用）：

```sh
ffmpeg -y -i models/SenseVoiceSmall/example/zh.mp3 -t 4 -ar 16000 -ac 1 -c:a pcm_s16le /tmp/voice-probe.wav
python scripts/diagnose_voice_live.py --audio /tmp/voice-probe.wav --scenario both
```

脚本连接真实 ASR/LLM/TTS 并使用真实 Silero VAD；以本地音频接收器代替硬件，以无副作用的 diagnostic_echo 代替业务工具。分别验证 direct_answer 和工具后二次 LLM；收到的输出 Opus 会解码校验。单轮测试音频限 15 秒，结果保存到 `tmp/voice-diagnostic-results.json`。这不代表已经验证实体硬件播放。

2026-11-14 本次执行环境无法解析讯飞域名，连 example.com 的 DNS 解析也失败，因此真实云服务鉴权与端到端耗时尚未验证；没有将模拟服务耗时当成实际性能数据。
