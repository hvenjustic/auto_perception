# 完成说明

## 1. 已实现功能

1. 三层感知架构已实现：
- 第一层（入口监控）：
  - 网页抓取通过 Firecrawl 单页抓取（`/v2/scrape`，用于首页 HTML）
  - RSS/Atom 自动探测与对比（`rss.xml` / `atom.xml` / `feed` + 首页 `link[rel=alternate]`）
  - `sitemap.xml` 解析与 `lastmod` 对比（支持 `sitemapindex` 递归）
  - 首页/列表页结构指纹对比（基于标题链接结构哈希）
- 第二层（深度扫描）：
  - 网页抓取通过 Firecrawl 单页抓取（候选页面抓取）
  - 对候选页面做核心内容提取（优先 `content_selector`，其次 `article/main/section/div`）
  - 对核心内容做 SHA-256 指纹对比
  - 保存页面快照用于后续对比
- 第三层（差异摘要）：
  - 使用 DeepSeek API 生成摘要
  - 使用固定提示词：`对比旧网页和新网页的差异，总结网页更新内容，仅输出总结部分`
  - DeepSeek 不可用时自动使用内置文本差异摘要兜底

2. 配置导入与状态持久化：
- 支持通过 API 导入站点配置（可全量替换）
- 本地持久化数据库：`SQLite`（默认文件 `data/state.db`）
- 兼容迁移：若存在 `data/state.json`，启动时会自动迁移到 SQLite
- 持久化内容包括：
  - 站点配置
  - 运行态指纹（RSS、Sitemap、首页哈希、页面哈希、页面快照）
  - 变更历史记录

3. 首次扫描基线逻辑：
- 首次扫描仅建立基线，不对“首次抓到的新页面”报变更
- 第二次及以后开始基于基线输出变化

## 2. 暴露的 Web API

1. `GET /healthz`
- 用途：健康检查
- 返回：`{"status":"ok"}`

2. `POST /api/v1/config/import`
- 用途：导入/更新监控站点配置
- 请求体：
```json
{
  "replace_all": false,
  "sites": [
    {
      "site_id": "demo",
      "name": "Demo Site",
      "url": "https://example.com",
      "content_selector": "article",
      "enabled": true,
      "max_pages_per_scan": 10
    }
  ]
}
```
- 返回：导入数量、站点 ID 列表、是否全量替换

3. `GET /api/v1/config/sites`
- 用途：查询当前站点配置列表
- 返回：站点配置数组

4. `GET /api/v1/monitor/urls`
- 用途：获取当前参与监测的 URL 数组（仅 `enabled=true`）
- 返回示例：
```json
{
  "urls": ["https://example.com", "https://news.ycombinator.com"]
}
```

5. `DELETE /api/v1/monitor/by-url`
- 用途：按 URL 删除监测条目
- 请求体：
```json
{
  "url": "https://example.com"
}
```
- 返回：删除数量、删除的站点 ID 列表
- 失败：若条目不存在或已删除，返回 404，提示 `条目不存在或已删除`

6. `POST /api/v1/scan/run`
- 用途：触发扫描
- 请求体：
```json
{
  "site_ids": ["demo"]
}
```
- 说明：`site_ids` 不传时扫描全部已启用站点
- 返回：本次扫描站点列表 + 本次检测到的变更列表

7. `GET /api/v1/changes?site_id=demo&limit=50`
- 用途：分页查看历史变更（按时间倒序）
- 参数：
  - `site_id`：可选，按站点过滤
  - `limit`：可选，默认 50，最大 500

8. `GET /api/v1/changes/{change_id}`
- 用途：查看单条变更详情
- 返回：指定变更记录；不存在返回 404

9. `GET /api/v1/result`
- 用途：获取最近一次监测任务结果汇总
- 返回字段：
  - `task_start_time`：任务开始时间
  - `task_end_time`：任务结束时间
  - `monitor_count`：监测数量
  - `detected_count`：已检测数量
  - `pending_count`：待检测数量
  - `changes`：变动列表（每项包含 `url` 和 `summary`）

## 3. 关键文件

- `app/main.py`：FastAPI 入口与路由
- `app/service.py`：业务编排（导入配置、执行扫描、记录变化）
- `app/monitor.py`：三层感知核心逻辑（入口监控 + 深度扫描）
- `app/summarizer.py`：LLM 摘要与兜底摘要
- `app/storage.py`：状态持久化
- `app/models.py`：数据模型
- `requirements.txt`：依赖清单

## 4. 运行方式

1. 安装依赖：
```bash
pip install -r requirements.txt
```

2. 启动服务：
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

3. （可选）配置 DeepSeek 摘要能力：
- `DEEPSEEK_API_BASE`：DeepSeek API 地址（默认 `https://api.deepseek.com`）
- `DEEPSEEK_API_KEY`：DeepSeek API Key
- `DEEPSEEK_MODEL`：模型名（默认 `deepseek-chat`）

4. （可选）配置数据库路径：
- `AUTO_PERCEPTION_DB_FILE`：SQLite 文件路径（默认 `data/state.db`）
- `AUTO_PERCEPTION_LEGACY_STATE_FILE`：旧 JSON 状态文件路径（默认 `data/state.json`）

5. （可选）配置 Firecrawl 抓取：
- `FIRECRAWL_API_KEY`：Firecrawl API Key（配置后启用 Firecrawl 单页抓取）
- `FIRECRAWL_API_BASE`：Firecrawl API 地址（默认 `https://api.firecrawl.dev`）
- `FIRECRAWL_TIMEOUT_MS`：Firecrawl 抓取超时毫秒（默认 `30000`）
- `FIRECRAWL_MAX_AGE_MS`：Firecrawl 缓存 TTL 毫秒（默认 `0`，尽量拿最新）
- `AUTO_PERCEPTION_ENV_FILE`：运行时配置文件路径（默认 `config/runtime.env`）
- 当前已写入配置文件：`config/runtime.env`
