# astrbot_plugin_image_files_api

把发给模型的图片从 base64 data URI 换成 Files API 的 `file_id`，请求体里不再塞整张图。

## 为什么

AstrBot 默认会在请求前把每张图片转成 `data:image/...;base64,...` 塞进 content，
一张大图就能把请求体撑到几 MB。DeepSeek 的 chat/completions 有 48 MiB 请求体上限，
图片多的时候很容易撞线。

Files API 走的是另一条路：图片先上传换一个 `file-api-xxx` 编号，请求里只带编号。

## 怎么做的

落点是 `ProviderRequest.assemble_context()` —— 整条链路上最后一个还看得见本地图片
路径、又真正决定请求块长相的地方。再往后就只剩 data URI 了，拦不到。

插件给这个方法打一次类级补丁：

1. `image_urls` 里的本地路径先按内容 sha1 查缓存；
2. 没命中就上传，`purpose` 用 `user_data`（DeepSeek 只认这个）；
3. 命中的路径从 `image_urls` 摘掉，让原方法照常拼文本块；
4. 把 `{"type": "file", "file_id": "..."}` 补进 content。

为了让 AstrBot 内部的消息校验认这个新块，插件还向
`astrbot.core.agent.message.ContentPart` 的注册表里注册了一个 `file` 子类，
并给块带上 `_no_save`，落库时会被剔除。

## 细节

- **缓存**：sqlite，键是图片内容的 sha1，同一张图不重复上传。
- **时效**：上传时带 `expires_after`（默认 30 天，可配 1 小时到 30 天）；
  本地缓存比服务端早一天作废，免得拿刚过期的号去请求。
- **GIF 放行**：按文件头认，动图仍旧走 base64。
- **失败回退**：任何一步出岔子都退回原方法，不影响正常对话。

## 配置

面板里可改：

| 键 | 说明 | 默认 |
|---|---|---|
| `enable` | 总开关 | true |
| `allowed_api_base` | provider 的 api_base 含这个串才动手 | api.deepseek.com |
| `upload_purpose` | 上传用途 | user_data |
| `ttl_seconds` | 服务端有效期 | 2592000 |
| `local_expire_buffer_seconds` | 本地提前作废的秒数 | 86400 |
| `cache_db` | 缓存库路径 | /AstrBot/data/koko/data/image_files_api/cache.db |
| `skip_extensions` | 跳过的后缀 | .gif |

## 注意

- 只对配置里那个 api_base 生效，换别家 provider 请自行确认是否支持 `file` 块。
- DeepSeek 单用户配额是 25 GiB / 10000 个文件，记得让时效兜着。
