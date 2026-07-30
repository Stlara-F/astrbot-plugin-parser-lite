# parser-lite-astrbot 特征库

> 自动生成 2026-07-29 | 基于 thin bridge + PR#205 合并后的上游 main

---

## 一、命令签名表

| 命令 | 触发方式 | 输出模式 | 发送方法 | 参数 |
|------|---------|---------|---------|------|
| `/parse <url>` | `filter.command("parse")` | generator yield | `event.plain_result()` | event → 自动提取 URL |
| `/parse_dl <url>` | `filter.command("parse_dl")` | generator yield | `event.plain_result()` | event → 懒会话/URL |
| `xz` / `下载` | `filter.regex` | generator yield | `event.plain_result()` | event → 懒会话触发 |
| `/parse_clean` | `filter.command("parse_clean")` | generator yield | `event.plain_result()` | event |
| `/parse_status` | `filter.command("parse_status")` | generator yield | `event.plain_result()` | event |
| `/parse_enable` | `filter.command("parse_enable")` | generator yield | `event.plain_result()` | event → 群聊检查 |
| `/parse_disable` | `filter.command("parse_disable")` | generator yield | `event.plain_result()` | event → 群聊检查 |
| `/parse_doctor` | `filter.command("parse_doctor")` | generator yield | `event.plain_result()` | event |
| `/parse_test` | `filter.command("parse_test")` | generator yield | `event.plain_result()` | event → subprocess |
| `/parse_install_chromium` | `filter.command("parse_install_chromium")` | generator yield | `event.plain_result()` | event |
| `bm <BV>` | `filter.command("bm")` | generator yield | `event.plain_result()` | event → B站音频 |
| `blogin` | `filter.command("blogin")` | generator yield | `event.plain_result()` | event → QR扫码 |
| `parse_url(url)` | `filter.llm_tool` | return str | N/A | event, url |
| 任意含 URL 消息 | `filter.regex(r"https?://[^\s]+")` | silent (generator占位) | `await event.send()` 内部 | event → 跳过 `/` 开头 |

---

## 二、AstrBot 事件 API 兼容性

| API | AiocqhttpMessageEvent | OneBot V11 | 说明 |
|-----|:---:|:---:|------|
| `event.get_message_str()` | ✅ | ✅ | 获取消息文本 |
| `event.get_sender_id()` | ✅ | ✅ | 发送者 UID |
| `event.unified_msg_origin` | ✅ | ✅ | 群:用户 标识 |
| `event.plain_result("text")` | ✅ | ✅ | 发送文本 (推荐) |
| `event.chain_result([Comp.xxx])` | ✅ | ✅ | 发送组件链 |
| `event.send(...)` | ✅ | ✅ | 直接发送 |
| `event.make_return_message()` | ❌ | ❌ | **不存在, 不要用** |
| `event.get_user_id()` | ❌ | ❌ | **不存在, 用 get_sender_id()** |
| `event.get_group_id()` | ❌ | ❌ | **不存在, 用 unified_msg_origin** |

---

## 三、Comp 组件 API

| API | Aiocqhttp | 说明 |
|-----|:---:|------|
| `Comp.Plain("text")` | ✅ | 文本消息 |
| `Comp.Image.fromFileSystem(path)` | ✅ | **主路** 发送图片 |
| `Comp.Image(raw=bytes)` | ✅ | **备路** raw bytes (不可靠, 仅 fallback) |
| `Comp.Video.fromFileSystem(path)` | ✅ | **主路** 发送视频 |
| `Comp.Video.fromBase64(b64)` | ✅ | **备路** base64 |
| `Comp.Image(file=...)`  | ⚠️ | 需要 `file` 位置参数 |

---

## 四、消息发送模式

```
┌─ 命令 handler (generator) ─────────────────────┐
│ yield event.plain_result("text")                │  ← AstrBot 自动发送
│ yield event.plain_result("text2")               │
│ (generator 结束 = 命令完成)                       │
└─────────────────────────────────────────────────┘

┌─ 渲染/媒体 handler (regular async) ────────────┐
│ await event.send(event.chain_result([           │  ← 手动发送
│     Comp.Image.fromFileSystem(path)             │
│ ]))                                             │
└─────────────────────────────────────────────────┘
```

---

## 五、配置字段 (Upstream Config)

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `plite_bili_ck` | `str\|None` | None | B站 Cookie |
| `plite_max_size` | `int` | 90 | 最大文件 MB |
| `plite_duration_maximum` | `int` | 480 | 最大时长 秒 |
| `plite_max_comments` | `int` | 5 | 评论上限 |
| `plite_max_retries` | `int` | 3 | 重试次数 |
| `plite_lazy_download` | `bool` | False | 懒下载开关 |
| `plite_lazy_download_timeout` | `int` | 30 | 懒会话超时 |
| `plite_disabled_platforms` | `list` | [] | 禁用平台 |
| `plite_blacklist_users` | `list` | [] | 黑名单 UID |
| `plite_bili_video_quality` | enum | _1080P | B站画质 |
| `plite_bili_video_codes` | list | [AVC,AV01,HEV] | B站编码 |
| `plite_use_base64` | `bool` | False | base64 发送 |
| `plite_append_url` | `bool` | False | 附加原始 URL |
| `plite_headless` | `bool` | False | 无头模式 |

---

## 六、上游 API 调用点

| 类/函数 | 方法 | 桥梁行号 | 用途 |
|---------|------|---------|------|
| `BaseParser` | `get_all_subclass()` | L96 | 遍历解析器 |
| `BaseParser` | `search_url(url)` | L98 | URL 匹配 |
| `Parser` | `parse(kw, mwp)` | L105 | 执行解析 |
| `BridgeConfig` | `configure(**dict)` | L64 | 注入配置 |
| `BridgeConfig` | `get_config()` | L73 | 读取配置 |
| `RENDERER` | `resolve_parse_result(r)` | L521 | 模板数据 |
| `RENDERER` | `templates_dir` | L522 | 模板路径 |
| `DOWNLOADER` | `ensure_client()` | L96 | 重建 HTTP 客户端 |
| `DOWNLOADER` | `download_audio()` | L770 | 下载音频 |
| `DOWNLOADER` | `aclose()` | L128 | 关闭客户端 |
| `FFmpeg` | `convert_audio_to_mp3()` | L771 | 转 MP3 |
| `CacheManager` | `clean_expired()` | L333 | 清理缓存 |
| `BilibiliParser` | `extract_download_urls()` | L765 | B站音频 URL |
| `BilibiliParser` | `login_with_qrcode()` | L780 | B站登录 QR |
| `BilibiliParser` | `check_qr_state()` | L783 | 轮询登录状态 |
| `safe_src` | `safe_src(obj, method)` | L524 | 模板安全 URL |

---

## 七、已修复 Bug 清单

| # | 日期 | Bug | 修复 |
|---|------|-----|------|
| 1 | 0728 | `filter.command("/parse")` 双重 / | → `"parse"` |
| 2 | 0728 | `event.get_user_id()` 不存在 | → `get_sender_id()` |
| 3 | 0728 | `event.make_return_message()` 不存在 | → 删除, 用 yield |
| 4 | 0728 | `async for _ in self._send_card()` 非生成器 | → `await self._send_card()` |
| 5 | 0728 | `UniMessage[Any]` 不可下标 | → `from __future__ import annotations` + `__class_getitem__` |
| 6 | 0728 | `logger.success()` 不存在 | → `logging.Logger.success = logging.Logger.info` |
| 7 | 0728 | `PARSER_LITE_BASE_DIR` 指向 package 根目录 | → 默认值为插件根目录 |
| 8 | 0728 | `Comp.Image(raw=...)` Aiocqhttp 不支持 | → `fromFileSystem()` |
| 9 | 0728 | `cmd_status` uptime 秒数当分钟 | → `divmod(uptime // 60, 60)` |
| 10 | 0728 | `_parse_raw` 一个 parser 失败后 raise | → continue 试下一个 |
| 11 | 0728 | `parse_url` 不重建 HTTP 客户端 | → `DOWNLOADER.ensure_client()` 每轮 |

---

## 八、环境变量

| 变量 | 作用 | 设置位置 |
|------|------|---------|
| `PARSER_LITE_STANDALONE=1` | 激活独立模式 | main.py L28 |
| `PARSER_LITE_BASE_DIR` | 数据/缓存根目录 | main.py L29 |
| `PLAYWRIGHT_BROWSERS_PATH` | Chromium 安装路径 | initialize() L295 |

---

## 九、已知限制

1. `_send_card` 每次启动新 Chromium (未复用 browser context)
2. 卡片渲染无持久化缓存 (仅内存, 重启丢失)
3. `_extract_card_url` 对 JSON/MiniProgram 卡片的解析比 fat bridge 简单
4. `cmd_doctor` 不像 fat bridge 运行 5 套外部测试
5. 部分平台测试 URL 为猜测值, 需要真实链接验证
