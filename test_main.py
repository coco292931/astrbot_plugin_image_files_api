#!/usr/bin/env python3
"""image_files_api 自测：缓存库路径兜底 + 补丁生命周期。

覆盖：

1. ``_resolve_db_path``：空白值、未替换的 ``${plugin_data}`` 占位符、
   ``%(x)s`` / ``{{x}}`` 模板串都必须回退 ``DEFAULT_DB``；正常路径原样保留。
2. ``FileIdCache`` 构造：任何一种脏路径都不能再抛 ``FileNotFoundError``。
3. ``ProviderRequest.assemble_context`` 补丁：安装、幂等、热重载换实例、
   ``terminate`` 还原。

运行::

    python test_main.py
    python test_main.py -v
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = "ifa_under_test"
DATA_DIR = Path(tempfile.mkdtemp(prefix="ifa_test_"))


# ---------------------------------------------------------------------------
# AstrBot 桩
# ---------------------------------------------------------------------------


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        try:
            self.warnings.append(str(msg) % a if a else str(msg))
        except Exception:
            self.warnings.append(str(msg))

    def error(self, *a, **k):
        pass


LOGGER = _Logger()


def _filter_factory(*args, **kwargs):
    def decorator(func):
        return func

    return decorator


class _Star:
    def __init__(self, context):
        self.context = context


class _ProviderRequest:
    """对齐 astrbot.api.provider.ProviderRequest 的可打补丁形态。"""

    def __init__(self, image_urls=None, prompt="hi"):
        self.image_urls = list(image_urls or [])
        self.prompt = prompt

    async def assemble_context(self):
        blocks = [{"type": "text", "text": self.prompt}]
        for url in self.image_urls:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": "user", "content": blocks}


def _install_stubs() -> None:
    if getattr(sys.modules.get("astrbot"), "_ifa_stub", False):
        return

    def make(name, **attrs):
        mod = types.ModuleType(name)
        mod.__path__ = []
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    make("astrbot", _ifa_stub=True)
    api = make("astrbot.api", logger=LOGGER)
    make(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        filter=types.SimpleNamespace(on_llm_request=_filter_factory),
    )
    make("astrbot.api.provider", ProviderRequest=_ProviderRequest)
    make(
        "astrbot.api.star",
        Context=type("Context", (), {}),
        Star=_Star,
        register=lambda *a, **k: (lambda cls: cls),
    )
    core = make("astrbot.core")
    core.__path__ = []
    utils = make("astrbot.core.utils")
    utils.__path__ = []
    make(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_plugin_data_path=lambda: str(DATA_DIR),
    )
    agent = make("astrbot.core.agent")
    agent.__path__ = []
    make("astrbot.core.agent.message", ContentPart=type("ContentPart", (), {}))


_install_stubs()


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(HERE)]
    sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.main")


plugin_mod = _load()
FileIdCache = plugin_mod.FileIdCache
ImageFilesApiPlugin = plugin_mod.ImageFilesApiPlugin
DEFAULT_DB = plugin_mod.DEFAULT_DB
resolve_db_path = plugin_mod._resolve_db_path
cfg_int = plugin_mod._cfg_int


class _Ctx:
    def get_using_provider(self):
        return None


def _make_plugin(config=None) -> ImageFilesApiPlugin:
    return ImageFilesApiPlugin(_Ctx(), config or {})


# ---------------------------------------------------------------------------
# 一、缓存库路径兜底
# ---------------------------------------------------------------------------


class TestResolveDbPath(unittest.TestCase):
    def test_empty_falls_back(self):
        for value in ("", "   ", None, 0):
            with self.subTest(value=value):
                self.assertEqual(resolve_db_path(value), DEFAULT_DB)

    def test_placeholder_falls_back(self):
        for value in (
            "${plugin_data}/cache.db",
            "%(plugin_data)s/cache.db",
            "{{plugin_data}}/cache.db",
            "/x/%(data)s/cache.db",
        ):
            with self.subTest(value=value):
                self.assertEqual(resolve_db_path(value), DEFAULT_DB)

    def test_normal_path_kept(self):
        path = os.path.join(str(DATA_DIR), "sub", "cache.db")
        self.assertEqual(resolve_db_path(path), path)

    def test_relative_path_kept(self):
        self.assertEqual(resolve_db_path("./cache.db"), "./cache.db")


class TestFileIdCacheInit(unittest.TestCase):
    def test_empty_path_does_not_raise(self):
        cache = FileIdCache("")
        self.assertEqual(cache.path, DEFAULT_DB)
        self.assertTrue(cache.get("nope") is None)

    def test_placeholder_path_does_not_create_junk_dir(self):
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp(prefix="ifa_cwd_"))
            cache = FileIdCache("${plugin_data}/cache.db")
            self.assertEqual(cache.path, DEFAULT_DB)
            self.assertFalse(
                os.path.exists("${plugin_data}"),
                "不能在工作目录下建出 ${plugin_data} 垃圾目录",
            )
        finally:
            os.chdir(cwd)

    def test_sweep_and_put_roundtrip(self):
        db = os.path.join(str(DATA_DIR), "rt", "cache.db")
        cache = FileIdCache(db)
        cache.put("digest", "file-abc", 10, None)
        self.assertEqual(cache.get("digest"), "file-abc")
        self.assertEqual(cache.sweep(), 0)


class TestCfgInt(unittest.TestCase):
    def test_bad_values_fall_back(self):
        for value in (None, "", "abc", [], {}):
            with self.subTest(value=value):
                self.assertEqual(cfg_int(value, 100), 100)

    def test_minimum_enforced(self):
        self.assertEqual(cfg_int(-1, 100, minimum=0), 100)
        self.assertEqual(cfg_int(0, 100, minimum=0), 0)

    def test_valid_value_kept(self):
        self.assertEqual(cfg_int("3600", 100), 3600)


# ---------------------------------------------------------------------------
# 二、补丁生命周期
# ---------------------------------------------------------------------------


class TestAssembleContextPatch(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        # 兜底：任何用例失败都强制还原类级补丁，避免污染其他用例
        PR = plugin_mod.ProviderRequest
        if getattr(PR, "_ifa_patched", False):
            original = getattr(PR, "_ifa_original", None)
            if original is not None:
                PR.assemble_context = original
                del PR._ifa_patched
                del PR._ifa_original
        plugin_mod._ACTIVE["plugin"] = None

    def _install(self, plugin):
        plugin._client = object()  # 只要有值就走上传分支
        plugin._install_patch()

    async def test_patch_installed_and_idempotent(self):
        p1 = _make_plugin()
        self._install(p1)
        first = plugin_mod.ProviderRequest.assemble_context
        self._install(p1)
        self.assertIs(plugin_mod.ProviderRequest.assemble_context, first)

    async def test_patch_points_to_latest_instance(self):
        p1 = _make_plugin({"skip_extensions": ".gif"})
        self._install(p1)

        # 模拟热重载：新实例接管
        p2 = _make_plugin({"skip_extensions": ".png"})
        self._install(p2)
        self.assertIs(plugin_mod._ACTIVE["plugin"], p2)

    async def test_terminate_restores_original(self):
        original = getattr(
            plugin_mod.ProviderRequest, "_ifa_original", None
        ) or plugin_mod.ProviderRequest.assemble_context

        p = _make_plugin()
        self._install(p)
        self.assertIsNot(plugin_mod.ProviderRequest.assemble_context, original)

        await p.terminate()
        self.assertIs(plugin_mod.ProviderRequest.assemble_context, original)
        self.assertFalse(getattr(plugin_mod.ProviderRequest, "_ifa_patched", False))

    async def test_after_terminate_request_is_untouched(self):
        p = _make_plugin()
        self._install(p)
        await p.terminate()

        req = _ProviderRequest(image_urls=["/tmp/a.jpg"])
        result = await req.assemble_context()

        # 还原后走原生实现：image_urls 不被摘除
        self.assertEqual(req.image_urls, ["/tmp/a.jpg"])
        self.assertEqual(result["content"][1]["type"], "image_url")

    async def test_no_active_plugin_delegates_to_original(self):
        p = _make_plugin()
        self._install(p)
        plugin_mod._ACTIVE["plugin"] = None

        req = _ProviderRequest(image_urls=["/tmp/a.jpg"])
        result = await req.assemble_context()

        self.assertEqual(req.image_urls, ["/tmp/a.jpg"])
        self.assertEqual(result["content"][1]["type"], "image_url")


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
