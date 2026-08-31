"""
通过 youtube-transcript-api 获取 YouTube 字幕。
优先人工字幕，其次自动生成字幕。不依赖 yt_dlp，无需下载任何文件。
"""

from typing import List, Optional

from youtube_transcript_api import YouTubeTranscriptApi

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger
from app.utils.url_safety import sanitize_error_text, sanitize_url

logger = get_logger(__name__)


class YouTubeSubtitleFetcher:
    """通过 youtube-transcript-api 获取 YouTube 字幕。"""

    def __init__(self):
        # 配了全局代理就给 youtube-transcript-api 套一个带 proxies 的 requests.Session，
        # 否则国内拉字幕同样会超时。代理未配置时退回默认无代理客户端。
        proxy = ProxyConfigManager().get_proxy_url()
        if proxy:
            try:
                import requests
                session = requests.Session()
                session.proxies = {"http": proxy, "https": proxy}
                self._api = YouTubeTranscriptApi(http_client=session)
                # 保留引用以便显式 close：Session 不 close 会泄漏连接池条目
                # 直到 GC，且 youtube-transcript-api 不会替我们释放（#125 B16）
                self._session = session
                # 代理 URL 可能含 user:pass@（docs/05 第 16 轮 A4）：日志只留 host
                logger.info(f"YouTube 字幕走代理: {sanitize_url(proxy)}")
            except Exception as e:
                logger.warning(f"为 youtube-transcript-api 注入代理失败，回退无代理: {sanitize_error_text(e)}")
                self._api = YouTubeTranscriptApi()
        else:
            self._api = YouTubeTranscriptApi()

    def close(self) -> None:
        """显式释放代理 Session（连接池/打开 fd）。"""
        session = getattr(self, "_session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
            self._session = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def fetch_subtitles(
        self,
        video_id: str,
        langs: Optional[List[str]] = None,
    ) -> Optional[TranscriptResult]:
        if langs is None:
            langs = ["zh-Hans", "zh", "zh-CN", "zh-TW", "en", "en-US", "ja"]

        try:
            # 1. 列出所有可用字幕
            transcript_list = self._api.list(video_id)

            available = []
            for t in transcript_list:
                available.append(
                    f"{t.language_code}({'auto' if t.is_generated else 'manual'})"
                )
            logger.info(f"可用字幕轨道: {', '.join(available)}")

            # 2. 按优先级查找：先人工字幕，再自动字幕
            transcript = None
            try:
                transcript = transcript_list.find_manually_created_transcript(langs)
                logger.info(f"选中人工字幕: {transcript.language_code} ({transcript.language})")
            except Exception:
                try:
                    transcript = transcript_list.find_generated_transcript(langs)
                    logger.info(f"选中自动字幕: {transcript.language_code} ({transcript.language})")
                except Exception:
                    # 都没匹配，取第一个可用的
                    for t in transcript_list:
                        transcript = t
                        source = "auto" if t.is_generated else "manual"
                        logger.info(f"使用首个可用字幕: {t.language_code} ({source})")
                        break

            if not transcript:
                logger.info(f"YouTube 视频 {video_id} 没有任何可用字幕")
                return None

            # 3. 获取字幕内容
            # 兼容两种返回：新版 youtube-transcript-api 是 FetchedTranscriptSnippet
            # dataclass（.text/.start/.duration），旧版是 dict。不能用 str(snippet)——
            # dataclass 的 str() 是整条 repr，会把每条字幕变成垃圾文本。
            fetched = transcript.fetch()
            segments = []
            for snippet in fetched:
                if isinstance(snippet, dict):
                    text = (snippet.get("text") or "").strip()
                    start = float(snippet.get("start", 0))
                    duration = float(snippet.get("duration", 0))
                else:
                    text = (getattr(snippet, "text", "") or "").strip()
                    start = float(getattr(snippet, "start", 0))
                    duration = float(getattr(snippet, "duration", 0))
                if not text:
                    continue
                segments.append(TranscriptSegment(
                    start=start,
                    end=start + duration,
                    text=text,
                ))

            if not segments:
                logger.warning(f"YouTube 字幕内容为空: {video_id}")
                return None

            full_text = " ".join(seg.text for seg in segments)
            logger.info(f"成功获取 YouTube 字幕，共 {len(segments)} 段")

            return TranscriptResult(
                language=transcript.language_code,
                full_text=full_text,
                segments=segments,
                raw={
                    "source": "youtube_transcript_api",
                    "language": transcript.language,
                    "language_code": transcript.language_code,
                    "is_generated": transcript.is_generated,
                },
            )

        except Exception as e:
            logger.warning(f"YouTube 字幕获取失败: {sanitize_error_text(e)}")
            return None
