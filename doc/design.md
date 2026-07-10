# tg-comment-uploader 设计说明

## 目标

这个项目用于按 profile 把本地视频依次上传到 Telegram 频道评论区，或直接发送到指定频道/群组。

超限视频的算法、临时目录、实例锁和媒体组设计详见 `doc/oversize-upload-design.md`。

Telegram 的 channel comment 本质上是 linked discussion group 里的 reply。profile 配置 `reply_message_id` 时，工具向 `chat_id` 发送视频，并通过 `reply_parameters.message_id` 回复指定消息；未配置或设为 `null` 时，不发送 `reply_parameters`，视频会作为 `chat_id` 中的独立消息发布。普通视频和压缩结果使用 `sendVideo`，拆分结果使用 `sendMediaGroup` 组合显示。

核心约束：

- 使用 local Telegram Bot API server；项目统一采用保守安全阈值 2,000,000,000 bytes，所有上传前后大小判断引用同一个常量。
- server 只监听 `127.0.0.1`，不开放公网端口。
- bot token、api id、api hash、chat id、reply message id 全部放在 JSON config。
- 不使用 environment variables 存 secret。
- 上传必须按命令行传入顺序逐个执行。
- 不并发上传，也不并发拆分或压缩；同一项目一次只允许一个 `upload` 实例。
- profile 可选择回复模式或直发模式；真正的目标频道/群组始终由 `chat_id` 决定。
- 只对明确的临时错误按配置重试；确定性错误立即停止，后续文件不再上传。
- 上传路径必须是 absolute path。
- 上传前一次性校验全部路径和文件大小；默认 `--oversize-policy error` 下任一文件超限时不发送任何文件，`split`/`compress` 则按顺序延迟处理超限文件。

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

超限文件默认报错。显式选择无损拆分或有损压缩：

```sh
just upload -o split '/absolute/path/to/videos/large.mp4'
just upload --oversize-policy compress '/absolute/path/to/videos/large.mp4'
```

`-o` 是 `--oversize-policy` 的短写；两者是同一个选项。不提供 `--auto-split` 或 `--auto-compress` 布尔别名。

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
uv run python -m tg_comment_uploader upload --config config/config.json --retries 5 --oversize-policy error <paths...>
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
- 使用跨平台 `filelock.FileLock` 获取项目级非阻塞上传锁；另一个 `upload` 实例正在运行时立即退出。
- 在上传前一次性校验全部路径必须是 absolute path、存在、是 regular file 且非空，并校验实际字节数。
- 全局硬阈值 `SAFE_UPLOAD_LIMIT_BYTES = 2_000_000_000` 保持唯一；拆分目标为 98%（1,960,000,000 bytes），压缩目标为更保守的 95%（1,900,000,000 bytes）。
- `--oversize-policy error` 为默认值；任一文件超限时在第一个请求前退出。
- `--oversize-policy split` 使用 ffprobe 包大小和关键帧规划最少且尽量均衡的分块，再用 FFmpeg stream copy 无损拆分；分块名在原文件最后一个扩展名前插入 ` part-NNNN`。
- `--oversize-policy compress` 使用 `libx264` 的 `veryfast` preset 进行 H.264/AAC 两遍编码生成一个接近目标体积的 MP4；实际超限时降低码率并从源文件重做。
- 找不到 `ffmpeg` 或 `ffprobe` 时立即退出，并提示运行 `just dev-shell` 后重试。
- 拆分和压缩严格串行；每个源文件按“处理、验证、上传、清理”完成后才处理下一个。
- 临时产物固定放在源视频同目录的 `.tg-comment-uploader-work`，处理前清理，结束时在 `finally` 清理；不做跨命令缓存。
- 普通视频和压缩结果调用 `sendVideo`；拆分结果按 2 到 10 个组成 `sendMediaGroup`，超过 10 个时使用最少且尽量均衡的合法媒体组。
- 媒体组通过 local Bot API 的本地 `file://` URI 引用分块，避免构造巨大的 multipart 请求；caption 只放在每组第一个视频。
- 普通 `sendVideo` 使用 Python 标准库 streaming multipart，按 chunk 读取，不把完整视频读入内存。
- multipart 上传时打印客户端侧进度；拆分和压缩实时解析 FFmpeg 进度。TTY 使用单行进度条显示百分比、elapsed 和 ETA；非 TTY 不输出回车控制字符，只按开始、每 10 秒和完成低频记录。
- 本地请求体发送完成后，TTY 使用 spinner 持续显示等待 Telegram 响应的 elapsed；非 TTY 立即输出状态并每 30 秒输出心跳。该状态不伪造百分比或 ETA。
- 单次 HTTP 请求 timeout 默认为 6 小时，避免大文件上传或 Telegram 处理阶段被短 timeout 提前中断。
- 每个源视频的 caption 默认由原始 `Path(path).stem` 生成，不使用临时文件名。
- profile 设置 `reply_message_id` 时，请求带上 `reply_parameters = {"message_id": reply_message_id}`；否则完全省略 `reply_parameters`，作为独立消息直发。
- `--retries` 表示首次失败后的额外重试次数；默认值 5 表示最多尝试 6 次。同一次命令的上传重试复用已验证产物，不重复运行 FFmpeg。
- 仅重试 HTTP/Bot API 408、429、5xx，以及请求体发送完成前发生的明确瞬时网络错误；429 会遵守 JSON 或 HTTP header 中的 `retry_after`。
- 请求体已经完整发送、但响应丢失时结果未知；为避免重复消息或媒体组，工具不会自动重试。
- `FILE_PARTS_INVALID`、FFmpeg 确定性失败、其他确定性 4xx、本地文件错误等不会套用网络重试。
- 重试次数用完仍失败时立即退出，后续文件不上传；多个媒体组部分成功时明确警告重跑可能产生重复。

## 已实现文件

```text
pyproject.toml
flake.nix
src/tg_comment_uploader/__main__.py
src/tg_comment_uploader/cli.py
src/tg_comment_uploader/locking.py
src/tg_comment_uploader/media_split.py
src/tg_comment_uploader/media_compress.py
src/tg_comment_uploader/media_workflow.py
tests/
```

CLI 当前通过以下入口运行：

```sh
uv run python -m tg_comment_uploader server --config config/config.json
uv run python -m tg_comment_uploader upload --config config/config.json --retries 5 --oversize-policy error <paths...>
```

## 安全边界

- `config/config.json` 不提交。
- `bot token` 泄漏等于 bot 被接管。
- `api_hash` 不应公开。
- local Bot API server 只绑定 `127.0.0.1`。
- 不通过 Caddy、Nginx 或 firewall 暴露这个服务。
