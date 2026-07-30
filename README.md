# ✨ AstrBot 链接分享自动解析插件 ✨

基于 [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 的 AstrBot 适配版。上游核心解析能力完整保留，AstrBot 桥接层零侵入。

> 桥接代码: `main.py` (~800行) | 上游改动: `requirements.txt` (1文件, 适配AstrBot依赖)

## 📖 支持的平台

上游支持的 26 个平台全部可用（B站、抖音、微博、小红书、快手、AcFun、X、贴吧、知乎、豆瓣、酷安、虎扑、小黑盒、米游社、LOFTER、5EPlay、ILLU、堆糖、BUFF、豆包、Linux Do、完美世界 及 网易云/酷狗/汽水/酷我音乐）。详见 [上游 README](https://github.com/sokoko-org/nonebot-plugin-parser-lite#readme)。

## 💿 安装

将整个 `nonebot_plugin_parser_lite/` 目录放入 AstrBot `data/plugins/`，重启即可。首次启动自动安装 Chromium（卡片渲染用）。

```shell
# 在 AstrBot 插件目录下安装依赖
pip install -r requirements.txt
```

## 🎉 指令

| 指令 | 说明 |
|------|------|
| `parse <url>` | 解析链接，返回卡片+媒体文件 |
| `parse_doctor` | 全自动诊断（7阶段, 含堆栈+修复建议） |
| `parse_status` | 运行状态 |
| `parse_clean` | 清理缓存 |
| `parse_enable` / `parse_disable` | 群开关 |
| `parse_install_chromium` | 手动安装 Chromium |
| `cmd_bm <BV号>` | 下载B站音频 |
| `cmd_blogin` | B站扫码登录（发送二维码图片） |

AstrBot 中命令前缀 `/` 会自动识别，输入 `/parse_doctor` 或 `parse_doctor` 均可触发。自动解析：群内发送任何含 URL 的消息（文本/卡片/小程序）自动解析，无需命令。

## ⚙️ 配置

在 AstrBot WebUI 插件配置面板中修改。所有配置项（含下拉选项、平台列表、解析器开关）从上游代码**动态注入**，0 hardcode。上游新增字段/解析器自动出现在面板中。

首次安装后配置自动注入，`.injected` 文件标记已注入状态，后续重启跳过注入保留用户修改。删除 `.injected` 或恢复 `__INJECT__` 标记可强制重新注入。

## 🧠 工作原理

```
消息到达 → _extract_urls (全类型URL抽取: 文本/JSON/XML/转发/小程序)
  → ParserLite.parse_url (O(1)特征路由 + 双路代理重试)
  → BaseParser.parse (上游 HTTP 解析 → ParseResult)
  → _send_card (Playwright 卡片渲染 → JPEG, 含缓存)
  → _send_any (媒体三路发送: fromFileSystem → raw/fromBytes → fromURL)
```

**动态注入**: 模块加载时扫描 `Config.model_fields` (17 plite_* 字段)、`BaseParser.get_all_subclass()` (26 解析器)、`PlatformEnum` (27 平台)、`CustomParser.SCHEMA` (24 字段模板) → 写入 `_conf_schema.json` → AstrBot WebUI 即时可用。

## 📁 目录结构

```
nonebot_plugin_parser_lite/
├── main.py                  AstrBot 适配层 (~800行)
├── _conf_schema.json        配置骨架 (1条 __INJECT__)
├── metadata.yaml            插件元信息
├── requirements.txt         依赖声明
├── README.md                本文档
├── test/
│   ├── __init__.py
│   └── test_parsers.py      解析器测试 (含13条内置URL)
└── src/
    └── nonebot_plugin_parser_lite/  上游包 (0行改动)
        ├── __init__.py
        ├── parsers/           26 平台解析器
        ├── download/          流下载器
        ├── render/            卡片渲染 + 模板
        └── utils/             FFmpeg/Cache/Browser
```

## 🎉 致谢

本项目核心代码来自 [sokoko-org/nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 及 [fllesser/nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)，请前往原仓库给作者点个 Star。
