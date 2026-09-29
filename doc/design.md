# tg-comment-uploader 设计说明

> 状态：当前最终设计。上传通过 Telethon 直接连接 Telegram MTProto。

## 目标

这个项目用于按 profile 把本地视频依次上传到 Telegram 频道评论区，或直接发送到指定频道/群组。

另有独立的 `just reupload`：监听指定频道或 Bot 私聊中的转发，下载后重新上传到同一会话。配置、支持范围和恢复方式详见 [转发备份设计与使用](reupload-design.md)。以下上传流程主要描述 `just upload`。

Telegram channel comment 本质上是 linked discussion group 中的 reply。profile 配置 `reply_message_id` 时，工具向 `chat_id` 发送视频，并用 MTProto `InputReplyToMessage` 回复指定消息；省略或设为 `null` 时，视频作为 `chat_id` 中的独立消息发布。

普通视频和压缩结果发送为单视频；拆分结果按 2–10 项组成相册，超过 10 项时继续使用项目既有的最少且均衡分组。

超限视频算法、workspace、实例锁和媒体组规则详见 [超限视频处理设计](oversize-upload-design.md)。

核心约束：

- Telegram transport 固定为 Telethon 1.x MTProto。
- Python 运行时依赖固定为 `telethon>=1.44,<2`、`cryptg>=0.6,<1`、`hachoir>=3.3,<4`、`pydantic>=2.13,<3` 和 `filelock>=3.29.7,<4`，实际版本统一由 `uv.lock` 锁定。它们分别负责 MTProto、加速 AES、视频元数据、严格配置模型和跨平台项目锁。
- bot 使用 `api_id + api_hash + bot token` 登录，不需要帐号密码、手机号、短信码或用户 session。
- 项目统一采用 2,097,152,000 bytes（2000 MiB）的文件上限，对应 4000 个 512 KiB MTProto 分片；所有上传前后大小判断引用同一个常量。
- config 使用严格 Pydantic v2 模型；顶层允许 `bot`、`profiles` 及可选的 `reupload`，每一层都拒绝未知字段和未声明的类型强制转换。
- source、文件和媒体组条目严格按命令行顺序串行处理，不并发拆分、压缩或 final send；唯一例外是单个文件内部的 MTProto SavePart 流水线。同一项目的 `upload` 和 `reupload` 共用一个实例锁，整个命令运行期间互斥。
- 上传前一次性校验全部路径、初始大小和 caption；默认 `error` policy 下任一文件超限时不发送任何文件。
- 一个未完成 logical send 的应用层重试和受支持的进程重启恢复始终复用已落盘的 MTProto `random_id`。
- 一条命令完成并清除 pending state 后，再次上传相同文件是新的发布，会得到新的 random ID。

## 使用流程

进入源码 checkout：

```sh
cd ~/dev/tg-comment-uploader
```

本项目是单用户自用的源码仓库工具。只支持在包含 `justfile` 的 checkout 中通过 `just` 或 `uv run python -m tg_comment_uploader` 运行；不承诺 wheel、pip、全局安装或脱离源码树后的 console script 可用。

进入开发环境：

```sh
just dev-shell
```

复制示例配置：

```sh
cp config/example.json config/config.json
chmod 600 config/config.json
```

编辑 `config/config.json`，填入 bot token、API ID/hash、目标 chat ID 和可选 reply message ID。首次上传会在本地创建 Telethon bot session；这个过程不应询问手机号或验证码。后续命令复用与 config、bot 和凭据绑定的 session。

上传文件：

```sh
just upload \
  '/absolute/path/to/videos/video-001.mp4' \
  '/absolute/path/to/videos/video-002.mp4'
```

选择直发 profile：

```sh
just upload-profile direct '/absolute/path/to/videos/video-001.mp4'
```

超限文件默认报错。显式选择无损拆分或有损压缩：

```sh
just upload -o split '/absolute/path/to/videos/large.mp4'
just upload --oversize-policy compress '/absolute/path/to/videos/large.mp4'
```

`-o` 是 `--oversize-policy` 的短写；不提供 `--auto-split` 或 `--auto-compress` 布尔别名。

默认首次失败后额外重试 5 次。指定其他次数：

```sh
just upload-retry 8 '/absolute/path/to/videos/video-001.mp4'
just retries=8 upload '/absolute/path/to/videos/video-001.mp4'
```

使用其他 config：

```sh
just upload-config config/other.json '/absolute/path/video.mp4'
just upload-config-retry config/other.json 8 '/absolute/path/video.mp4'
```

## Config

真实业务配置默认位于：

```text
config/config.json
```

`.gitignore` 忽略 `config/` 下除 `config/example.json` 外的文件、备份和子目录。忽略规则不能阻止 `git add -f`，提交前仍需检查暂存内容中是否出现 bot token、api hash、session 或其他私密数据。

示例结构：

```json
{
  "bot": {
    "token": "123456789:REPLACE_WITH_BOT_TOKEN",
    "api_id": 123456,
    "api_hash": "REPLACE_WITH_API_HASH"
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

- `bot.token`：BotFather 给出的 bot token。
- `bot.api_id`：从 `my.telegram.org` 获取的正整数 API ID。
- `bot.api_hash`：对应 API hash。
- `profiles.<name>.chat_id`：目标频道、群组或 discussion group 的十进制 ID、Telegram username 或邀请链接。纯十进制字符串会在 MTProto 边界先转换为整数，避免 `-100...` 被当成电话号码；不支持可能命中 session 缓存中歧义对象的显示名，也不支持电话号码。
- `profiles.<name>.reply_message_id`：可选正整数。存在时回复该消息；省略或 `null` 时 direct。
- `profiles.<name>.caption`：默认 `{stem}`，`null` 表示空 caption。只允许 `{name}`、`{stem}`、`{suffix}`、`{parent}`、`{path}`；禁止属性、下标、conversion 和 format spec。
- `profiles.<name>.supports_streaming`：默认 `true`；显式 `false` 会传到每个视频属性。

配置 JSON 在解析阶段递归拒绝重复字段；配置模型在顶层、`bot` 和每个 profile 上统一使用 `strict=True`、`extra="forbid"`、`frozen=True` 和 `hide_input_in_errors=True`。字段拼写错误和其他未知字段均在任何 pending 或网络动作前报错；错误信息不包含 token、API hash 等原始输入值。只有 schema 明确声明的业务规范化例外：整数 `chat_id` 转成稳定十进制字符串，`caption: null` 转成空字符串。`api_id` 和 `reply_message_id` 均限制为 Telegram 可序列化的正 signed 32-bit 范围。

POSIX 系统上每次读取真实 config 时收紧为 `0600`。

## Just CLI

项目常用入口：

```sh
just dev-shell
just upload <absolute-path>...
just upload-profile <profile> <absolute-path>...
just upload-retry <retries> <absolute-path>...
just upload-profile-retry <profile> <retries> <absolute-path>...
just upload-config <config> <absolute-path>...
just upload-config-profile <config> <profile> <absolute-path>...
just upload-config-retry <config> <retries> <absolute-path>...
just upload-config-profile-retry <config> <profile> <retries> <absolute-path>...
just check
just test
```

默认 config、profile 和 retries 分别是 `config/config.json`、`default`、`5`。现有参数顺序和含空格路径转发保持不变。

静态检查：

```sh
uv run ruff format --check
uv run ruff check .
uv run ty check
```

测试：

```sh
uv run pytest
```

项目级上传锁通过源码树中的 `justfile` 定位项目根，因此隔离安装后的 upload 不属于支持场景。

## Python CLI

正常上传入口：

```sh
uv run python -m tg_comment_uploader upload \
  --config config/config.json \
  --profile default \
  --retries 5 \
  --oversize-policy error \
  <absolute-paths...>
```

`upload` 的编排顺序：

1. 安装 SIGTERM→`KeyboardInterrupt` 的正常解栈处理，并获取项目级非阻塞锁。
2. 用严格 Pydantic v2 模型读取并验证 bot/profiles config；任何未知字段或类型不匹配立即退出。
3. 选择 profile 和 policy。
4. 一次性校验全部路径必须 absolute、存在、regular、非空，并读取初始字节数。
5. 一次性渲染全部 caption；后续 caption 无效时不能先发送前面的文件。
6. 输出有序上传计划。
7. 先只读严格解析已有 pending，并确认它可由当前命令安全处理，再串行计算源文件内容 hash 和 command intent fingerprint；此时尚未创建 state 或联网。
8. 同 owner 且 intent 完全相同时恢复原 operation；若它已经 fully-confirmed，则只清理终态残留并退出，不重新发送。可信且全部仍为 `planned` 的 state 可跨 intent、config 和 bot owner 原子替换；同 owner 的 fully-confirmed 终态在不同 intent 到来时也可原子替换并开始新 operation。其余不匹配停止并要求人工检查。
9. 按顺序检查 source-completed 标记；已完成 source 直接跳过 `prepare_media()`。
10. 对未完成 source 进入 `prepare_media()`。未超限文件仍直接选择原文件；split/compress 则先从单次打开的 source fd 流式复制并计算 SHA-256，创建仅供本次命令使用的私有快照。复制前后检查 regular file、元数据、精确 EOF，且快照 size/hash 必须与初始 intent identity 完全一致；只有验证成功后，FFprobe/FFmpeg 才读取该快照，绝不再按原 source 路径取媒体输入。随后校验并计算产物 size/hash、写入 pending、构造携带 expected size/hash 的 `UploadItem` 并持久化 logical unit 的 random IDs；首次实际发送时才创建整条命令唯一的 `MtprotoSender`，再用实际上传的同一 fd 复核内容后同步发送或跳过已 confirmed unit。
11. 当前 source 全部处理完后写 source-completed 标记并清理 workspace；最后一个标记与 `completion_ready=true` 同次落盘。
12. 关闭 sender，删除 pending state，最后释放项目锁。

阈值和媒体行为：

- 硬阈值为 2,097,152,000 bytes（2000 MiB）。
- split target 为 2,055,208,960 bytes，compress target 为 1,992,294,400 bytes。
- `error` 为默认 policy；任一文件超限时在第一个 Telegram 动作前退出。
- split 继续用 FFmpeg stream copy 和既有关键帧/最少均衡规划。
- compress 继续用 H.264/AAC、`veryfast` 和 two-pass。
- 只有实际处理超限文件时才要求 ffmpeg/ffprobe；普通视频的元数据由固定 Python 依赖 Hachoir 读取，不新增外部二进制要求。
- workspace 固定为源文件同目录 `.tg-comment-uploader-work`；其中 `input/` 保存本次已验证快照，`output/` 保存产物，二者都不是缓存并随 workspace 清理。

MTProto 发送：

- 一个命令只使用一个 Telethon client/event loop。
- 单个文件固定切成 512 KiB MTProto transport chunks（最后一块可以更小）；同一 MTProto 连接最多同时保有 8 个在途 SavePart 请求，不创建额外连接扩并发，也不新增 CLI/config 调节项。
- 这 8 个在途请求只属于当前一个文件。一个文件上传结束后才处理媒体组下一条目或下一个 source；`UploadMediaRequest`、`SendMediaRequest` 和 `SendMultiMediaRequest` 仍严格串行。
- `UploadItem` 携带 coordinator 已写入 pending 的 expected size/SHA-256；sender 必须从同一 fd 顺序读取、计算 digest 并构造 SavePart，任何不匹配都发生在 final RPC 前。
- Hachoir 为常见视频补全真实时长和尺寸；若扩展名或元数据无法识别，仍显式补一个保守的 `DocumentAttributeVideo`，不能退化为普通 document。
- 单视频通过 same-fd SavePart 流水线上传，最终用 raw `messages.SendMediaRequest`。
- media group 逐项上传并经 `messages.UploadMediaRequest` 转成 document media，最终用 raw `messages.SendMultiMediaRequest`。
- reply 使用 `InputReplyToMessage`；direct 不传 reply。
- caption 是纯文本，不启用 Telethon 默认 parse mode。
- 每个 final send 使用预先原子持久化的 non-zero signed 64-bit random ID。
- 完整构造 `SendMediaRequest` 或 `SendMultiMediaRequest` 后，sender 先在本地执行一次 TL 字节序列化；只有序列化成功才调用 `before_final_request` 把 unit 标为 `sending`。UTF-8、整数范围或其他本地编码失败是确定性 non-retryable 错误，不调用 final RPC、不启动确认 spinner，且错误不回显 caption。文件 SavePart 和媒体组的 `UploadMediaRequest` 此时可能已经完成，但它们不创建可见消息。
- 返回的公开 TL Updates 必须能完整映射为有序整数 message IDs；缺失或异常映射不能报成功。

重试：

- `--retries` 表示额外应用层尝试，默认 5 即最多 6 次。
- Telethon 内部自动重连和请求重试固定关闭；重连、退避和次数预算统一由应用层 retry 管理。
- final send 前的文件分片失败可重传；同一次命令复用已生成产物，不重复运行 FFmpeg。项目不保存 SavePart 确认位图或上传 offset，当前未完成文件的每次应用层 retry/进程重跑都从字节 0 重新上传。本地 TL 序列化失败发生在 `sending` 状态转换之前，不消耗一次结果不确定的 final send。
- SavePart 阶段收到 `CancelledError` 时，只有 Telethon client 明确报告已经断开连接，才把它分类为可安全重试的 transport 失败；unit 保持 `planned`，下一次尝试从字节 0 重传。
- FloodWait/SlowMode 使用 Telegram 指定的等待值，仍受 24 小时上限约束；否则按 1、2、4、8、16、30 秒封顶退避。
- final send 最长等待 10 分钟；越过 final request 边界后，client 明确断开导致的 `CancelledError` 属于 outcome uncertain，与超时、断线或响应解析失败一样保留 `sending` state，并复用相同 stable random ID；不会换 ID 重发。
- client 仍连接或连接状态无法确认时，`CancelledError` 不转换为网络失败；用户 SIGINT/SIGTERM 也始终保持中断语义，由中断收尾路径 cancel、drain 后退出。
- 确定性配置、文件、权限、peer/reply 或媒体错误立即停止。
- 当前 unit 失败后停止后续 unit 和后续源文件。

## Session 与 pending state

### Telethon session

session 位于：

```text
<project-root>/.local/tg-comment-uploader/mtproto/sessions/
```

文件名由 resolved config path、bot ID 和完整 API credential fingerprint 决定，不包含明文凭据。token 或 API credential 变化会得到新 session。

每次连接后用 `get_me()` 验证当前身份是 token 对应的 bot。POSIX 上目录为 `0700`，session 文件和 sidecar 为 `0600`。session auth key 等价于登录凭据，不能加入 Git、日志或错误 `repr`。

### Pending upload

固定状态文件：

```text
<project-root>/.local/tg-comment-uploader/mtproto/pending-upload-v1.json
```

它保存 command intent、文件/产物 expected size/hash、group 边界、random IDs、`planned/sending/confirmed`、message IDs、source-completed 列表和 command-level `completion_ready`，不保存视频、token、api hash 或 session auth key。JSON 读取与配置共用递归拒绝重复字段的严格 loader，随后再校验精确 schema 与状态机不变量。coordinator 先计算并写入 expected 值，sender 再对实际上传的同一 fd 校验，避免路径预检与 reopen 之间的 TOCTOU。更新使用同目录临时文件、fsync 和 atomic replace；POSIX 权限为 `0600`。

pending JSON schema 保持 v1；媒体处理身份是独立版本，当前 `media_algorithm_version` 为 `2`，表示 FFmpeg 前的 verified snapshot 以及 `input/`、`output/` 布局。旧算法 v1 的全 `planned` checkpoint 会由现有安全替换规则原子淘汰；v1 的 `sending` 或部分 confirmed checkpoint 仍阻止新命令，不能因版本升级自动丢弃。

恢复限制：

- 同一 config/bot 下，profile/peer/reply/policy、有序输入、caption、文件内容和算法版本全部相同时，恢复原 operation 和 random IDs。
- `completion_ready=false`、`completed_sources` 为空且所有已登记 unit 都是 `planned` 的可信 state 可被新的 operation 原子替换；unit 尚为空、已经安全绑定 peer，或 config/bot owner 已改变都不妨碍替换，因为 final send 尚未开始。替换会输出明确提示。旧 state 与新 state 通过同一次 `os.replace()` 切换：replace 前失败保留旧 checkpoint，replace 后读者只会看到完整的旧或新 checkpoint。
- 存在 `sending` 或只完成一部分的 `confirmed`/source-completed 状态时，intent 不匹配就停止并要求人工检查；不自动删除或换 ID。若 owner 不同，这些状态也会在源文件 hash 之前被拒绝。
- split/compress 产物不会跨命令缓存；只有仍有未 confirmed units 的 source 才在恢复时重新生成并验证内容 hash，source 已全 confirmed 时跳过 FFmpeg。
- 已 confirmed 的 source/group 跳过；未确认 unit 复用原 ID。
- 只有 source-completed 标记已原子落盘时才不再运行 FFmpeg；不能根据当前登记的 units 猜测规划是否完整。部分 groups confirmed 的 split source 重新生成并校验 parts，但只发送未确认 groups。
- state 损坏、状态自相矛盾或未知 schema 时，在连接 Telegram 前停止；非全 `planned` 的 config/bot owner 不匹配也会停止。username/link 解析出的 peer 在连接后、文件上传前与 state 比较。
- 最后一个 source-completed 标记与 `completion_ready=true` 在同一次 atomic replace 中持久化，随后删除 state；下一次相同命令生成新 IDs。首个 unit 登记前失败形成的空 state 可直接安全清理。
- 同一 intent 的 fully-confirmed state 只清理残留 state，不重新发送；同 owner 的不同 intent 会原子替换这条已经完成的终态记录并开始新 operation。部分 confirmed 绝不自动清理。
- 这不是文件分片级断点续传；当前未完成文件的 retry/restart 固定从字节 0 开始。恢复也不覆盖另一个 checkout、手工删除 state 或修改输入后的场景。

## Pending 维护命令

查看异常残留 state：

```sh
uv run python -m tg_comment_uploader pending-status \
  --config config/config.json
```

无 state 时输出 `pending upload: <none>`。有 state 时先按完整 schema 和状态机严格读取，即使 owner 不属于当前 config/bot 也允许只读查看；输出额外标记 `owner: current|foreign`，并只显示 schema、operation ID、阶段、unit 数和 confirmed 数，不显示 owner 指纹、bot ID、caption、绝对路径、完整 hash 或凭据。阶段严格区分 `planned`、`sending`、`partially-confirmed` 和 `confirmed`；只要已经确认过任何消息，就不会显示成 `planned`。

人工确认后放弃 state：

```sh
uv run python -m tg_comment_uploader pending-discard \
  --config config/config.json \
  --operation-id <full-operation-id>
```

默认 discard 要求完整 operation ID 且 owner 与当前 config/bot 匹配。若 `pending-status` 明确显示 `owner: foreign`，人工检查 Telegram 后还必须显式增加 `--force-foreign-owner`：

```sh
uv run python -m tg_comment_uploader pending-discard \
  --config config/config.json \
  --operation-id <full-operation-id> \
  --force-foreign-owner
```

force 只放宽 owner 匹配，不绕过严格 state 校验、完整 operation ID 或项目锁，也不会读取另一份 config 的凭据。discard 只删除 pending state，不删除 session、workspace、原文件或 Telegram 消息。全 `planned` 的不匹配 state 可跨 owner 自动淘汰，无需手工 discard；同 owner 的 fully-confirmed 终态在相同 intent 下只清理并退出，在不同 intent 下由新 operation 原子替换。foreign 非 `planned`、`sending` 或部分 confirmed/完成标记不会被 upload 自动覆盖，必须先人工确认；显式 discard 后重跑可能产生重复消息。

维护命令没有 just recipe。

## 进度和用户输出

- 单文件进度只累计 Telegram 已确认完成的 SavePart 字节；即使最多 8 个请求乱序完成，`sent_bytes` 也必须在 `[0,total_bytes]` 内单调不减，再据此显示 elapsed 和 ETA。
- media group 按顺序显示 `item i/n`，不能按文件个数伪造字节百分比。
- 文件传输到 100% 后显示“媒体已上传，等待 Telegram 消息确认”的 spinner/heartbeat。
- TTY 使用单行刷新；非 TTY 不输出回车控制符，并按低频完整行记录。
- 成功后按输入顺序输出 message IDs。
- 结果不确定但 state 完整时，提示正在恢复同一 pending operation，并复用已持久化 random ID 或媒体组 ID 向量；不能只稳定 command operation ID。
- renderer、wait indicator 和 sender 在成功、失败和中断路径都必须收尾。SIGINT/SIGTERM 打断事件循环时，必须先对当前发送 Task 执行 cancel 并 drain；只有确认它不能继续越过 final-send 边界后，才允许重入事件循环执行 disconnect。

## 安全边界

- `config/` 除示例外由 Git 忽略；`.local/` 覆盖 lock、session 和 pending state。提交前仍检查 staged diff。
- config 为 `0600`；MTProto session/state 目录为 `0700`，文件为 `0600`。
- bot token、api hash 和 session auth key 泄漏都可能导致 bot 或 API 凭据被滥用。
- 凭据只传给当前 Python 进程中的 Telethon，不进入 argv、子进程环境或交互 shell。
- 所有外部异常文本在输出前精确脱敏当前 token/api hash/session-sensitive 值、转义控制字符并截断。
- 不打印 Telethon client、session、Updates 或异常对象中可能包含秘密的原始 `repr`。
- state 虽不含登录凭据，仍包含本地路径、caption、chat/message IDs，按私有运行数据处理。
- 项目是单用户、单 checkout 工具；项目锁不协调另一个 checkout。

## 最终运行边界

正常入口只有：

```sh
uv run python -m tg_comment_uploader upload ...
uv run python -m tg_comment_uploader pending-status ...
uv run python -m tg_comment_uploader pending-discard ...
```

三个入口共用同一套配置、session、pending 状态和 MTProto sender；bot 身份只通过 API ID/hash 与 bot token 建立。
