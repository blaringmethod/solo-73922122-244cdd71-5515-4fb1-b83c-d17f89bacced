# 方志馆藏公告抓存索引 (Gazetteer Web-Archive Index)

地方志编纂员用于回答「馆藏公告的正文在哪两次抓存之间改过」的查询服务。
FastAPI 提供查询接口，SQLite 保存抓存索引、分段响应与摘要校验结果，
原始 WARC 卷留档于磁盘。

一条公告的 HTTP 响应可能被拆进几卷 WARC（WARC 1.1 记录分段：首段是
`response`，其余是 `continuation`，以首段的 `WARC-Record-ID` 经
`WARC-Segment-Origin-ID` 关联）。服务按段入库，续段可以先于首段入馆；
拼接成完整响应并通过核验后，该抓存才为 `/changes`、也为同摘要的重访作证。

## 运行

```bash
docker compose up --build        # 服务监听 :8000
# 打开 http://127.0.0.1:8000/docs  (Swagger UI)
```

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
DATA_DIR=./data uvicorn app.main:app --port 8000
python -m pytest tests/ -q       # 自测
```

## 演示

```bash
docker compose up --build -d             # 启动服务
python scripts/make_demo_warcs.py demo   # 生成演示 WARC 卷（含分段卷）
scripts/demo_flow.sh                     # 默认打 http://127.0.0.1:8000
```

在 <http://127.0.0.1:8000/docs> 打开 Swagger UI，即可用「Try it out」逐步
复现同一流程：

- `PUT /volumes/{volume}/warc` 上传 `demo/*.warc`（`{volume}` 填 `vol1`、
  `vol2`、`vol1b`、`seg-head`、`seg-tail`、`seg-witness` 等）；
- 用 `GET /segments?record_id=urn:uuid:5e6f7a8b-0001-4000-8000-000000000001`
  或路径形式 `GET /segments/{record_id}` 查段表（尖括号可省）；
- `GET /changes?url=…` 看区间与缺证；`DELETE /volumes/{volume}` 撤卷。

脚本分两条线：

1. **换卷前后查区间**：上传 vol1（t1=V1, t2=V2 两个 response）与 vol2
   （t3=revisit→V2, t4=V3, t5=摘要损坏的 response, t6=revisit→V1）→ 查
   `/changes` 得换卷前区间 (t1→t2, t3→t4, t4→t6，缺证 t5) → 用 vol1b
   （只含 t1=V1）整卷替换 vol1 → 再查得 t1→t4, t4→t6，缺证变为 t3+t5
   （vol2 的 t3 重访因最后一份 V2 正文被移除而失去证据）→ 跨卷同刻冲突
   返回 409 且旧卷保留。
2. **分段响应的缺段、补齐、撤段**：
   - 先传 seg-head（只有首段）→ `GET /segments?record_id=<首段 ID>` 显示
     `incomplete / missing_last_segment`，首段抓存与 seg-witness 里同摘要的
     重访都缺证；
   - 再传 seg-tail（末段，带 `WARC-Segment-Total-Length`）→ 状态转为
     `verified`，两段分处两卷，抓存与重访恢复作证；
   - 删除 seg-tail 卷 → 复核后回到 `incomplete`，首段与仅靠它作证的抓存
     都转为缺证。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `PUT` | `/volumes/{volume}/warc` | 按卷号上传**未压缩** WARC 1.0/1.1（multipart 文件）；同号卷整卷替换，失败保留旧卷 |
| `GET` | `/volumes` | 列出馆藏卷（文件名、SHA-256、记录数、段数、入库时间） |
| `DELETE` | `/volumes/{volume}` | 移除整卷，并复核受影响的 segmented response |
| `GET` | `/segments?record_id=…` | 凭首段 `WARC-Record-ID`（可带尖括号，也接受任一段的 ID）查各段段号、所在卷、核验状态 |
| `GET` | `/segments/{record_id}` | 同上，ID 放在路径里（便于 `/docs` 演示） |
| `GET` | `/series` | 列出全部 segmented response 及其核验状态（含已撤除的墓碑行） |
| `GET` | `/changes?url=…` | 对指定 URL 按时刻比较相邻两次**有证据**的抓存，正文不同即返回这两个时刻；另列缺证抓存 |

`GET /segments` 响应（核验通过时）：

```json
{
  "record_id": "urn:uuid:…",              // 查询用的记录 ID
  "origin_id": "urn:uuid:…",              // 首段记录 ID（续段的 Origin-ID）
  "status": "verified",                   // verified|pending|incomplete|invalid|absent
  "verified": true,
  "reason": null, "detail": null, "problems": [],
  "url": "…", "capture_ts": "…", "digest": "…",
  "first_segment_volume": "seg-head",
  "http_status": 200,
  "declared_total_length": 117,           // 末段 WARC-Segment-Total-Length
  "assembled_length": 117,                // 实际拼接长度
  "capture": {"url": "…", "ts": "…", "volume": "seg-head", "valid": 1, …},
  "segment_count": 2,
  "missing_segments": [],                 // 1..最大段号之间缺失的段号
  "segments": [
    {"segment_number": 1, "present": true, "volume": "seg-head",
     "warc_type": "response", "record_id": "…", "content_length": 60,
     "declares_total_length": null, "verification": "verified"},
    {"segment_number": 2, "present": true, "volume": "seg-tail",
     "warc_type": "continuation", "record_id": "…", "content_length": 57,
     "declares_total_length": 117, "verification": "verified"}
  ]
}
```

`status` 含义（逐段 `verification` 取整个序列的状态，段不单独算作证）：

| status | 含义 |
|---|---|
| `verified` | 序列完整且全部核验通过，首段抓存可作证 |
| `incomplete` | 缺首段、缺末段或段号有缺口（`missing_segments` 列出缺号，`reason` 给出原因） |
| `invalid` | 序列自相矛盾：段号重复、多个末段、多个首段、网址不一致、拼接长度不符、正文摘要不符等 |
| `pending` | 卷变更已提交、复核尚未完成（正常请求后不会停留在此状态） |
| `absent` | 该序列的段已全部撤除（`segments` 为空） |

`/changes` 响应：

```json
{
  "url": "…",
  "changes": [{"from_ts": "…", "to_ts": "…", "from_digest": "…", "to_digest": "…"}],
  "unevidenced": ["2025-03-01T09:00:00Z"],
  "captures": [{"ts": "…", "type": "response|revisit", "digest": "…",
                "evidenced": true, "volume": "vol1", "http_status": 200,
                "segment_origin": "urn:uuid:…|null", "segment_status": "verified|null"}]
}
```

## 处理规则

- **记录范围**：只处理带 `WARC-Payload-Digest: sha1:…` 的 `response`、
  `revisit` 与 WARC 1.1 的 `continuation` 记录；其余类型（warcinfo/request/
  metadata 等）与其他摘要算法跳过并计数。WARC 1.0 记录上的分段字段不予索引。
- **HTTP 正文限无编码**：`Content-Encoding`（非 identity）或非 chunked 的
  `Transfer-Encoding` 一律跳过；`Transfer-Encoding: chunked` 先还原分块再校验。
  分段序列在拼接之后再套用同一规则。
- **摘要校验**：response 的正文 SHA-1（base32）与卷头摘要一致才可作证
  （`valid=1` 存入 SQLite）；不一致仍为抓存但缺证。
- **分段响应入库**：首段（`WARC-Segment-Number: 1` 的 `response`）产生一条抓存，
  同时产生段行；`continuation` 只产生段行，可先于首段到达。序列以首段的
  `WARC-Record-ID` 为键，与后续段用 `WARC-Segment-Origin-ID` 对上。
- **分段核验**（全部满足才 `verified`）：恰有一个末段（唯一带
  `WARC-Segment-Total-Length` 的 continuation，且必须是最大段号）；段号自首段
  到末段连续、无重复；各段 `WARC-Target-URI` 一致；按段号拼接后的内容长度等于
  末段声明的总长；拼接出的 HTTP 响应的正文通过首段 `WARC-Payload-Digest` 的
  SHA-1 校验。段号重复、多个末段、多个首段、网址不一致、长度不符、摘要不符
  判为 `invalid`；缺首段、缺末段、段号有缺口判为 `incomplete`——两者都不作证。
- **分段证据与重访**：只有 `verified` 的分段抓存才为 `/changes` 和同摘要的
  revisit 作证（revisit 的证据仍按现存卷内 `valid=1` 的 response 摘要判定，
  故分段抓存核验通过后自动为其他卷的重访作证）。
- **按需复核**：卷替换/删除只复核受影响（新旧卷涉及的）段序列，其他卷的段
  不重扫、不阻塞。段行与 `pending` 标记在写事务内提交；拼接与哈希在事务外
  进行。复核结果带序列版本号（`revision`）乐观提交：若期间该序列又被改动，
  迟到的结果被丢弃，不会复活证据。启动时补齐上次中断留下的 `pending`。
- **撤段与缺证**：序列变为 `incomplete`/`invalid`/`absent` 的同一事务里，
  首段抓存 `valid` 置 0，`/changes` 立即把它和仅靠它作证的重访列为缺证；
  迟到的核验不能恢复这份证据。
- **重访证据**：revisit 只有在**现存卷**中存在通过校验的同摘要 response
  正文时才可作证。该判定在查询时对联（部分索引
  `idx_verified_response_digest`），因此移除最后一份匹配正文后，其他卷的
  重访自动失去证据，且换卷无需重扫他卷。
- **时刻唯一**：`(url, ts)` 为全库主键（UTC 规范化后的 ISO-8601）。同号卷
  替换时与本卷旧记录不冲突；与他卷冲突或文件内重复 → `409`，整卷不入库。
- **整卷替换**：新卷先完整解析校验，再在单个事务里「删旧卷行 + 冲突检查 +
  插新行 + 更新卷目」，任一步失败即回滚，旧卷（索引与留档文件）原样保留；
  原卷文件先写临时文件 fsync，事务提交后才 `os.replace` 落位。
- **换卷代价**：删除按卷索引、插入与冲突检查只触及新旧两卷的记录数；
  重访证据不在入库时物化，索引在换卷后立即可查。
- **查询一致性**：每次查询在单个 SQLite 读事务内完成，只看到一个已提交状态
  （WAL 模式，写入不阻塞读取）。

## 存储

```
$DATA_DIR/
├── index.sqlite3        # volumes / captures / segments / series 四张表
└── volumes/{卷号}.warc   # 原卷留档（上传字节原样保存）
```

`segments` 保存每段的原始内容与段号，因此拼接不依赖原卷文件仍在馆；
`series` 保存每个分段序列的核验状态、结果与版本号（撤除后保留墓碑行，
版本号不回退）。旧版数据库启动时自动补列迁移。

## 已知限制

- 上传整体读入内存解析，未做流式分块（超大卷需调内存/改为流式）。
  段内容也整段存入 SQLite，大段会放大索引体积。
- 单文件 SQLite，适合单实例；多实例部署需换外部数据库。
- 段核验在写事务之后同步执行：同一卷内若有大量分段的超大序列，单次上传
  的响应时间由拼接与哈希耗时决定（但不持有写锁，其他卷仍可提交）。
- 分段 `revisit` 不索引（WARC 规范不建议对 revisit 分段）；仅支持 WARC 1.1
  的分段字段。
- URL 精确匹配，不做规范化（大小写、默认端口等不合并）。
- 未鉴权，默认部署在内网。
