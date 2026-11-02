## 0.4.2
- 识别未替换的 schema 占位符：`${plugin_data}` / `%(x)s` / `{{x}}` 一律回退默认路径，
  不再在工作目录下建出名叫 `${plugin_data}` 的垃圾目录
- `cache_db` 默认值改为空字符串，由代码统一解析到 `DEFAULT_DB`
- 日志路径不再硬编码 `/AstrBot/data/koko/logs/...`，改到插件数据目录
- 修复热重载后补丁仍持有旧实例：改用模块级引用，卸载时 `terminate` 还原
  `ProviderRequest.assemble_context`
- `local_expire_buffer_seconds` 非法时只回退该值，不再让"上传已成功"变成整体失败
- 补上 `no_save` 配置项（代码里一直在读，但 schema 里没有）

## 0.4.1
- 修复 cache_db 配成空字符串时 os.makedirs 崩溃，空值兜底到默认路径
