"""
直接调用 B 站官方 API 抓取弹幕 + 评论，供后续喂给 LLM 做笔记增强。

流程：
1. 从 URL 提 BV id / p 参数（已有 utils.url_parser 的两个函数）
2. GET /x/web-interface/view?bvid=BVxxx[&p=N] → 拿 (aid, cid)
3. 弹幕：GET /x/v1/dm/list.so?oid={cid} → XML（<d p="时间,模式,...">文本</d>）
   按 30 秒时间窗聚类，找出高密度时段 + 高频关键词，拼成 danmaku_summary
4. 评论：GET /x/v2/reply/main?type=1&oid={aid}&mode=3&next={page} → JSON
   翻页（最多 2 页）、按 rpid 去重、按 likes 降序取前 limit 条

防御：所有字段一律 .get() + 兜底；code != 0 记日志返回 ok=False；网络/解析异常
try/except 包住返回 error，绝不抛出去。
"""

import re
from collections import Counter
from typing import List, Optional, Tuple
from xml.etree import ElementTree

import requests

from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger
from app.utils.url_parser import (
    extract_bilibili_p_number,
    extract_video_id,
    resolve_bilibili_short_url,
)
from app.utils.url_safety import sanitize_error_text

logger = get_logger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 弹幕时间窗长度（秒）
DANMAKU_WINDOW_SECONDS = 30
# 高密度时段展示条数
DANMAKU_TOP_WINDOWS = 5
# 高频关键词展示条数
DANMAKU_TOP_KEYWORDS = 10
# 弹幕 XML 最大体积（字节，#142 A3）：正常视频弹幕远小于此值；超限视为异常/恶意响应，
# 拒绝解析——不设上限时恶意响应可撑爆内存/解析耗时（数据源是网络，非可信输入）
DANMAKU_MAX_XML_BYTES = 20 * 1024 * 1024


class BilibiliCommentFetcher:
    """通过 B 站官方 API 抓取弹幕与评论。"""

    def __init__(self):
        self._cookie = CookieConfigManager().get("bilibili") or ""

    def _headers(self) -> dict:
        h = {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com",
        }
        if self._cookie:
            h["Cookie"] = self._cookie
        return h

    @staticmethod
    def _normalize_video_url(video_url: str) -> str:
        # 统一 resolve 短链，避免 extract_video_id 和 extract_bilibili_p_number 各 resolve 一次
        if "b23.tv" in video_url:
            video_url = resolve_bilibili_short_url(video_url) or video_url
        return video_url

    def _get_meta(self, bvid: str, p: Optional[int] = None) -> Optional[Tuple[int, int]]:
        """拿视频元信息，返回 (aid, cid)，失败返回 None。"""
        url = "https://api.bilibili.com/x/web-interface/view"
        params = {"bvid": bvid}
        if p is not None and p >= 1:
            params["p"] = p
        try:
            resp = requests.get(url, params=params, headers=self._headers(), timeout=10)
            data = resp.json()
        except Exception as e:
            logger.warning(f"获取视频元信息失败: {sanitize_error_text(e)}")
            return None
        if data.get("code") != 0:
            logger.warning(
                f"view API 返回错误: code={data.get('code')}, msg={sanitize_error_text(data.get('message'))}"
            )
            return None

        d = data.get("data") or {}
        aid = d.get("aid")
        # 分 P 视频：data.pages[N-1] 对应第 N 集
        pages = d.get("pages") or []
        if pages:
            if p is not None and 1 <= p <= len(pages):
                cid = pages[p - 1].get("cid")
            elif p is not None:
                # 显式 p 越界：返回 None（调用方按「获取元信息失败」处理），
                # 绝不静默取第 1 集冒充第 p 集（#121 B4）
                logger.warning(f"p 越界: bvid={bvid} p={p} 但共 {len(pages)} 集，返回 None")
                return None
            else:
                cid = pages[0].get("cid")
        else:
            cid = d.get("cid")

        if not aid or not cid:
            logger.warning(f"view API 缺少 aid/cid: bvid={bvid} p={p}")
            return None
        return int(aid), int(cid)

    @staticmethod
    def _parse_danmaku_xml(xml_text: str) -> List[Tuple[float, str]]:
        """解析弹幕 XML，返回 [(time_sec, text), ...]。XML 损坏时抛异常，由调用方捕获。

        #142 A3：弹幕 XML 来自网络（B 站接口），是不可信输入——ElementTree 默认解析器
        允许 DTD/实体，恶意响应可触发 XXE（读本地文件/内网探测）或实体扩展 DoS。
        防御：① 解析前拒绝任何 DOCTYPE/ENTITY 声明——无 DTD 的文档无法定义实体，
        等价于 forbid_dtd/forbid_entities（Python 3.11–3.13 的 C 版 XMLParser 尚不支持
        这两个参数，3.14 才引入）；② 响应体积上限见 DANMAKU_MAX_XML_BYTES（fetch 侧检查）。
        """
        upper = xml_text.upper()
        if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
            raise ValueError("弹幕 XML 含 DTD/实体声明，拒绝解析")
        root = ElementTree.fromstring(xml_text)
        items: List[Tuple[float, str]] = []
        for d in root.iter("d"):
            p = d.get("p") or ""
            fields = p.split(",")
            try:
                t = float(fields[0])
            except (ValueError, IndexError):
                continue
            text = (d.text or "").strip()
            if text:
                items.append((t, text))
        return items

    @staticmethod
    def _fmt_ts(seconds: int) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _extract_keywords(texts: List[str], top_n: int = DANMAKU_TOP_KEYWORDS) -> List[str]:
        """对弹幕文本做简单分词计数：取连续中文（≥2 字）与连续字母数字（≥2 位），去掉过短词。"""
        counter: Counter = Counter()
        for text in texts:
            t = (text or "").strip()
            if not t:
                continue
            for m in re.findall(r"[一-鿿]{2,}", t):
                counter[m] += 1
            for m in re.findall(r"[A-Za-z0-9]{2,}", t):
                counter[m] += 1
        return [w for w, _ in counter.most_common(top_n)]

    def _build_danmaku_summary(self, danmaku: List[Tuple[float, str]]) -> str:
        """把弹幕聚合为摘要：高密度时段 + 高频关键词。弹幕为空返回空串。"""
        if not danmaku:
            return ""

        # 每 30 秒一个时间窗，统计每窗数量
        window_counts: Counter = Counter()
        for t, _ in danmaku:
            window_counts[int(t) // DANMAKU_WINDOW_SECONDS * DANMAKU_WINDOW_SECONDS] += 1
        # 数量前 5 的窗口，同数量按时间先后
        top_windows = sorted(
            window_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )[:DANMAKU_TOP_WINDOWS]
        dense = " ".join(
            f"{BilibiliCommentFetcher._fmt_ts(w)}-{BilibiliCommentFetcher._fmt_ts(w + DANMAKU_WINDOW_SECONDS)}({c}条)"
            for w, c in top_windows
        )

        keywords = BilibiliCommentFetcher._extract_keywords([text for _, text in danmaku])
        kw_str = "、".join(keywords)

        return f"弹幕高密度时段：{dense}\n高频弹幕：{kw_str}"

    def fetch_danmaku(self, video_url: str) -> dict:
        """抓取弹幕并聚合摘要。返回 {"ok", "source", "bvid", "cid", "danmaku_summary", "error"}。"""
        video_url = BilibiliCommentFetcher._normalize_video_url(video_url)

        bvid = extract_video_id(video_url, "bilibili")
        if not bvid:
            logger.info("无法从 URL 提取 BV id")
            return {
                "ok": False, "source": "bilibili", "bvid": "", "cid": 0,
                "danmaku_summary": "", "error": "无法从 URL 提取 BV id",
            }

        p = extract_bilibili_p_number(video_url)
        meta = self._get_meta(bvid, p)
        if not meta:
            return {
                "ok": False, "source": "bilibili", "bvid": bvid, "cid": 0,
                "danmaku_summary": "", "error": "获取视频元信息失败",
            }
        aid, cid = meta

        try:
            resp = requests.get(
                "https://api.bilibili.com/x/v1/dm/list.so",
                params={"oid": cid},
                headers=self._headers(),
                timeout=10,
            )
            # 体积上限在 decode 前检查（#142 A3）：先解码再拒绝会先把恶意大响应拉进内存
            if len(resp.content) > DANMAKU_MAX_XML_BYTES:
                logger.warning(f"弹幕 XML 过大（{len(resp.content)} 字节），拒绝解析: bvid={bvid} cid={cid}")
                return {
                    "ok": False, "source": "bilibili", "bvid": bvid, "cid": cid,
                    "danmaku_summary": "", "error": f"弹幕 XML 过大（{len(resp.content)} 字节），拒绝解析",
                }
            xml_text = resp.content.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(f"获取弹幕失败: {sanitize_error_text(e)}")
            return {
                "ok": False, "source": "bilibili", "bvid": bvid, "cid": cid,
                "danmaku_summary": "", "error": sanitize_error_text(e),
            }

        try:
            danmaku = BilibiliCommentFetcher._parse_danmaku_xml(xml_text)
        except Exception as e:
            logger.warning(f"解析弹幕 XML 失败: {sanitize_error_text(e)}")
            return {
                "ok": False, "source": "bilibili", "bvid": bvid, "cid": cid,
                "danmaku_summary": "", "error": f"弹幕 XML 解析失败: {sanitize_error_text(e)}",
            }

        summary = self._build_danmaku_summary(danmaku)
        logger.info(f"B站弹幕抓取成功: {bvid} cid={cid} 共 {len(danmaku)} 条")
        return {
            "ok": True, "source": "bilibili", "bvid": bvid, "cid": cid,
            "danmaku_summary": summary, "error": None,
        }

    def fetch_comments(self, video_url: str, limit: int = 20) -> dict:
        """抓取热门评论。返回 {"ok", "source", "bvid", "aid", "comments", "error"}。"""
        video_url = BilibiliCommentFetcher._normalize_video_url(video_url)

        bvid = extract_video_id(video_url, "bilibili")
        if not bvid:
            logger.info("无法从 URL 提取 BV id")
            return {
                "ok": False, "source": "bilibili", "bvid": "", "aid": 0,
                "comments": [], "error": "无法从 URL 提取 BV id",
            }

        p = extract_bilibili_p_number(video_url)
        meta = self._get_meta(bvid, p)
        if not meta:
            return {
                "ok": False, "source": "bilibili", "bvid": bvid, "aid": 0,
                "comments": [], "error": "获取视频元信息失败",
            }
        aid, _ = meta

        url = "https://api.bilibili.com/x/v2/reply/main"
        seen: dict = {}
        page = 0
        for _ in range(2):  # 最多翻 2 页
            try:
                resp = requests.get(
                    url,
                    params={"type": 1, "oid": aid, "mode": 3, "next": page},
                    headers=self._headers(),
                    timeout=10,
                )
                data = resp.json()
            except Exception as e:
                logger.warning(f"获取评论失败: {sanitize_error_text(e)}")
                return {
                    "ok": False, "source": "bilibili", "bvid": bvid, "aid": aid,
                    "comments": [], "error": sanitize_error_text(e),
                }
            if data.get("code") != 0:
                logger.warning(
                    f"评论 API 返回错误: code={data.get('code')}, msg={sanitize_error_text(data.get('message'))}"
                )
                return {
                    "ok": False, "source": "bilibili", "bvid": bvid, "aid": aid,
                    "comments": [], "error": f"评论 API 返回错误: {sanitize_error_text(data.get('message'))}",
                }

            d = data.get("data") or {}
            replies = d.get("replies") or []
            for r in replies:
                rpid = r.get("rpid")
                if rpid is not None and rpid in seen:
                    continue
                seen[rpid] = r

            next_page = (d.get("cursor") or {}).get("next")
            if len(seen) >= limit or not next_page:
                break
            page = next_page

        comments = []
        for r in seen.values():
            member = r.get("member") or {}
            content = r.get("content") or {}
            comments.append({
                "user": member.get("uname") or "",
                "content": content.get("message") or "",
                "likes": r.get("like") or 0,
                "ctime": r.get("ctime") or 0,
            })
        comments.sort(key=lambda c: c["likes"], reverse=True)
        comments = comments[:limit]

        logger.info(f"B站评论抓取成功: {bvid} aid={aid} 共 {len(comments)} 条")
        return {
            "ok": True, "source": "bilibili", "bvid": bvid, "aid": aid,
            "comments": comments, "error": None,
        }
