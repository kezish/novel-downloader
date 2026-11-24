# 故障排查

下面是常见问题的排查步骤。命令默认在项目根目录或已安装 `novel-cli` 的环境中执行；Windows 下可使用 Git Bash 或 PowerShell。示例中的 `<site>`、`<book_id>`、`<IP>` 需要替换为实际值。

如果问题是「太慢 / 太占内存 / 磁盘不够」而不是报错，先看[性能与容量](./performance.md)。

---

## 0. 先收集基本信息

先运行内置诊断命令，它会检查 Python 版本、包安装、`settings.toml`、输出目录、章节库、站点/搜索器、音乐依赖与 API 端口占用：

```bash
novel-cli doctor

# 检查其它端口是否可用
novel-cli doctor --port 8080
```

失败项会给出建议；`✓` 表示通过，`✗` 表示失败（可选依赖会标注 `optional`）。

如果怀疑是「目录缺失 / 临时文件堆积」这类本地状态问题，先预览自动修复，再执行：

```bash
novel-cli doctor --fix --dry-run   # 只报告会创建/删除什么，零改动
novel-cli doctor --fix             # 创建缺失的输出/缓存/日志目录，清理过期 .tmp/.part
```

`--fix` 只在配置的输出、缓存、日志三个目录内动作，拒绝 `..` 与符号链接，不改配置、导出文件或登录态；默认只删除 7 天前的临时文件（`--fix-age-days` 可调）。配置错误、依赖缺失、端口占用等问题它不会代修，仍需按检查项的建议处理。

### 搜索结果缓存：相同搜索仍反复访问站点

`doctor` 的「搜索缓存」(`search_cache`) 行给出进程内的搜索缓存计数，字段与读法见 [CLI 用法 § doctor](./cli-examples.md#doctor-subcommand) 的「搜索缓存诊断」。

* `hits`、`misses`、`evictions` 是**当前进程**的累计值。单独运行 `novel-cli doctor` 通常全为 `0`，因为该进程没有做过搜索，这不代表异常；桌面端「设置 → 搜索缓存」显示的是桌面进程自己的 内存条目 / 磁盘条目 / 缓存命中 / 过期条目。
* **过期多**：`expired_entries` 接近 `memory_entries`，说明常驻条目大多已超过 TTL（默认 300 秒）。诊断只统计不清理，下一次写入或读取该键时才移除；若 `misses` 同时很高，重复搜索会重新访问站点。
* **命中低**：在足够多的相同查询下，若 `misses` 明显多于 `hits`，先确认 `--limit`、过滤条件、站点选择完全一致，再看 `evictions` 是否持续增长（条目数超过内存容量，默认 64），以及 `fingerprint` 是否在两次运行之间变化（搜索器/浏览器辅助配置变了，旧聚合条目全部失效）。
* 磁盘条目为 `0` 且 `disk_enabled=false` 是默认的仅内存模式，不是故障；磁盘层需在搜索模块首次导入前（对 CLI 即进程启动前）设置 `NOVELDL_SEARCH_CACHE_DIR` 才会启用。
* 缓存只保存短期结果。需要强制重查时，用桌面端设置页的 **清除搜索缓存**（清空条目，累计计数保留），或在仅内存模式下重启进程。`novel-cli clean cache` 针对配置的缓存目录（站点 Cookie / 章节缓存），不清空搜索缓存的内存条目。

要看完整字段（仍然只有计数与指纹，不含关键词）：

```bash
novel-cli doctor --json | jq '.checks[] | select(.key == "search_cache")'
```

然后再按上面的规律判断；这一条命令里的 `hits`/`misses` 仍只是该新进程的计数。

### 章节库：`raw_data` 缺失、不可写或库损坏

`doctor` 的「章节库」(`storage`) 行只读检查配置的 `raw_data` 目录, 不会创建目录或 `-wal`/`-shm` 旁文件, 也不会读取章节标题/正文。字段与判断方法见 [CLI 用法 § doctor](./cli-examples.md#doctor-subcommand) 的「章节库诊断」。

* `exists=false`: 尚未下载过内容; 首次下载会自动创建。`doctor --fix` 只处理输出/缓存/日志目录, 不创建 `raw_data`。
* `writable=false`: 检查 `raw_data` 的权限, 或修改配置中的 `general.raw_data_dir`。
* `corrupt > 0`: 先用 `novel-cli storage verify --json` 确认范围, 再用 `novel-cli storage restore` 从备份恢复; 不要先删除主库。
* `schema_unsupported > 0`: 库由更新版本写入, 应升级应用, 不要用旧版本覆盖。
* `skipped > 0` (`not_opened`): 库旁存在 WAL 或 hot journal。这是崩溃后的正常状态, 下一次正常打开/写入会恢复; 诊断命令跳过了它以免创建旁文件。需要完整校验时运行 `novel-cli storage verify`。
* `free_bytes` 低于 `low_free_bytes` (512 MiB): 清理空间或把 `raw_data` 移到其它卷, 否则下载可能被截断。

```bash
novel-cli doctor --json | jq '.checks[] | select(.key == "storage")'
```

然后收集版本与日志：

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.python_version(), novel_downloader.__version__)"
tail -n 100 logs/novel_downloader.log
```

如果日志目录不是默认的 `./logs`，查看配置中的 `general.debug.log_dir`：

```bash
grep -n "log_dir\|log_level" settings.toml
```

需要更详细日志时，把 `settings.toml` 中的 `general.debug.log_level` 改为 `DEBUG` 后重试。

---

## 1. 站点返回 403 / Cloudflare 验证

**现象**：请求被拒绝、返回 `403`，或出现 Cloudflare 验证页、TLS 握手失败。

**验证**：先用浏览器同款请求头直接访问站点（把 `example.com` 换成目标站点）：

```bash
curl -I -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36" "https://example.com/"
```

若 `curl` 返回 `403` 或 `5xx`，说明站点对当前网络特征敏感，通常是 TLS 指纹或 HTTP 版本问题。

检查可选后端是否安装：

```bash
python -c "import importlib.util as u; mods=["mutagen", "pathvalidate", "PIL", "qrcode", "websocket", "m3u8", "emoji", "bleach", "filetype", "puremagic", "fake_useragent", "json_repair", "tinytag", "tabulate", "prettytable", "wcwidth", "prompt_toolkit", "aigpy", "ytmusicapi", "pywidevine"]; print([m for m in mods if not u.find_spec(m)])"
```

**解决**：

1. 安装后端：

   ```bash
   pip install "novel-downloader[all-backends]"
   ```

2. 在 `settings.toml` 中切换后端，优先尝试 `curl_cffi` 的浏览器仿真：

   ```toml
   [general]
   backend = "curl_cffi"
   impersonate = "chrome136"

   # 或尝试 httpx + HTTP/2：
   # backend = "httpx"
   # http2 = true
   ```

3. 重试目标站点：

   ```bash
   novel-cli download --site <site> <book_id>
   ```

4. 若返回 `503 Service Temporarily Unavailable`，降低请求频率：

   ```toml
   [general]
   max_rps = 1.0
   request_interval = 1.0
   ```

   已下载的章节会从 `chapter.raw.sqlite` 续爬，不会重复抓取。

> 不同站点对后端的反应可能相反：有的站点在 HTTP/2 下正常，有的反而必须使用 HTTP/1.1。请按站点分别调整，不要全局强行使用同一种后端。

---

## 2. 登录失效 / Cookie 过期

**现象**：站点提示未登录、只能看到部分章节，或返回权限错误。

**验证**：

```bash
# 查看支持交互式登录的站点
novel-cli login --list

# 检查缓存的 Cookie 文件是否存在
ls -l novel_cache/<site>/*.cookies
```

**解决**：

1. 重新走交互式登录（推荐）：

   ```bash
   novel-cli login <site>
   ```

   登录成功后会自动写入 `<缓存目录>/<site>/aiohttp.cookies`。

2. 或清理该站点的缓存后重试：

   ```bash
   novel-cli clean cache --site <site> --yes
   ```

3. 也可以在配置中提供 Cookie 或账号密码：

   ```toml
   [sites.<site>]
   login_required = true
   cookies = "完整的 Cookie 字符串"
   # 或者
   # username = "your_username"
   # password = "your_password"
   ```

4. 确认配置中的站点键与实际使用的 `--site` 一致（例如 `fanqienovel`，不是显示名）。

> Cookie 方式不会自动续期；过期后需要重新复制或重新登录。

---

### 番茄小说：部分章节只有很短的内容 / 被标记为失败

番茄对未登录的匿名访问会**按章**加锁：目录页给每个章节带 `isChapterLock`，
锁定的章节在阅读页只返回约 200 字的预览。

下载器的行为：

- 目录里标为锁定的章节记为 `accessible: false`，默认**直接跳过**，不会逐章白抓；
- 如果强制抓取（`fetch_inaccessible = true`），解析层会识别预览并判为失败，
  不会把 200 字预览当成正文写进书里。

想拿到这些章节需要登录态：

```bash
novel-cli login fanqienovel
```

或把浏览器里的 Cookie 写进配置（见上一节）。匿名状态下能下载的是本书中未锁定的章节——
这一点与是否使用本工具无关，是站点的访问控制。

---

## 3. API 返回 401 / 413 / 415 / 端口被占用

### 3.1 401 Unauthorized

**原因**：`novel-api` 启动时设置了 `--token` 或环境变量 `NOVEL_API_TOKEN`，请求没有携带匹配的 token。

**验证**：

```bash
# 未带 token 时返回 401
curl -i http://127.0.0.1:8081/api/v1/health

# 带上正确 token 后返回 200
export NOVEL_API_TOKEN="your-token"
curl -i -H "Authorization: Bearer $NOVEL_API_TOKEN" http://127.0.0.1:8081/api/v1/health
```

PowerShell 中设置环境变量：

```powershell
$env:NOVEL_API_TOKEN = "your-token"
curl.exe -i -H "Authorization: Bearer $env:NOVEL_API_TOKEN" http://127.0.0.1:8081/api/v1/health
```

也可以使用 `X-API-Key`：

```bash
curl -i -H "X-API-Key: your-token" http://127.0.0.1:8081/api/v1/health
```

**解决**：

* 确认客户端与服务器使用同一个 token。
* 如果只是本机使用，可以不设置 token：

  ```bash
  novel-api
  ```

* Android 端在“设置”中填写同一个“API 密钥”。

### 3.2 端口被占用

**现象**：启动时报 `[Errno 10048]`（Windows）或 `Address already in use`（Linux/macOS）。

**验证**：

```bash
netstat -ano | grep ":8081"
```

PowerShell：

```powershell
Get-NetTCPConnection -LocalPort 8081 | Select-Object LocalAddress,LocalPort,OwningProcess
Get-Process -Id <PID>
```

**解决**：

1. 结束占用端口的进程，或
2. 换一个端口启动：

   ```bash
   novel-api --port 8082
   ```

   同时把 Android 的 API 地址改为 `http://<IP>:8082/`。

3. 确认访问路径包含 `/api/v1`，例如 `http://127.0.0.1:8081/api/v1/sites`；根路径没有接口。

### 3.3 用 X-Request-ID 定位一次失败请求

**现象**：某个 API 请求偶发失败（4xx / 5xx / 超时），日志里请求很多，无法确认服务端收到的是哪一次、耗时多久。

**做法**：

1. 用 `curl` 复现并保存响应头与响应体。API 在**每个**响应里回写 `X-Request-ID`，包括 `401`、`422`、`429` 和 `500`：

   ```bash
   BASE=http://127.0.0.1:8081

   curl -sS -D /tmp/api_headers.txt -o /tmp/api_body.json \
     -H "Authorization: Bearer $TOKEN" \
     "$BASE/api/v1/sites"

   grep -i '^x-request-id:' /tmp/api_headers.txt
   ```

2. 在启动 `novel-api` 的终端或服务日志里搜索 `[<该 ID>]`。访问日志行形如：

   ```text
   [3f2a1c8e-...] GET /api/v1/sites 500 12.4ms
   ```

   查询字符串和响应体从不写入日志，所以只需要这个 ID 就能定位到对应那一行。

3. 如果请求里自带了 `X-Request-ID`，只有 `1`–`64` 个 `A-Z a-z 0-9 . _ : -` 字符的值会被沿用；非法或超长的值会被服务端静默替换为 UUID4。因此请以**响应头回写的值**为准。

4. 上报问题时提供：`X-Request-ID`、大致时间、请求方法与路径、状态码。不要附带 `Authorization`、token、Cookie 或请求体。

这个 ID 只用于关联排障，不参与鉴权、限流计数或请求去重；服务重启后旧 ID 不再对应任何日志。

### 3.4 API 返回 413 / 415 {#api-413-415}

**现象**：请求体看起来正常却收到 `413 Request Entity Too Large`；或客户端明明发的是 JSON，却收到 `415 Unsupported Media Type`。

两者都由**请求体守卫**（`apps/api/middleware.py` 的 `BodySizeLimitMiddleware`）在**读取请求体之前**直接返回，路由和业务代码从未执行。`detail` 是固定文案，可据此区分三种情况：

| 状态码 | `detail` |
| --- | --- |
| `413` | `Request body too large: the limit for this endpoint is 1048576 bytes` |
| `415` | `Unsupported Content-Type: send the request body as application/json` |
| `415` | `Unsupported Content-Encoding: send the request body uncompressed (identity)` |

响应带 `Cache-Control: no-store`，并照常回写 `X-Request-ID`；用上一小节的方法可在日志里定位到那一行。两种拒绝都不会执行任何业务副作用（不建任务、不写追更条目）。

**作用范围**：`/api/v1` 下所有 `POST` / `PUT` / `PATCH` / `DELETE`，包括没有请求体的 `/tasks/{id}/cancel`、`DELETE /watchlist/...`，也包括不存在的路径和将来新增的路由（守卫按前缀生效，不按路由清单）。`GET` / `HEAD` / `OPTIONS` 与 `/downloads` 不在守卫范围内，请求体不会被检查（`GET` 带 1 MB body 仍会正常处理）。上限是 `1 MiB`（`1048576` 字节，含边界）：恰好 1 MiB 会通过守卫进入字段校验（可能随后返回 `422`），多 1 字节即 `413`。大小按**原始字节**计，不是字符数：中文在 UTF-8 下每字 3 字节。

#### 413：先确认是声明超限还是流式累计

两条路径的响应完全一样，区别只在服务端**读了多少字节**：

| 触发方式 | 服务端行为 | 客户端可观察到的现象 |
| --- | --- | --- |
| `Content-Length` 已超限 | 一个字节都不读，立即回 `413` | 带 `Expect: 100-continue` 的客户端（curl 对大请求体默认如此）在上传前就收到 `413`，`size_upload=0` |
| 无 `Content-Length`（`Transfer-Encoding: chunked`） | 逐块读取，累计超过 1 MiB 的那一块立即中止 | 请求体已几乎全部上传后才收到 `413`，`size_upload` ≈ 实际大小 |

确认方法：

```bash
BASE=http://127.0.0.1:8081

# 看请求头：有 Content-Length 且值 > 1048576，就是声明超限
curl -sv -o /dev/null -H "Content-Type: application/json" \
  --data-binary @payload.json "$BASE/api/v1/search" 2>&1 \
  | grep -E '^[<>] (Content-Length|Transfer-Encoding|HTTP/)'

# 看实际发出的字节数：413 时 uploaded=0 → 声明超限；uploaded≈文件大小 → 流式累计
curl -s -o /dev/null -w 'code=%{http_code} uploaded=%{size_upload}\n' \
  -H "Content-Type: application/json" \
  --data-binary @payload.json "$BASE/api/v1/search"
```

**哪些请求真的会这么大**：`search`、`browse`、`tasks`、`watchlist`、`login/requests` 的正常请求体只有几百字节到几 KB，出现 `413` 基本说明客户端发错了内容（上传了文件、把响应体原样回传、循环拼接了字段）。真正接近上限的是音乐端点：`music/download`、`music/export`、`music/jobs` 各接受最多 `200` 首曲目，每首的 `lyric`（单首最多 `100000` 字符）、`download_url`、`http_headers`、`extra`、`path`、`sources` 都计入体积。实测 200 首各带 6000 字符歌词约 1.2 MB，直接 `413`；同样 200 首只发 `provider` + `id` 约 7 KB，正常通过。

**怎么把请求改小**（按优先级）：

1. **回传 `search_id`，只发最小曲目**。搜索结果里的 `search_id` 能让服务端取回原始 Track 对象，请求里每首只需 `provider` + `id`：

   ```bash
   curl -s -X POST "$BASE/api/v1/music/download" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"search_id":"9f2c1b0a9d8e4f7c8b6a5d4e",
          "tracks":[{"provider":"netease","id":"186016"}]}'
   ```

   `search_id` 缓存 30 分钟、最多 32 份；过期后需重新搜索（服务端会按提交的字典降级处理，通常表现为逐曲失败，而不是 `413`）。

2. **删掉不需要的字段**。下载请求不需要 `lyric`、`extra`、`http_headers`、`cover_url`、`download_url`、`sources`；只有 `music/export` 需要 `path`。字段本身也有各自上限（如 `lyric` 10 万字符、`http_headers` 32 项），超限是 `422`。
3. **分批**。`tracks` 有独立的 `200` 首上限（超出是 `422`，不是 `413`），所以既不能靠删字段绕过条数上限，也不能靠分批绕过大小上限——两个限制都要满足。批量大时改用异步 `POST /api/v1/music/jobs`，按每批 `≤ 200` 首提交，再轮询各自任务。

**不要直接把上限调大**：`novel-api` 没有对应参数或环境变量，唯一入口是代码里的 `create_app(max_body_bytes=...)`；内嵌引擎固定为 1 MiB。调大只会让每个并发请求多占内存、把 JSON 解析成本推给服务端，并不会放宽 `200` 首、字段长度这些 `422` 边界。`413` 几乎总是客户端构造请求的问题，先按上面三步改小。

**和 401 / 429 的关系**：守卫在最内层，限流（`429`）在它外层、token 校验（`401`）在它内层。因此预算耗尽后，连超限请求体也直接得到 `429`（此时守卫根本没运行）；而未带 token 的超大请求在 REST 传输上返回 `413` 而不是 `401`。先解决 413，再排查 401 / 429。

#### 415：Content-Type 与压缩

守卫只接受 JSON 媒体类型：`application/json`，以及任何 `application/*+json`（如 `application/vnd.api+json`）。比较前会去掉参数并忽略大小写，所以 `application/json; charset=utf-8`、`APPLICATION/JSON` 都通过。其余一律 `415`：

- `text/plain`、`text/json`、`application/octet-stream`、`application/x-www-form-urlencoded`、`multipart/form-data`；
- **压缩请求体**：`Content-Encoding` 只接受缺省或 `identity`；`gzip`、`br`、`deflate` 以及 `identity, gzip` 这类组合都在读取前被拒（API 只解析未压缩 JSON，压缩体只可能是压缩炸弹）；
- 头存在但值为空（`Content-Type:` 后面什么都没有）也是 `415`。

这条规则与请求体大小无关：即使请求体为空、甚至端点本身不接收请求体（如 `POST /api/v1/tasks/{id}/cancel`、`DELETE /api/v1/watchlist/{site}/{book_id}`），只要显式声明了非 JSON 的 `Content-Type`，同样返回 `415`。反过来，不声明这个头就不会被守卫拦下。

**缺失** `Content-Type` 头是例外：REST 传输把它交给 FastAPI，得到 `422`（`Input should be a valid dictionary or object ...`），而不是 `415`；内嵌引擎则直接按 JSON 解码。

curl 常见写法对照：

```bash
BASE=http://127.0.0.1:8081

# 错误 1：-d 默认发 application/x-www-form-urlencoded → 415
curl -i -d '{"keyword":"santi"}' "$BASE/api/v1/search"

# 错误 2：-F 发 multipart/form-data → 415
curl -i -F 'keyword=santi' "$BASE/api/v1/search"

# 错误 3：压缩请求体 → 415（detail 提到 Content-Encoding）
curl -i -H "Content-Type: application/json" -H "Content-Encoding: gzip" \
  --data-binary @payload.json.gz "$BASE/api/v1/search"

# 正确：显式声明 JSON；--data-binary 不改变字节，适合含中文的文件
curl -i -H "Content-Type: application/json" \
  --data-binary @payload.json "$BASE/api/v1/search"

# 正确：curl 7.82+ 的 --json 会自动加 Content-Type 与 Accept
curl -i --json '{"keyword":"santi"}' "$BASE/api/v1/search"
```

Windows 上不要把含中文的 JSON 直接写在命令行里：Git Bash / PowerShell 的代码页可能把参数转成非 UTF-8 字节，服务端解不出 JSON，返回 `400 There was an error parsing the body`（这是解码失败，不是 `415`）。把 JSON 写进文件（UTF-8、无 BOM）再用 `--data-binary @file`。

`415` 优先于 `413`：同一个请求既压缩又超限时返回 `415`，因为编码在读取前就被拒。`415` 也优先于 `401`（REST）：未带 token 但 `Content-Type` 非 JSON 时返回 `415`。

#### 与 422（字段校验）的区分

| 状态码 | 含义 | 典型例子 |
| --- | --- | --- |
| `413` | 请求体**没被解析**：超过 1 MiB | 200 首带完整歌词的曲目（约 1.2 MB） |
| `415` | 请求体**没被读取**：媒体类型或编码不是 JSON | `curl -d`（表单）、`Content-Encoding: gzip` |
| `422` | 请求体**已解析**，字段不合法 | `tracks` 201 首、缺 `keyword`、`keyword` 超 200 字符、`limit` 越界 |
| `400` | 传输层畸形：`Content-Length` 非法，或请求体不是合法 UTF-8（仅 REST 传输） | `Content-Length: abc`、命令行把中文转成了非 UTF-8 字节 |

JSON **语法**错误属于 `422`（`errors[].type` 为 `json_invalid`，`detail` 含 `JSON decode error`），不是 `400`，也不是 `415`：守卫只检查声明，不解析内容。空请求体同样是 `422`（`Field required`）。

判断顺序：先看状态码落在哪一层，再看 `detail` / `errors`。同时可能命中多条限制时，实际返回取决于层序：`415` 先于 `413`，`413` 先于 `422`；`429` 在更外层，预算耗尽时以上都被 `429` 覆盖。

#### 内嵌引擎（桌面端 / Android 独立模式）的差异

`apps/embedded/server.py` 的 stdlib HTTP 引擎（Android 独立模式由 `start_server` 启动）镜像同一套契约，但有四处差别：

| 项目 | REST（`novel-api` / uvicorn） | 内嵌引擎 |
| --- | --- | --- |
| 上限与范围 | 1 MiB；`/api/v1` 下 `POST`/`PUT`/`PATCH`/`DELETE` | 相同，`413` 文案也相同 |
| `Transfer-Encoding: chunked` | 逐块累计，超限 `413` | **`501 Chunked request bodies are not supported`**（不实现 chunked 解码） |
| 缺失 `Content-Type` | 交给 FastAPI → `422` | 直接按 JSON 解码 |
| 拒绝后的连接 | 保持 keep-alive，可复用 | 关闭连接，客户端必须重连 |

因此在内嵌引擎上：

- 客户端必须先缓冲请求体、算出 `Content-Length` 再发。流式上传（`chunked`）无论多小都返回 `501`，`curl -H "Transfer-Encoding: chunked"` 或某些 HTTP 库的流式 body 都会命中。
- 非法或负数 `Content-Length` 返回 `400 Invalid Content-Length header`。
- 若内嵌引擎启用了 token（`start_server(..., token=...)` 或 `NOVEL_DOWNLOADER_EMBEDDED_TOKEN`；未设置时校验关闭），**鉴权先于请求体检查**：未带 token 的超大请求得到 `401`，而不是 REST 传输的 `413`。先确认 token，再看 413 / 415。
- 无请求体的写端点（`POST /api/v1/tasks/{id}/cancel`）同样受大小限制，超大 body 仍返回 `413`；`GET` / `HEAD` 不检查请求体（状态码与 REST 一致，但内嵌引擎读到超大 body 时会关闭连接，REST 会继续复用）。

---

## 4. 路径包含中文或空格 {#path-chinese-spaces}

**现象**：导出失败、资源文件找不到、第三方工具报编码错误，或 Android 下载链接异常。

**验证**：确认 Python 能正常读写中文路径：

```bash
python -c "import sys; from pathlib import Path; p = Path.cwd(); print('filesystem encoding:', sys.getfilesystemencoding()); q = p / '中文测试目录'; q.mkdir(exist_ok=True); print('ok:', q.resolve()); q.rmdir()"
```

若上面的命令成功，说明 Python 本身可以处理中文路径；问题通常来自终端代码页、旧版第三方工具或配置中的路径写法。

**解决**：

1. 把数据目录显式指向纯 ASCII 路径（路径使用正斜杠，避免 TOML 转义问题）：

   ```toml
   [general]
   raw_data_dir = "C:/novel-downloader-data/raw_data"
   output_dir = "C:/novel-downloader-data/downloads"
   cache_dir = "C:/novel-downloader-data/cache"
   ```

2. 或通过 `NOVEL_HOME` 把应用主目录移到纯 ASCII 路径：

   ```bash
   # Git Bash
   export NOVEL_HOME="C:/novel-downloader-home"
   ```

   ```powershell
   # PowerShell
   $env:NOVEL_HOME = "C:\novel-downloader-home"
   ```

3. Windows 控制台出现乱码时，切换到 UTF-8 代码页：

   ```powershell
   chcp 65001
   ```

4. 如果路径位于 OneDrive 等同步目录，先暂停同步再导出，避免文件被占用。

---

## 5. Android 连不上电脑

**现象**：App 搜索/任务一直转圈，提示连接失败或超时。

**验证与解决**：

1. 确认 API 绑定到局域网地址，而不是仅本机：

   ```bash
   novel-api --host 0.0.0.0 --port 8081 --token "your-token"
   ```

2. 查询电脑的局域网 IPv4 地址：

   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '127.*'} | Select-Object IPAddress,InterfaceAlias
   ```

   在 Git Bash 中也可以直接运行 `ipconfig`，找到当前 Wi-Fi/以太网适配器的 IPv4 地址。

3. 确认手机与电脑在同一网段：

   ```bash
   ping -n 4 <电脑IP>
   ```

4. 用手机浏览器打开 `http://<电脑IP>:8081/api/v1/health`。绑定非回环地址时 `/docs` 会自动关闭，因此改用健康检查端点：返回 `401`（`Invalid or missing API token...`）说明网络可达且鉴权生效；若连接超时或被拒绝，继续检查防火墙。

5. 放行 Windows 防火墙端口（管理员 PowerShell）：

   ```powershell
   New-NetFirewallRule -DisplayName "NovelDownloader API 8081" -Direction Inbound -Protocol TCP -LocalPort 8081 -Action Allow
   ```

   或使用命令行：

   ```bash
   netsh advfirewall firewall add rule name="NovelDownloader API 8081" dir=in action=allow protocol=TCP localport=8081
   ```

6. 模拟器访问电脑使用 `http://10.0.2.2:8081/`；真机使用 `http://<电脑IP>:8081/`。保留地址末尾的 `/`。

7. 关闭路由器的“AP 隔离/客户端隔离”，并确认手机没有连接到访客网络。

8. Android App 使用 HTTP 明文访问；跨网络时请使用 VPN（如 Tailscale）或 HTTPS 反向代理，并始终设置 token。

### 5.1 自定义下载目录 (SAF) 不可用 {#saf-custom-download-directory}

**现象**：设置里选过下载目录，但下载仍落到系统「下载」目录；或设置页出现红色提示「自定义下载目录不可用，请在设置中重新选择」。

**原因**：Android 通过 SAF (`ACTION_OPEN_DOCUMENT_TREE`) 授权目录，应用保存的是持久化 URI 权限。以下情况会让授权失效：用户在系统设置中撤销权限、卸载/更换了提供该目录的应用、目录被删除或移走、备份恢复到另一台设备。

**行为与处理**：

* 设置页会保留原来选的目录（方便直接替换），同时明确提示不可用；历史页下载会**自动回退到系统 Downloads**，并提示「自定义下载目录不可用，已改为保存到系统 Downloads」，不会把文件写到错误位置，也不会让下载直接失败。
* 重新点「选择目录」并确认系统授权即可恢复；选择时会请求并保存读/写权限，保存失败会提示「无法保留该目录的写入权限，请换一个目录」，此时不会覆盖原来可用的目录。
* 点「恢复默认」会释放持久化权限并回到系统 Downloads。
* 文件实际写入所选目录下的 `NovelDL/` 子目录；同名文件按 `name (1).ext`、`name (2).ext` … 去重（大小写不敏感），不会静默覆盖已有文件。
* 该设置只影响「历史」页导出文件的下载；音乐文件仍按音乐引擎自己的目录/系统下载流程保存。

### 5.2 SAF 下载失败或进度停住 {#saf-download-failure}

**现象**：历史页下载在自定义目录下失败，提示「存储空间不足」「文件提供方写入失败…」或「网络错误…」，文件没有出现在 `NovelDL/`；或者进度一直停在「正在连接，准备写入…」。

**原因与处理**：

1. **提示「存储空间不足」**：目标卷（内部存储或 SD 卡）已满或配额超限（`ENOSPC` / `quota exceeded`）。此时**不会**回退到系统 Downloads——换目标往往同样失败，还会掩盖真正的问题。清理空间，或在「设置 → 下载」换一个目录 / 点「恢复默认」。
2. **提示「文件提供方写入失败…」**：目录提供方（系统 Files、第三方文件管理器、云盘等）自身报错，无法创建或写入文档。先重试；仍失败则换目录或「恢复默认」，系统 Downloads 由 DownloadManager 写入，不依赖该提供方。
3. **提示「网络错误…」或「服务器返回 HTTP …」**：文件读取失败，与目标目录无关，因此也不回退。按 [§ 5 Android 连不上电脑](#5-android) 检查 API 地址与网络后重试。
4. **进度一直停在「正在连接，准备写入…」**：目标文档已创建，但服务器还没返回响应。连接超时 10 秒、读取超时 60 秒，超时后会按网络错误失败；先确认电脑端 API 仍在运行、手机可访问。
5. **进度条不定、只显示「已写入 X」**：服务器没有提供 `Content-Length`（例如分块传输），这是正常表现，不是卡住；写入完成时会正常收尾。
6. 只有权限/目录不可用（见 § 5.1）才会自动回退系统 Downloads；失败或取消后本次创建的不完整文件会被删除，重试会从头下载，不会从半截文件续传。

---

## 6. 音乐下载缺少依赖

**现象**：`novel-cli music ...` 报模块缺失，或平台列表为空、下载失败。

**验证**：

```bash
novel-cli music platforms
python -c "import importlib.util as u; print('mutagen', bool(u.find_spec('mutagen'))); print('pathvalidate', bool(u.find_spec('pathvalidate'))); print('PIL', bool(u.find_spec('PIL'))); print('qrcode', bool(u.find_spec('qrcode'))); print('websocket', bool(u.find_spec('websocket')))"
```

**解决**：

1. 安装音乐可选依赖：

   ```bash
   pip install "novel-downloader[music]"
   ```

2. 重新查看平台列表：

   ```bash
   novel-cli music platforms
   ```

3. 部分平台有额外要求：

   * Apple Music / SoundCloud 需要 `pywidevine`；DRM 解密还需自备 CDM（`.wvd` 设备文件），SoundCloud 的 HLS 下载还需要外部 `N_m3u8DL-RE`。
   * YouTube 搜索需要 `ytmusicapi`（不需要 Node）；下载依赖第三方解析服务，网络不可达时会失败。
   * 部分 HLS/流媒体格式需要系统安装 `ffmpeg`。

   检查：

   ```bash
   python -c "import importlib.util as u, shutil; print('pywidevine', bool(u.find_spec('pywidevine'))); print('ytmusicapi', bool(u.find_spec('ytmusicapi'))); print('ffmpeg', shutil.which('ffmpeg'))"
   ```

4. 需要账号凭据的平台（如 Spotify、TIDAL、汽水音乐）还要在平台凭据中配置 Cookie 或 token，否则搜索可能为空。

5. 单个平台失败时，换平台验证基础功能是否正常：

   ```bash
   novel-cli music search "关键词" --platform netease --limit 5
   ```

**歌词没有生成（`.lrc` 缺失）**：

1. 确认音乐设置里的 `save_lyric` 未被关闭（默认 `true`）；它位于音乐数据目录的 `settings.json`。
2. 平台可能本来就没有歌词，或只返回占位值；此时不会创建文件，也不影响音频成功。
3. 歌词文件与音频同名：`<歌名> - <歌手>.lrc`，有译文时为 `.zh.lrc`（只有译文时会写进 `.lrc`）。若音频用了 ` [<id>]` 去重后缀，歌词也带同一后缀——到音频所在目录按同名查找。
4. 音频已存在且被 `skip_existing` 跳过时，只有 `.lrc` 不存在才会补写；已有歌词不会被覆盖，也不会重新联网。
5. 超过 1 MiB 的歌词响应会被当作错误页跳过（日志里有 WARNING）。把日志级别调到 `DEBUG` 可以看到具体原因。

---

## 7. 代理 / VPN 导致超时或 SSL 错误 {#proxy-vpn}

**现象**：所有站点都超时、出现 SSL 证书错误，或开启/关闭 VPN 后行为不一致。

**验证**：

```bash
# 查看当前环境变量
env | grep -i proxy

# 测试代理是否可用
curl -x http://127.0.0.1:7890 -I https://www.google.com
```

**解决**：

1. 在 `settings.toml` 中显式配置代理：

   ```toml
   [general]
   proxy = "http://127.0.0.1:7890"
   # 代理需要认证时：
   # proxy_user = "username"
   # proxy_pass = "password"
   trust_env = false
   ```

2. 如果希望复用系统环境变量中的代理，设置：

   ```toml
   [general]
   trust_env = true
   ```

3. 避免同时启用“系统代理”和配置中的 `proxy`，造成双重代理。

4. SSL 错误通常来自代理的中间人证书。可临时关闭校验定位问题（仅用于排查，不建议长期使用）：

   ```toml
   [general]
   verify_ssl = false
   ```

5. 切换后端验证是否为 TLS 兼容性问题：

   ```toml
   [general]
   backend = "httpx"
   http2 = true
   ```

6. 音乐平台可能自行读取系统代理；若小说可下载但音乐超时，优先检查系统代理与平台可达性。

### 7.1 SSE 流空闲后被断开（502 / 504 / Unexpected EOF）

**现象**：`/api/v1/search/stream`、`/api/v1/tasks/stream` 或
`/api/v1/login/requests/stream` 刚连上时正常，静默一段时间后断开；反向代理
返回 `502` / `504`，或客户端报 `Unexpected EOF` / `connection reset`。站点
响应慢、搜索结果迟迟不返回时最容易命中。

**机制**：三个流每静默 15 秒会发送一个 `: keepalive` 注释帧，用于复位代理、
CDN 与 NAT 的空闲计时器。它是注释，不产生 `results` / `snapshot` /
`requests` / `done` 事件。间隔常量在
`src/novel_downloader/apps/api/routes.py` 的 `SSE_KEEPALIVE_INTERVAL`，
完整行为见 [API 文档的「SSE 心跳」](api.md#sse-keepalive)。

**排查**：

1. 直连服务，确认心跳确实在写：

   ```bash
   curl -N -v "http://127.0.0.1:8081/api/v1/tasks/stream" \
     -H "Authorization: Bearer $TOKEN"
   ```

   正常时应每 15 秒左右出现一行 `: keepalive`。

2. 直连稳定、经代理后断开：检查每一跳的空闲/读超时。
   - nginx：`proxy_read_timeout` 必须大于 15 秒（默认 60 秒即可），并设置
     `proxy_buffering off;`。`X-Accel-Buffering: no` 只对 nginx 生效。
   - CDN / 云负载均衡：确认空闲上限大于心跳间隔（如 Cloudflare 为 100 秒），
     并关闭对 `text/event-stream` 的缓存。
3. 客户端读超时不要小于 15 秒。`EventSource` 会自动忽略注释帧并自行重连；
   自写的 `fetch` 解析器应跳过以 `:` 开头的行，不要把空事件块当成 JSON 或
   断流信号。
4. NAT 网关常在 30–120 秒回收空闲连接。若本地能看到心跳、跨过某段网络后
   却消失，多半是该段缓冲了响应；应修代理/NAT，而不是缩短服务端心跳。

**仍被断开时**：把日志级别调到 `DEBUG`，确认服务端在写心跳；再用
`ss` / `netstat` 或代理访问日志对照断开时刻。

---

## 8. 音乐聚合搜索里部分平台没反应 / 很慢 {#music-aggregate-search}

**现象**：选择「全部平台」搜索时，进度条走完只回来几首；某些平台一直没有结果，或者整次搜索都要等到预算用完。单独指定该平台时却可能正常。

**机制**（`apps/music/aggregate.py`）：

* 每个平台一个独立线程，整次聚合搜索有墙钟预算：默认 5 秒（`DEFAULT_TIMEOUT`），桌面渐进式聚合搜索传入 6 秒（`SEARCH_SITE_TIMEOUT`）。
* 只要有一个平台先返回结果，再等 2 秒就收工（`EARLY_EXIT_GRACE`）；没回来的平台本次直接略过，不计为失败。
* 抛异常的平台，以及预算耗尽时仍未返回的平台，记为失败并熔断 **10 分钟**（`FAILURE_TTL`），期间聚合搜索会跳过它；因 2 秒早退被略过的平台不记失败。
* 连续两次耗时 ≥2 秒且零结果，会被判为「慢且空」，跳过 **5 分钟**（`SLOW_EMPTY_STREAK` / `SLOW_EMPTY_TTL`）。
* 这些状态写入音乐数据目录的 `search_stats.json`，重启后仍然生效。默认位置是 `%LOCALAPPDATA%\novel_downloader\music\search_stats.json`；设置 `NOVEL_MUSIC_HOME` 可以整体替换音乐数据目录。

**排查**：

1. 先单平台验证，显式指定平台时不会走熔断跳过：

   ```bash
   novel-cli music search "关键词" --platform netease --limit 5
   ```

2. 等 10 分钟（失败熔断）或 5 分钟（慢空跳过）自动恢复；也可以先退出所有客户端，再删除 `search_stats.json`，下次搜索会重建。
3. 想保留原设置又要一个干净状态，可临时把 `NOVEL_MUSIC_HOME` 指向空目录：

   ```bash
   # Git Bash
   export NOVEL_MUSIC_HOME="$TEMP/novel-music-clean"
   ```

   ```powershell
   # PowerShell
   $env:NOVEL_MUSIC_HOME = Join-Path $env:TEMP "novel-music-clean"
   ```

4. 如果所有平台都在熔断期，代码会自动清空一次缓存再重试；仍然全空时，按第 6 节检查依赖、凭据与网络。

---

## 9. 桌面自动更新：安装版与便携版行为不同

**现象**：点更新后应用退出但没有升级；升级后出现两份程序；或者提示找不到 PowerShell。

**机制**：

* 客户端先判断安装形态：优先读取卸载注册表的 `InstallLocation` / `DisplayIcon`（`{AppId}_is1` 键），其次看可执行文件旁是否有 `unins000.exe`，最后才检查默认目录 `%LOCALAPPDATA%\Programs\NovelDownloader`。因此安装到自定义目录仍算**安装版**；其它已打包目录视为**便携版**；源码运行则是 `source`。
* **安装版**下载 `NovelDownloader-setup-<version>-x64.exe`，退出后以静默参数启动 Inno Setup，原地覆盖安装目录。安装器会先删除 `_internal` 再写入新版本，并通过命名互斥量 `NovelDownloader-UpdateMutex` 关闭仍在运行的旧实例；静默安装结束后会直接启动新版本，无需手动打开。
* **便携版**下载 zip，退出后由 PowerShell 辅助脚本 `updates/_apply_portable_update.ps1` 等待进程结束，解压到 `%TEMP%`，先删除当前程序目录中的 `_internal` 再覆盖并重新启动，因此已移除的插件不会残留；`app_home` 下的设置与下载不受影响。不会另装一份到 `%LOCALAPPDATA%`。
* 便携版更新依赖 `powershell` 或 `pwsh`。两者都找不到时，更新会中止并提示手动解压。

**排查**：

1. 更新包在 `%LOCALAPPDATA%\novel_downloader\updates\`。失败后 zip/安装包通常仍在这里，便携版可以直接解压覆盖 `NovelDownloader.exe` 所在目录，再手动启动。
2. 清理规则：`.part` 未完成文件保留 1 天，完整安装包/zip 保留 7 天，过期自动删除。
3. 提示找不到 PowerShell：手动解压 `updates\` 里的 zip，把所有文件覆盖到当前程序目录。
4. 怀疑升级没生效或出现两份：安装版目录由卸载注册表（`InstallLocation` / `DisplayIcon`）或 `unins000.exe` 确定，未必是默认的 `%LOCALAPPDATA%\Programs\NovelDownloader`；确认自己启动的是哪一份。侧栏底部显示的 `v...` 是当前运行版本。
5. 日志在 `%LOCALAPPDATA%\novel_downloader\logs\novel-desktop.log`，可搜索 `Launched the update installer` 或 `Launched the portable update helper` 确认更新器是否真正启动。

安装、升级、卸载时的用户数据行为见[安装](./installation.md)一节。

---

## 10. TIDAL / YouTube / Apple Music / SoundCloud：能搜到不等于能下载 {#tidal-youtube-apple-music-soundcloud}

**现象**：这些平台出现在列表里，但搜索为空、只返回试听，或下载时报「HLS 流暂不支持直链下载」「该平台未返回可下载直链」。

**说明**：

* 依赖已打包：桌面安装包和 `pip install "novel-downloader[music]"` 都包含 `aigpy`（TIDAL）、`ytmusicapi`（YouTube）、`pywidevine`（Apple Music / SoundCloud 的 DRM），不需要再装 Node。
* 「能搜索」和「能下载」是两件事：这些平台在搜索阶段就会尝试解析可下载直链；拿不到直链时，列表可能为空，或只有试听片段。
* **TIDAL**：必须在平台凭据里配置 token JSON，否则搜索为空。凭据保存在 `<音乐数据目录>/platform_creds.json` 的 `tidal` 键下；默认路径是 `%LOCALAPPDATA%\novel_downloader\music\platform_creds.json`。旧版音乐桌面工具有「平台凭据」对话框可写入，也可以按 JSON 直接编辑。
* **Apple Music**：需要账号才能拿到完整曲目；没有账号通常只有预览片段，且需要能访问 `music.apple.com`。
* **SoundCloud**：搜索可用；DRM/HLS 曲目需要自备 CDM（`.wvd`）与外部 `N_m3u8DL-RE`。
* 对这四个 musicdl 平台，内置下载器只接受非 HLS 的直链：解析时遇到 HLS(m3u8) 会直接返回「HLS 流暂不支持直链下载」。因此即使准备了 CDM 和 `N_m3u8DL-RE`，Apple Music / SoundCloud 的 DRM/HLS 曲目也不能在应用内下载，需要用支持 Widevine 与 `N_m3u8DL-RE` 的外部工具。TIDAL / YouTube 只有在平台返回 HTTP 直链时才能下载。

**验证**：

```bash
novel-cli music platforms
novel-cli music search "关键词" --platform tidal --limit 5
```

* 平台列表里没有它：说明依赖或注册条件不满足（例如缺 `aigpy` 时 TIDAL 不会注册）。
* 能看到但搜索为空：先查凭据与网络。
* 能搜到但下载失败：看错误原文；「HLS 流暂不支持直链下载」「该平台未返回可下载直链」都属于当前版本的预期限制，不是网络故障。

依赖安装与自检命令见第 6 节。

---

## 11. 搜索结果点「下载」提示「站点和书籍 ID 不能为空」

**现象**：在搜索结果卡片点「下载」或「追更」，弹出「站点和书籍 ID 不能为空」，但卡片上明明有书名和站点。

**原因**：v1.28.1 之前的桌面页面把站点写在 `data-add` / `data-follow` 属性上，点击处理却读取 `dataset.site`，永远取到空字符串。v1.28.1 已改为读取 `dataset.add` / `dataset.follow`，下载和追更恢复正常。

**解决**：

1. 升级到 v1.28.1 或更高版本。侧栏底部的 `v...` 是当前运行版本；安装版和便携版并存时，确认启动的是刚升级的那一份。
2. 如果升级后仍出现，收集以下信息：
   * 侧栏底部显示的版本号；
   * 日志 `%LOCALAPPDATA%\novel_downloader\logs\novel-desktop.log`；
   * 出问题的站点 key、搜索关键词，以及同一关键词下是否所有结果都失败。
3. 如果只有某个站点的结果失败、其它站点正常，可能是该站点的搜索结果本身没有返回书籍 ID（卡片的 `data-bid` 为空）。空参数在进入任务队列前就被拦下，日志中不会出现 `Failed to queue`；这类情况请在 Issue 中附上站点和关键词。

---

## 12. 音乐歌单导出返回 400 / 音乐任务返回 429 {#music-playlist-export}

**现象**：`POST /api/v1/music/export` 返回 `400`；或 `POST /api/v1/music/jobs`
返回 `429`，即使并没有触发按 IP 的请求限流。

**先看 `detail`**（两种情况的消息都是固定的，不会回显你提交的路径）：

| `detail` | 含义 |
| --- | --- |
| `Music out_dir must be inside the music output directory` | 下载目录越界 |
| `Playlist out_path must be a .m3u/.m3u8 file inside the music output directory` | 歌单路径越界或后缀不合法 |
| `Too many music jobs are already running (limit 4); retry later` | 活跃音乐任务已达上限 |

**排查 `out_dir` / `out_path` 的 400**：

1. 允许写入的根目录只有两个：音乐设置里的 `out_dir`，以及平台默认音乐目录
   （通常是 `~/Music/novel_downloader`）。Windows 上音乐设置文件在
   `%LOCALAPPDATA%\novel_downloader\music\settings.json`，检查其中的 `out_dir`；
   设置 `NOVEL_MUSIC_HOME` 会整体替换音乐数据目录。

   ```bash
   # Git Bash
   cat "$LOCALAPPDATA/novel_downloader/music/settings.json"
   ```

2. 把 `out_dir` 改成上述根目录内的绝对路径，或直接省略该字段使用默认目录。
   相对路径是相对**默认目录**解析的，不是相对 API 进程的当前目录。
3. `out_path` 只要带后缀，就必须是 `.m3u` / `.m3u8`；`.txt`、`.json` 等会被拒绝。
   不带后缀的值按目录处理。`..`、指向根目录之外的软链接、以及根目录之外的绝对路径
   都会被拒绝，即使目标目录确实存在。
4. 如果返回的是 `500` 而不是 `400`，说明路径已通过校验但写入失败（权限、磁盘、
   目录被占用）。`detail` 只含异常类名（`"Could not write playlist: <类名>"`），
   具体原因在服务端日志里。

**排查 `429`**：

1. 确认 `detail` 是 `Too many music jobs...` 而不是限流消息。两者都带
   `Retry-After` 头：任务上限的 `429` 固定为 `5` 秒（等待约一个下载槽位），
   限流 `429` 的值取决于限流窗口何时排空。
2. 同时 `queued` / `running` 的音乐任务最多 `4` 个（`done` / `failed` 不计数）。
   先轮询已有任务，等它们进入终态后再提交：

   ```bash
   curl -s "$BASE/api/v1/music/jobs/<job_id>" -H "Authorization: Bearer $TOKEN"
   ```

3. 批量下载也可以改用同步端点 `POST /api/v1/music/download`（会阻塞到全部完成），
   或拆成更小的批次。

---

## 13. 自动更新拒绝 http 资产 / 下载被清理

**现象**：桌面端更新提示 `Refusing to download an update from an insecure source`；
`novel-cli update --yes` 以退出码 `1` 结束；`novel-cli update --json --yes`
输出里 `error` 为 `insecure-url`、`missing-digest` 或 `no-cli-asset`；下载中断后
`updates\` 里留下 `.part` 文件。

**机制**（桌面端与 CLI 共用 `apps/common/updater.py`）：

* `novel-cli update` 显式请求 `shape="cli"`，只接受 `novel-cli-<version>-<platform>.zip`。
  即使清单/API 只返回桌面安装包，也会丢弃该回退并停止，不会下载或运行
  `NovelDownloader-setup-*.exe`。对应地，`--json` 的 `asset_shape` 在选中 CLI 资产时
  为 `cli`，没有 CLI 资产或检查失败时为 `null`。
* 发布清单提供的资产 URL 必须是 `https://`。CLI 在询问/下载之前就拒绝明文地址；
  桌面端同样拒绝。
* 下载器本身只接受 `http://` / `https://`；如果 HTTPS 请求被重定向到明文 HTTP，
  会直接拒绝（校验和只能保证完整性，不能保证保密性）。
* 单个资产上限 `1 GiB`（`1024**3` 字节），超过即拒绝，避免被篡改的清单/镜像写满磁盘。
* 下载失败时：非续传模式在**下一次重试**前删除 `.part`；启用续传时保留已下载前缀，
  下次用 `Range` 继续；SHA-256 校验失败则一定删除 `.part`（旧版本残留没有继续使用的价值）。
  所有重试都失败后，未完成文件可能留在 `updates\`，桌面端会在 1 天后自动清理。

**排查**：

1. `insecure-url`：确认用的是官方发布页或 HTTPS 镜像。`NOVEL_UPDATE_MIRROR`
   是用户自己配置的前缀，但镜像若把 HTTPS 重定向到 HTTP，也会在下载阶段被拒绝。
2. `missing-digest`：该 release 没有为你的平台资产提供 SHA-256，客户端不会下载
   无法校验的文件；到发布页手动下载并核对摘要。
3. `no-cli-asset`：当前平台没有匹配的 CLI 资产（`novel-cli-*.zip`，例如发布漏传或架构不受支持）。CLI 不会回退到桌面安装包；请改用 `pip install -U novel-downloader`，或到发布页手动下载对应 CLI zip。
4. 反复校验失败：关闭应用后删除 `%LOCALAPPDATA%\novel_downloader\updates\`
   下对应的 `.part` 再重试；桌面端也会自动清理超过 1 天的 `.part`。
5. 需要机器可读原因时用 `novel-cli update --json --yes`：它不会进入交互，
   下载失败的原因在 `error` 字段里，`asset_shape` 标明实际选择的资产形态
   （CLI 只会是 `cli` 或 `null`；只加 `--json` 等价于 `--check`，不会尝试下载）。

### 13.1 清单验签失败（启用签名后）

**现象**：`novel-cli update` 打印 `Update check failed: untrusted-manifest: …`，或
`error` 为 `untrusted-manifest: …` / `manifest-unavailable`；桌面端侧栏不出现「有新版本」，
也没有任何错误提示。

**机制**：`TRUSTED_UPDATE_KEYS` 非空时，清单是唯一信任锚，且**不再回退** GitHub API。
`untrusted-manifest: …` 表示清单拿到了但不可信（未签名、未知 `key_id`、签名不符）；
`manifest-unavailable` 表示根本没拿到（release 没带 `latest.json`、仍是草稿、网络失败）。
桌面端不把检查错误显示成提示条，所以这两种情况在 UI 上都表现为「没有更新」。

**排查**：先看清单里的公开字段（`key_id` 与是否有 `signature`），再确认 `key_id` 与内置
公钥的摘要前 16 位一致，最后用公钥复核签名。完整步骤、症状对照与「不要做什么」见
[更新清单签名 § 故障排查](update-signing.md#signing-troubleshooting)。不要为了让检查通过而清空
`TRUSTED_UPDATE_KEYS`，也不要打印私钥。

---

## 14. 清理或恢复前先确认会删什么

`clean cache`、`clean book` 与 `storage --clean` 都支持 `--dry-run`: 只报告路径与占用,
**不删除任何文件**, 并与真实删除共用同一套路径校验 (越界站点名、保护目录同样报错)。

```bash
novel-cli clean cache --site qidian --dry-run
novel-cli clean book --site qidian 1010868264 --all --dry-run
novel-cli storage --clean --dry-run
```

脚本消费时加 `--json` (必须与 `--dry-run` 同用), 输出单行
`{"dry_run", "command", "site", "book_id", "book_ids", "items", "paths", "files", "bytes"}`。

如果章节数据库已被误删, 可用备份恢复:

```bash
# 先备份所有章节数据库
novel-cli storage backup --out ~/novel-backups

# 只读校验 raw_data 里所有在用数据库 (不修改任何文件)
novel-cli storage verify

# 从备份恢复; 覆盖已有数据库需要 -y
novel-cli storage restore --from ~/novel-backups/<site>/<book>/chapter.raw.<时间戳>.sqlite --force
```

`restore` 会先校验备份文件 (完整性、schema 版本、必需列) 再原子替换; 覆盖已有数据库时
旧库会先保存为同目录的 `*.pre-restore-*.bak`。目标路径必须位于 `raw_data_dir` 内。

`storage backup` 默认在写入后重新读取每个副本并做完整性/schema 校验; 任一失败会删除本次运行写出的整组备份并以 `1` 退出, 源库和已有旧备份都不动。只有备份由更新版本写入、当前版本无法校验 (新 schema) 时才用 `--no-verify`; 这样的副本恢复前要自行用 `sha256sum` / `PRAGMA integrity_check` 确认。

`storage verify` 在终端把逐库进度写到 stderr, 按 Ctrl+C 会停止并打印已查汇总后以 `1` 退出; `--json` 始终只在 stdout 输出一行, 中断时带 `"interrupted": true`。

备份只含 `raw_data` 下的章节数据库, 不含 Cookie/token、`settings.toml`、日志或媒体文件;
恢复后仍需重新登录, 且备份是明文, 请按敏感数据保管。完整的
[「备份 (写入后自动校验) → 恢复」流程](./cli-examples.md#storage-subcommand)
见 CLI 用法示例。

定时备份时可用 `--keep` 自动轮转。先预览会删哪些旧备份，确认后再真正执行：

```bash
# 只报告会删除的旧备份，新备份仍会写入，零删除
novel-cli storage backup --out ~/novel-backups --keep 7 --dry-run

# 每本书、每个 stage 只保留最新 7 份
novel-cli storage backup --out ~/novel-backups --keep 7
```

`--keep` 只删除 `--out` 下严格匹配 `chapter.<stage>.<UTC 时间戳>.sqlite` 的文件，目录、符号链接、手工改名或旧版本留下的文件都不会被碰；本次运行刚写出的备份永远受保护。`--keep 0` 关闭清理，负数或以 `--dry-run` 单独使用会以用法错误 (`2`) 退出。清理失败时命令以 `1` 退出，但已完成的备份保留。

---

## 15. 搜索过滤后没有结果

**现象**：加了 `--min-words` / `--max-words` / `--updated-after`（或 API 的 `min_words` / `max_words` / `updated_after`）后，结果从很多变成 0，但书名明明存在。

**原因**：过滤只保留**能解析出对应字段**的行。启用字数过滤时，站点没返回 `word_count` 或格式无法解析的行会被丢弃；启用日期过滤时，没有 `update_date` 的行同样被丢弃。这是有意的「宁缺毋滥」，不是请求失败。

**验证与解决**：

1. 先去掉过滤，确认书本身能搜到：

   ```bash
   novel-cli search "书名" --site <site>
   ```

2. 逐项加上过滤，定位是哪一项把结果筛掉：

   ```bash
   # 先不带过滤
   novel-cli search "书名" --site <site>

   # 只加日期
   novel-cli search "书名" --site <site> --updated-after 2024-01-01

   # 只加字数
   novel-cli search "书名" --site <site> --min-words 300000
   ```

3. 注意单位与边界：字数是**按本**的数值（`"30万字"` 会解析为 300000），上下界都含边界；`--updated-after` 是**含当天**的下界，格式必须严格为 `YYYY-MM-DD`，否则命令以 `2` 退出而不是静默忽略。
4. 跨站合并 (`--group`) 与 `--limit` 在过滤之后执行，因此过滤会让最终行数变少是预期行为。

---

## 16. 音乐下载提示「单曲 / 整批超过大小上限」 {#music-size-limit}

**现象**：某首歌在逐曲结果或批量汇总里显示 `单曲超过大小上限（1.0GB），已删除不完整文件`；或整批在若干首之后开始失败，并显示 `整批下载超过大小上限（8.0GB），已删除不完整文件`。

**原因**：音乐引擎有两道硬上限——单曲 1 GiB、整批 8 GiB（一次 `download_many` / `download_mixed` 调用内所有工作线程与平台回退共享）。它们用于阻止失效直链或异常 CDN 无限写入。命中后当前曲目的 `.part` 会被删除，且这类失败不会重试。

**排查与处理**：

1. 单曲超限通常意味着直链返回的不是音频（例如被重定向到错误页，但 `Content-Type` 未暴露），或文件确实异常巨大。先换一个来源或平台重试；聚合搜索的备选来源会按顺序尝试，但同样受上限约束。
2. 整批超限说明本次任务计入预算的下载量已接近 8 GiB。分批下载（减少一次提交的曲目数）即可继续；已经成功的文件不受影响。
3. 传输失败或取消留下的 `.part` 会保留用于续传，但不占用整批预算；删除它们不影响已完成的下载。若某个 `.part` 本身已超过单曲上限，下次下载会先删除它再重新开始。
4. CLI、桌面端、Android 独立模式和 REST API 没有调整这两个上限的参数，始终使用默认值；只有以 Python 库方式调用时才能通过 `DownloadOptions(max_track_bytes=..., max_batch_bytes=...)` 覆盖。详见 [音乐下载](./music.md) 的「下载大小上限」小节。

---

## 17. Windows 下 CLI 输出乱码 / JSON 解析失败

**现象**：把 `novel-cli` 的输出重定向到文件或管道给脚本后，中文变成乱码（如 `锟斤拷`、`????`），`jq`、`json.loads` 或 `ConvertFrom-Json` 报解析错误；直接看终端却正常。

**原因**：v1.34.1 起，冻结 CLI 在非 TTY 下把 stdout/stderr 统一写成 UTF-8（见 [CLI 用法 § 输出编码契约](./cli-examples.md#output-encoding-contract)）。旧的消费方仍按系统代码页（cp936/GBK）或 Windows PowerShell 5.1 的默认编码读取，就会把 UTF-8 字节解错。终端里正常，是因为 TTY 保持控制台原生编码，不走重定向路径。

**解决**：

1. 确认版本不低于 v1.34.1，并使用官方冻结 CLI；旧版只有机器可读的 stdout（`--json`/补全脚本）是 UTF-8。
2. Windows PowerShell 5.1 直接管道时，先设置控制台输出编码：

   ```powershell
   [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
   $r = novel-cli search --json "三体" | ConvertFrom-Json
   ```

3. 先写文件再读时，`Get-Content` 必须显式指定编码：

   ```powershell
   novel-cli search --json "三体" | Set-Content -Encoding UTF8 result.json
   Get-Content -Encoding UTF8 result.json | ConvertFrom-Json
   ```

4. Python 调用方显式传 `encoding="utf-8"`（不要依赖 `locale.getpreferredencoding()`）；读取 CLI 写出的文件时用 `open(..., encoding="utf-8")`。
5. `cmd.exe` 下直接 `type` 文件仍可能按当前代码页显示乱码；`chcp 65001` 只影响显示，文件内容已经是 UTF-8。

---

## 18. 桌面设置导入被拒绝 / 导入后设置未生效 (Desktop settings import)

**现象**：点「导入设置」后卡片状态行或 Toast 报错；或提示导入成功，但某些设置看起来没变。

**导入被拒绝**：导入在写文件前完成全部校验，被拒绝时现有配置**没有任何改动**，也不会生成 `.bak`。按报错逐项检查：

1. 路径为空、文件不存在或指向目录：填写完整的 JSON 文件路径，不要填文件夹。
2. 文件超过 1 MiB：偏好文件通常只有几 KB，多半是误选了别的大文件；重新导出。
3. 报「不是合法 JSON」：文件必须是 UTF-8（可带 BOM），不要用 UTF-16，也不要用残缺或手工拼接的 JSON。
4. 报版本或字段错误：只接受 `"app": "novel-downloader"`、`"kind": "desktop-preferences"`、`"format_version": 1` 的文件；未知字段、缺少白名单键、`"false"` 这类字符串开关值都会被拒绝。不要手工往 `settings` 里加代理 / 账号字段。
5. 用文本编辑器改过导出文件后，先确认引号、逗号和括号完整；拿不准就重新导出一份再对比。

**导入后设置未生效**：导入只覆盖白名单里的六个偏好（主题、三个更新开关、`search_filters`、`search_sort`），并且是合并写入。以下内容本来就不会随导入改变：

* 输出目录、代理、音乐账号 token、界面语言——它们存在 `settings.toml` 或独立状态文件里，需在设置页手动修改；
* 不在白名单内的其他偏好键保持原值；
* 导入会立即刷新主题和设置卡片，但不会重启进程；如果界面仍显示旧值，切到别的页面再回到设置页，或重启应用后确认。

导入成功后，卡片状态行会显示「已从 <路径> 导入设置」，并给出备份文件路径。

**`.bak` 的用途**：每次真正走到写入阶段的导入，都会先把当前偏好文件复制为同目录的 `desktop_prefs.json.bak`（原文件名加 `.bak`），因此可用它回滚**最近一次**导入。它只有一份，会被下一次导入覆盖；导入被校验拒绝时不会生成。需要回滚时，先退出桌面端（避免进程内状态覆盖），把 `.bak` 复制回 `desktop_prefs.json` 再启动。偏好文件默认位于应用数据目录的 `music/` 下（Windows 通常为 `%LOCALAPPDATA%\novel_downloader\music\`），设置 `NOVEL_MUSIC_HOME` 时以该目录为准。

完整契约与白名单见 [设置项参考 § 桌面设置导出 / 导入](./settings-reference.md#settings-export-import)。

---

## 19. API 返回 304 但没有看到更新 {#api-304-no-update}

**现象**：轮询 `GET /api/v1/history`（或 `/sites`、`/music/platforms`）时收到 `304 Not Modified`、响应体为空，客户端仍显示旧数据；或者刚导出了新文件，`ETag` 看起来没有变化。

**先分清 304 与缓存**

| 你看到的现象 | 实际发生了什么 | 有响应体吗 |
| --- | --- | --- |
| 请求没有到达服务器，界面直接复用旧结果 | 上述参考端点的 `200` 带 `Cache-Control: private, max-age=60`；60 秒内浏览器或本地缓存可以不再询问服务器 | 有，来自本地缓存 |
| 请求到达服务器，返回 `304 Not Modified` | 请求带了 `If-None-Match`，服务器重新生成当前响应并计算摘要，结果与你保存的校验器逐字节相同 | **没有** |
| 请求到达服务器，返回 `200` | 内容变了、校验器不匹配，或请求没带 `If-None-Match` | 有，完整响应 |

`304` 只表示「当前表示与这个 `ETag` 相同」。它既不是「有新版本」，也不是「服务器跳过了查询」：路由仍然执行，只是不传输响应体。客户端收到 `304` 必须复用**上次保存的响应体**；把 `304` 当空 JSON 解析，表现就是「没有更新」。

**哪些端点支持**

只有下面三个幂等 `GET` 端点参与条件请求；路径必须匹配（末尾的 `/` 会被规范化），查询参数（如 `/history?limit=20`）是表示的一部分：

| 端点 | 内容 | 什么时候会变 |
| --- | --- | --- |
| `GET /api/v1/sites` | 搜索/下载支持的站点表 | 插件集合或版本变化；与任务、搜索结果无关 |
| `GET /api/v1/history` | `general.output_dir` 下已导出的 `.txt` / `.epub` 文件列表 | 文件新增/删除，或名称、大小、`mtime` 改变 |
| `GET /api/v1/music/platforms` | 已注册的音乐平台与说明 | 音乐依赖或平台注册变化；与歌曲搜索/下载结果无关 |

`/api/v1/tasks*`、`/api/v1/login*`、`/api/v1/music/jobs*`、`/api/v1/watchlist*`、`POST /api/v1/search` 和 `/downloads/*` **不参与**，不会发送 `ETag`，也不会返回 `304`。条件请求只处理 `GET`，所以不要用 `curl -I`（`HEAD`）探测 `ETag`。内嵌引擎（桌面端 / Android 独立模式）也只为这三个路径提供同样的强 `ETag` 与 `304`。完整契约见 [本地 API 服务 § 条件请求](./api.md#conditional-requests)。

**确认条件请求是否真的在工作**

先发一次**不带** `If-None-Match` 的请求，保存响应头与响应体：

```bash
BASE=http://127.0.0.1:8080

# 配置了 token 时，给下面每条 curl 都加上 -H "Authorization: Bearer $TOKEN"
curl -sS -D /tmp/novel-h1.txt -o /tmp/novel-h1.json "$BASE/api/v1/history"
grep -i '^etag:' /tmp/novel-h1.txt
```

拿到 `ETag` 后再发条件请求：

```bash
etag=$(grep -i '^etag:' /tmp/novel-h1.txt | sed 's/^[^:]*: //' | tr -d '\r')
curl -sS -D - -o /dev/null -H "If-None-Match: $etag" "$BASE/api/v1/history"
```

预期结果：

- 内容未变：状态行 `304`，**没有** `Content-Length` / `Content-Type`，仍带 `ETag`、`Cache-Control: private, max-age=60`、`X-Request-ID` 与安全响应头；
- 内容已变：`200` 与新 `ETag`；
- 若 `200` 里根本没有 `ETag`：路径不在上表，或响应超过 512 KiB（此时不缓冲、不哈希）；
- 若 `200` 带 `ETag` 但条件请求仍是 `200`：校验器不匹配，或该 `ETag` 是路由自己设置的（路由级校验器优先，中间件不会改写它，也不会据此返回 `304`）；
- 想确认中间件是否生效，可临时用 `-H 'If-None-Match: *'`：对参与端点的一次成功响应（小于 512 KiB 且没有路由级 `ETag`）应得到 `304`（`*` 匹配任意当前表示）。

**内容为什么看起来没变**

1. **端点选错了**：`/history` 只列**已导出**的 `.txt` / `.epub` 文件，不反映下载中/未导出的任务，也不反映搜索或听歌记录。任务进度看 `GET /api/v1/tasks/{task_id}`（该端点不返回 `304`）；`/sites` 与 `/music/platforms` 只在注册表变化时改变。
2. **文件没有进入历史**：导出目录与 API 进程读取的 `general.output_dir` 不一致、文件后缀不是 `.txt` / `.epub`，或导出任务尚未完成，列表与 `ETag` 都不会变。用不带条件的 `200` 响应对照 `total` 与 `files`：

   ```bash
   curl -sS "$BASE/api/v1/history" | jq '{total, files: .files[0:3]}'
   ```

3. **查询参数不同**：`/history?limit=5` 与 `/history?limit=20` 是不同表示，各自的 `ETag` 不能混用。客户端应按**完整 URL（含查询串）**保存校验器与响应体。
4. **文件被原地替换但大小和 `mtime` 都没变**：`/history` 的表示只含名称、后缀、大小与 `mtime`；三者都相同就会得到同一个响应，`304` 是正确结果。若确实需要反映内容变化，请让导出器重写文件并更新时间。
5. **浏览器缓存而不是 304**：DevTools 里显示 "(disk cache)" / "from cache" 且没有请求时，是 `max-age=60` 在起作用，不是服务器返回 `304`。用 `curl`、无痕窗口或禁用缓存复测，等待 60 秒后再看。
6. **客户端把 304 当成了空响应**：这是「304 但没有更新」最常见的原因。收到 `304` 时应继续使用本地缓存的 body；只有收到 `200` 时才替换 body 与 `ETag`，并且只在响应完整读取成功后更新缓存。

**相关**：[本地 API 服务 § 响应头与 CORS 契约](./api.md#conditional-requests) 说明了 `ETag` 的生成方式、`If-None-Match` 的弱比较/列表/`*` 规则，以及 512 KiB 上限。

---

## 20. 桌面窗口位置或大小异常 {#desktop-window-trouble}

**现象**：桌面端启动后窗口完全在屏幕外、跑到已拔掉的显示器上、只剩一条标题栏，或每次都以异常大小打开；取消最大化后窗口变成全屏大小。

**启动时如何恢复**（值来自桌面偏好文件 `desktop_prefs.json` 的 `window_*` 键）：

| 存储状态 | 启动行为 |
| --- | --- |
| 五个 `window_*` 键都不存在 | 先尝试旧版遗留的 `window_state.json`；也没有则使用默认 `1220×800`，位置由窗口管理器决定，不最大化 |
| 宽度/高度缺失、非整数、≤ 0、过大，或小于 `980×640` | 该维度回退到默认值（宽 `1220` / 高 `800`） |
| 窗口尺寸大于当前虚拟桌面 | 收缩到虚拟桌面宽/高；但不会低于 `980×640`，所以屏幕本身更小时仍可能超出 |
| 只有 `window_x` 或只有 `window_y` | 两个都丢弃，位置交给窗口管理器 |
| `window_x` / `window_y` 是整数 | 保留；负值合法，表示主屏左侧/上方的显示器 |
| 保存的窗口矩形与所有已连接显示器都不相交 | 在主屏居中打开（典型场景：保存位置所属的显示器已拔掉） |
| 窗口只有一部分在屏幕外 | 移回虚拟桌面范围内 |
| `window_maximized` 是 JSON `true` | 以最大化启动；它与尺寸/位置独立，即使尺寸键缺失也会最大化 |

显示器边界只在 **Windows** 上查询（`GetSystemMetrics` 的虚拟桌面与主屏尺寸）。其他平台或取不到指标时，启动会直接使用保存的 `x` / `y`，不做屏外校正；如果窗口完全在屏外，只能按下面的方法重置。

**保存时机**：拖动/缩放期间最多每秒写一次；最大化、还原和关闭窗口时强制写入；值与上次相同则不重复写。窗口最大化时保存的是**最大化前的普通尺寸**，因此取消最大化会回到原尺寸，而不是「全屏大小的普通窗口」。保存是尽力而为：偏好文件不可写时只记日志，不会阻止窗口操作。

**重置方法（优先用设置页按钮）**

1. 打开 **设置 → 窗口（Window）**，点击 **重置窗口位置与大小**。它只删除 `desktop_prefs.json` 里的五个 `window_*` 键，主题、更新开关、搜索筛选等其余偏好保持不变。
2. 当前窗口**不会立刻**移动或还原；默认 `1220×800`、位置由窗口管理器决定、不最大化，这些在**下次启动**时生效。点击成功后，本次运行剩余时间不再保存窗口几何，因此关闭窗口不会把旧几何写回去。
3. 重启桌面端。如果窗口仍在旧位置/旧大小，检查偏好文件同目录是否有旧版本留下的 `window_state.json`：重置按钮**不删除**它，而它会在五个 `window_*` 键都不存在时作为兼容回退被读取。完全退出桌面端后删除该文件，再启动一次。
4. 只想局部重置（不删全部五个键）时，完全退出桌面端后手动编辑 `desktop_prefs.json`：Windows 默认在 `%LOCALAPPDATA%\novel_downloader\music\`；设置 `NOVEL_MUSIC_HOME` 时以该目录为准。只删 `window_x` / `window_y` 会让系统重新选位置，但保留大小与最大化状态；只删 `window_maximized`（或设为 `false`）可单独取消最大化。运行中编辑会被关闭窗口时的写入覆盖。
5. 只有在愿意重置该文件中的**所有**桌面偏好（可能包括主题、更新开关、代理密码等历史键）时，才直接删除整个 `desktop_prefs.json`；删除前先备份。
6. 重新启动桌面端。

如果每次启动都回到默认大小，说明状态没有写成功：检查偏好文件是否只读、被同步盘/杀毒软件锁住；把日志级别调到 `DEBUG` 后查找 `Could not persist desktop prefs`。

**相关**：[设置项参考 § 桌面窗口位置与大小记忆](./settings-reference.md#desktop-window-geometry) 列出这些键的类型、默认值与导入/导出行为。

---

## 21. 结果很少、为空或排序不符 {#search-guide}

这三类问题先看 [搜索指南](./search.md)，它按处理顺序说明了关键词规范化、查询意图、排序、过滤与缓存：

- **结果很少或为空** → [搜索指南 § 结果很少或为空](./search.md#few-or-no-results)：先看汇总行的 `completed / failed / skipped`，再检查过滤条件、浏览器辅助站点与 `--site` 拼写。
- **排序不符合预期** → [搜索指南 § 排序看起来不对](./search.md#order-looks-wrong)：`quality` 是综合分而非纯相关度；用 `--json` 看 `rank_score`、`match_tier` 与 `query_intent`，或显式换用 `--sort title|author|words|update`。
- **相关度下限与过滤细节** → [排序](./search.md#ranking)、[过滤](./search.md#filters)。
- **重复搜索仍访问站点** → [缓存](./search.md#cache)，以及上文「搜索结果缓存」。

CLI 的完整选项与示例见 [CLI 用法 § search 子命令](./cli-examples.md)；REST 请求形状、`total`/`results`/`query_intent` 与流式搜索见 [本地 API 服务 § 搜索](./api.md)。

---

## 22. 磁盘满 / 权限不足 / 文件被占用 {#disk-permission-locks}

这三类问题都发生在**写盘**这一步：`ENOSPC` 是卷或配额已满，`EACCES` / `EPERM` 是没有写权限，Windows 上还常见文件被其它进程或杀毒软件短暂占用。先确认是哪一种，再按下面的顺序处理。备份、校验与容量规划的完整说明见[存储与备份](./storage.md)和[性能与容量](./performance.md#disk-backup)，本节只讲排障与恢复。

### 22.1 `ENOSPC`：空间或配额不足 {#enospc}

**现象**：命令或任务以 `OSError: [Errno 28] No space left on device`、Windows `[WinError 112] 磁盘空间不足`、或 macOS 的配额错误结束。不同入口的表现：

| 入口 | 表现 | 已经写入的内容 |
| --- | --- | --- |
| `storage backup` | 写快照临时文件时失败；`--json` 输出一行 `ok:false`，`error` 为稳定分类（磁盘满常见 `io_error` 或 `database_error`），退出码 `1` | 本次运行写出的整组副本被回滚删除；源库与旧备份不动 |
| `storage restore` | 写快照或 `.pre-restore-*.bak` 时失败；退出码 `1` | 已安装的目标回滚，原库（或 `.pre-restore` 副本）保留 |
| 音乐下载 | 逐曲结果标为失败（`retryable: true`），批量任务继续处理其余曲目 | 保留 `<目标>.part`，下次运行按 `Range` 续传；不会被当成已完成 |
| `config import` / 桌面设置导入 | 报 `Could not write ...` / 「无法导出设置」，退出码 `1` | 临时文件删除，原 `settings.toml` / `desktop_prefs.json` 保持不变 |
| 桌面自动更新 | 新传输开始前就比较声明的总大小与剩余空间，不够时直接拒绝；传输中满盘会重试 | 保留可续传的 `.part`；更新目录中 `.part` 超过 1 天、完整安装包超过 7 天会被清理 |
| TXT / HTML 导出 | 原子写入失败并报错 | 临时文件删除，上一次导出的成品保持原样 |

**确认**：

```bash
novel-cli doctor --json | jq '.checks[] | select(.key == "disk_space")'
novel-cli storage --json        # 四个数据目录各自的 free_bytes 与 top
```

`doctor` 的 `disk_space` 检查**输出目录所在卷**，低于 512 MiB 时给出警告；它是建议项（`fatal: false`），只提示，不影响退出码。`storage` 的 `free_bytes` 按目录分别统计，空间不足往往只发生在 `raw_data`、音乐目录或更新目录所在的那个卷。Windows 上还要看资源管理器里的磁盘配额、OneDrive 的「释放空间」状态和虚拟磁盘容量。

**处理**：

1. 先预览清理，再执行：

   ```bash
   novel-cli clean cache --dry-run
   novel-cli clean book --site qidian <book_id> --all --dry-run
   novel-cli storage --clean --dry-run
   novel-cli doctor --fix --dry-run   # 只清理输出/缓存/日志目录下 7 天前的 .tmp/.part
   ```

   `doctor --fix` 不会碰 `raw_data`、备份目录或音乐目录；它只创建缺失的输出/缓存/日志目录，并删除这些目录里过期的临时文件。

2. 把备份目录算进容量。`storage backup` 是完整快照，不是增量；每次运行至少需要与源库总大小相当的空闲空间，`--keep N` 的占用约为 `N × 单库大小`。轮转旧备份前先预览：

   ```bash
   novel-cli storage backup --out /path/to/backups --keep 7 --dry-run
   novel-cli storage backup --out /path/to/backups --keep 7
   ```

   `--keep` 只删除 `--out` 下严格匹配 `chapter.<stage>.<UTC 时间戳>.sqlite` 的旧文件；本次运行写出的备份永远受保护。

3. 换卷或换更短的根目录：把 `raw_data_dir` / `output_dir` / `cache_dir` 指到空间充足的本地盘，或用 `NOVEL_HOME` 整体迁移。不要在下载、备份或恢复运行期间移动这些目录。

4. 处理 `.part`：音乐和更新下载的 `.part` 是**有意的断点**，空出空间后重跑同一命令会续传；确认没有进程在使用且不再续传时才手工删除。`storage backup` 的正常失败（含 Ctrl+C）会删除本次写出的副本；进程被强杀后，下一次备份会在同一目录清理残留的占位和临时文件。输出/缓存/日志目录里的过期临时文件可交给 `doctor --fix`。`doctor --fix` 不扫描备份目录，那里的残留需要手工删除。

### 22.2 `EACCES` / `EPERM`：没有权限或文件被占用 {#permission-denied}

**现象**：`PermissionError: [Errno 13] Permission denied`；Windows 常见 `[WinError 5] 拒绝访问`、`[WinError 32] 另一个程序正在使用此文件，进程无法访问`。也可能表现为「无法创建临时文件」或「无法重命名」。

**常见原因**（按出现频率）：

- 目录或文件只读：ACL 没有写权限、文件带只读属性、卷以只读方式挂载；
- Windows 上目标文件被另一个进程打开：编辑器/阅读器预览、同步盘（OneDrive/坚果云）、杀毒软件实时扫描、搜索索引器；
- 受控文件夹访问（Controlled Folder Access）或企业策略阻止写入；
- 用管理员 / `sudo` 运行过一次，之后普通用户无法写同一目录；或目录属于另一个用户。

**确认**：

```bash
# POSIX：看目录权限与当前用户，再实际试写一个文件
ls -ld raw_data downloads
id
touch raw_data/.write-test && rm raw_data/.write-test

# Windows PowerShell：看 ACL 和只读属性
Get-Acl .\raw_data | Format-List
attrib .\raw_data
```

`novel-cli doctor --json` 的 `storage` 检查会给出 `writable`、`wal_read_only` 和具体建议；输出/缓存/日志目录的检查会实际写一个探测文件，失败时直接报告 `EACCES`。Windows 上可用资源监视器（`resmon`）的「关联的句柄」搜索文件路径，确认是哪个进程持有句柄。

**恢复**：

1. 关闭可能占用文件的程序（编辑器、预览、同步客户端），暂停同步，或把数据目录加入杀软 / 索引器排除项；受控文件夹访问需要显式允许。
2. 去掉只读属性并恢复写权限：Windows 用 `attrib -R <path>`、`icacls <path> /grant <user>:(OI)(CI)M`；POSIX 用 `chmod u+w` 或修正 ACL。不要用 `sudo novel-cli` 写用户目录，那会把新文件变成 root 所有。
3. `raw_data` 里的数据库只读且旁边有 `-wal` 时，先恢复该库的写权限再启动；SQLite 需要写 WAL 才能 checkpoint。之后运行 `novel-cli storage verify`，确认只读期间没有漏掉已提交的数据。
4. 权限修好后重跑原命令即可；已完成的下载/导出会跳过或覆盖，不会重复计费。

**关于「半文件」**（按当前实现）：

- 配置导入/导出、桌面设置导入、TXT/HTML 导出、`storage backup` / `restore` 都走「同目录临时文件 → `fsync` → `os.replace`」；出错时删除临时文件（进程被强杀时可能残留 `.tmp`，但不影响旧文件），**目标文件保持旧版本**，不会出现截断的成品。
- 章节库写入使用 SQLite 事务（`BEGIN IMMEDIATE`，失败时 `ROLLBACK`），失败的批次不会留下半行数据；`-wal` / `-shm` 旁文件可能正常存在，不要手工删除正在使用的它们。
- 音乐和更新下载只在完整校验后把 `.part` 改名为最终文件；失败留下的 `.part` 是续传点，不是可用的成品。
- 例外在重命名这一步：Windows 上目标文件只读时 `os.replace` 会失败。`storage backup` / `restore` 对杀软或索引器造成的短暂共享冲突（`WinError 5` / `32` / `33`）会在约 5 秒内重试；其它写入直接报错，旧文件仍然完整。`config init --force` 会在写之前检测只读目标并拒绝。

### 22.3 `MAX_PATH` 与中文路径 {#long-path}

**现象**：Windows 报 `[WinError 206] 文件名或扩展名太长`，或 POSIX 报 `ENAMETOOLONG`；导出或音乐保存失败，但同一本书换个目录就成功。

**原因**：Windows 默认把完整路径限制在 260 个 UTF-16 单元（`MAX_PATH`），除非系统启用长路径；POSIX 限制的是单个路径分量 255 **字节**。中文在 UTF-8 中通常占 3 字节/字，所以同一串中文名在 Linux/macOS 上更容易撞到分量上限；Windows 的 260 则按 UTF-16 单元计算。

**当前实现已经做的缩短**：

- TXT 导出把「父目录 + 文件名」控制在 240 字符以内（给原子写入的 `.<name>.XXXXXXXX.tmp` 留余量），并把单个文件名限制在 200 UTF-8 字节；HTML 导出同样按 240 字符预算缩短名称，章节名另有更紧的上限（72 字符，随输出目录变深继续缩小）；
- 音乐下载按 UTF-8 字节限制单个文件名（主体最多 240 字节，完整文件名最多 255 字节），但不缩短父目录；输出根目录很深时仍可能超过 Windows 的 260；
- `sanitize_filename` 替换 Windows 非法字符，并按平台上限截断。

因此普通长书名通常不会失败；仍然报错时，多半是**根目录本身太深**，或媒体、封面等导出文件名之外的路径过长。

**处理**：

1. 把数据目录换到短根路径（如 `C:\nd`、`D:\books`），或用 `NOVEL_HOME` 迁移应用主目录；这比逐本书改名更可靠。
2. Windows 10 1607+ 可启用长路径（注册表 `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled=1`，重启生效），但不要依赖它：第三方工具、压缩包解压和部分 API 仍可能按 260 处理，保持短路径最稳。
3. 中文路径本身不需要改成 ASCII：Python 和本项目按 Unicode 处理。若只是终端显示乱码，按[§4 路径包含中文或空格](#path-chinese-spaces) 切换 UTF-8 代码页或把数据目录指到纯 ASCII 路径；若确实写入失败，优先缩短目录层级与文件名前缀。
4. 同步盘（OneDrive 等）会额外占用路径长度并可能锁定文件；长路径加同步目录是最容易同时触发 `ENAMETOOLONG` 和 `EACCES` 的组合。

### 22.4 与 `storage verify`、`doctor` 配合 {#disk-doctor-storage}

磁盘满或权限问题处理完后，按这个顺序确认数据没有损坏：

```bash
# 1. 环境与余量：disk_space / storage 两类检查
novel-cli doctor --json | jq '.checks[] | select(.key == "disk_space" or .key == "storage")'

# 2. 只读校验所有在用章节库
novel-cli storage verify

# 3. 需要机器可读结果时（stdout 只有一行）
novel-cli storage verify --json
```

- `doctor` 的 `storage` 行是**采样**：最多检查 3 个数据库（全部 ≤ 1 MiB 时全量），只读打开，不创建 `-wal` / `-shm`，也不读章节正文；它报告 `writable`、`corrupt`、`wal_read_only`、`free_bytes`。要覆盖全部库，用 `storage verify`。
- `storage verify` 是只读的完整扫描；发现损坏时退出码非 `0`，`--json` 仍只输出一行（中断时带 `"interrupted": true`）。它不会替你修复。
- 确认损坏后，按[存储与备份 § 从备份恢复一个数据库](./storage.md#restore-one) 操作：先 `storage backup` 到另一个卷（源库仍可读时），再 `storage restore --from ... --force`。`restore` 会先校验每个备份、原子替换，并在失败时回滚整组；覆盖前的旧库保存在同目录的 `.pre-restore-*.bak`。
- `storage backup` 的 `--keep` 与默认写后校验的完整语义见[存储与备份 § 备份](./storage.md#storage-backup)；容量估算与 `doctor` 字段见[性能与容量 § 磁盘与备份](./performance.md#disk-backup) 和 [§ doctor](./performance.md#doctor)。

---

## 23. 仍有问题？

带上下面的信息提交 Issue：

```bash
novel-cli --version
python -c "import platform, novel_downloader; print(platform.platform()); print(platform.python_version()); print(novel_downloader.__version__)"
tail -n 200 logs/novel_downloader.log
```

同时说明：目标站点、执行的完整命令、`settings.toml` 中相关片段（请先删除账号、Cookie、token 等敏感信息），以及是否能稳定复现。