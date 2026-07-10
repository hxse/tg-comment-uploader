# 超限视频处理设计

> 状态：已实现。
>
> 本文只描述超过 Telegram 单文件安全阈值时的拆分、压缩、上传、临时文件和并发控制。项目的基础配置与普通上传行为见 `doc/design.md`。

## 目标

当输入视频超过 Telegram local Bot API 的单文件限制时，允许用户明确选择以下一种处理方式：

- `error`：直接报错，不运行 FFmpeg，也不发送任何请求。
- `split`：使用 FFmpeg 无损拆分为尽量少、大小尽量均衡的多个视频，再按顺序上传。
- `compress`：使用 FFmpeg 有损压缩为一个尽量接近、但不超过安全阈值的视频，再上传。

整个过程继续遵守项目已有约束：

- 输入文件按命令行顺序处理。
- 不并发上传。
- 不并发处理多个视频。
- 不支持同时运行多个 `upload` 命令实例。
- 不做跨命令缓存，也不支持断点续传。
- 原视频永不修改、覆盖或删除。

## CLI

只增加一个语义选项，提供短、长两种写法，不提供布尔别名：

```text
-o, --oversize-policy {error,split,compress}
```

默认值为 `error`，保持当前行为。

示例：

```sh
just upload -o split '/absolute/path/video.mp4'
just upload --oversize-policy compress '/absolute/path/video.mp4'
```

该选项仅影响超限文件。未超限文件始终直接上传，不做无意义的拆分或重编码。只有实际遇到需要处理的超限文件时，才要求 `ffmpeg` 和 `ffprobe` 可用。

`-o` 只是 `--oversize-policy` 的短写。本设计不增加 `--auto-split`、`--auto-compress` 等布尔别名，也不在 profile 中增加重复配置。

## 统一文件大小阈值

Telegram local Bot API 官方说明的上传上限为 2000 MB。项目采用一个全局、保守的硬阈值：

```python
SAFE_UPLOAD_LIMIT_BYTES = 2_000_000_000
```

以下所有位置必须引用同一个常量，禁止散落 `2 GB`、`2000 MB` 或 `2000 MiB` 字面值：

- 原始文件是否超限的判断。
- 拆分数量和切点规划。
- 压缩目标大小计算。
- FFmpeg 产物的最终校验。
- 发送前的最后一道大小校验。
- Bot API 文件大小错误的诊断信息。

硬校验条件为：文件必须非空，并且大小不超过 `SAFE_UPLOAD_LIMIT_BYTES`。

拆分和压缩不能把硬阈值本身当作目标值。拆分重做成本较低且需要优先减少分块数，因此保留 2% 余量；压缩重做非常耗时，因此预留更保守的 5% 余量：

```python
split_target_bytes = floor(SAFE_UPLOAD_LIMIT_BYTES * 0.98)
compress_target_bytes = floor(SAFE_UPLOAD_LIMIT_BYTES * 0.95)
```

两个 target 只用于各自策略的规划；产物能否上传仍以 `SAFE_UPLOAD_LIMIT_BYTES` 的实际字节数校验为准。

## 总体处理流程

命令启动后先获取全局上传锁，然后一次性完成所有输入路径的基础校验。基础校验包括：

- 路径必须是绝对路径。
- 文件必须存在且是普通文件。
- 文件必须非空。
- 读取并保存每个文件的实际字节数。
- 保留命令行传入顺序。

当 policy 为默认的 `error` 时，只要预检发现任意文件超限，就必须在发送第一个请求之前终止。这保持当前“一次性预检全部文件，任一超限则一个都不上传”的行为。

之后逐个处理源文件：

```text
获取全局锁
  校验全部输入路径
  policy=error 且任一文件超限：在任何上传前报错
  按命令行顺序遍历源文件
    未超限：直接上传
    已超限且 policy=split/compress：
      清理并创建临时工作目录
      运行 ffprobe 和 ffmpeg
      校验全部产物
      上传该源文件的全部产物
      清理临时工作目录
  释放全局锁
```

每个源文件必须完整走完“处理、上传、清理”后，才开始下一个源文件。这样始终只有一个活动的 FFmpeg 任务和一组活动产物，并控制磁盘占用。

基础路径和大小校验在任何上传前完成，但 `split`、`compress` 的 FFprobe/FFmpeg 处理按文件顺序延迟执行，因此整个批次仍不具备事务性。若前几个文件已经上传，后续文件处理或上传失败，前面已经发送的消息不会回滚。

## `split`：无损拆分

### 优化目标

拆分规划按以下优先级执行：

1. 所有分块均能通过统一大小阈值校验。
2. 分块数量尽量少。
3. 在分块数量相同的前提下，各分块大小尽量均衡。
4. 保持原始时间顺序，并尽量从关键帧开始播放。

“无损”表示音视频流使用 FFmpeg stream copy，不重新编码。由于只能在适合的关键帧附近切割，分块大小不一定能做到完全相等。

### 规划算法

1. 使用 `ffprobe` 获取时长、流信息、包大小、时间戳和关键帧位置。
2. 以 `ceil(source_size / split_target_bytes)` 作为最少分块数的初始下界。
3. 按累计媒体字节数寻找接近等分目标的关键帧，而不是只按平均时长切割。
4. 在当前分块数下选择一组合法切点，使最大分块尽可能小、各分块尽可能均衡。
5. 使用 FFmpeg segment muxer 和 stream copy 一次生成所有分块。
6. 按编号和时间顺序检查全部实际输出。

产物命名只用于当前工作目录，并在原文件的最后一个扩展名前插入分块编号。例如源文件为 `archive.final.mp4`：

```text
archive.final part-0001.mp4
archive.final part-0002.mp4
```

如果封装开销导致实际分块超限，必须丢弃本轮全部产物、收紧规划目标并重新规划；必要时增加一个分块。任何分块超限时，都不能开始上传该源文件。

如果源视频关键帧分布导致无法安全地进行无损拆分，应明确报错并建议用户改用 `--oversize-policy compress`。`split` 不得静默降级为有损压缩。

### 分块产物校验

FFmpeg 返回成功不代表产物可上传。上传前必须确认：

- 至少生成两个分块。
- 所有分块存在、是普通文件且非空。
- 所有分块实际大小不超过统一安全阈值。
- 分块编号连续，时间顺序正确。
- 每个分块能被 `ffprobe` 正常解析，并包含可用视频流。

## `compress`：有损压缩

压缩的目标是生成一个接近 `compress_target_bytes`、且最终不超过统一安全阈值的 MP4 文件。输出使用兼容性较好的 H.264、AAC、`yuv420p` 和 faststart。

建议流程：

1. 使用 `ffprobe` 获取准确时长、音视频流和原始码率信息。
2. 从 `compress_target_bytes` 和时长反算允许的总码率。
3. 为音频和 MP4 封装开销预留预算，其余分配给视频。
4. 使用 `libx264` 的 `veryfast` preset 进行两遍编码，在保留体积可预测性的同时优先提高编码速度。
5. 编码完成后，以实际文件字节数和 `ffprobe` 结果进行硬校验。

输出文件固定放在当前工作目录，例如：

```text
compressed.mp4
```

如果实际结果仍然超限，应删除结果，从原视频重新编码，并根据“目标大小 / 实际大小”的比例降低视频码率。重编码次数必须有明确上限；多次尝试仍无法得到合法产物时，报错并停止。

不能使用 FFmpeg 的 `-fs` 作为大小保证，因为它可能截断输出，而且最终结果仍可能略微超过指定大小。安全性只由完整编码后的实际文件大小校验保证。

压缩 caption 必须根据原始文件路径生成，不能使用 `compressed.mp4` 这个临时文件名。

## Telegram 上传方式

### 普通文件和压缩文件

未超限的原文件以及压缩后的单个文件继续使用 `sendVideo`：

- `default` profile 携带 `reply_parameters.message_id`。
- `direct` profile 省略 `reply_parameters`，直接发送到 `chat_id`。

### 拆分文件

拆分得到多个视频时使用 `sendMediaGroup`：

- 2 到 10 个分块：发送为一个媒体组。
- 超过 10 个分块：保持时间顺序，在保证每组 2 到 10 个的前提下使用最少媒体组，并尽量均衡各组数量，避免产生只有一个视频的非法尾组。
- `default` profile 的每个媒体组都回复同一个 `reply_message_id`。
- `direct` profile 不携带回复参数，直接发布到目标频道或群组。
- caption 基于原始文件生成，只放在每个媒体组的第一个视频上。
- 每个 `InputMediaVideo` 继承 profile 的 `supports_streaming` 设置。

Telegram 客户端会把同一 `media_group_id` 的消息显示为一个相册。API 层面每个视频仍然是独立的 `Message`，并不是真正把多个视频对象放进一条 `Message`。一个媒体组最多包含 10 个媒体项。

项目使用 local Bot API server。媒体组应通过 local Bot API 的本地绝对路径机制引用临时分块，避免把多个接近 2 GB 的文件组合成一个巨大的 multipart HTTP 请求。local Bot API server 必须能够读取临时目录中的文件。

## 临时工作目录

不使用系统临时目录保存视频产物。临时工作目录固定放在当前源视频的同级目录：

```text
<source.parent>/.tg-comment-uploader-work/
```

如果一次命令的输入文件来自不同目录，处理到哪个源文件，就使用它同级的工作目录；由于处理严格串行，任何时刻仍然只有一个活动工作目录。

工作目录是一次运行期间的 workspace，不是缓存：

- 获取全局锁后、开始拆分或压缩前，先清理同名旧目录。
- 每次都从原视频重新拆分或压缩。
- 不按文件名、mtime、大小、哈希或 manifest 复用上一次命令的产物。
- 同一次命令内，上传发生可重试错误时可以复用本次刚生成且已经验证的产物，不重复运行 FFmpeg。
- 当前源文件处理结束后，在 `finally` 中清理整个工作目录。

清理覆盖成功、转换失败、上传失败和 `Ctrl+C`。`SIGKILL`、机器断电等情况无法执行 `finally`，遗留目录会在下一次处理同目录视频时、取得全局锁后首先清理。

实现清理时必须遵守以下安全边界：

- 只允许删除精确命名的 `.tg-comment-uploader-work` 目录。
- 删除前验证其父目录确实是当前源文件目录。
- 工作目录不能是符号链接；发现符号链接或异常文件类型时停止，不递归跟随。
- FFmpeg 输出路径必须位于工作目录中。
- 原始视频路径不得位于工作目录中，也不得作为输出目标。
- 工作目录和产物只授予当前用户所需权限。

如果上传已经成功但最终清理失败，应醒目输出清理警告，并保留“上传成功”的退出语义，不能把它描述成“上传失败”，否则用户重跑命令可能产生重复消息。下一次处理同目录视频时仍会先尝试清理残留；如果前置清理失败，则在发送任何新请求前终止。

## 项目级全局实例锁

固定工作目录在并发命令下会发生互相清理和误上传，因此 `upload` 子命令必须使用项目级、非阻塞独占锁。锁应在 Python CLI 内实现，而不是只写在 `justfile` 中，以免直接执行 Python 命令时绕过保护。

不直接使用只适用于 Unix 的 `fcntl.flock`。项目增加 Python 依赖 `filelock>=3.29.7,<4`，通过 `filelock.FileLock` 在不同平台选择合适的操作系统锁：Linux/macOS 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`。

锁文件固定放在项目本地运行目录：

```text
<project-root>/.local/tg-comment-uploader/upload.lock
```

`project-root` 是包含 `justfile` 的项目根目录。锁路径不依赖视频目录、配置 profile、UID、`XDG_RUNTIME_DIR` 或 `/tmp`，同一项目中的所有 `upload` 调用必须解析到同一个绝对锁路径。

使用非阻塞模式，并保持锁对象的强引用：

```python
from filelock import FileLock, Timeout

lock = FileLock(lock_path, blocking=False, lifetime=None)

try:
    with lock:
        run_upload_locked()
except Timeout as exc:
    raise AppError(
        "another tg-comment-uploader upload command is already running"
    ) from exc
```

锁的生命周期：

1. 创建锁文件父目录，并把锁路径解析为绝对路径。
2. `upload` 命令开始处理前尝试获取非阻塞锁。
3. 获取失败时立即报错退出，不等待，也不尝试清理临时工作目录。
4. 从清理旧工作目录之前开始持锁。
5. 在最后一个文件上传、最终清理完成后离开 context manager 并释放锁。
6. Linux、macOS 和 Windows 上的操作系统级锁会在进程退出或崩溃后释放。

不设置锁的 lifetime/TTL；一次拆分、压缩或上传可能持续数小时，锁不能在命令仍运行时自动过期。

锁文件是否存在不代表锁是否被占用，正确性只依赖 `FileLock` 的获取结果。代码不得手动删除锁文件，也不得依赖库在特定平台是否保留或删除锁文件。

不额外维护 PID 文件，也不承诺显示持锁进程 PID。为诊断信息增加第二份状态文件会引入新的过期状态和清理竞态；锁冲突时只需明确提示另一个上传命令正在运行。

该锁只限制当前项目的 `upload` 命令，不影响 local Bot API `server` 子命令持续运行。

## 并发模型

项目不提供任务级并发：

- 同一时间只处理一个源文件。
- 同一时间只运行一个 FFmpeg/ffprobe 处理流程。
- 同一时间只发送一个 `sendVideo` 或 `sendMediaGroup` 请求。
- 同一时间只允许一个 `upload` 命令实例。

FFmpeg 编码器内部使用多个工作线程属于单个编码任务的内部实现，不视为多个视频并发处理，也不强制设置为单线程。

## 重试、失败和部分成功

- FFmpeg 拆分或压缩不套用网络上传的 `--retries` 次数。
- 拆分规划修正和压缩码率修正属于有上限的内部处理尝试。
- 产物一旦生成并全部验证通过，同一命令中的网络重试复用这些产物。
- `sendMediaGroup` 的重试单位是整个媒体组，而不是其中某一个视频。
- 请求体已经发送完成但响应丢失时，结果未知；继续保持“不自动重试”，避免重复消息或重复媒体组。
- 一个媒体组成功、后续媒体组失败时，立即停止该源文件剩余媒体组和后续源文件。
- 当前不记录上传 checkpoint。用户重新执行整条命令可能重复发送已经成功的媒体组，错误信息必须明确提示部分成功状态。
- 无论最终成功还是失败，都尝试清理当前工作目录。

## 进度和日志

每个超限源文件至少显示以下阶段：

```text
probing
planning split / planning compression
split attempt a/n / compress attempt a/n pass p/2
validating outputs
uploading media group i/n / uploading compressed video
waiting for Telegram response (elapsed ...)
cleaning workspace
```

FFmpeg 使用 `-progress pipe:1` 提供机器可解析的进度，不解析面向终端的动态 stderr 文本。子进程通过参数数组启动，不拼接 shell 命令，并使用 `-nostdin` 防止意外读取终端输入。

交互式终端使用固定宽度的单行进度条，显示实际媒体时间百分比、已用时间和 ETA。拆分的每次重规划、压缩的每次重编码与每一遍编码分别计时。非 TTY 输出不包含回车控制字符，只在阶段开始、每 10 秒和完成时输出一行。

使用 local Bot API 本地路径发送媒体组时，客户端 HTTP 请求很小，无法显示有意义的字节进度。请求体完整发送后，TTY 每秒用单行 spinner 显示等待 Telegram 响应的 elapsed；非 TTY 立即输出一行并每 30 秒输出心跳。该状态只证明客户端仍在等待，不声称掌握 Telegram 服务端百分比或 ETA。

## 中断处理

收到 `Ctrl+C` 或正常终止信号时：

1. 先终止当前 FFmpeg 子进程。
2. 等待并回收子进程，避免它继续向工作目录写数据。
3. 执行工作目录清理。
4. 释放全局锁并以非零状态退出。

无法捕获的 `SIGKILL` 和断电只能依靠下次运行前清理残留。

## 依赖

- 在 `flake.nix` 的开发环境中加入 `ffmpeg`。该包同时提供 `ffmpeg` 和 `ffprobe`。
- 找不到 `ffmpeg` 或 `ffprobe` 时立即报错，并提示运行 `just dev-shell` 后重试。
- 在 `pyproject.toml` 中加入 `filelock>=3.29.7,<4`，并由 uv 锁定实际安装版本。
- 除 `filelock` 外，不为本功能增加其他 Python 第三方依赖。

## 非目标

本阶段明确不实现：

- `--auto-split`、`--auto-compress` 等别名。
- 多个源视频并发拆分或压缩。
- 多个 Telegram 请求并发上传。
- 多个 `upload` 命令实例同时运行。
- 跨命令缓存、缓存命中判断或产物持久化。
- 上传 checkpoint 和断点续传。
- `split` 自动回退为有损压缩。
- 用户自定义上传阈值。

## 验收与测试范围

至少覆盖以下行为：

- `-o`/`--oversize-policy` 默认值及三个合法值。
- 非法 policy 立即报错，且不存在任何布尔别名。
- 三种 policy 对未超限文件都直接上传原文件。
- `error` 遇到超限文件时不启动 FFmpeg、不发送请求。
- 找不到 `ffmpeg` 或 `ffprobe` 时提示运行 `just dev-shell`。
- 所有大小判断都引用统一安全阈值。
- 拆分优先使用最少分块，并验证每个分块不超限。
- 压缩产物超限时降低目标并从原文件重做。
- 任一产物验证失败时，该源文件一个分块都不上传。
- 2 到 10 个分块使用一个媒体组，超过 10 个时正确分组。
- reply profile 和 direct profile 的媒体组参数正确。
- caption 始终来自原始路径，而不是临时文件名。
- 路径包含空格和特殊字符时 FFmpeg 与上传参数仍然正确。
- 拆分和压缩在 TTY 中实时单行显示百分比、elapsed 和 ETA，阶段切换及异常后不与普通日志黏行。
- 非 TTY 进度无回车控制字符，并按低频完整行输出。
- 上传重试复用当前运行的产物，但下一条命令必定重新生成。
- 两个 `upload` 实例竞争时，第二个立即失败且不会清理第一个的工作目录。
- `FileLock` 使用非阻塞模式，并覆盖预清理、FFmpeg、上传重试和最终清理的完整生命周期。
- 锁文件已经存在但未被占用时可以正常获取锁；代码不依赖锁文件存在性判断并发状态。
- 长时间上传不会因为 TTL 到期而释放锁。
- 正常成功、FFmpeg 失败、上传失败和 `Ctrl+C` 都执行清理。
- 崩溃遗留目录在下一次取得锁后、处理开始前被清理。
- 清理失败不会把已经成功的上传误报为上传失败。
- 请求结果未知时不重试媒体组。
- 后续文件失败时明确保留并报告前面已经发生的成功上传。

## 官方参考

- [Telegram Bot API：Local Bot API](https://core.telegram.org/bots/features#local-bot-api)
- [Telegram Bot API：sendMediaGroup](https://core.telegram.org/bots/api#sendmediagroup)
- [Telegram Bot API：Message.media_group_id](https://core.telegram.org/bots/api#message)
- [Telegram local Bot API server](https://github.com/tdlib/telegram-bot-api#usage)
- [Telegram local Bot API 大请求体限制说明](https://github.com/tdlib/telegram-bot-api/issues/671)
- [FFmpeg segment muxer](https://ffmpeg.org/ffmpeg-formats.html#segment)
- [FFmpeg streamcopy](https://ffmpeg.org/ffmpeg.html#Streamcopy)
- [ffprobe](https://ffmpeg.org/ffprobe.html)
- [filelock 文档](https://py-filelock.readthedocs.io/en/latest/index.html)
- [filelock 非阻塞锁](https://py-filelock.readthedocs.io/en/latest/how-to.html#use-non-blocking-locks)
- [filelock PyPI](https://pypi.org/project/filelock/)
