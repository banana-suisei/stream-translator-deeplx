# SSE 译文输出对接文档

本文档用于对接 `stream-translator-gpt` 新增的本地 SSE 输出能力，适合浏览器扩展、桌面悬浮窗、字幕插件等前端场景。

## 1. 启动方式

启用翻译并监听本地 SSE 端口：

```bash
stream-translator-gpt "你的输入源" \
  --translation_prompt "翻译以下日语为中文，只输出译文" \
  --openai_api_key "YOUR_KEY" \
  --sse_port 8765
```

可选参数：

- `--sse_port`：启用本地 SSE 服务并监听指定端口
- `--sse_host`：监听地址，默认 `127.0.0.1`

默认情况下，SSE 地址为：

```text
http://127.0.0.1:8765/events
```

健康检查地址：

```text
http://127.0.0.1:8765/health
```

## 2. 协议说明

服务端返回标准 `text/event-stream` 响应，并附带：

- `Content-Type: text/event-stream; charset=utf-8`
- `Cache-Control: no-cache`
- `Access-Control-Allow-Origin: *`

前端可直接使用 `EventSource` 连接。

## 3. 事件类型

### `ready`

连接建立后立即发送一次。

示例：

```text
event: ready
data: {"connected_at":"2026-03-21T12:34:56.000000+00:00","path":"/events"}
```

### `result`

每当一条分段结果完成输出时发送一次。若开启了 LLM 翻译，则 `translation` 字段为译文；若未开启翻译或翻译失败，可根据 `translation` 和 `translation_failed` 做兜底处理。

示例：

```text
id: 12
event: result
data: {"seq":12,"created_at":"2026-03-21T12:35:01.000000+00:00","time_range":{"start":15.2,"end":18.6,"start_srt":"00:00:15,2","end_srt":"00:00:18,6"},"transcript":"こんにちは","translation":"你好","translation_failed":false}
```

字段说明：

- `seq`：递增事件序号
- `created_at`：服务端生成事件的 UTC 时间
- `time_range.start`：起始秒数
- `time_range.end`：结束秒数
- `time_range.start_srt`：SRT 风格起始时间
- `time_range.end_srt`：SRT 风格结束时间
- `transcript`：原始识别文本
- `translation`：译文；没有译文时可能为 `null`
- `translation_failed`：本次翻译是否失败

### `close`

程序结束或输出服务关闭时发送一次。

## 4. 前端接入示例

### 浏览器 / WebView

```js
const eventSource = new EventSource("http://127.0.0.1:8765/events");

eventSource.addEventListener("ready", (event) => {
  const payload = JSON.parse(event.data);
  console.log("SSE connected:", payload);
});

eventSource.addEventListener("result", (event) => {
  const payload = JSON.parse(event.data);
  const text = payload.translation || payload.transcript || "";

  if (!text) return;

  console.log("new text:", text);
  console.log("time range:", payload.time_range.start, payload.time_range.end);

  // 这里写入你的字幕面板 / 悬浮窗 / 插件 UI
  // renderSubtitle(text);
});

eventSource.addEventListener("close", () => {
  console.log("SSE closed");
  eventSource.close();
});

eventSource.onerror = (error) => {
  console.error("SSE error:", error);
};
```

### 浏览器扩展注意事项

如果是扩展页面或 content script，需要允许访问本地地址，例如：

```json
{
  "host_permissions": [
    "http://127.0.0.1:8765/*"
  ]
}
```

如果你的插件会动态切换端口，请把对应端口加入权限列表。

## 5. Postman 调试

可直接请求：

```text
GET http://127.0.0.1:8765/events
```

你会先收到 `ready` 事件，后续每条翻译结果会持续追加 `result` 事件。

也可以先访问：

```text
GET http://127.0.0.1:8765/health
```

返回示例：

```json
{
  "ok": true,
  "host": "127.0.0.1",
  "port": 8765,
  "path": "/events",
  "clients": 0
}
```

## 6. 对接建议

- 前端展示时优先使用 `translation`
- 当 `translation` 为空时，可回退到 `transcript`
- 可用 `seq` 去重，避免重连后重复展示
- 可用 `time_range.start` / `time_range.end` 做字幕定位
- 若需要更严格的断线重连策略，建议在前端自行封装重连逻辑
