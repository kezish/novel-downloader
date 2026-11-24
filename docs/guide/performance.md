# 性能与容量

本页面向书库较大、章节较多或需要长时间批量下载的场景，说明哪些参数真正影响吞吐、内存和磁盘，以及怎样定位瓶颈。所有默认值、上限和行为都对应仓库当前实现；**推荐值只是起点，不是效果承诺**——站点容忍度、网络质量和磁盘类型不同，结果会明显不同。

相关页面：[设置项参考](./settings-reference.md) · [存储与备份](./storage.md) · [故障排查](./troubleshooting.md) · [CLI 用法](./cli-examples.md) · [本地 API 服务](./api.md) · [音乐下载](./music.md)

---

## 先量再调 {#measure-first}

调参前先建立基线，否则无法判断改动是否有效：

```bash
# 1. 环境、配置、磁盘、章节库、搜索缓存、站点插件、API 端口
novel-cli doctor
novel-cli doctor --json

# 2. 四个数据目录的占用和剩余空间
novel-cli storage --json

# 3. 固定同一本书、同一章节区间，只测抓取与落盘（跳过导出）
time novel-cli download <book_id> --site <site> \
  --start <first_chapter_id> --end <last_chapter_id> --no-export
```

建议：

* **一次只改一个参数**，用同一本书、同一时段、同一区间复测。
* `--no-export` 把「抓取 + 解析 + 写库」和「导出 TXT/EPUB/HTML」分开；两者瓶颈通常不同。
* 断点续爬是默认行为：`cache_chapter = true` 时已完成的章节会跳过，所以第一次运行和后续运行的时间不可直接比较。测速前确认区间内没有已缓存章节，或换一段未下载的区间。
* 书籍信息与目录各只需要少量请求，章节数越多，稳态抓取越占主导。

---

## 参数速查 {#parameters}

### 下载与并发 {#download-concurrency}

以下键都可以写在 `[general]`，也可以写到 `[sites.<site>]` 覆盖全局值（站点级优先）。

| 键 | 默认值 | 有效范围 | 作用 | 调大 | 调小 |
| --- | --- | --- | --- | --- | --- |
| `workers` | `4` | 1–4096 | **每本书**同时抓取章节的协程数 | 提高单本站点吞吐 | 降低站点压力与内存 |
| `request_interval` | `0.5` | ≥ 0 | **每本书**每个章节完成后等待的秒数（带抖动） | 更保守 | 更快，但更容易触发限流 |
| `max_connections` | `10` | 1–4096 | 单站点 fetcher 的连接池上限 | 高延迟链路可提高 | 站点或代理吃连接时降低 |
| `max_rps` | `1000.0` | ≥ 0，`0` 关闭 | 单站点令牌桶速率上限；页面、图片、字体请求共享 | 放宽 | 主动限速 |
| `timeout` | `10.0` | > 0 | 单次请求超时（秒） | 慢站点/大图 | 快速失败 |
| `retry_times` | `3` | ≥ 0 | 请求层重试次数（仅超时/连接错误与 408/425/429/5xx） | 不稳定链路 | 减少无效等待 |
| `backoff_factor` | `2.0` | ≥ 0 | 重试退避因子 | 避免连续撞限流 | 更快重试 |
| `download_retry_times` | `2` | ≥ 0 | 整书下载任务的额外重试次数 | 偶发断流 | 快速失败 |
| `storage_batch_size` | `1` | 1–65536 | 每批提交的章节数（一次 SQLite 事务） | 减少提交次数 | 降低崩溃时丢失量与内存峰值 |
| `cache_book_info` | `true` | bool | 复用书籍信息 | — | 强制重新抓取目录 |
| `cache_chapter` | `true` | bool | 跳过已写入的章节 | — | 强制重下 |

**并发不是简单相加。** 每本书有自己的 `workers` 个协程；桌面端/API 同一站点同一时间只跑一本书（同站 FIFO），不同站点可以并行。若同时有多个站点在下载，总协程数约为「各站点 `workers` 之和」，内存也按此增长。

单本书的稳态吞吐大致是：

```text
每章耗时 ≈ 请求耗时 + request_interval（每章完成后等待）
单本 RPS ≈ min(max_rps, workers ÷ 每章耗时)
```

这解释了为什么把 `workers` 从 4 提到 16、而 `request_interval` 仍是 0.5 时，吞吐最多接近 4 倍而不是 16 倍：每个协程每章后都要睡 0.5 秒。若 `max_rps` 设得很低，它才是硬上限，此时继续加 `workers` 只会增加排队协程和内存。

### 搜索 {#search-limits}

搜索使用独立于下载的连接池和超时，不受 `[sites.*]` 的 `workers` 影响。

| 入口 | 参数 | 默认值 | 上限 |
| --- | --- | --- | --- |
| CLI | `--site-limit` | `10` | 无（实现侧按站点处理） |
| CLI | `--limit` | 不限 | 无 |
| CLI | `--timeout` | `5.0` 秒/站点 | 无 |
| REST | `per_site_limit` | `5` | 100 |
| REST | `limit` | 不限 | 500 |
| REST | `sites` | 全部站点 | 100 个 |
| REST | `timeout` | `5.0` 秒/站点 | 120 |

聚合搜索的连接池固定为 **64 个总连接、每主机 8 个**，DNS 结果缓存 300 秒。因此把 `--site-limit` 调得很大不会让单个站点跑得更快，只会让每个站点多抓几行结果。

搜索结果缓存默认 **TTL 300 秒、最多 64 条 LRU**（内存）；只有设置 `NOVELDL_SEARCH_CACHE_DIR` 时才启用磁盘层（最多 256 个文件、单个快照上限 2 MiB）。缓存键包含关键词、站点集合、每站条数、超时与过滤条件；排序和分组在缓存之后应用，所以改变排序或分组不会重新访问站点。

### 音乐 {#music-limits}

| 项 | 默认值 | 说明 |
| --- | --- | --- |
| 下载并发 `workers` | `3` | 来自音乐设置（`settings.json`，桌面端设置页可改）；CLI/API 没有 `--workers` 开关 |
| 单曲上限 | 1 GiB | 超过即中止并删除不完整文件 |
| 整批上限 | 8 GiB | 所有并发 worker 共享的原子预算；失败/取消会归还未占用字节 |
| 封面上限 | 8 MiB | 单张封面 |
| 分块大小 | 64 KiB | 写入粒度 |
| 聚合搜索线程 | 32 | 每个平台一个线程，受平台数量限制 |
| API 同时活跃音乐任务 | 4 | 超过返回 `429`；最多保留 100 个任务记录 |

1 GiB / 8 GiB 是**大小上限**，不是内存占用：下载按 64 KiB 分块写盘，内存里不会缓存整首歌。整批预算耗尽时后续曲目失败，需要拆成更小的批次。

### 章节库（SQLite） {#storage-limits}

* 每个 `chapter.<stage>.sqlite` 使用 WAL 模式、`synchronous = NORMAL`、`busy_timeout = 5000` ms。
* 下载端只有一个写连接；`upsert_chapters` 在一个事务里提交整批。`storage_batch_size = 1` 表示逐章提交，值越大提交次数越少、单次事务越长。
* WAL 允许导出器在写入时读取同一本书，但**同一本书仍然只应有一个人写**。两个进程同时写同一数据库只会在 `busy_timeout` 后报 `database is locked`。
* `storage backup` 使用 SQLite 在线备份 API，能拿到已提交的 WAL 帧；不要在运行中直接复制 `.sqlite` 文件来代替备份。

### 本地 API 服务 {#api-limits}

| 项 | 默认值 | 说明 |
| --- | --- | --- |
| 每 IP 限流 | 120 请求 / 60 秒 | `/api/v1/health` 等探活路径豁免；`--no-rate-limit` 可关闭 |
| 请求体上限 | 1 MiB | 超过返回 `413`；声明 `Content-Length` 与实际流式累计都会检查 |
| 同时连接数 | 100 | uvicorn `limit_concurrency`；SSE 长连接也占一个 |
| 阻塞端点超时 | 搜索 120s / 浏览 60s / 音乐搜索 120s / 音乐下载 1800s | 超时返回 `504`（音乐任务记为失败） |
| 下载任务并发 | 每站点 1 个 | 同站点 FIFO，不同站点并行；桌面端与 API 使用同一套任务管理器实现，各自进程维护并持久化自己的队列 |

!!! note "限流计数在进程内"
    `novel-api` 入口拒绝多 worker；只有手工用 `uvicorn --workers N` 部署时，每个 worker 各有一份限流计数，实际上限会变成 `N × 120`。不要依赖那种部署方式来提高容量。

---

## 按书库规模推荐 {#library-size}

先按章节总数分档。下面的值是**起点**，站点敏感时以站点实际返回为准；改完用「先量再调」的方法复测。

| 设置 | 小书库（< 1,000 章） | 中书库（1,000–10,000 章） | 大书库（> 10,000 章） |
| --- | --- | --- | --- |
| `workers` | `4`（默认） | `6–8` | `8–12`（站点扛得住时） |
| `request_interval` | `0.5` | `0.5` | `0.5–1.0` |
| `max_connections` | `10` | `16` | `24` |
| `max_rps` | 默认即可 | 按站点设 `2–10` | 按站点设 `2–10`，不要留 1000 |
| `storage_batch_size` | `20–50` | `50–100` | `100–200` |
| `cache_chapter` / `cache_book_info` | `true` / `true` | `true` / `true` | `true` / `true` |
| 运行方式 | 单本顺序下载 | 单本，或每站一本并行 | 按站点分批，同站一次一本 |

### 小书库（少于 1,000 章） {#small-library}

默认值足够。唯一值得改的是 `storage_batch_size`：默认逐章提交，章节少时提交开销不明显，但改成 `20–50` 可以减少磁盘同步次数，同时崩溃时最多丢一批未提交章节。若站点本身很快，`workers = 4` 通常已经不是瓶颈。

### 中书库（1,000–10,000 章） {#medium-library}

* 把 `workers` 提到 `6–8`，同时给常见站点设置明确的 `max_rps`（例如 `2–10`）；默认 `1000` 等于不限制，只在站点明显宽容时保留。
* `max_connections` 提到 `16` 对高延迟链路有帮助；本地低延迟、站点响应快时收益有限。
* `storage_batch_size` 用 `50–100`，把逐章 fsync 的开销摊薄。
* 保持 `cache_chapter = true`，中断后续爬会跳过已完成章节。
* 搜索时用 `--site-limit` 控制每站抓取量；中书库常见做法是搜索阶段小页、下载阶段单独跑。

### 大书库（超过 10,000 章） {#large-library}

* **瓶颈几乎总在站点而不是本机。** 优先按站点设置 `max_rps` 与 `request_interval`，`workers` 不超过站点能稳定承受的并发；盲目加到 32/64 往往只是把限流错误堆在队列里。
* 分而治之：按站点拆成多次运行，同一站点一次只跑一本（桌面端/API 本身就是这个模型；CLI 不要开多个进程抓同一站点）。
* `storage_batch_size` 可用 `100–200`，但要接受两个代价：单次事务更长（WAL 增长、锁持有更久）和崩溃时丢失更多未提交章节。写入慢的机械盘或网络盘上不要开得太大。
* `max_connections` 不必随章节数线性增加；`24` 左右已经超过多数站点的容忍度。
* 关掉 `general.debug.save_html`。大书库长期开启会把每章原始 HTML 全量落盘，占用与解析耗时都会明显上升。
* 导出单独安排。10,000 章以上的 EPUB 构建会同时持有章节 XHTML 与图片，内存峰值高于下载阶段；必要时按卷导出（`general.output.split_mode = "volume"`）或先只下载不导出（`--no-export`）。

---

## 网络慢或站点限流 {#slow-network}

先确认是「网络慢」还是「站点拒绝」：

| 现象 | 更可能是 | 先做什么 |
| --- | --- | --- |
| 请求普遍耗时接近或超过 `timeout`，但很少报错 | 网络/代理慢 | 提高 `timeout`，检查代理，运行 `novel-cli doctor --online` |
| 出现 `429` / `503` / `Retry-After` | 站点限流 | 降 `max_rps`、增 `request_interval`，不要加 `workers` |
| 连接重置、TLS 错误、间歇超时 | 链路或站点风控 | 开 `retry_times`、`backoff_factor`，考虑站点专用 `backend` / `impersonate` |
| 只有搜索慢、下载正常 | 搜索扇出过大 | 减少站点、降 `--site-limit`、提高 `--timeout` |

站点级覆盖示例（只影响 `example` 站点）：

```toml
[general]
request_interval = 0.5
max_rps = 5.0
timeout = 15.0

[sites.example]
workers = 2
request_interval = 2.0
max_rps = 1.0
timeout = 30.0
retry_times = 5
backoff_factor = 3.0
```

要点：

* `max_rps` 是**单站点**令牌桶，页面、图片、字体请求共享。站点返回 429 时把它降到 1–2，比反复重试有效。
* `request_interval` 是每章完成后的等待，带随机抖动；慢站点上把它设为 1–2 秒，通常比降 `workers` 更直接。
* `timeout` 只影响单次请求；慢但能返回的站点把它提到 20–30 秒，避免每章都重试三次。
* 重试会遵守服务端 `Retry-After`；`retry_times` 与 `backoff_factor` 只对可重试错误生效，403/404 等确定性状态不会重试。
* 搜索扇出用 CLI `--site <site>` / `--site-limit 5` / `--timeout 10`，或 REST 的 `sites` / `per_site_limit` / `timeout`。站点多不等于结果好，慢站点会拖住整个聚合预算。
* `batch` 的 `--interval` 是**两次书籍搜索之间**的等待，和章节级的 `request_interval` 是两件事。
* 需要浏览器指纹的站点，参考 [书源](./book-sources.md) 配置 `backend` / `impersonate`；代理与证书问题见[故障排查](./troubleshooting.md#proxy-vpn)。

---

## 磁盘与备份 {#disk-backup}

### 先看占用 {#disk-usage}

```bash
novel-cli storage            # raw_data / cache / downloads / logs 的占用与剩余空间
novel-cli storage --json     # 机器可读，便于脚本监控
```

`raw_data` 是章节库（SQLite），`downloads` 是导出成品，`cache` 含 Cookie、字体、图片与搜索缓存，`logs` 是运行日志。四者增长原因不同，不要只看总量。

### 备份频率与保留 {#backup-cadence}

```bash
# 每次大批量下载后、清理前、升级前
novel-cli storage backup --out /path/to/backups --keep 7

# 只看 --keep 会删什么，不删除
novel-cli storage backup --out /path/to/backups --keep 7 --dry-run

# 备份后默认会执行完整性校验；确认不需要时才跳过
novel-cli storage backup --out /path/to/backups --no-verify
```

建议：

* **活跃书库**：每天一次，或在每次大批量下载结束后一次。章节库只会追加/更新，备份成本与库大小成正比。
* **低频书库**：每周或每月一次即可；`storage backup` 是完整快照，不是增量。
* **`--keep N`**：按「每本书 × 每个 stage」保留最新 N 份，`0` 表示不清理。规划空间时按 `N × 单库大小` 估算，而不是按增量估算。
* 备份只覆盖 `raw_data` 下的 `chapter.<stage>.sqlite`；**不包含** `downloads/`、`cache/`、`settings.toml` 与登录态。成品文件要单独备份，换机后需要重新登录。
* 恢复前先 `novel-cli storage verify`；`storage restore` 覆盖现有库需要 `-y`。完整字段与失败语义见[存储与备份](./storage.md#storage-backup)。

!!! warning "不要把在线库直接交给同步盘"
    SQLite 的 WAL 依赖同目录的 `-wal`/`-shm` 文件与文件锁。让 OneDrive/坚果云等实时同步客户端直接写入正在运行的 `raw_data`，可能导致锁冲突或旁文件不同步。正确做法是让同步盘同步**备份目录**，而不是活的 `raw_data`。同理，不要把 `raw_data` 放在 SMB/NFS 等网络文件系统上。

---

## 内存与 CPU 观测 {#memory-cpu}

### doctor {#doctor}

```bash
novel-cli doctor
novel-cli doctor --json

# 只看磁盘余量
novel-cli doctor --json | jq '.checks[] | select(.key == "disk_space")'

# 只看章节库采样结果
novel-cli doctor --json | jq '.checks[] | select(.key == "storage")'

# 只看搜索缓存计数
novel-cli doctor --json | jq '.checks[] | select(.key == "search_cache")'
```

`doctor` 会检查 Python/包版本、`settings.toml`、配置合法性、可选依赖、输出/缓存/日志目录、磁盘余量（低于 512 MiB 警告）、章节库（最多采样 3 个数据库；全部 ≤ 1 MiB 时全量检查）、搜索缓存、站点与插件、音乐依赖、API 端口；`--online` 才发起出口探测。它不测吞吐，也不报告进程内存。

### 日志 {#logs}

* 位置由 `general.debug.log_dir` 决定（默认 `./logs`），CLI 的日志文件按天轮转、保留 7 份。
* `general.debug.log_level` 控制**控制台**级别（默认 `INFO`）；CLI 写日志文件时使用 `DEBUG`，所以文件比终端更详细，长时间大书库运行会增加日志体积。
* 日志会脱敏凭据；不要为了排查把 `save_html` 长期打开，原始 HTML 会进入 `debug/` 目录。

### 任务队列 {#task-queue}

* 桌面端/API 使用同一个任务管理器：**每站点一个队列、一个 worker**，同站点严格 FIFO，不同站点并行；另有单线程的处理/导出 worker。
* 观察队列：桌面端任务页，或 `GET /api/v1/tasks`（快照 `{pending, running, completed}`）与 `GET /api/v1/tasks/stream`（SSE 每秒推送）。
* 已完成历史默认保留最近 100 条，只是内存里的小对象，不是主要内存来源。
* 如果 `pending` 堆积在同一个站点，说明该站点的单 worker 正在按 `request_interval` 逐章推进；此时增加别的站点的任务不会加快它。

### 内存主要来源 {#memory-sources}

按影响从大到小：

1. **搜索结果合并**：聚合搜索会把各站点返回的行全部收集后再清洗、排序、分组，最后才应用总 `limit`。内存大致与「站点数 × `per_site_limit`」成正比；站点多、页大时峰值明显。搜索缓存另存最多 64 条合并结果（每条是一份副本），磁盘层单文件上限 2 MiB。
2. **`storage_batch_size`、`workers` 与写入队列**：每个 worker 同时持有一章的正文；交给写库队列后，队列最多再缓冲 10 章；写库前批次再累积最多 `storage_batch_size` 章。它们共同决定下载阶段的内存峰值。`storage_batch_size = 1` 最省内存，`100–200` 会明显增加峰值。
3. **导出**：EPUB 构建同时持有章节 XHTML 与图片，单张图片上限 16 MiB；章节多时峰值高于下载阶段。TXT/HTML 相对轻。
4. **OCR 与字体解密**：`general.parser.enable_ocr = true` 会加载 PaddleOCR 模型，CPU 与内存都远高于纯文本解析；模型名、`device`、`cpu_threads` 都会影响占用。
5. **翻译缓存**：翻译处理器默认最多缓存 4096 条，长时间运行会稳定占用一块内存。
6. **音乐**：并发 3、按 64 KiB 分块写盘，内存占用小；1 GiB/8 GiB 是磁盘字节预算。

系统层面用任务管理器（Windows）或 `ps`/`top` 观察进程 RSS，再对照上面的开关逐项排除。没有内置的内存指标，`doctor` 也不报告 RSS。

### CPU {#cpu}

* 抓取是异步 I/O；解析在 `asyncio.to_thread` 线程池中执行。纯文本解析通常不是瓶颈，CPU 主要花在字体解密、图片章节、OCR、EPUB 构建与翻译上。
* `workers` 超过 CPU 核心数并不必然提速；如果 CPU 已接近 100%，优先减少 OCR/翻译/导出，或分批运行，而不是继续加协程。
* 只测抓取与落盘时加 `--no-export`，可以把导出阶段的 CPU 从基线里去掉。

---

## 常见瓶颈与判断方法 {#bottlenecks}

### 站点吞吐（最常见） {#site-throughput}

**特征**：每章耗时稳定偏高；日志出现 429/503/`Retry-After`；提高 `workers` 后总耗时几乎不变，只是错误或等待变多。

**判断**：用固定区间跑两次——一次默认，一次把 `max_rps` 设得更低（例如 2）。如果更低的上限反而更稳定、总完成章节数更多，说明瓶颈在站点容忍度，不在本机。也可以只对单站点测：`--site <site>`。

**处理**：按站点降 `max_rps`、增 `request_interval`、适度增 `timeout`；不要靠重试硬顶。站点级配置见上文示例。

### SQLite 锁 {#sqlite-lock}

**特征**：日志出现 `database is locked`、`storage_error`，或进度卡在写库阶段；`-wal`/`-shm` 文件存在且长时间不消失。

**判断**：

```bash
# 只读校验，不修改文件
novel-cli storage verify

# 看是否有多个进程在写同一本书
# Windows: 任务管理器 / PowerShell Get-Process
# Linux/macOS: ps aux | grep novel
```

同时确认 `raw_data` 不在 SMB/NFS、没有被实时杀毒/索引/同步客户端反复打开。SQLite 的写锁是整库级别的；`busy_timeout` 只有 5 秒，超过就报错。

**处理**：同一本书只保留一个写进程；把 `raw_data` 放到本地盘；把数据目录排除出实时扫描；备份用 `storage backup` 而不是复制文件。`storage_batch_size` 越大，单次事务持锁越久，锁冲突时反而应先调小。

### 磁盘 I/O {#disk-io}

**特征**：抓取很快但进度停顿；`--no-export` 明显更快；机械盘或网络盘上尤其明显；`storage` 显示剩余空间紧张。

**判断**：对比 `--no-export` 与正常导出的耗时；观察 `storage --json` 的 `free_bytes`；在 Windows 上看任务管理器的磁盘活动率。低于 512 MiB 时 `doctor` 会警告。

**处理**：把 `raw_data` 与 `downloads` 放到本地 SSD；减少同时导出的格式；大书库按卷导出；清理旧 `logs`、`debug/` 与不再需要的 `raw_data`（清理前先备份）。`cache_chapter` 只能避免重复抓取，不能减少首次写库的 I/O。

### 内存 {#memory-bottleneck}

**特征**：进程 RSS 随搜索或批次大小上升；系统开始换页，整体变慢。

**判断**：分别在大搜索、纯下载（`--no-export`）、导出三个阶段观察 RSS；缩小 `storage_batch_size` 或站点集合后复测。

**处理**：降低 `storage_batch_size`、减少并发站点/`workers`、搜索时限制站点与 `--site-limit`、分批导出、关闭 OCR/翻译。搜索缓存无需手动清理，TTL 300 秒后自然过期。

### API 服务 {#api-bottleneck}

**特征**：客户端收到 `429`、`413`、`504`，或连接被拒。

**判断**：

* `429`：每 IP 120 请求/60 秒的限流，或音乐任务已达 4 个活跃上限。
* `413`：请求体超过 1 MiB。
* `504`：阻塞端点超过预算（搜索 120s、浏览 60s、音乐搜索 120s、音乐下载 1800s）。
* 连接堆积：同时连接上限 100，SSE 长连接也占名额。

**处理**：客户端降低轮询频率、改用 `/tasks/stream`、把大请求拆小；确属可信本地环境才考虑 `--no-rate-limit`。多 worker 手工部署不会共享限流计数，也不能突破单进程的任务模型。

### 音乐下载 {#music-bottleneck}

**特征**：整批在中途失败并提示大小上限；或同时提交多个任务收到 `429`。

**判断**：单曲超过 1 GiB 或整批超过 8 GiB 会中止；API 同时只允许 4 个活跃音乐任务。外部依赖（CDM、`N_m3u8DL-RE`、平台可达性）也会让个别平台长时间无响应。

**处理**：拆小批次、降低并发 `workers`（音乐设置，默认 3）、先用 `novel-cli music health` 确认平台可用；大小上限的语义见[故障排查](./troubleshooting.md#music-size-limit)。

---

## 容量估算 {#capacity-estimate}

不要凭固定「每章多少 KB」估算，先测自己的书：

1. 选一本已下载完的书，记录章节数与 `raw_data/<site>/<book>/` 下 `chapter.*.sqlite` 的总字节数。
2. 平均每章字节 ≈ 数据库总字节 ÷ 章节数。文本站点、图片章节、不同 stage 差异很大。
3. 目标书库容量 ≈ 平均每章字节 × 预计章节数 ×（1 + 余量）。余量用于 WAL、临时文件和后续更新。
4. 导出成品单独估算：先测一本已导出书的 `downloads/` 体积，再按书数放大；EPUB 含图片时明显大于纯文本。
5. 备份空间 ≈ `--keep N` × 章节库总大小（完整快照，不是增量）。

`novel-cli storage --json` 提供各目录的实际字节数与剩余空间，适合把这些估算变成可监控的基线。

---

## 相关页面 {#see-also}

* [设置项参考](./settings-reference.md) —— `[general]` / `[sites.*]` 全部键与默认值
* [存储与备份](./storage.md) —— `storage backup` / `verify` / `restore` 的完整语义
* [故障排查](./troubleshooting.md) —— 限流、403、登录、API 413/415、SQLite 损坏等具体症状
* [CLI 用法](./cli-examples.md#doctor-subcommand) —— `doctor` 的检查项与 `--json` 字段
* [本地 API 服务](./api.md) —— 限流、请求体、任务与 SSE
* [音乐下载](./music.md) —— 平台、预算与依赖
