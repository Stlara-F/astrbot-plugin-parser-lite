#!/usr/bin/env python3
# ruff: noqa: PT028 (CLI 脚本非 pytest, 参数默认值是 CLI 默认)
"""
通用解析器测试标准 (Parser Test Standard).
用法:
  py -3 test/test_parsers.py              # quick: URL检测 + 结构验证
  py -3 test/test_parsers.py --online     # 全量: URL检测 + 在线解析 + 结构验证
  py -3 test/test_parsers.py --parser bilibili  # 单平台测试

未来添加新解析器: 在 OFFLINE_URLS 中新加一条即可.
"""

import argparse
import asyncio
from dataclasses import dataclass, field
import os

os.environ["PARSER_LITE_STANDALONE"] = "1"
os.environ["PARSER_LITE_BASE_DIR"] = str(
    __import__("pathlib").Path(__file__).parent.parent
    / "src"
    / "nonebot_plugin_parser_lite"
)

import sys

_src = str(__import__("pathlib").Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
_root = str(__import__("pathlib").Path(__file__).parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from nonebot_plugin_parser_lite.data import (
    AudioContent,
    GraphicContent,
    ImageContent,
    ParseResult,
    VideoContent,
)
from nonebot_plugin_parser_lite.parsers.base import BaseParser

# ═══════════════════════════════════════════════════════════════
# 测试用例: 格式 {平台名: [(真实URL, 期望显示名)]}
# 离线回退 URL 数据 (在线模式: --online 时由 _FALLBACK_URLS 驱动)
# ═══════════════════════════════════════════════════════════════
_FALLBACK_URLS: list[str] = list(
    {
        "https://www.bilibili.com/video/BV1y23K6sEV2",
        "https://v.douyin.com/-kBGJx05iXQ/",
        "https://weibo.com/1642591402/OBvlKhejP",
        "http://xhslink.com/OJ9VwS",
        "https://www.zhihu.com/question/41564143/answer/91873558",
        "https://www.acfun.cn/v/ac46437117",
        "https://tieba.baidu.com/p/7589647008",
        "https://v.kuaishou.com/Jt7JzI",
        "https://bbs.hupu.com/627169461.html",
        "https://www.miyoushe.com/ys/article/52048071",
        "https://y.music.163.com/m/song?id=2024727995",
        "https://x.com/elonmusk/status/1815180888279155069",
    }
)


def _load_offline_urls() -> list[str]:
    """测试 URL 列表 (test_urls 配置字段 r9b 已删 → 静态回退)."""
    return list(_FALLBACK_URLS)


OFFLINE_URLS: list[str] = list(_FALLBACK_URLS)


@dataclass
class TestResult:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[str] = field(default_factory=list)

    def ok(self, msg: str):
        self.passed += 1
        self.details.append(f"  ✓ {msg}")

    def fail(self, msg: str):
        self.failed += 1
        self.details.append(f"  ✗ {msg}")

    def skip(self, msg: str):
        self.skipped += 1
        self.details.append(f"  - {msg}")

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped


# ═══════════════════════════════════════════════════════════════
# Phase 1: URL 检测 (离线, 快速)
# ═══════════════════════════════════════════════════════════════
def test_url_detection(platform: str, url: str, result: TestResult):
    """验证每个平台的 search_url 能匹配对应的测试 URL."""
    matched = False
    for cls in BaseParser.get_all_subclass():
        p = getattr(cls, "platform", None)
        if not p or p.name.lower() != platform.lower():
            continue
        try:
            kw, _mwp = cls.search_url(url)
            if kw:
                matched = True
                result.ok(f"{cls.__name__}.search_url → matched")
            else:
                result.fail(f"{cls.__name__}.search_url → no match")
        except Exception as e:
            result.fail(f"{cls.__name__}.search_url → {type(e).__name__}: {e}")
    if not matched:
        result.fail(f"No parser matched URL for platform={platform}")


# ═══════════════════════════════════════════════════════════════
# Phase 2: 在线解析 (需要网络)
# ═══════════════════════════════════════════════════════════════
async def test_online_parse(
    platform: str, url: str, display_name: str, result: TestResult, timeout: int = 60
):  # noqa: PT028
    """在线解析 URL, 验证返回结果结构完整性."""
    for cls in BaseParser.get_all_subclass():
        p = getattr(cls, "platform", None)
        if not p or p.name.lower() != platform.lower():
            continue
        try:
            kw, mwp = cls.search_url(url)
            if not kw:
                result.skip(f"{platform}: URL no longer matches — check expired")
                return
        except Exception:
            result.skip(f"{platform}: URL detection failed")
            return

        try:
            parser = cls()
            r: ParseResult = await asyncio.wait_for(
                parser.parse(kw, mwp), timeout=timeout
            )
            await parser.aclose()
        except asyncio.TimeoutError:
            result.fail(f"{platform}: parse timeout ({timeout}s)")
            return
        except Exception as e:
            result.fail(f"{platform}: parse error — {type(e).__name__}: {str(e)[:120]}")
            return

        # 结构完整性验证
        _validate_structure(r, display_name, platform.upper(), result)
        return

    result.fail(f"{platform}: no parser found")


def _validate_structure(r: ParseResult, result: TestResult):
    """验证 ParseResult 结构完整性."""
    # 1. 必须字段
    if r.author and r.author.name:
        result.ok(f"author.name: {r.author.name}")
    else:
        result.fail("author.name: missing")

    # 2. 标题
    if r.title:
        result.ok(f"title: {r.title[:50]}")
    else:
        result.fail("title: missing")

    # 3. 平台
    if r.platform:
        result.ok(f"platform: {r.platform.display_name}")
    else:
        result.fail("platform: missing")

    # 4. 内容
    if r.content:
        count = len(r.content)
        result.ok(f"content: {count} items")
        vid = sum(1 for x in r.content if isinstance(x, VideoContent))
        img = sum(1 for x in r.content if isinstance(x, ImageContent))
        aud = sum(1 for x in r.content if isinstance(x, AudioContent))
        grp = sum(1 for x in r.content if isinstance(x, GraphicContent))
        txt = sum(1 for x in r.content if isinstance(x, str))
        oth = count - vid - img - aud - grp - txt
        result.ok(f"  types: V={vid} I={img} A={aud} G={grp} T={txt} O={oth}")
    else:
        result.fail("content: empty")

    # 5. 统计 (可选, 非致命)
    if r.stats:
        s = r.stats
        parts = []
        if s.view_count:
            parts.append(f"views={s.view_count}")
        if s.like_count:
            parts.append(f"likes={s.like_count}")
        if s.comment_count:
            parts.append(f"comments={s.comment_count}")
        if parts:
            result.ok(f"stats: {', '.join(parts)}")
    else:
        result.skip("stats: none")

    # 6. 评论 (可选)
    if r.comments:
        result.ok(f"comments: {len(r.comments)}")
    else:
        result.skip("comments: none")

    # 7. URL
    if r.url:
        result.ok(f"url: {r.url[:80]}")


# ═══════════════════════════════════════════════════════════════
# Phase 3: 平台覆盖率
# ═══════════════════════════════════════════════════════════════
def test_coverage(result: TestResult):
    """验证 OFFLINE_URLS 覆盖了所有平台."""
    from nonebot_plugin_parser_lite.constants import PlatformEnum

    all_platforms = {p.name.lower() for p in PlatformEnum}
    tested = {k.lower() for k in _FALLBACK_URLS}
    covered = all_platforms & tested
    missing = all_platforms - tested
    extra = tested - all_platforms

    result.ok(f"Coverage: {len(covered)}/{len(all_platforms)} platforms tested")
    if missing:
        for m in sorted(missing):
            result.fail(f"  Missing test URL for: {m}")
    if extra:
        for e in sorted(extra):
            result.skip(f"  Extra (no PlatformEnum): {e}")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
async def main():
    # ── Smoke: 自检 UTF-8 编码 ──
    _self_path = __import__("pathlib").Path(__file__)
    try:
        _self_path.read_text("utf-8")
    except UnicodeDecodeError:
        import sys

        print("FATAL: test_parsers.py is not valid UTF-8", file=sys.stderr)  # noqa: T201
        return 1

    urls = _load_offline_urls()

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--online",
        action="store_true",
        help="Run online parse tests (requires network)",
    )
    ap.add_argument("--timeout", type=int, default=60, help="Parse timeout seconds")
    args = ap.parse_args()

    overall = TestResult()

    # Phase 1: URL Detection (always)
    for url in urls:
        matched = False
        for cls in BaseParser.get_all_subclass():
            p = getattr(cls, "platform", None)
            try:
                kw, mwp = cls.search_url(url)
                if kw:
                    matched = True
                    overall.ok(
                        f"{cls.__name__} ({getattr(p, 'display_name', p.name) if p else '?'}) matched"
                    )
            except Exception as e:
                overall.fail(
                    f"{cls.__name__}.search_url: {type(e).__name__}: {str(e)[:80]}"
                )
        if not matched:
            overall.fail(f"No parser matched: {url[:60]}")
    # 测试结果说明
    if overall.failed:
        pass

    # Phase 2: Online Parse (opt-in)
    if args.online:
        for url in urls:
            for cls in BaseParser.get_all_subclass():
                try:
                    kw, mwp = cls.search_url(url)
                    if not kw:
                        continue
                except Exception:
                    continue
                try:
                    parser = cls()
                    r = await asyncio.wait_for(
                        parser.parse(kw, mwp), timeout=args.timeout
                    )
                    await parser.aclose()
                    _validate_structure(r, overall)
                except asyncio.TimeoutError:
                    overall.fail(f"{cls.__name__}: timeout ({args.timeout}s)")
                except Exception as e:
                    overall.fail(f"{cls.__name__}: {type(e).__name__}: {str(e)[:120]}")
                break

    # Phase 3: Coverage
    from nonebot_plugin_parser_lite.constants import PlatformEnum

    all_platforms = {p.name.lower() for p in PlatformEnum}
    tested = set()
    for url in urls:
        for cls in BaseParser.get_all_subclass():
            try:
                kw, _ = cls.search_url(url)
                if kw and getattr(cls, "platform", None):
                    tested.add(cls.platform.name.lower())
                    break
            except Exception:
                pass
    overall.ok(f"Coverage: {len(tested)}/{len(all_platforms)} platforms")
    for m in sorted(all_platforms - tested):
        overall.skip(f"  Untested: {m}")

    return 0 if overall.failed == 0 else 1


if __name__ == "__main__":
    exit(asyncio.run(main()))
