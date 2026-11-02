"""image_files_api —— 把发给模型的图片从 base64 换成 Files API 的 file_id

落点：`ProviderRequest.assemble_context()`（astrbot/core/provider/entities.py）。
这是整条链路上最后一个还看得见本地图片路径、又真正决定请求块长相的地方；
再往后就是 data URI 了，拦不到。

做法：给它打一次类级补丁。本地路径先上传换 file_id，从 image_urls 里摘掉，
再让原方法照常拼文本块，最后把 file 块补进 content。

- 图片按内容 sha1 缓存 file_id，同一张图不重复上传；
- GIF 按文件头识别，直接放行；
- 任何一步失败都退回原方法，不影响正常对话。
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from astrbot.core.agent.message import ContentPart

    class FilePart(ContentPart):  # type: ignore[misc]
        """给 Message 校验用的 file 内容块，注册进 ContentPart 的类型表。"""

        type: str = "file"
        file_id: str

    _HAS_FILE_PART = True
except Exception:  # pragma: no cover - 老版本没有 ContentPart 时退化
    _HAS_FILE_PART = False

PLUGIN_NAME = "astrbot_plugin_image_files_api"
DEFAULT_DB = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME, "cache.db")
LOG_PATH = os.path.join(
    get_astrbot_plugin_data_path(), PLUGIN_NAME, "image_files_api.log"
)
TAG = "[image_files_api]"

_PLACEHOLDER_MARKS = ("${", "%(", "{{")
"""AstrBot 不支持 schema 占位符，未替换的模板串必须识别出来并回退默认。"""

_ACTIVE: dict[str, Any] = {"plugin": None}
"""当前生效的插件实例；补丁通过它取配置与 client，热重载后自动指向新实例。"""


def _resolve_db_path(raw: Any) -> str:
    """把配置里的缓存库路径规范化。

    AstrBot 的 ``_config_schema_to_default_config`` 只是原样拷贝 schema 的
    default，**不做任何变量替换**。因此像 ``${plugin_data}/cache.db`` 这样的
    模板串会被原样拿到，直接使用会在工作目录下建出名叫 ``${plugin_data}``
    的目录。这里统一识别未替换的占位符与空白值，回退到 ``DEFAULT_DB``。

    Args:
        raw: 配置里的原始值。

    Returns:
        可直接交给 ``sqlite3.connect`` 的路径。
    """
    text = str(raw or "").strip()
    if not text or any(mark in text for mark in _PLACEHOLDER_MARKS):
        return DEFAULT_DB
    return text


def _cfg_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    """安全地把配置值转成 int，失败或越界时回退默认。"""
    try:
        num = int(value)
    except Exception:
        return default
    if minimum is not None and num < minimum:
        return default
    return num


def _sha1_of(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except Exception:
        return None


def _trace(payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


class FileIdCache:
    def __init__(self, db_path: str):
        # 空值/占位符/无目录部分都必须兜底：os.path.dirname("") 是 ""，而
        # os.makedirs("") 会抛 FileNotFoundError: [Errno 2] ... ''。
        self.path = _resolve_db_path(db_path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS file_ids ("
            "digest TEXT PRIMARY KEY, file_id TEXT NOT NULL, size INTEGER, "
            "updated_at TEXT, expires_at TEXT)"
        )
        try:
            self._conn.execute("ALTER TABLE file_ids ADD COLUMN expires_at TEXT")
        except Exception:
            pass
        self._conn.commit()

    def get(self, digest: str) -> str | None:
        row = self._conn.execute(
            "SELECT file_id, expires_at FROM file_ids WHERE digest = ?", (digest,)
        ).fetchone()
        if not row:
            return None
        file_id, expires_at = row
        if expires_at:
            try:
                if datetime.now(timezone.utc) >= datetime.fromisoformat(expires_at):
                    self._conn.execute("DELETE FROM file_ids WHERE digest = ?", (digest,))
                    self._conn.commit()
                    return None
            except Exception:
                pass
        return file_id

    def put(self, digest: str, file_id: str, size: int, expires_at: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO file_ids (digest, file_id, size, updated_at, expires_at) "
            "VALUES (?, ?, ?, datetime('now'), ?)",
            (digest, file_id, size, expires_at),
        )
        self._conn.commit()

    def sweep(self) -> int:
        removed = self._conn.execute(
            "DELETE FROM file_ids WHERE expires_at IS NOT NULL "
            "AND datetime(expires_at) <= datetime('now')"
        ).rowcount
        self._conn.commit()
        return removed


@register(
    "astrbot_plugin_image_files_api",
    "koko",
    "把请求里的图片从 base64 改成 Files API file_id，减小请求体",
    "0.3.0",
)
class ImageFilesApiPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._cache: FileIdCache | None = None
        self._client = None
        self._last_expires: str | None = None

    def _cfg(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key)
        return default if value is None else value

    @property
    def cache(self) -> FileIdCache:
        if self._cache is None:
            self._cache = FileIdCache(_resolve_db_path(self._cfg("cache_db", "")))
        return self._cache

    def _is_skipped(self, path: str) -> bool:
        exts = [
            e.strip().lower()
            for e in str(self._cfg("skip_extensions", ".gif")).split(",")
            if e.strip()
        ]
        if os.path.splitext(path)[1].lower() in exts:
            return True
        try:
            with open(path, "rb") as fh:
                if fh.read(6) in (b"GIF87a", b"GIF89a"):
                    return True
        except Exception:
            pass
        return False

    async def _upload(self, path: str) -> str | None:
        if self._client is None:
            return None
        self._last_expires = None
        ttl = _cfg_int(self._cfg("ttl_seconds", 2592000), 2592000, minimum=0)
        try:
            with open(path, "rb") as fh:
                resp = await self._client.files.create(
                    file=fh,
                    purpose=self._cfg("upload_purpose", "user_data"),
                    extra_body={
                        "expires_after": {"anchor": "created_at", "seconds": ttl}
                    },
                )
            file_id = getattr(resp, "id", None)
            if file_id and ttl:
                # 配置值非法时不能把"上传已经成功"变成整体失败，
                # 这里只影响本地过期时间，回退默认即可。
                buffer_sec = _cfg_int(
                    self._cfg("local_expire_buffer_seconds", 86400),
                    86400,
                    minimum=0,
                )
                self._last_expires = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=max(60, ttl - buffer_sec))
                ).isoformat()
            return str(file_id) if file_id else None
        except Exception as exc:
            logger.warning(f"{TAG} 上传失败 {os.path.basename(path)}: {exc}")
            return None

    async def _resolve_id(self, path: str) -> str | None:
        digest = _sha1_of(path)
        if not digest:
            return None
        cached = self.cache.get(digest)
        if cached:
            return cached
        file_id = await self._upload(path)
        if file_id:
            try:
                self.cache.put(
                    digest, file_id, os.path.getsize(path), self._last_expires
                )
            except Exception:
                pass
        return file_id

    def _install_patch(self) -> None:
        # 热重载时旧实例的补丁会留在 ProviderRequest 上，闭包仍指向旧实例
        # （配置、client 都是旧的）。这里统一走模块级引用，重载后自动指向
        # 新实例。
        _ACTIVE["plugin"] = self
        if getattr(ProviderRequest, "_ifa_patched", False):
            return
        original = ProviderRequest.assemble_context
        ProviderRequest._ifa_original = original

        async def patched(req_self):
            plugin = _ACTIVE["plugin"]
            if plugin is None:
                return await original(req_self)
            paths = [
                str(u)
                for u in (getattr(req_self, "image_urls", None) or [])
                if str(u).startswith("/") and not plugin._is_skipped(str(u))
            ]
            if not paths or plugin._client is None:
                return await original(req_self)

            file_ids: list[str] = []
            for path in paths:
                file_id = await plugin._resolve_id(path)
                if not file_id:
                    return await original(req_self)
                file_ids.append(file_id)

            req_self.image_urls = [
                u for u in req_self.image_urls if str(u) not in paths
            ]
            result = await original(req_self)
            content = result.get("content") if isinstance(result, dict) else None
            if isinstance(content, list):
                for fid in file_ids:
                    block = {"type": "file", "file_id": fid}
                    if plugin._cfg("no_save", True):
                        block["_no_save"] = True
                    content.append(block)
            _trace({"hit": [os.path.basename(p) for p in paths], "ids": file_ids})
            return result

        ProviderRequest.assemble_context = patched
        ProviderRequest._ifa_patched = True
        _trace({"patched": "ProviderRequest.assemble_context"})

    async def terminate(self) -> None:
        """插件卸载时还原 ``ProviderRequest.assemble_context``。

        补丁是类级的，不还原会一直留到进程结束；若期间插件被卸载，
        闭包里的实例仍在，后续请求会走一套已失效的配置与 client。
        """
        # 只有当前实例仍是生效实例时才清空，避免"先加载新实例、后卸载旧实例"
        # 的顺序把新实例的补丁关掉。
        if _ACTIVE["plugin"] is self:
            _ACTIVE["plugin"] = None
        if not getattr(ProviderRequest, "_ifa_patched", False):
            return
        original = getattr(ProviderRequest, "_ifa_original", None)
        if original is None:
            return
        ProviderRequest.assemble_context = original
        del ProviderRequest._ifa_patched
        if hasattr(ProviderRequest, "_ifa_original"):
            del ProviderRequest._ifa_original
        _trace({"unpatched": "ProviderRequest.assemble_context"})

    @filter.on_llm_request()
    async def enable_files_api(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("enable", True):
            return
        provider = self.context.get_using_provider()
        if provider is None:
            return
        base = str(getattr(provider, "provider_config", {}).get("api_base") or "").lower()
        allow = str(self._cfg("allowed_api_base", "api.deepseek.com")).lower()
        if allow not in base:
            return
        client = getattr(provider, "client", None)
        if client is not None:
            self._client = client
        try:
            self.cache.sweep()
        except Exception:
            pass
        self._install_patch()
