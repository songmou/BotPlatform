# 网站资料抓取

`web_crawler` 面向管理员配置的 HTTP/HTTPS 网站，适合把固定站点持续转换为
可追溯正文、结构化记录和知识库来源。它与一次性的 `web_research` 并存：前者
有持久队列、增量更新和版本历史，后者适合临时搜索与单页调研。

## 启用

插件默认禁用。在平台插件中心启用 `web_crawler` 后，完整停止旧进程并重启。
组织侧入口为 `/organization/crawler`。浏览器动态回退由本插件直接复用
Playwright，不要求同时启用 `browser_automation` 插件。

## 抓取边界

- 仅接受无凭据的 HTTP/HTTPS URL；可访问公网、内网和本机回环服务，但始终拒绝链路本地、云元数据、组播、未指定及保留地址。每次请求和重定向都会重新检查地址。
- 强制读取 `robots.txt`：404 表示无规则，401/403 表示禁止，超时和 5xx 会重试。
- 每个主机最多一个并发请求，默认请求间隔至少 1 秒，并尊重更长的 crawl-delay。
- 只发现配置范围内的 HTML 与 PDF；URL 会移除片段和常见跟踪参数后去重。
- 429、5xx 和网络超时最多执行三次指数退避；下载和正文长度均受平台设置限制。
- 首版不处理登录、验证码、Office 文档、代理池、反爬绕过或分布式队列。

## 提取模板

一个抓取源可配置多个模板，每项包含：

```json
{
  "name": "延时行情",
  "url_pattern": "/h5_sjzx/yshq$",
  "schema": {
    "type": "object",
    "properties": {
      "contract": {"type": "string"},
      "latest": {"type": "number"}
    },
    "required": ["contract", "latest"],
    "additionalProperties": false
  },
  "fields": {
    "contract": {"selector": ".contract"},
    "latest": {"selector": ".latest"}
  }
}
```

HTML 字段使用 CSS `selector`，可选 `attribute`、`all` 和二次 `regex`；PDF 或
正文使用 `regex` 与可选 `group`。确定性规则先执行，仅缺失的必填字段会交给
模型补全。最终对象必须通过模板 JSON Schema；提取方式、模型运行编号和校验
错误都会保存在结构化记录中。

## 保存与分析

SQLite 保存抓取源、运行、队列、页面、记录和事件。原始 HTML/PDF 位于
`data/users/<tenant_id>/plugins/web_crawler/snapshots/`，默认每页只保留最近五个
发生变化的版本；组织删除时数据库外键和租户目录共同完成清理。管理页可查看
运行进度、失败重试、页面正文差异、字段表格和数值趋势。

绑定目标知识库后，每个页面以 `web` 来源写入并按规范 URL 覆盖更新。知识检索
和 `crawler_query` 均返回来源 URL 与抓取时间，智能体回答时应同时引用这两项。
