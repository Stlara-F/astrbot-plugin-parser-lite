# 使用指南

## 安装

```shell
cd /AstrBot/data/plugins
git clone https://github.com/Stlara-F/astrbot-plugin-parser-lite.git
cd astrbot-plugin-parser-lite
pip install -r requirements.txt
```

首次启动自动安装 Chromium（卡片渲染用），约 2-5 分钟。

## 配置

重启后在 AstrBot WebUI → 插件配置中找到 `链接分享自动解析插件`。

### 网络

解析请求默认直连 (r8 起代理体系已收敛, 不再支持全局代理配置).

### Cookie

`parsers.items.cookies` 填写 JSON，以平台名为 key：

```json
{"bilibili": "SESSDATA=xxx; bili_jct=yyy", "zhihu": "z_c0=zzz"}
```

B 站 Cookie 从浏览器 F12 → Application → Cookies → bilibili.com 复制。

### 开关

`features` 勾选列表控制 11 项功能（Lazy Download / Headless / Use Base64 等）。勾选 = 开启，取消 = 关闭。

### 发送策略

`send_strategy` 控制发出什么类型的媒体文件（image/video/audio/card）。默认全部勾选。

## 指令

| 指令 | 说明 |
|------|------|
| `parse <url>` | 解析链接 |
| `parse_doctor` | 全自动诊断（配置/FFmpeg/下载器/Chromium/网络/解析器/平台/渲染管线） |
| `parse_status` | 运行状态 |
| `parse_clean` | 清理缓存 |
| `parse_enable` | 在本群开启解析 |
| `parse_disable` | 在本群关闭解析 |
| `cmd_bm <BV号>` | B 站音频下载 |
| `cmd_blogin` | B 站扫码登录 |
| `xz` / `下载` | 懒下载触发（需开启 Lazy Download） |

命令前缀 `/` 自动识别，`/parse_doctor` 与 `parse_doctor` 均可。

## 自动解析

群内发送含 URL 的消息（文本/Json卡片/小程序/转发记录）自动触发解析，无需命令。

## 测试

```shell
# 快速（代理/配置/编码/启动）
py -3 test/run_all.py --smoke

# 全量（含解析器功能）
py -3 test/run_all.py

# 在线验证（发真实 HTTP 请求）
py -3 test/run_all.py --online
```

## 排查

### 网络异常排查
1. 确认解析源可访问 (`curl https://www.bilibili.com` 等)
2. 查看日志定位超时/失败的解析阶段
3. 若使用旧版插件实例, 检查 `enabled_plugins_name` 确认当前运行的实例名

### 卡片渲染失败 / 模板缺失
1. 确认 `render/templates/default.html.jinja` 已部署
2. 看日志 `卡片模板缺失:` 行，确认路径
3. 插件会自动从 `src/` 树复制模板到目标路径（日志含 `模板已从...复制到...`）
4. 检查 `pip install jinja2 playwright` 已安装

### X/Twitter 解析失败
1. 确认已配置代理（X.com 需代理访问）
2. 查看日志 `[ParserLite] downloader proxy:` 是否显示代理地址
3. 解析走 `easycomment.ai` 中间 API，下载走 X.com CDN，都需要代理
4. 测试代理：`curl -x http://ip:port https://x.com/elonmusk`

### 消息发送失败 / ApiNotAvailable
插件已内置异常保护，单条消息发送失败不影响其他功能。如频繁出现，检查 OneBot 服务端 WebSocket 连接状态。

### 插件列表出现两个实例
```shell
# 保留一个，删除多余的
rm -rf /AstrBot/data/plugins/astrbot-plugin-parser-lite
# 或
rm -rf /AstrBot/data/plugins/astrbot_plugin_parser_lite
```
重新部署后 `enabled_plugins_name` 只含一个。
