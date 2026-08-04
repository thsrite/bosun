# Spec：项目问题来源（外部拉取 → finding → 任务）

## 目标
每个项目可配置外部「问题来源」，拉取其中的问题/内容 → 进问题收件箱当 finding → 人勾选转可执行任务。
复用现有 finding → 收件箱 → to_task 管线；新增的是上游「可插拔连接器 + 授权」。

## v1 范围（已对齐）
- 只做**通用 HTTP/REST API** 一种连接器；连接器接口留好，Redis/数据库后续同接口加。
- 拉来条目 → `_add_finding` 进收件箱当 finding，人勾选转任务（复用人在环）。相关性靠**来源侧过滤/query 配置**，不加 LLM。
- 授权凭据**明文存 SQLite**（用户知情选择，本地单用户）；加**读取脱敏**（列表/详情不回传明文，掩码显示）降低泄露面。

## 数据模型
`issue_source` 表（新建）：
- `id` PK, `project_id`, `name`, `type`(默认 'http'), `enabled`(1), `config`(TEXT JSON), `created_at`

HTTP 连接器 config JSON：
```json
{
  "url": "https://api.example.com/issues",
  "method": "GET",                    // GET | POST
  "headers": {"X-Foo": "bar"},
  "query": {"state": "open"},         // 拼到 URL
  "body": {},                         // POST 时的 JSON body
  "auth_type": "none",                // none | bearer | api_key | basic
  "auth_header": "X-API-Key",         // api_key 时的头名
  "auth_credential": "***",           // 明文存库；读取时掩码
  "items_path": "data.issues",        // 点路径定位数组；"" = 根即数组
  "title_field": "title",
  "detail_field": "body",
  "severity": "info",                 // finding 默认严重度
  "attachment_url_template": "https://api.example.com/file/{filename}" // 可选；item.attachments 无 url 时使用
}
```
授权拼装：bearer→`Authorization: Bearer <cred>`；api_key→`<auth_header>: <cred>`；basic→`Authorization: Basic base64(<cred>)`(cred="user:pass")；none→无。

## 连接器抽象
`app/issue_sources.py`：
- `FETCHERS = {"http": fetch_http}`（注册表；Redis/DB 后续加同签名函数）
- `fetch_http(config) -> list[dict]`：发请求(urllib+certifi SSL)、按 `items_path` 取数组、按字段映射成 `{title, detail, severity, attachments}`
- `attachments`：读取 item 的 `attachments` 数组，支持 `{type,name,filename,url}`；无 `url` 时优先用 `attachment_url_template`，否则对 License Bug API 从 `/bugs` 推导 `/bug/file/{filename}`。
- `pull(source_id) -> int`：fetch → 下载附件到项目 `.bosun-uploads/issue-sources/{source_id}/{item_id}/`，把本地路径写入 finding.detail → 逐条 `discovery._add_finding(pid, f"ext:{name}", sev, title, detail)`（复用去重/失败记忆）→ emit findings.updated → 返回新增数
- `test(config) -> {count, sample}`：只 fetch 返回条数+前几条样例，不写库

附件同步到 task：finding 转任务时沿用 `detail`，因此 prompt 会保留「附件」清单和本地路径；下载失败时保留下载地址/失败原因，不阻断文本问题入库。

## 接口
- `GET  /api/projects/{pid}/sources`  列来源（凭据掩码）
- `POST /api/projects/{pid}/sources`  新建
- `PUT  /api/sources/{id}`            改（凭据留空=不变）
- `DELETE /api/sources/{id}`          删
- `POST /api/sources/{id}/pull`       立即拉取 → findings
- `POST /api/projects/{pid}/sources/test`  连接测试（不写库，返回样例）

## 前端
- 「问题来源」管理弹窗（项目级），入口放问题收件箱/项目页：
  - 来源列表（名称/类型/启用开关/上次拉取/立即拉取/编辑/删除）
  - 新增/编辑表单：名称、URL、method、headers/query、auth 类型+凭据、items_path、title/detail 字段、severity
  - 「测试」按钮：拉一次显示条数+样例，不写库
  - 「立即拉取」：写 finding，收件箱刷新

## 安全
- 凭据明文存库（知情）；**读取脱敏**：list/get 把 auth_credential 换 "***"；update 时凭据留空=保留原值
- 凭据不进日志；DB 在 ~/.bosun（不进 git 仓库）
- 附件文件写入项目 `.bosun-uploads/`，git 项目会幂等写入 `.gitignore`
- 外部输入边界：url/method 白名单校验，超时、大小限制（响应截断），SSRF 面用户自负（本地工具）

## 触发（v1 默认）
- 手动「立即拉取」按钮。定时拉取后续复用 policy 那套（非目标）。

## 非目标
- Redis / 数据库连接器（接口留好，后续）
- LLM 相关性筛选/归纳
- 定时自动拉取、分页/增量、OAuth 流程
