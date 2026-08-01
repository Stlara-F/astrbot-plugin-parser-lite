<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_parser_lite?name=astrbot_plugin_parser_lite&theme=miku&padding=6&offset=114&align=top&scale=1.3&pixelated=1&darkmode=auto)

[![Code style: djlint](https://img.shields.io/badge/html%20style-djlint-blue.svg)](https://www.djlint.com)

</div>

> [!IMPORTANT]
>
> 严禁将本项目用于任何非法用途
>
> 由于使用不当造成的一切责任由使用者承担，本项目维护者无任何责任

# ✨ [AstrBot](https://github.com/astrbotdevs/astrbot) 链接分享自动解析插件 ✨

基于 [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 的 AstrBot 适配版。上游核心解析能力完整保留，AstrBot 桥接层零侵入。

## 支持的平台

上游支持的 26 个平台全部可用。详见 [上游 README](https://github.com/sokoko-org/nonebot-plugin-parser-lite#readme)。

## 安装

```shell
# AstrBot 插件目录
cd /AstrBot/data/plugins
git clone https://github.com/Stlara-F/astrbot-plugin-parser-lite.git
cd astrbot-plugin-parser-lite
pip install -r requirements.txt
```

## 指令

| 指令 | 说明 |
|------|------|
| `parse <url>` | 解析链接 |
| `parse_doctor` | 全自动诊断 |
| `parse_status` / `parse_clean` | 状态 / 清理缓存 |
| `parse_enable` / `parse_disable` | 群开关 |
| `cmd_bm <BV号>` | B站音频下载 |
| `cmd_blogin` | B站扫码登录 |

AstrBot 中命令前缀 `/` 自动识别。自动解析：群内发送含 URL 的消息自动解析，无需命令。

## 配置

首次启动后 WebUI 会出现以下配置项：

| 配置 | 说明 |
|------|------|
| `features` | 功能开关（11 项 bool，双向映射） |
| `plite_http_proxy` | HTTP/SOCKS 代理（`ip:port` 自动匹配协议） |
| `parsers.items.proxied` | 指定走代理的解析器 |
| `parsers.items.cookies` | 平台 Cookie（JSON 格式） |
| `parser_extra` | 解析器专属扩展（B站画质/编码 等） |
| `send_strategy` | 发送策略（image/video/audio/card） |
| `custom_parsers` | 自定义解析器模板 |
| `test_urls` | 测试链接（诊断用） |

配置项由 `_conf_schema.json` 骨架动态注入，上下游新增字段自动出现在 WebUI。

## 代理

`plite_http_proxy` 支持裸 `ip:port` 输入，自动匹配协议：

| 输入 | 解析 |
|------|------|
| `192.168.1.1:10809` | 自动轮询 `http://` `https://` `socks5://` `socks5h://` |
| `socks5 192.168.1.1:10809` | `socks5://192.168.1.1:10809` |
| `http://192.168.1.1:10809` | 原样 |

代理直接注入 `httpx.AsyncClient` 和 `curl_cffi.AsyncSession` 实例，作用域仅限插件内部。

## 测试

```
test/
├── _base.py              # 共享断言与路径
├── run_all.py             # 自动发现运行
├── test_parsers.py        # 解析器功能（--online 在线验证）
├── test_proxy.py          # 代理配置、协议轮询、Cookie
├── test_config.py         # 配置桥接、Schema 注入、env var
├── test_message.py        # 消息处理、卡片渲染、去重门
├── test_startup.py        # 启动、生命周期、模块缓存
└── test_integrity.py      # UTF-8 编码、gitignore、文件泄漏
```

```shell
py -3 test/run_all.py           # 全量
py -3 test/run_all.py --smoke   # 快速（跳过解析器）
py -3 test/run_all.py --online  # 在线解析验证
```

## 致谢

本项目核心代码来自 [sokoko-org/nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 及 [fllesser/nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)，请前往原仓库给作者点个 Star。
