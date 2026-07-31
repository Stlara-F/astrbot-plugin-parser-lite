# ✨ [AstrBot](https://github.com/astrbotdevs/astrbot) 链接分享自动解析插件 ✨

基于 [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 的 AstrBot 适配版。上游核心解析能力完整保留，AstrBot 桥接层零侵入。

## 支持的平台

上游支持的 26 个平台全部可用（B站、抖音、微博、小红书、快手、AcFun、X、贴吧、知乎、豆瓣、酷安、虎扑、小黑盒、米游社、LOFTER、5EPlay、ILLU、堆糖、BUFF、豆包、Linux Do、完美世界 及 网易云/酷狗/汽水/酷我音乐）。详见 [上游 README](https://github.com/sokoko-org/nonebot-plugin-parser-lite#readme)。

## 安装

将整个 `nonebot_plugin_parser_lite/` 目录放入 AstrBot `data/plugins/`，重启即可。首次启动自动安装 Chromium（卡片渲染用）。

```shell
# 在 AstrBot 插件目录下安装依赖
pip install -r requirements.txt
```

## 指令

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

AstrBot 薄桥接层。上游 `src/nonebot_plugin_parser_lite/` 保持原样（仅 `__init__.py`、`helper.py`、`_flags.py` 三处微调确保 standalone 模式安全启动），所有新代码放在仓库根目录，完全不修改上游解析、下载、渲染逻辑。

## 架构

```
repo/
├── main.py                     # AstrBot 适配器 (~1940 行)
├── _conf_schema.json           # 配置骨架 (仅 features: ["__INJECT__"])
├── metadata.yaml               # AstrBot 插件元数据
├── test/
│   ├── test_regressions.py     # 回归测试 (C1-C30, 30 个检查点)
│   ├── test_parsers.py         # 解析器功能测试
│   └── run_all.py              # 统一测试入口 (--smoke / --online)
└── src/nonebot_plugin_parser_lite/   # 上游源码 (3 处桥接适配)
```

## 核心能力

### 零硬编码配置注入
`_conf_schema.json` 仅提交 `features: ["__INJECT__"]` 骨架。模块加载时动态扫描：
- `_UpConfig.model_fields` → bool/int/string/list/slider 字段
- `BaseParser.get_all_subclass()` → 平台列表
- `PlatformEnum` / `BiliVideoCodecs` / `BiliVideoQuality` → 枚举选项
- `CustomParser.SCHEMA` → 自定义解析器模板
- `_BRIDGE_FIELDS` → 桥接层专属字段 (proxied/cookies/send_strategy/plite_http_proxy)
- `test/test_parsers._FALLBACK_URLS` → 测试 URL

`.injected` 标记文件防止重复注入，不覆盖用户 WebUI 编辑。

### 消息处理链路
`_extract_urls` 统一扫描纯文本、`Comp.Json`、XML 卡片、回复、Markdown、转发 — BFS 遍历卡片 JSON 提取所有含 `url`/`link` 的键。`_handle_card_message` 带 60s TTL 去重门，`send_strategy` lambda 懒求值门控媒体发送（image/video/audio/card）。

### 代理支持
`_apply_downloader_proxy()` 直接 monkey-patch `DOWNLOADER.client` 的 `httpx.AsyncClient` 和 `curl_cffi.AsyncSession` 实例——这两个库**不读取 HTTP_PROXY 环境变量**，必须通过构造函数 `proxy=` 参数注入。作用域仅限插件内部，不污染 AstrBot 进程全局。

### 卡片渲染
Playwright + Jinja2 + 内存 LRU 缓存（10 条），cache hit 直接 `fromBytes(data)` 不写磁盘。

### 配置桥接
- features 标签 ↔ `plite_*` bool 双向映射（`data[k] = _label(k) in features_list`）
- `parsers.items.cookies` → B 站 cookie 自动写入 `plite_bili_ck`（通过 `next(iter(cookies.values()))` 裸值传递，匹配上游 `ck2dict()` 格式）
- `parsers.items.proxied` → 解析器级别代理开关
- `parser_extra` 单选/多选区分：`type: "string"` + `options`（下拉框）vs `type: "list"` + `options`（多选列表）

### 诊断 & 测试
- `parse_doctor`：8 阶段自检（配置/FFmpeg/下载器/Chromium/网络/解析器/平台/渲染管线）
- 30 个回归测试（C1-C30）覆盖所有曾修复的 bug
- `ruff check` + test suite CI

### B 站音频下载
`cmd_bm` 三路 BV 提取（当前消息 / 懒下载会话 / 回复消息），正确解包 `(video_url, audio_url)` 并 `finally: aclose()`。

## 上游适配（仅 3 处）

| 文件 | 变更 | 原因 |
|------|------|------|
| `__init__.py` | 移除 `setdefault("PARSER_LITE_STANDALONE","1")` | 避免破坏 NoneBot 插件加载路径 |
| `helper.py` | `_STANDALONE` 守卫 + 15 个类型 stub | 防止 `from nonebot.adapters import Event` 崩溃 |
| `utils/_flags.py` | git 跟踪修复 | 上游遗漏此文件导致 GitHub zip 不含 |

`main.py` 第 22 行 `os.environ.setdefault("PARSER_LITE_STANDALONE", "1")` 是唯一的 standalone 入口。

## 🎉 致谢

本项目核心代码来自 [sokoko-org/nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 及 [fllesser/nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)，请前往原仓库给作者点个 Star。
