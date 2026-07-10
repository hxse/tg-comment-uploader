# tg-comment-uploader 设计说明

## 目标

这个项目用于按 profile 把本地视频依次上传到 Telegram 频道评论区，或直接发送到指定频道/群组。

Telegram 的 channel comment 本质上是 linked discussion group 里的 reply。profile 配置 `reply_message_id` 时，工具向 `chat_id` 发送 `sendVideo`，并通过 `reply_parameters.message_id` 回复指定消息；未配置或设为 `null` 时，不发送 `reply_parameters`，视频会作为 `chat_id` 中的独立消息发布。

核心约束：

- 使用 local Telegram Bot API server；单文件上限为 2000 MiB（2,097,152,000 bytes）。
- server 只监听 `127.0.0.1`，不开放公网端口。
- bot token、api id、api hash、chat id、reply message id 全部放在 JSON config。
- 不使用 environment variables 存 secret。
- 上传必须按命令行传入顺序逐个执行。
- 不并发上传。
- profile 可选择回复模式或直发模式；真正的目标频道/群组始终由 `chat_id` 决定。
- 只对明确的临时错误按配置重试；确定性错误立即停止，后续文件不再上传。
- 上传路径必须是 absolute path。
- 上传前一次性校验全部文件；任一文件超限时不发送任何文件。

## 使用流程

进入项目目录：

```sh
cd ~/dev/tg-comment-uploader
```

进入开发环境：

```sh
just dev-shell
```

复制示例配置：

```sh
cp config/example.json config/config.json
```

编辑 `config/config.json`，填入真实的 bot token、api id、api hash、discussion group id 和 reply message id。默认 local server 端口是 `48973`；如果你已经复制过旧配置，需要把真实 config 里的 `server.port` 也改成 `48973`，或者保持 server 和 upload 使用同一个端口。

启动 local Bot API server：

```sh
just server
```

另开一个 terminal，上传文件：

```sh
just upload \
  '/absolute/path/to/videos/video-001.mp4' \
  '/absolute/path/to/videos/video-002.mp4'
```

`just upload` 默认使用 `default` profile。选择不带回复的直发 profile 时：

```sh
just upload-profile direct '/absolute/path/to/videos/video-001.mp4'
```

默认每个文件在首次失败后重试 5 次。如果要指定其他重试次数：

```sh
just upload-retry 8 '/absolute/path/to/videos/video-001.mp4'
```

也可以通过 just variable 覆盖默认值：

```sh
just retries=8 upload '/absolute/path/to/videos/video-001.mp4'
```

如果要使用非默认 config 文件：

```sh
just server-config config/other.json
just upload-config config/other.json '/absolute/path/video.mp4'
just upload-config-retry config/other.json 8 '/absolute/path/video.mp4'
```

## Config

真实业务配置默认放在：

```text
config/config.json
```

这个文件会被 `.gitignore` 忽略，不应该上传 GitHub。

提交到 GitHub 的只有：

```text
config/example.json
```

示例结构：

```json
{
  "bot": {
    "token": "123456789:REPLACE_WITH_BOT_TOKEN",
    "api_id": 123456,
    "api_hash": "REPLACE_WITH_API_HASH"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 48973,
    "binary": "telegram-bot-api",
    "work_dir": ".local/telegram-bot-api"
  },
  "profiles": {
    "default": {
      "chat_id": "-1001234567890",
      "reply_message_id": 12345,
      "caption": "{stem}",
      "supports_streaming": true
    },
    "direct": {
      "chat_id": "-1009876543210",
      "caption": "{stem}",
      "supports_streaming": true
    }
  }
}
```

字段说明：

- `bot.token`: BotFather 给出的 bot token。
- `bot.api_id`: 从 `my.telegram.org` 获取的 API ID。
- `bot.api_hash`: 从 `my.telegram.org` 获取的 API hash。
- `server.host`: local Bot API server 监听地址，默认只允许 `127.0.0.1`。
- `server.port`: local Bot API server 端口。
- `server.binary`: `telegram-bot-api` 可执行文件名。
- `server.work_dir`: local Bot API server 的本地工作目录。
- `profiles.<name>.chat_id`: 目标频道或群组的 chat id；直发频道时需要填写频道 ID，并确保 bot 有发帖权限。
- `profiles.<name>.reply_message_id`: 可选。设置整数时回复该消息；省略或设为 `null` 时直接发送到 `chat_id`。
- `profiles.<name>.caption`: caption 模板，默认 `{stem}` 表示文件名去扩展名。
- `profiles.<name>.supports_streaming`: 是否对视频开启 streaming flag。

## Just CLI

项目入口全部通过 `just`：

```sh
just dev-shell
just server
just upload <absolute-path>...
just upload-profile <profile> <absolute-path>...
just upload-retry <retries> <absolute-path>...
just check
just test
```

静态检查固定为：

```sh
uvx ruff format
uvx ty check
```

测试固定为：

```sh
uv run pytest
```

`just upload` 使用 `set positional-arguments` 和 shell `"${@:2}"` 传递文件路径，目的是保留路径里的空格。

## Python CLI 设计

Python module 名称：

```text
tg_comment_uploader
```

入口命令：

```sh
uv run python -m tg_comment_uploader server --config config/config.json
uv run python -m tg_comment_uploader upload --config config/config.json --retries 5 <paths...>
```

`server` 子命令职责：

- 读取 JSON config。
- 从 config 取 `api_id`、`api_hash`、host、port、work_dir。
- 创建 `work_dir`，相对路径按当前工作目录解析。
- 启动 `telegram-bot-api --local --http-ip-address 127.0.0.1 --http-port <port>`。
- 默认拒绝监听非本机地址。

`upload` 子命令职责：

- 读取 JSON config。
- 根据 `--profile` 选择目标配置；省略时使用 `default`。
- 在上传前校验全部文件路径必须是 absolute path。
- 在上传前校验全部文件存在且是 regular file。
- 在上传前校验全部文件不超过 2000 MiB；超限时显示实际大小和精确上限，并且不发送任何请求。
- 按传入顺序逐个调用 local Bot API `sendVideo`。
- 使用 Python 标准库 streaming multipart 上传文件，按 chunk 读取，不把完整视频读入内存。
- 上传时打印客户端侧进度：已发送字节数、百分比、速度和 ETA。
- 进度表示本工具发送给 local Bot API server 的进度；local server/Telegram 后续处理可能还会继续等待。
- 单次 HTTP 请求 timeout 默认为 6 小时，避免大文件上传或 Telegram 处理阶段被 60 秒短超时提前中断。
- 每个视频的 caption 默认由 `Path(path).stem` 生成。
- profile 设置 `reply_message_id` 时，请求带上 `reply_parameters = {"message_id": reply_message_id}`；否则完全省略 `reply_parameters`，作为独立消息直发。
- `--retries` 表示首次失败后的额外重试次数；默认值 5 表示最多尝试 6 次。
- 仅重试 HTTP/Bot API 408、429、5xx，以及请求体发送完成前发生的明确瞬时网络错误；429 会遵守 JSON 或 HTTP header 中的 `retry_after`。
- 请求体已经完整发送、但响应丢失时结果未知；为避免重复评论，工具不会自动重试。
- 文件超限、`FILE_PARTS_INVALID`、其他确定性 4xx、本地文件错误等不会重试。
- 重试次数用完仍失败时立即退出，后续文件不上传。

## 已实现文件

```text
pyproject.toml
src/tg_comment_uploader/__main__.py
src/tg_comment_uploader/cli.py
tests/test_cli.py
```

CLI 当前通过以下入口运行：

```sh
uv run python -m tg_comment_uploader server --config config/config.json
uv run python -m tg_comment_uploader upload --config config/config.json --retries 5 <paths...>
```

## 安全边界

- `config/config.json` 不提交。
- `bot token` 泄漏等于 bot 被接管。
- `api_hash` 不应公开。
- local Bot API server 只绑定 `127.0.0.1`。
- 不通过 Caddy、Nginx 或 firewall 暴露这个服务。
