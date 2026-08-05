# 薄桥接重构方案 (bridge-standalone)

> 设计理念参考 PR #1 (zero-invasion thin bridge) + brigde2astrbot 分支 + 上游 standalone。
> 目标：架构明晰、0 硬编码动态注入决策树、保留原始调用逻辑、扩展功能完全解耦。

## 1. 现状问题

| 问题 | 位置 |
|---|---|
| main.py 1750 行大杂烩（注入/代理/发送/命令/清理全混） | main.py |
| bridge/ 16 模块职责模糊（core.py 726 行） | bridge/core.py |
| 渲染补丁堆叠（render_patch 3 层包装） | bridge/render_patch.py |
| 配置多来源（已部分统一 platforms） | bridge/core.py |
| 消息链路复杂（_send_any/_send_card/url_extract 分散） | main.py |

## 2. 目标架构（薄桥接）

```
repo/
├── main.py                        # AstrBot 薄入口: 仅注册命令/事件 → 委托 bridge
├── bridge/                        # 薄桥接层 (新)
│   ├── context.py                 # 桥接上下文: 上游引用聚合 (DOWNLOADER/RENDERER/BaseParser)
│   ├── inject.py                  # 0 硬编码动态注入决策树 (从 main.py 抽取重构)
│   ├── resolve.py                 # 解析适配: 调上游 parse_url (原始调用保留, 不做干预)
│   ├── proxy.py                   # 代理注入: monkey-patch DOWNLOADER.client (扩展, 解耦)
│   ├── send.py                    # 结果转发: ParseResult → AstrBot 消息 (卡片/文本/媒体)
│   ├── commands.py                # AstrBot 命令 (doctor/status/install/clean...)
│   └── tests/                     # 测试
└── src/nonebot_plugin_parser_lite/   # 上游 standalone (原样, 零修改)
```

## 3. 核心原则

1. **原始调用链保留**：上游 `ParserLite.parse_url(url)` 内部编排（路由→解析器→DOWNLOADER→Creator）完全不变；bridge 只在其**前后**做适配（前: 代理注入/配置热载; 后: 结果转发）。
2. **扩展完全解耦**：上游不 import bridge；bridge 通过上游公开 API 调用；扩展功能（AstrBot 发送/命令/代理）只存在于 bridge。
3. **0 硬编码决策树**：一切配置项从 `_UpConfig.model_fields` / `BaseParser.get_all_subclass()` / 枚举 / `CustomParser.SCHEMA` 动态扫描生成，无硬编码平台/字段清单。
4. **渲染回归上游**：删除 render_patch 的 autoescape 重建包装，渲染直接用上游 `RENDERER.render_image`；模板 `| e`/`~` 拼接问题由上游模板修复（或保留最小 pl_esc 注册）。
5. **单一事实来源**：配置读取统一 `_bridge_cfg`；平台配置统一 `platforms`。

## 4. 工作清单

### 阶段 A: 基础抽取
- [ ] A1. `bridge/context.py` — 上游引用聚合 + BridgeConfig 单例 (configure/get_config/features 映射)
- [ ] A2. `bridge/inject.py` — 注入决策树 (从 main.py _inject_dynamic_options_static 抽取, 0 硬编码)
- [ ] A3. main.py 注入相关代码移除 → 委托 bridge.inject

### 阶段 B: 解析与代理
- [ ] B1. `bridge/proxy.py` — DOWNLOADER 代理注入 (platforms 勾选语义 + 超时 + 回退直连)
- [ ] B2. `bridge/resolve.py` — parse_url 薄封装 (调用上游, 超时守卫, httpx 重建)
- [ ] B3. 删除 bridge/core.py 中代理/解析编排 (迁入 proxy/resolve)

### 阶段 C: 发送与渲染 (解耦: 调上游渲染, AstrBot 发送)
- [x] C1a. `bridge/send.py` — 发送层核心:
      `send_card(event, result)`: 上游 RENDERER.render_image → Comp.Image.fromBytes (LRU 10)
      → 失败回退 format_full 纯文本; `should_send(media_type)`: send_strategy 门控;
      `get_sendable_types()`: 上游 ContentItem Union 动态扫描
- [ ] C1b. `bridge/send.py` — 媒体发送 (MediaContent 遍历 → 三路 fromFileSystem/fromBytes/fromURL)
      + FFmpeg 转换回调注入 (main.py 提供, 保持发送层纯扩展)
- [ ] C2. 渲染: 删除 render_patch 的 render_html autoescape 重建包装
      (回上游原样 env), 保留 safe_src 默认 method (上游模板 bug)
- [x] C3a. main.py `_send_card` 委托 bridge.send (文本回退保留)

### 阶段 D: 命令与入口
- [ ] D1. `bridge/commands.py` — doctor/status/install_chromium/clean/enable/disable/bm/blogin
      (AstrBot 命令注册从 main.py 迁出, 委托 bridge.doctor/send 等)
- [ ] D2. main.py 薄化: 仅 ParserLitePlugin(Star) + 命令/事件注册 (逻辑委托 bridge 模块)

### 阶段 E: 收尾
- [ ] E1. 测试对齐 + 解耦回归 (上游模块不可反向 import bridge)
- [ ] E2. check_schema 适配 + CI + 部署验证

## 5. 决策树 (注入)

```
inject_schema(schema)
├─ _injected 标记存在? → 跳过 (保留用户编辑)
├─ _UpConfig.model_fields 遍历:
│   ├─ bool → "features" 列表 (标签双向映射)
│   ├─ int/float → 数字字段 (min/max/step 动态)
│   ├─ str 枚举 (PlatformEnum 等) → options 下拉
│   ├─ list[str] 枚举 → options 多选
│   ├─ slider (day_range) → slider 配置
│   └─ 其余 str → 文本字段
├─ BaseParser.get_all_subclass() → platforms 模板 (enable/proxy/cookies)
├─ CustomParser.SCHEMA → custom_parsers 模板
└─ 桥接扩展字段 (显式声明, 非硬编码平台):
    send_strategy / plite_http_proxy / plite_direct_link / ... / delay_send / arbiter
```
