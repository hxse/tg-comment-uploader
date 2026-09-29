# 超限视频处理设计

> 状态：MTProto-only 最终设计。媒体算法、workspace、锁、阈值和串行模型保持既有实现；上传 transport、幂等与恢复统一为 Telethon/MTProto。
>
> 项目的严格配置、session、pending 状态机和维护命令统一见 [设计说明](design.md)。

## 目标

当输入视频超过项目的 Telegram 单文件安全阈值时，用户明确选择一种处理方式：

- `error`：直接报错，不运行 FFmpeg，也不连接 Telegram。
- `split`：使用 FFmpeg 无损拆分为尽量少、大小尽量均衡的多个视频，再按顺序发送为一个或多个相册。
- `compress`：使用 FFmpeg 有损压缩为一个尽量接近、但不超过安全阈值的视频，再发送为单视频。

整个过程遵守：

- 输入文件按命令行顺序处理。
- source、文件和媒体组条目严格串行；唯一允许的上传并发是当前单个文件内部、同一 MTProto 连接最多 8 个在途 SavePart 请求。
- 同一项目不允许同时运行多个 `upload` 命令。
- 不做跨命令媒体缓存，不持久化 split/compress 产物。
- pending state 只恢复 logical send 和 stable random IDs，不保存 SavePart offset；retry/restart 时当前未完成文件从字节 0 重新上传。
- 原视频永不修改、覆盖或删除。

## CLI

只有一个语义选项，提供短、长两种写法：

```text
-o, --oversize-policy {error,split,compress}
```

默认值为 `error`。

```sh
just upload -o split '/absolute/path/video.mp4'
just upload --oversize-policy compress '/absolute/path/video.mp4'
```

该选项只影响超限文件。未超限文件在三种 policy 下都直接上传，不拆分、不重编码。只有实际处理超限文件时才要求 `ffmpeg` 和 `ffprobe` 可用。

`-o` 只是长选项的短写；不增加 `--auto-split`、`--auto-compress` 等布尔别名，也不在 profile 中增加重复配置。

## 统一文件大小阈值

`upload`、`reupload` 和底层发送器共用同一个文件上限：

```python
SAFE_UPLOAD_LIMIT_BYTES = 4000 * 512 * 1024  # 2,097,152,000 bytes / 2000 MiB
```

此值对应 [官方配置示例](https://core.telegram.org/api/config#upload-max-fileparts-default) 中的 4000 个分片，以及 [MTProto 上传规则](https://core.telegram.org/api/files#uploading-files) 允许的每片 512 KiB。它是项目固定上限，不动态获取账号额度；最终请求仍由 Telegram 服务端校验。

以下位置必须引用同一个常量：

- 原始文件是否超限；
- 拆分数量和切点规划；
- 压缩目标大小；
- FFmpeg 产物最终校验；
- MTProto 上传前的 same-fd 最终校验；
- Telegram 文件大小错误诊断。

文件必须非空，且大小不超过该阈值；MTProto transport 不改变项目阈值。

拆分保留 2% 余量，压缩保留 5% 余量：

```python
split_target_bytes = floor(SAFE_UPLOAD_LIMIT_BYTES * 0.98)
compress_target_bytes = floor(SAFE_UPLOAD_LIMIT_BYTES * 0.95)
```

即 2,055,208,960 和 1,992,294,400 bytes。target 只用于规划；最终能否发送仍由硬阈值的实际字节数决定。

## 总体处理流程

命令取得项目级上传锁后，一次性完成全部输入路径、初始大小和 caption 的基础预检：

- 路径必须 absolute；
- 文件存在且是 regular file；
- 文件非空；
- 保存初始字节数并保持输入顺序；
- 所有 caption 模板都成功渲染。

预检发生在 pending state 创建、sender 连接、peer 解析和任何 Telegram 动作之前。`error` policy 下只要任一文件超限，整个命令在发送第一个请求前终止。

之后逐个处理：

```text
获取项目锁
  校验全部路径、大小和 caption
  policy=error 且任一文件超限：联网前报错
  顺序计算源文件内容 hash 和 command intent fingerprint
  相同 intent 恢复；若已 fully-confirmed，只清理终态并退出
  不匹配且全 planned 时可跨 owner 原子替换
  同 owner fully-confirmed 且 intent 不同时原子替换；其他不匹配停止
  按命令行顺序遍历源文件
    若 state 中该 source 的 source-completed 标记已落盘：跳过 FFmpeg 和发送
    否则：
      未超限：选择原文件作为实际发送项
      已超限且 policy=split/compress：
        清理并创建临时 workspace
        从单次打开的 source fd 流式复制、计算 hash，创建私有输入快照
        快照 size/hash 与初始 identity 完全一致后，FFprobe/FFmpeg 只读取快照
        校验全部产物
        计算产物内容 hash
      对本源的全部实际发送项：
        把 expected size/hash 写入 pending 和 UploadItem
        为各 logical units 持久化 random IDs
        首次实际发送时按需创建 MtprotoSender
        sender 对实际上传的同一 fd 复核 size/hash，再发送或跳过 confirmed units
      全部 unit confirmed 后原子写入本 source 的 source-completed 标记
      若创建了 workspace：清理 workspace
  最后一个 source-completed 与 completion_ready 同次落盘
  关闭 sender
  删除 pending state
释放项目锁
```

每个源文件走完“准备、验证、发送、清理”后才处理下一个，任何时刻只有一个活动 FFmpeg 任务和一组活动产物。

split/compress 的输入冻结发生在任何 FFprobe/FFmpeg 进程之前。`prepare_media()` 从 source 路径只打开一次 regular file，把该 fd 的字节流复制到 workspace 私有快照，同时计算 SHA-256，并在复制前后检查 size、device/inode、mtime、ctime 和精确 EOF。快照 size/hash 必须与命令开始时写入 intent 的 identity 一致；路径替换、同 inode 原地修改，以及“处理期间临时修改、随后恢复”都不能让 FFmpeg 得到一份未经验证的输入：若变化进入快照，identity 校验失败且不启动 FFmpeg；若快照仍逐字节等于初始内容，它就是安全输入。快照验证后原 source 再发生变化不会影响本次产物，因为 FFprobe/FFmpeg 只读取快照。任何失败都发生在 `fingerprint_prepared_files()`、`state.register_unit()` 和 sender 调用之前。

批次不具备 Telegram 事务回滚：前面已经 confirmed 的消息不会因后续文件失败而删除。不过 pending state 会保存 confirmed units，匹配恢复时跳过它们，避免工具主动重发前序成功消息。

## `split`：无损拆分

### 优化目标

拆分规划按以下优先级：

1. 所有分块均通过统一大小阈值。
2. 分块数量尽量少。
3. 分块数量相同时，各块大小尽量均衡。
4. 保持原始时间顺序，并尽量从关键帧开始播放。

“无损”表示音视频流使用 FFmpeg stream copy，不重新编码。关键帧约束可能使实际分块大小不能完全相等。

### 规划算法

1. 使用 `ffprobe` JSON 获取小型 format/stream 元数据，读取 disposition，选择首个非 `attached_pic` 视频流。
2. 使用 compact 逐行 packet 输出，流式累计所有流的包大小，只保留真实视频流的关键帧边界；不在内存保存完整 packet JSON。逐行解析提前失败时仍排空 stdout、等待进程退出并读取 stderr；ffprobe 非零退出码和真实诊断优先于派生解析错误。
3. 以 `ceil(source_size / split_target_bytes)` 作为最少分块数初始下界。
4. 按累计媒体字节数寻找接近等分目标的关键帧，不按平均时长简单切割。
5. 在当前块数下选择合法切点，使最大分块尽量小且整体均衡。
6. 使用 FFmpeg segment muxer 和 stream copy 一次生成所有分块，并把所选视频流全局索引传为 `reference_stream`。
7. 按编号和时间顺序检查实际输出。

产物在原文件最后一个扩展名前插入编号。例如 `archive.final.mp4`：

```text
archive.final part-0001.mp4
archive.final part-0002.mp4
```

封装开销导致分块超限时，丢弃本轮全部产物、收紧规划目标并重做；必要时增加分块数。任一分块超限时不得发送该源文件的任何 part。

关键帧分布导致无法安全无损拆分时，明确报错并建议 `--oversize-policy compress`。`split` 不得静默降级为有损压缩。

### 分块产物校验

FFmpeg 成功退出后仍须确认：

- 至少两个分块；
- 每个分块存在、是 regular file、非空且不超限；
- 编号连续、时间顺序正确；
- 每个分块可由 ffprobe 解析并包含视频流；
- 实际内容 hash 与新建或恢复的 pending intent 匹配。

仍有未 confirmed groups 时，恢复重新生成分块，不从旧 workspace 复用文件。只有重新生成的有序产物、group 边界和内容 hash 全部一致，才能复用 pending random IDs；该 source 已全 confirmed 时不运行 FFmpeg。

## `compress`：有损压缩

目标是生成接近 `compress_target_bytes`、且最终不超过硬阈值的 MP4。输出继续使用 H.264、AAC、`yuv420p` 和 faststart。

冻结流程：

1. 使用 ffprobe 获取时长、音视频流和原始码率。
2. 从目标大小和时长反算允许总码率。
3. 为音频和 MP4 封装开销预留预算，其余给视频。
4. 使用 libx264 `veryfast` preset 两遍编码。
5. 用实际文件字节数和 ffprobe 做硬校验。

输出固定为当前 workspace 的 `output/` 子目录中：

```text
output/compressed.mp4
```

实际结果超限时删除结果，从原视频重做，并按“目标大小 / 实际大小”比例降低视频码率。包含首次编码在内最多保持既有 3 次尝试；仍无法得到合法产物则停止。

不使用 FFmpeg `-fs` 作为大小保证，因为它可能截断输出。caption 和 pending intent 始终来自原始文件，不使用临时名 `compressed.mp4`。

compress unit 尚未 confirmed 时，进程重启恢复重新压缩，并要求产物内容 hash 与 pending state 相同；无法稳定重生成相同内容时停止，不换 random ID，也不自动 discard。unit 已 confirmed 时不重新压缩。

## Telegram MTProto 上传

### 普通文件和压缩文件

未超限原文件和压缩产物都作为一个 logical unit：

1. coordinator 计算实际发送文件的 expected size/SHA-256，写入 pending，并构造对应 `UploadItem`。
2. 为 unit 生成或读取 pending state 中的单个 non-zero signed 64-bit random ID。
3. sender 只打开文件一次，先用该 fd 校验 regular 和初始 size，并读取视频 attributes。
4. 使用同一个 fd 以固定 512 KiB chunks 上传并同步计算 SHA-256；末块可以更小，同一 MTProto 连接最多 8 个 SavePart 请求在途。全部 part 确认后再检查 EOF、最终 stat 和 digest，确保它们仍与初始 stat 及 expected identity 一致。
5. 构造带视频 attributes 的 `InputMediaUploadedDocument`。
6. 完整构造 raw `messages.SendMediaRequest`，并在本地执行一次 TL 字节序列化；编码失败时保持 `planned`，不调用 final RPC。
7. 本地序列化成功后，`before_final_request` 才把 unit 原子标为 `sending` 并发布请求。
8. reply profile 使用 `InputReplyToMessage(reply_message_id)`；direct profile 不传 reply。
9. 完整验证 `random_id → message_id` 后标记 confirmed。

caption 是源文件模板渲染结果，按纯文本发送，不启用 Telethon parse mode。`supports_streaming` 映射到视频 attribute。

### 拆分文件

拆分 part 的分组保持：

- 2–10 项使用一个 group；
- 超过 10 项时保持时间顺序，使用最少且尽量均衡的合法 groups；
- 不允许单元素尾组，例如 11 项是 6+5，而不是 10+1；
- 每个 group 回复同一个 `reply_message_id`，或全部 direct；
- 每个 group 只有第一项带源文件 caption；
- 每项继承 `supports_streaming`。

每个 group 的 MTProto 流程：

1. coordinator 先把每项 expected size/hash 和有序 random IDs 写入 pending；sender 严格逐项处理。每项内部使用固定 512 KiB、最多 8 个在途 SavePart 的同连接流水线，但当前条目完全结束后才开始下一条目。
2. 为每项构造 `InputMediaUploadedDocument`，再用 `messages.UploadMediaRequest` 得到 document media。
3. 为每项构造 `InputSingleMedia`，使用 pending 中有序、独立的 random ID。
4. 完整构造 raw `messages.SendMultiMediaRequest` 并先在本地执行 TL 字节序列化；只有成功后才把整个 group 标为 `sending` 并调用 final RPC。
5. 验证返回了等量、有序、同一 `grouped_id` 的 message IDs 后，才标记本 group confirmed。

本地 TL 序列化覆盖完整 final request，可在 final RPC 的网络调用前发现孤立 Unicode surrogate、整数越界和其他 Telethon 编码错误；失败是确定性 non-retryable，不调用 `before_final_request`、不启动确认 spinner，也不回显 caption。文件 SavePart 和媒体组的 `UploadMediaRequest` 此时可能已经完成，但它们不在聊天中创建可见消息；final `SendMultiMediaRequest` 开始后出现 timeout、断线或 Updates 不完整时，整个 group 保持 `sending`，重试和重启恢复都复用同一 random ID 向量。

不得交给 Telethon 高层 API 自动分组，也不得用高层 `send_file()` 生成不可持久化控制的 final random IDs。

## 临时工作目录

不使用系统临时目录保存媒体产物。workspace 固定为：

```text
<source.parent>/.tg-comment-uploader-work/
```

不同目录的输入分别使用各自同级 workspace；严格串行保证任何时刻只有一个活动目录。

workspace 是一次运行期间的临时目录，不是缓存：

- 获得全局锁后、开始拆分或压缩前清理同名旧目录；
- 创建权限为 `0700` 的 `input/` 和 `output/` 子目录；
- 每次都把原视频复制并校验为 `input/<source.name>` 私有快照，再从该快照重新 split/compress；
- 不按文件名、mtime、大小、hash 或 manifest 复用上次媒体产物；
- 同一次命令的 Telegram retry 可复用本次已生成、验证的产物；
- 当前源文件结束后在 `finally` 清理整个目录；
- pending state 不改变这些规则，也不保存视频。

一次处理期间的目录结构为：

```text
.tg-comment-uploader-work/
  input/<source.name>       # 已验证的 0600 私有快照
  output/<prepared files>   # split/compress 产物
```

因此源文件所在文件系统必须额外容纳一份完整 source 快照和正在生成的产物；本工具不以硬链接、可变原路径或跨命令缓存换取空间。快照只要求进程内一致性，不作为崩溃恢复数据持久化。

固定目录是单用户、单 checkout 的已接受约束：不增加随机目录或所有权 marker。位于合法源目录下、精确同名的 `.tg-comment-uploader-work` 一律视为本工具专用临时目录，并可能在处理前被递归清理；用户不得在其中保存其他数据。另一个 checkout 的项目锁不会协调同一源目录，因此使用者不能从多个 checkout 同时处理同一目录。

清理覆盖成功、快照复制/校验失败、处理失败、发送失败和 Ctrl-C。清理位置在准备开始时由 canonical source parent 冻结；快照完成后即使原 source 被删除、改名或替换，也不得因此拒绝清理 workspace。SIGKILL/断电遗留的目录在下一次取得锁后、处理该目录前先清理；只有未 confirmed units 的恢复才重新生成快照和产物。

安全边界：

- 只删除精确命名的 `.tg-comment-uploader-work`；
- 删除前确认父目录是当前源文件目录；
- workspace 不能是符号链接，异常类型时停止且不递归跟随；
- 输入快照必须位于 `input/`，FFmpeg 输出必须位于 `output/`；
- 原视频不能位于 workspace，也不能成为输出目标；
- workspace、子目录、快照和产物只授予当前用户所需权限。

如果 Telegram 已 confirmed 但最终 workspace 清理失败，输出警告并保持发送成功语义。不能把它改报为发送失败，否则用户可能显式 discard/re跑而造成新消息。

处理前的旧 workspace 清理失败则是相反边界：立即停止，并在清理成功前不发任何新的 Telegram 请求。不能带着未知残留继续准备或上传。

## 项目级全局实例锁

`upload` 使用项目级、非阻塞独占锁；直接执行 Python CLI 也不能绕过。依赖 `filelock>=3.29.7,<4`。

锁文件：

```text
<project-root>/.local/tg-comment-uploader/upload.lock
```

project root 是包含 `justfile` 的源码根。锁路径不依赖视频目录、profile、UID、`XDG_RUNTIME_DIR` 或 `/tmp`。

保持锁对象强引用并使用非阻塞、无 TTL 配置：

```python
lock = FileLock(lock_path, blocking=False, lifetime=None)

with lock:
    run_upload_locked()
```

获取时捕获 `filelock.Timeout` 并立即转换为“另一个 upload 正在运行”的应用错误。不直接使用平台专用 `fcntl.flock`；操作系统级锁在进程退出或崩溃后释放。

冻结生命周期：

1. 创建 lock parent，解析绝对 lock path。
2. upload 开始时非阻塞获取；失败立即退出，不清理别的命令 workspace。
3. 从旧 workspace 预清理之前开始持锁。
4. 锁覆盖预检、state 读写、FFmpeg、Telegram retry、workspace 清理、sender close 和 state 收尾。
5. 最后释放锁；进程崩溃由操作系统释放。

`pending-status` 和 `pending-discard` 使用同一把锁，不能与 upload 并发读写 state。

不设置 lifetime/TTL，不手工删除 lock file，不通过“文件存在”判断锁是否占用，也不增加 PID 文件。

## 并发模型

项目不提供任务级并发：

- 同一时间一个源文件；
- 同一时间一个 ffmpeg/ffprobe 处理流程；
- 同一时间上传一个媒体项；
- 同一时间执行一个 final `SendMediaRequest` 或 `SendMultiMediaRequest`；
- 同一时间一个 upload 命令。

当前媒体项的单个文件固定使用 512 KiB MTProto chunks，并在同一连接上维持最多 8 个在途 `SaveFilePart`/`SaveBigFilePart` 请求。这是 transport 内部的有界流水线，不代表同时上传多个文件：媒体组下一条目、下一个 source、`UploadMediaRequest` 和所有 final send 都要等待当前文件流水线结束。

FFmpeg 内部工作线程、Telethon 内部加密/network worker 和上述 SavePart 流水线都属于单任务内部实现，不改变应用层条目串行契约。

## Pending、重试和部分成功

pending 文件固定为：

```text
<project-root>/.local/tg-comment-uploader/mtproto/pending-upload-v1.json
```

它记录 command intent、source/output hash、group 边界、每个 unit 的 random IDs、`planned/sending/confirmed` 和 message IDs。POSIX 权限为 `0600`，使用 fsync + atomic replace；损坏、矛盾或未知 schema 在联网前失败。不匹配的新 intent 仅可原子替换没有完成标记且所有 unit 都是 `planned` 的可信 state。

重试规则：

- FFmpeg split/compress 不消耗网络 `--retries`；算法内部修正保持既有上限。
- 本次运行产物验证通过后，网络 retry 复用它们。
- Telethon 内部自动重连和请求重试固定关闭；重连、退避和次数预算统一由应用层 retry 管理。
- SavePart 上传和 `UploadMediaRequest` 失败时尚无可见消息，可安全重做。SavePart 阶段的 `CancelledError` 只有在 Telethon client 明确报告已经断开连接时才转换为可安全重试的 transport 失败，unit 保持 `planned`。项目不持久化已确认 part 或 offset；当前未完成文件的下一次应用层 retry/进程重跑从字节 0 开始。
- final send 前先持久化 IDs 并标记 `sending`。
- 越过 final request 边界后，client 明确断开导致的 `CancelledError` 属于 outcome uncertain；与 final response loss、timeout、断线或 Updates 不完整一样保留 `sending`，并在预算内复用原 stable random IDs。
- client 仍连接或连接状态无法确认时，`CancelledError` 不转换为网络失败；用户 SIGINT/SIGTERM 也始终保持中断语义，由中断收尾路径 cancel、drain 后退出。
- FloodWait/SlowMode 使用 Telegram 指定的等待值，仍受既有 24 小时上限约束；否则使用 1、2、4、8、16、30 秒封顶退避。
- 确定性文件、配置、权限、peer/reply 或媒体错误立即停止。
- 一个 group confirmed、后组失败时，立即停止后续 group/source，但保留前组 confirmed。
- 匹配恢复跳过 confirmed groups，只恢复未确认 units。
- source-completed 标记已落盘时不再运行 FFmpeg；不能仅根据当前登记 units 恰好全 confirmed 来猜测 source 已规划完整。split source 只有部分 groups confirmed 时仍重新生成并验证全部 parts，再只发送未确认 groups。
- 最后一个 source-completed 与 `completion_ready=true` 同次原子落盘，随后才删除 pending；下次相同命令是新的发布并生成新 IDs。

稳定 IDs 已落盘时，结果不确定提示“恢复同一 pending operation，并复用原 random ID/媒体组 ID 向量”，不使用旧的“重试可能主动重复媒体组”警告。command operation ID 只用于标识和 discard state，不承担 Telegram 去重。

恢复同一 operation 要求 config/bot/profile/原始 `chat_id`/reply/policy、输入顺序、caption、source hash、算法版本、输出 hash 和 group 边界全部匹配。本地可判断的不匹配在 Telegram 连接前处理；只有严格有效且全 `planned` 的旧 state 可跨 config/bot owner 原子替换。username/link 解析后的 peer 在连接后、文件上传和 final send 前与 state 比较。

当前媒体算法版本为 v2；v1 表示旧的 workspace 根目录产物布局，v2 表示 verified snapshot 与 `input/`、`output/` 隔离布局。算法版本升级不改变 pending JSON schema：旧 v1 全 `planned` state 自动替换，v1 `sending` 或部分 confirmed state 继续阻止并等待人工处理。

终态残留例外：同一 owner 下，全部 source-completed 标记齐全且 units 全 confirmed 的 state 只说明最终删除失败。同一 intent 只清理 state、不重发旧 unit；不同 intent 原子替换该终态记录后开始新 operation。部分 confirmed、存在 sending 或归属另一 owner 的非 planned state 仍阻止自动替换。

用户可通过 `pending-status` 严格读取最小状态；它对 owner 不匹配的合法 state 也只读成功，并显示 `owner: current|foreign`，但不输出 owner 指纹、bot ID、文件或凭据。状态阶段严格区分 `planned`、`sending`、`partially-confirmed` 和 `confirmed`。全 `planned` 且没有任何完成标记的 state 在新 intent 到来时可跨 owner 自动淘汰；同 owner 的 fully-confirmed 终态在相同 intent 下只清理并退出，在不同 intent 下由新 operation 原子替换。`sending`、部分 confirmed 或 foreign 非 `planned` state 不会自动 discard 或按时间过期。

`pending-discard` 始终要求完整 operation ID；默认还要求 current owner。foreign state 只有在人工检查 Telegram 后显式增加 `--force-foreign-owner` 才能删除，force 不绕过严格 schema/状态校验、operation ID 检查或项目锁。显式 discard 后重跑生成新 logical send；若旧 sending 请求其实成功，可能重复。

## 进度和日志

每个超限源文件至少显示：

```text
copying verified source snapshot
verifying stable source snapshot
probing
planning split / planning compression
split attempt a/n / compress attempt a/n pass p/2
validating outputs
uploading media group i/n / uploading compressed video
media uploaded; waiting for Telegram message confirmation
cleaning workspace
```

FFmpeg 继续用 `-progress pipe:1`，不解析动态 stderr。子进程以参数数组启动，使用 `-nostdin`。

TTY 使用固定宽度单行进度条，显示媒体处理百分比、elapsed 和 ETA；每次重规划、重编码和每遍编码分别计时。非 TTY 无回车控制字符，只在阶段开始、每 10 秒和完成时输出完整行。

MTProto sender 只在一个 SavePart 请求成功确认后累计该 chunk 的字节，并发布 `(sent_bytes, total_bytes)`。最多 8 个在途请求可乱序完成，但对外进度必须单调不减且始终位于 `[0,total_bytes]`：

- 单视频显示实际上传字节；
- group 逐项显示 `item i/n`，可按字节聚合；
- 不按文件数或 RPC 等待时间伪造百分比；
- 文件传输到 100% 后进入 Telegram message confirmation spinner；
- 非 TTY 立即输出等待状态并每 30 秒 heartbeat。

renderer 和 wait indicator 在成功、错误、中断和 sender close 路径都必须正确结束，不留下控制字符或线程。

## 中断处理

收到 Ctrl-C 或正常终止信号时：

1. 终止并回收当前 FFmpeg 子进程。
2. 先 cancel 并 drain 当前 Telethon 发送 Task，确认它不能在事件循环重入后继续越过 final-send 边界，再 disconnect client。
3. final send 已开始时保留 `sending` state 和原 IDs；final send 前中断保持可安全重传状态。
4. 执行 workspace 清理。
5. 完成 state/sender 收尾，最后释放项目锁并返回 130。

SIGKILL/断电无法执行 `finally`，依靠原子 pending state、session 和下次运行的 workspace 前置清理恢复。

## 依赖

- Nix 开发环境保留 `ffmpeg`，同时提供 ffmpeg/ffprobe。
- 找不到这两个程序时立即报错，并提示 `just dev-shell`。
- Python 运行时固定使用 `telethon>=1.44,<2`、`cryptg>=0.6,<1`、`hachoir>=3.3,<4`、`pydantic>=2.13,<3` 和 `filelock>=3.29.7,<4`，实际版本由 `uv.lock` 锁定。
- `cryptg` 是大量上传所需的固定运行时依赖，为 Telethon 提供高性能 AES 实现；不增加配置开关。
- Ruff 和 ty 的既有精确开发版本保持锁定；`just check` 只检查，不修改源码。

## 非目标

- `--auto-split`、`--auto-compress` 别名。
- 并发处理多个 source、媒体文件、媒体组条目或 final Telegram RPC；只保留单文件内部最多 8 个在途 SavePart 的冻结流水线。
- 多个 upload 实例。
- 跨命令媒体缓存或持久化 split/compress 视频。
- 文件分片级断点续传；pending 只恢复 logical send 和 stable IDs，当前未完成文件的 retry/restart 固定从字节 0 开始。
- 随机 workspace、所有权 marker 或跨 checkout 锁。
- split 自动降级为 compress。
- 用户自定义上传阈值。
- 用户自定义 MTProto chunk size 或在途窗口；固定为 512 KiB 和最多 8。
- userbot、手机号/短信码登录、Telethon 2.x alpha。

## 验收与测试范围

至少覆盖：

- policy 默认值、三个合法值、非法值和短选项；
- 未超限文件三种 policy 都直接发送；
- `error` 超限时不启动 FFmpeg、不创建 state、不连接 Telegram；
- ffmpeg/ffprobe 缺失提示；
- 全部大小位置引用统一阈值；
- split 最少且均衡、关键帧规则和每块硬校验；
- compress 超限后从原文件按既有算法重做；
- 任一产物验证失败时零发送该 source；
- split/compress 在 FFmpeg 前创建并验证私有输入快照；路径替换、同尺寸替换及“修改后恢复”不能使未经初始 identity 验证的字节进入产物，失败时不登记产物、不创建 sender；
- 2、10、11、20、21 项分组及每组 caption/reply/streaming；
- caption 来自源路径且按纯文本发送；
- 路径含空格和特殊字符时 FFmpeg 与 sender 参数正确；
- TTY/非 TTY 的 FFmpeg、MTProto byte progress 和 confirmation wait；
- 非末尾 SavePart 精确为 512 KiB、末块按实际长度；同一连接最大在途数为 8；
- 多个 SavePart 乱序确认时进度仍按已确认字节单调推进并最终等于 total；
- 当前文件流水线未结束时，媒体组下一条目、下一个 source 和 final RPC 都不会开始；
- SavePart 失败、应用层 retry 或进程重跑时不恢复 byte offset，当前未完成文件从字节 0 重传；
- retry 复用本次产物和同一 random IDs；
- state 写失败时 final send 调用次数为零；
- 单视频和媒体组 final TL request 本地序列化失败时不标记 `sending`、不调用 final RPC，也不回显 caption；
- final response loss 和进程重启不生成新 IDs；
- group 1 confirmed/group 2 failure 的恢复不重发 group 1；
- 本地可判断的 state mismatch/corruption 在 connect 前停止，resolved peer mismatch 在上传前停止；
- `pending-status` 可只读显示 foreign owner，foreign discard 必须同时提供完整 operation ID 和 `--force-foreign-owner`；
- 正常完成后再次上传同一输入生成新 IDs；
- 锁覆盖 workspace、state、sender 和全部 retry；
- 成功、FFmpeg failure、Telegram failure、Ctrl-C 都清理 workspace；
- 清理或 disconnect 失败不把已 confirmed 消息改报为发送失败；
- same-fd 上传抵抗路径替换；sender 返回后不再读取临时文件。
- coordinator hash 后用同尺寸不同内容替换路径时，sender 在 final RPC 前因 expected digest 不匹配而停止。

## 参考

- [Telegram：Bot 使用 MTProto API](https://core.telegram.org/api/bots)
- [Telegram：`random_id` 与 `UpdateMessageID`](https://core.telegram.org/api/updates#updatemessageid-updates)
- [Telegram：`messages.sendMedia`](https://core.telegram.org/method/messages.sendMedia)
- [Telegram：`messages.sendMultiMedia`](https://core.telegram.org/method/messages.sendMultiMedia)
- [Telegram：`messages.uploadMedia`](https://core.telegram.org/method/messages.uploadMedia)
- [Telegram：`upload.saveFilePart`](https://core.telegram.org/method/upload.saveFilePart)
- [Telegram：`upload.saveBigFilePart`](https://core.telegram.org/method/upload.saveBigFilePart)
- [FFmpeg segment muxer](https://ffmpeg.org/ffmpeg-formats.html#segment)
- [FFmpeg streamcopy](https://ffmpeg.org/ffmpeg.html#Streamcopy)
- [ffprobe](https://ffmpeg.org/ffprobe.html)
- [filelock 文档](https://py-filelock.readthedocs.io/en/latest/index.html)
- [filelock 非阻塞锁](https://py-filelock.readthedocs.io/en/latest/how-to.html#use-non-blocking-locks)
