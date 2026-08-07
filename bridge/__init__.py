"""ParserLite AstrBot 适配层 (bridge) — 镜像上游分层, 公共 API 聚合.

模块映射 (对齐 nonebot-plugin-parser-lite standalone 结构):
- config.py     ↔ 上游 config:   配置读取 (bridge_cfg) + AstrBot 配置单例 (BridgeConfig)
- context.py    ↔ 上游模块入口:  上游引用聚合 (up_config/up_renderer/...)
- pipeline.py   ↔ 上游 pipeline: 解析编排 (ParserLite → 上游 Parser 委托)
- render.py     ↔ 上游 render:   渲染补丁 (safe_src/pl_esc) + 卡片发送 (send_card)
- platform.py   ↔ 上游 matchers: 平台配置读取/勾选同步 (enabled_platforms → 上游过滤)
- browser.py    ↔ 上游 utils/browser: Chromium 安装编排 (ensure_chromium)
- send.py       ↔ 上游 helper:   媒体发送管线 (send_media_file/send_items/...)
- commands.py   — 命令业务 (parse/bm/doctor/...)
- inject.py     — AstrBot schema 注入 (0 硬编码决策树)
- core.py       — 兼容 re-export + disabled_groups 持久化 + 环境检测

依赖约束: 仅依赖 standalone 包公共 API + 上游 requirements 依赖 + astrbot 宿主;
上游 src/ 零修改.
"""

from __future__ import annotations

__version__ = "1.3.2"

__all__ = [
    # 配置
    "BridgeConfig",
    "ParserLite",
    "bridge_cfg",
    "configure",
    "dispatch_result",
    "doctor",
    "enabled_platforms",
    "ensure_chromium",
    "get_config",
    "parse_url_cmd",
    "send_card",
    "send_media_file",
    "status_text",
]
