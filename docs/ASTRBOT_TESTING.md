# 本地 AstrBot 实机测试指南

> 验证插件在真实 AstrBot 环境中的加载/配置/自检。已在 AstrBot v4.27.2 (Python 3.14) 实测通过。

## 1. 环境准备

```bash
pip install -U astrbot          # 最新版 (实测 4.27.2)
mkdir -p test_data/data/plugins
cd test_data
python -m astrbot.cli init      # 初始化 (输入 Y)
```

## 2. 部署插件

```powershell
# 复制插件到 AstrBot 插件目录 (main.py + bridge + src + metadata.yaml)
Copy-Item main.py, metadata.yaml -Destination data/plugins/astrbot_plugin_parser_lite
Copy-Item bridge -Destination data/plugins/astrbot_plugin_parser_lite -Recurse
Copy-Item src    -Destination data/plugins/astrbot_plugin_parser_lite -Recurse
```

## 3. 启动 + 验证

```bash
python -m astrbot.cli run
```

### 3.1 插件加载 (预期日志)

```
Loading plugin astrbot_plugin_parser_lite ...
[bridge.inject] schema injected: custom_parsers, platforms, features, ... (31 项)
Added llm tool: parse_url
Plugin astrbot_plugin_parser_lite (v1.3.1): ... initialize 完成
```

### 3.2 配置产物

| 文件 | 说明 |
|---|---|
| `data/plugins/.../_conf_schema.json` | 注入 schema (31 字段, object 全含 items) |
| `data/plugins/.../.injected` | 版本标记 (SCHEMA_VERSION=2) |
| `data/config/astrbot_plugin_parser_lite_config.json` | AstrBot 生成的插件配置 (utf-8-sig) |

### 3.3 doctor 自检

```bash
python -X utf8 scripts/astrbot_verify.py   # 见下
```

## 4. WebUI 验证 (可选)

1. 登录 http://localhost:6185 (初始密码在启动日志/CLI password 设置)
2. 插件管理 → 配置页 (schema 注入可见: platforms/代理/发送策略等)
3. 保存配置 → 触发 reload 热更新 (schema 变更生效)

> 注: `/api/plugins/config` 需插件 scope token (普通登录无权限, 属 AstrBot 权限设计)。

## 5. 验证脚本

```bash
python -X utf8 scripts/astrbot_verify.py
# 输出: AstrBot 生成配置键数 / doctor 10 项自检结果
```

## 6. 已知验证结论

- 注入决策树: import 时写 schema (31 字段) → AstrBot 读 schema (时序正确)
- 版本化注入: .injected 存 SCHEMA_VERSION, 插件更新后新字段自动注入
- AstrBotConfig: schema→默认配置, check_config_integrity 补新删旧
- 多端兼容: 无 astrbot/无上游可导入可测试 (bridge/tests/test_env_compat.py)
