"""语义工具单一来源 — 翻译/标签/bool 注解判定 (消除 context↔inject 循环依赖).

合并:
- inject.py 的 TRANSLATIONS/tr()
- context.py 的 label()/label_en()/_is_bool_annotation()
消费方 (context/inject/main) 统一 import 本模块, 不再互相惰性 import.
"""

from __future__ import annotations

# ── 配置项翻译列表: 注入字段 → 中文标签 ─────────────────────────────────────
# 未翻译的 key 原样显示 (新增字段以未翻译状态进入配置, 后续在此补充即可)
TRANSLATIONS: dict[str, str] = {
    "features": "功能开关",
    "platforms": "平台配置",
    "parser_extra": "平台解析扩展",
    "custom_parsers": "自定义解析器",
    "test_urls": "测试URL",
    "parsers": "解析器控制",
    "send_strategy": "发送策略",
    "plite_http_proxy": "全局代理",
    "plite_direct_link": "直链免下载",
    "plite_send_cover_only": "视频仅发封面",
    "plite_image_compress_mb": "图片压缩阈值MB",
    "plite_video_file_threshold_mb": "视频文件发送阈值MB",
    "plite_md5_fast_send": "媒体指纹缓存秒发",
    "plite_md5_cache_max": "md5指纹缓存上限",
    "plite_dedup_ttl": "链接去重TTL秒",
    "plite_cache_interval": "缓存清理间隔秒",
    "plite_forward_max_nodes": "合并转发最大节点数",
    "card_semantic": "QQ卡片语义注入",
    "push": "B站UP订阅推送",
    "push_interval": "推送轮询间隔秒",
    "delay_send": "延迟发送表情触发",
    "arbiter": "多Bot表情仲裁",
    "cookie_health": "Cookie健康检查",
    "plite_need_upload": "上传音视频文件",
    "plite_need_upload_audio": "上传音频文件",
    "plite_need_upload_video": "上传视频文件",
    "plite_use_base64": "Base64编码发送",
    "plite_max_size": "资源最大大小MB",
    "plite_duration_maximum": "视频音频最大时长秒",
    "plite_append_url": "结果附加原始URL",
    "plite_append_qrcode": "结果附加原始URL二维码",
    "plite_disabled_platforms": "禁用的解析平台",
    "plite_blacklist_users": "黑名单用户",
    "plite_bili_video_codes": "B站视频编码",
    "plite_bili_video_quality": "B站视频清晰度",
    "plite_need_forward_contents": "合并转发内容",
    "plite_lazy_download": "懒下载模式",
    "plite_lazy_download_tip": "懒下载命令提示",
    "plite_lazy_download_timeout": "懒下载等待命令超时",
    "plite_download_command": "懒下载命令列表",
    "plite_browser_path": "浏览器程序路径",
    "plite_live_photo": "Live Photo转码",
    "plite_headless": "无头浏览器",
    "plite_max_comments": "最大评论数量",
    "plite_forward_text_threshold": "纯文本强制转发阈值",
    "plite_max_retries": "最大下载重试次数",
    "plite_day_range": "白天时间范围",
}


def tr(key: str) -> str:
    """翻译查找: 未翻译 key 原样返回 (新增字段以未翻译状态进入配置)."""
    return TRANSLATIONS.get(key, key)


def label_en(k: str) -> str:
    """英文驼峰标签 (旧配置 features 值兼容)."""
    s = k.removeprefix("plite_").replace("_", " ")
    if s.startswith("bili "):
        s = "B站" + s[4:]
    return " ".join(w[0].upper() + w[1:] for w in s.split())


def label(k: str) -> str:
    """字段标签: 翻译表优先; 未翻译回退英文驼峰 (新增字段可见)."""
    return tr(k) if k in TRANSLATIONS else label_en(k)


def is_bool_annotation(ann) -> bool:
    """bool 注解判定 (兼容 bool | None / Optional[bool] 联合注解)."""
    if ann is bool:
        return True
    if hasattr(ann, "__args__"):
        return bool in ann.__args__
    return False
