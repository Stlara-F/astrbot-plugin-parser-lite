<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_parser_lite?name=astrbot_plugin_parser_lite&theme=miku&padding=6&offset=114&align=top&scale=1.3&pixelated=1&darkmode=auto)

[![Code style: djlint](https://img.shields.io/badge/html%20style-djlint-blue.svg)](https://www.djlint.com)

</div>

> 严禁将本项目用于任何非法用途。使用不当造成的一切责任由使用者承担。

# astrbot_plugin_parser_lite

AstrBot 链接分享解析插件。基于 [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 的薄桥接层——上游 26 个平台的解析、下载、渲染能力完整保留，AstrBot 适配代码全部在仓库根目录，不侵入上游源码。

## 原理

```
消息到达
  → _extract_urls()  扫描文本/Json卡片/XML/回复/Markdown/转发
  → _handle_card_message()  去重门(TTL 60s) → _parse_raw()
  → BaseParser.search_url()  O(1) 路由 → parser.parse()
  → _send_card()  Playwright渲染卡片 → _send_items()  媒体下载+发送
```

代理 (`plite_http_proxy`) 直接注入 `httpx.AsyncClient` 和 `curl_cffi.AsyncSession` 实例，支持 `ip:port` 裸输入 + 自动协议轮询（`http` / `socks5` / `socks5h`）。

## 架构

```
repo/
├── main.py                     AstrBot 适配器
├── _conf_schema.json           配置骨架 (仅 features: ["__INJECT__"])
├── metadata.yaml               插件元数据 (AstrBot 市场规范 v2026-06-27)
├── requirements.txt            桥接层依赖
├── test/
│   ├── _base.py                共享断言
│   ├── run_all.py              统一入口 (--smoke / --online)
│   ├── test_parsers.py         解析器功能
│   ├── test_proxy.py           代理配置
│   ├── test_config.py          配置桥接
│   ├── test_message.py         消息处理
│   ├── test_startup.py         启动/生命周期
│   └── test_integrity.py       编码/文件系统
└── src/nonebot_plugin_parser_lite/   上游源码 (仅 3 处微调)
```

## 零侵入配置注入

`_conf_schema.json` 仅提交 `features: ["__INJECT__"]` 骨架。模块加载时从上游动态扫描：

- `_UpConfig.model_fields` → bool/int/string/list/slider
- `BaseParser.get_all_subclass()` → 平台列表
- `PlatformEnum` / `BiliVideoCodecs` → 枚举选项
- `CustomParser.SCHEMA` → 自定义解析器模板
- `_BRIDGE_FIELDS` → 桥接专属字段 (proxy/cookie/send_strategy)

`.injected` 标记防止重复注入，不覆盖用户 WebUI 编辑。

## 上游适配（仅 3 处）

| 文件 | 变更 | 原因 |
|------|------|------|
| `__init__.py` | 移除 `setdefault("PARSER_LITE_STANDALONE")` | NoneBot 插件路径安全 |
| `helper.py` | `_STANDALONE` 守卫 + 15 类型 stub | 防止 `from nonebot.adapters import Event` 崩溃 |
| `utils/_flags.py` | git 跟踪修复 | 上游遗漏，zip 不含 |

## 致谢

核心代码来自 [sokoko-org/nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 及 [fllesser/nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)。

## 使用说明

详见 [GUIDE.md](GUIDE.md) — 安装、配置、指令、代理设置、测试、排查。
