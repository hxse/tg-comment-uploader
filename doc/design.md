# tg-comment-uploader 设计说明

## 目标

这个项目用于按 profile 把本地视频依次上传到 Telegram 频道评论区，或直接发送到指定频道/群组。

超限视频的算法、临时目录、实例锁和媒体组设计详见 `doc/oversize-upload-design.md`。

Telegram 的 channel comment 本质上是 linked discussion group 里的 reply。profile 配置 `reply_message_id` 时，工具向 `chat_id` 发送视频，并通过 `reply_parameters.message_id` 回复指定消息；未配置或设为 `null` 时，不发送 `reply_parameters`，视频会作为 `chat_id` 中的独立消息发布。普通视频和压缩结果使用 `sendVideo`，拆分结果使用 `sendMediaGroup` 组合显示。

核心约束：

- 使用 local Telegram Bot API server；项目统一采用保守安全阈值 2,000,000,000 bytes，所有上传前后大小判断引用同一个常量。
- server 仅允许绑定字符串白名单中的 loopback 地址：`127.0.0.1`、`localhost` 或 `::1`，不开放公网端口。
- upload 同样只接受 `127.0.0.1`、`localhost` 或 `::1`；在检查文件或发送 Bot token 前拒绝其他 host。
- bot token、api id、api hash、chat id、reply message id 全部持久保存在本地 JSON config；真实配置在 POSIX 系统上收紧为 `0600`。
- 启动 local Bot API server 时，api id 和 api hash 只注入该子进程的 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH` 环境变量，不放入进程命令行，也不导出到交互 shell。
- 上传必须按命令行传入顺序逐个执行。
- 不并发上传，也不并发拆分或压缩；同一项目一次只允许一个 `upload` 实例。
- profile 可选择回复模式或直发模式；真正的目标频道/群组始终由 `chat_id` 决定。
- 上传采用接受偶发重复消息的“至少一次投递”语义：临时错误和结果不确定的请求按配置重试，结果不确定时醒目警告重复风险；确定性错误立即停止，后续文件不再上传。
- 上传路径必须是 absolute path。
- 上传前一次性校验全部路径和文件大小；默认 `--oversize-policy error` 下任一文件超限时不发送任何文件，`split`/`compress` 则按顺序延迟处理超限文件。

## 使用流程

进入项目目录：

```sh
cd ~/dev/tg-comment-uploader
```

本项目是单用户自用的源码仓库工具。只支持在包含 `justfile` 的源码 checkout 中通过 `just` 或 `uv run python -m tg_comment_uploader` 运行；不承诺 wheel、pip、全局安装或脱离源码仓库后的 console script 可用。`pyproject.toml` 中保留的 console script 只作为源码开发环境中的便利入口。

进入开发环境：

```sh
just dev-shell
```

复制示例配置：

```sh
cp config/example.json config/config.json
chmod 600 config/config.json
```

编辑 `config/config.json`，填入真实的 bot token、api id、api hash、discussion group id 和 reply message id。程序在 POSIX 系统上每次读取配置时也会主动把该文件权限收紧为 `0600`；上面的显式 `chmod` 用于避免复制后、第一次运行前的权限窗口。默认 local server 端口是 `48973`；如果你已经复制过旧配置，需要把真实 config 里的 `server.port` 也改成 `48973`，或者保持 server 和 upload 使用同一个端口。

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

`.gitignore` 会忽略 `config/` 下除 `config/example.json` 以外的所有文件、备份和子目录；真实配置不应该上传 GitHub。忽略规则不能阻止显式的 `git add -f`，提交前仍需检查暂存内容中是否出现真实 bot token、api hash、私钥等凭据。

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
- `server.host`: local Bot API server 监听地址；只允许 `127.0.0.1`、`localhost` 或 `::1`，示例配置默认使用 `127.0.0.1`。
- `server.port`: local Bot API server 端口。
- `server.binary`: `telegram-bot-api` 可执行文件名。
- `server.work_dir`: local Bot API server 的本地工作目录；启动 server 时在 POSIX 系统上创建或收紧为 `0700`。
- `profiles.<name>.chat_id`: 目标频道或群组的 chat id；直发频道时需要填写频道 ID，并确保 bot 有发帖权限。
- `profiles.<name>.reply_message_id`: 可选。设置整数时回复该消息；省略或设为 `null` 时直接发送到 `chat_id`。
- `profiles.<name>.caption`: caption 模板，默认 `{stem}` 表示文件名去扩展名。占位符必须精确使用 `{name}`、`{stem}`、`{suffix}`、`{parent}` 或 `{path}`；禁止属性、下标、conversion 和 format spec。
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
uv run ruff format --check
uv run ty check
```

测试固定为：

```sh
uv run pytest
```

`just upload` 使用 `set positional-arguments` 和 shell `"${@:2}"` 传递文件路径，目的是保留路径里的空格。

项目级上传锁通过源码树中的 `justfile` 定位项目根目录，因此隔离安装后的 `upload` 不属于支持场景；本项目不增加 wheel 安装测试。

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

- 读取 JSON config，并在 POSIX 系统上把配置文件权限收紧为 `0600`。
- 从 config 取 `api_id`、`api_hash`、host、port、work_dir；持久配置仍以 JSON 为准。
- 创建 `work_dir`，相对路径按当前工作目录解析，并在 POSIX 系统上把目录权限收紧为 `0700`。
- 复制当前进程环境，只在传给 `telegram-bot-api` 的子进程环境中设置 `TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH`；不修改父进程环境，argv 中也不包含这两个值。
- 启动 `telegram-bot-api --local --http-ip-address <server.host> --http-port <port>`；`server.host` 已由上述 loopback 字符串白名单约束。
- 默认拒绝监听非本机地址。

`upload` 子命令职责：

- 读取 JSON config。
- 根据 `--profile` 选择目标配置；省略时使用 `default`。
- 在读取配置后、检查文件前确认 Bot API host 属于 `LOCAL_HOSTS`，避免把 token 或文件通过明文 HTTP 发往远端。
- 使用跨平台 `filelock.FileLock` 获取项目级非阻塞上传锁；另一个 `upload` 实例正在运行时立即退出。
- 在上传前一次性校验全部路径必须是 absolute path、存在、是 regular file 且非空，并校验实际字节数。
- 全局硬阈值 `SAFE_UPLOAD_LIMIT_BYTES = 2_000_000_000` 保持唯一；拆分目标为 98%（1,960,000,000 bytes），压缩目标为更保守的 95%（1,900,000,000 bytes）。
- `--oversize-policy error` 为默认值；任一文件超限时在第一个请求前退出。
- `--oversize-policy split` 先读取小型 ffprobe 元数据，再以 compact 逐行格式流式累计 packet 大小和关键帧，不在内存中保存完整 packet JSON；忽略 `attached_pic` 封面流并把首个真实视频流索引传给 segmenter；随后规划最少且尽量均衡的 FFmpeg stream-copy 分块。
- `--oversize-policy compress` 使用 `libx264` 的 `veryfast` preset 进行 H.264/AAC 两遍编码生成一个接近目标体积的 MP4；实际超限时降低码率并从源文件重做。
- 找不到 `ffmpeg` 或 `ffprobe` 时立即退出，并提示运行 `just dev-shell` 后重试。
- 拆分和压缩严格串行；每个源文件按“处理、验证、上传、清理”完成后才处理下一个。
- 临时产物固定放在源视频同目录的 `.tg-comment-uploader-work`，处理前清理，结束时在 `finally` 清理；不做跨命令缓存。
- 普通视频和压缩结果调用 `sendVideo`；拆分结果按 2 到 10 个组成 `sendMediaGroup`，超过 10 个时使用最少且尽量均衡的合法媒体组。
- 媒体组通过 local Bot API 的本地 `file://` URI 引用分块，避免构造巨大的 multipart 请求；caption 只放在每组第一个视频。
- HTTP 请求不手工添加 `Host`，由 Python 标准库根据 host、端口和 IPv6 语法生成唯一请求头。
- 普通 `sendVideo` 使用 Python 标准库 streaming multipart，按 chunk 读取，不把完整视频读入内存。
- 每次 multipart HTTP 尝试只打开文件一次：对该文件描述符执行 `fstat`、计算 `Content-Length` 并从同一文件描述符发送，不在统计大小后按路径重新打开，因此同尺寸路径替换不会改变本次实际上传内容。
- multipart 上传时打印客户端侧进度；拆分和压缩实时解析 FFmpeg 进度。TTY 使用单行进度条显示百分比、elapsed 和 ETA；非 TTY 不输出回车控制字符，只按开始、每 10 秒和完成低频记录。
- 本地请求体发送完成后，TTY 使用 spinner 持续显示等待 Telegram 响应的 elapsed；非 TTY 立即输出状态并每 30 秒输出心跳。该状态不伪造百分比或 ETA。
- 单次 HTTP 请求 timeout 默认为 6 小时，避免大文件上传或 Telegram 处理阶段被短 timeout 提前中断。
- 每个源视频的 caption 默认由原始 `Path(path).stem` 生成，不使用临时文件名；全部输入文件的 caption 在首次准备或上传前一次性渲染成功。
- profile 设置 `reply_message_id` 时，请求带上 `reply_parameters = {"message_id": reply_message_id}`；否则完全省略 `reply_parameters`，作为独立消息直发。
- `--retries` 表示首次失败后的额外重试次数；默认值 5 表示最多尝试 6 次。同一次命令的上传重试复用已验证产物，不重复运行 FFmpeg。
- 重试 HTTP/Bot API 408、429、5xx、明确的瞬时网络错误，以及“请求可能已经成功但无法确认结果”的错误；429 会遵守 JSON 或 HTTP header 中的 `retry_after`。
- 请求体已经完整发送但响应丢失、成功响应损坏、异常 408/429/5xx 响应无法解析或读取中断、或成功响应缺少有效 message ID 时，结果不确定。只要仍有重试预算就继续重试，并在每次重试前醒目提示可能产生重复消息或媒体组；有效 `retry_after` 仍会保留。
- 对 retryable HTTP 状态或 Bot API 状态，只有 JSON 对象中的 `ok` 严格等于 `false` 才视为服务端明确拒绝；缺少 `ok`、`ok:true` 或其他类型均按结果不确定处理。该规则也优先于 `FILE_PARTS_INVALID` 文本特判。
- 非 UTF-8、非法 JSON 或非对象 JSON 响应的错误信息只报告 HTTP 状态和响应字节数，绝不回显原始响应正文，避免 token URL 或终端控制字符泄漏到日志。
- HTTP 协议异常文本和 Bot API `description` 在展示前统一净化：脱敏 `/bot<TOKEN>` 路径段、把非 printable 字符转成可见转义，并将外部文本限制为最多 500 个字符；内部错误分类仍使用未经截断的原始字段。
- 没有有效 `retry_after` 时采用简单指数退避：第 1 次重试等待 1 秒，之后为 2、4、8、16 秒，最大 30 秒；避免立即连续请求。
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

- `config/` 下除占位内容的 `example.json` 外均由 Git 忽略；提交前仍检查暂存内容，不使用 `git add -f` 强行加入真实配置。
- POSIX 系统上的真实 config 使用 `0600`，Bot API `work_dir` 使用 `0700`。
- `bot token` 泄漏等于 bot 被接管。
- `api_hash` 不应公开；它只通过 `telegram-bot-api` 子进程专用环境传递，不出现在 argv。相同本地用户或 root 仍可能读取子进程环境，这是单用户自用场景接受的边界。
- local Bot API server 只允许绑定 `127.0.0.1`、`localhost` 或 `::1` 这三个 loopback 字符串白名单地址。
- 不通过 Caddy、Nginx 或 firewall 暴露这个服务。
- 当前凭据不因本地 argv 暴露而强制轮换；如果未来发现真实凭据进入 Git commit、远程仓库或共享备份，应立即轮换 bot token 并重新评估 api hash。
