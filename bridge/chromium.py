"""Chromium 安装统一编排 (r11: 后台自检与命令触发去重).

后台任务 (_auto_ensure_chromium) 与命令 (cmd_install_chromium) 共用
ensure_chromium(), 返回 (ok, messages) 逐步回显文本:
- 后台侧: messages 按前缀分级记日志 (✗→error / 失败·超时·异常→warning / 其余→info)
- 命令侧: messages 逐条 yield 给用户
"""

from __future__ import annotations

import asyncio
import os
import sys

# 镜像下载源 (npmmirror 优先, Azure 回退)
MIRRORS: list[tuple[str, str]] = [
    ("https://npmmirror.com/mirrors/playwright", "npmmirror"),
    ("https://playwright.azureedge.net", "Azure"),
]

# Chromium 运行所需系统库 (apt-get 补齐清单, 与 install-deps 交集)
SYSTEM_LIBS: list[str] = [
    "libnspr4",
    "libnss3",
    "libgbm1",
    "libasound2",
    "libxkbcommon0",
]

INSTALL_TIMEOUT = 600  # 浏览器下载/安装超时(秒)
DEPS_TIMEOUT = 600  # playwright install-deps 超时(秒)
APT_UPDATE_TIMEOUT = 300  # apt-get update 超时(秒)
APT_INSTALL_TIMEOUT = 600  # apt-get install 超时(秒)


def _is_root() -> bool:
    """root 判定 (Windows 无 geteuid → False)."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _log(log, msg: str) -> None:
    """按消息前缀分级记录日志 (后台侧)."""
    if log is None:
        return
    if msg.startswith("✗"):
        log.error(f"[ParserLite] {msg}")
    elif any(k in msg for k in ("失败", "超时", "异常", "无法启动")):
        log.warning(f"[ParserLite] {msg}")
    else:
        log.info(f"[ParserLite] {msg}")


async def _verify() -> bool:
    """BrowserManager 验证 Chromium 可启动 (复用上游单例)."""
    try:
        from nonebot_plugin_parser_lite.utils.browser import BrowserManager

        await BrowserManager.ensure_started()
        return True
    except Exception:
        return False


async def _download_chromium(browsers_path: str, messages: list[str]) -> bool:
    """镜像循环下载 (环境注入 PLAYWRIGHT_BROWSERS_PATH/PLAYWRIGHT_DOWNLOAD_HOST)."""
    for url, name in MIRRORS:
        env = os.environ.copy()
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = url
        if browsers_path:
            env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
        messages.append(f"尝试 {name} ({url}) ...")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=INSTALL_TIMEOUT
            )
            if proc.returncode == 0:
                messages.append(f"Chromium 安装完成 ({name})")
                return True
            err = stderr.decode(errors="replace").strip()[-300:]
            messages.append(f"Chromium 安装失败 ({name}): rc={proc.returncode} {err}")
        except asyncio.TimeoutError:
            messages.append(f"Chromium 安装超时 ({name}), 切换镜像...")
        except Exception as e:
            messages.append(f"Chromium 安装异常 ({name}): {e}")
    return False


async def _install_system_libs(messages: list[str]) -> bool:
    """系统库补齐: playwright install-deps 优先, apt-get 回退.

    P1-6: apt-get 输出量大, PIPE 缓冲会死锁 → 重定向到 DEVNULL.
    """
    from bridge.core import _detect_missing_libs

    missing = _detect_missing_libs()
    if not missing:
        return True
    messages.append(f"检测到缺失系统库, 尝试自动安装:\n{missing}")
    if not _is_root():
        messages.append(
            "✗ 非 root 用户无法安装系统库, 请在容器/服务器以 root 运行:\n"
            "  apt-get update && apt-get install -y "
            + " ".join(SYSTEM_LIBS)
            + "\n  或: python -m playwright install-deps chromium"
        )
        return False
    # ① playwright install-deps (全量依赖, 适配发行版包管理器)
    try:
        _deps_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install-deps",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _deps_out, _deps_err = await asyncio.wait_for(
            _deps_proc.communicate(), timeout=DEPS_TIMEOUT
        )
        if _deps_proc.returncode == 0:
            return True
        messages.append(
            f"playwright install-deps 失败 (rc={_deps_proc.returncode}), "
            f"回退 apt-get:\n{_deps_err.decode(errors='replace').strip()[-200:]}"
        )
    except asyncio.TimeoutError:
        messages.append("✗ playwright install-deps 超时, 回退 apt-get...")
    except Exception as e:
        messages.append(f"✗ 系统库安装异常: {e}, 回退 apt-get...")
    # ② 回退: 手写 apt-get 补齐核心库 (DEVNULL 防死锁)
    try:
        _apt1 = await asyncio.create_subprocess_exec(
            "apt-get",
            "update",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(_apt1.communicate(), timeout=APT_UPDATE_TIMEOUT)
        _apt2 = await asyncio.create_subprocess_exec(
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *SYSTEM_LIBS,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(_apt2.communicate(), timeout=APT_INSTALL_TIMEOUT)
        if _apt2.returncode != 0:
            messages.append(
                f"✗ apt-get 安装失败: rc={_apt2.returncode}\n"
                "请手动执行: apt-get update && apt-get install -y "
                + " ".join(SYSTEM_LIBS)
            )
            return False
        return True
    except asyncio.TimeoutError:
        messages.append("✗ apt-get 安装系统库超时, 请手动安装后重试")
        return False
    except Exception as e:
        messages.append(f"✗ apt-get 异常: {e}\n请手动安装系统库后重试")
        return False


async def ensure_chromium(
    browsers_path: str = "", log=None, started_msg: str = "Chromium 已就绪"
) -> tuple[bool, list[str]]:
    """统一编排: 验证 → 镜像下载 → 系统库补齐 → 最终验证.

    :param browsers_path: PLAYWRIGHT_BROWSERS_PATH (空则沿用环境已设置值)
    :param log: 日志对象 (后台侧按消息分级记录; 命令侧传 None, 逐条回显)
    :param started_msg: 验证通过时的成功提示 (后台/命令语境差异)
    :return: (ok, messages) — messages 为逐步回显文本列表
    """
    from bridge.core import _detect_missing_libs

    messages: list[str] = []
    try:
        if await _verify():
            messages.append(started_msg)
            for m in messages:
                _log(log, m)
            return True, messages
    except Exception:
        pass
    messages.append("Chromium 未安装, 开始异步安装...")
    installed = await _download_chromium(browsers_path, messages)
    if installed:
        if await _verify():
            messages.append(started_msg)
            for m in messages:
                _log(log, m)
            return True, messages
        messages.append("✗ Chromium 已下载但无法启动, 尝试补齐系统库...")
    # 下载失败或缺库 → 系统库补齐 (install-deps 优先 / apt-get 回退)
    if await _install_system_libs(messages):
        if await _verify():
            messages.append(started_msg)
            for m in messages:
                _log(log, m)
            return True, messages
    # 最终失败: 显式列出缺失库 + 修复指引
    _missing_now = _detect_missing_libs()
    messages.append(
        "✗✗ Chromium 环境自动组装失败, 卡片渲染将回退为文本 ✗✗\n"
        f"缺失系统库:\n{_missing_now or '(未检测到缺失库, 请检查 playwright 安装)'}\n"
        "修复方式(需容器 root):\n"
        "  1) apt-get update && apt-get install -y " + " ".join(SYSTEM_LIBS) + "\n"
        "  2) 或运行: python -m playwright install-deps chromium\n"
        "  3) 或发送指令 /parse_install_chromium 重试浏览器下载"
    )
    for m in messages:
        _log(log, m)
    return False, messages
