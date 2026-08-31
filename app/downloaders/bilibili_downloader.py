import glob as _glob
import json
import logging
import os
import shutil
import tempfile
import threading
from abc import ABC
from typing import List, Optional, Union

import yt_dlp

from app.downloaders.base import QUALITY_MAP, Downloader, DownloadQuality
from app.downloaders.bilibili_dm_patch import apply_bilibili_dm_img_patch
from app.downloaders.bilibili_subtitle import BilibiliSubtitleFetcher
from app.downloaders.common import ytdlp_cancel_hook, ytdlp_retry
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.cookie_manager import CookieConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_bilibili_p_number, extract_video_id
from app.utils.url_safety import assert_public_http_url, sanitize_url

logger = logging.getLogger(__name__)

# Inject the dm_img_* / web_location risk-control params Bilibili's wbi/playurl
# gateway now requires; without them the API path returns HTTP 412. See
# app/downloaders/bilibili_dm_patch.py for details.
apply_bilibili_dm_img_patch()


class BilibiliDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()
        self._cookie_mgr = CookieConfigManager()
        self._cookie = self._cookie_mgr.get('bilibili')
        self._cookiefile = self._write_netscape_cookie_file()

    def _write_netscape_cookie_file(self) -> Optional[str]:
        """将 Cookie 写入 Netscape 格式临时文件，返回文件路径（供 yt-dlp cookiefile 使用）"""
        if not self._cookie:
            logger.warning("B站 Cookie 未配置，下载可能失败")
            return None
        lines = ["# Netscape HTTP Cookie File\n"]
        for pair in self._cookie.split("; "):
            if "=" in pair:
                key, value = pair.split("=", 1)
                lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{key}\t{value}\n")
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        tmp.writelines(lines)
        tmp.close()
        try:
            os.chmod(tmp.name, 0o600)
        except OSError:
            pass
        logger.info("已生成 B站 Netscape Cookie 文件: %s (条目: %d)", tmp.name, len(lines) - 1)
        return tmp.name

    def _cleanup_cookie_file(self) -> None:
        path = getattr(self, "_cookiefile", None)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._cookiefile = None

    def __del__(self):
        self._cleanup_cookie_file()

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir=self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        # app 层自身的入口 SSRF 校验（#140 复扫 B1）：与 generic/youtube 下载器同款
        # 内部防线——MCP 入口有 _guard_remote_url 兜底，公共 app/ 层函数不依赖外层
        assert_public_http_url(video_url)

        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_path,
            'http_headers': {'Referer': 'https://www.bilibili.com'},
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    # quality 参数真正生效：fast=32k / medium=64k / slow=128k
                    'preferredquality': QUALITY_MAP.get(quality, '64'),
                }
            ],
            'noplaylist': True,
            'quiet': False,
            'progress_hooks': [ytdlp_cancel_hook(cancel_event)],
        }
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # skip_download=True（已有字幕只需元信息）：download=False 只取 metadata
            info = ytdlp_retry(ydl.extract_info, video_url, download=not skip_download)
            video_id = info.get("id")
            title = info.get("title")
            duration = info.get("duration", 0)
            cover_url = info.get("thumbnail")
            audio_path = os.path.join(output_dir, f"{video_id}.mp3")

        return AudioDownloadResult(
            file_path=audio_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="bilibili",
            video_id=video_id,
            raw_info={
                # 仅保留总结流程需要的标签；yt-dlp info 可能包含签名 URL、headers
                # 和 Cookie，不能进入音频缓存或 MCP 任务结果。
                "tags": [tag for tag in (info.get("tags") or []) if isinstance(tag, str)],
            },
            video_path=None  # ❗音频下载不包含视频路径
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """
        下载视频，返回视频文件路径
        """

        if output_dir is None:
            output_dir = get_data_dir()
        os.makedirs(output_dir, exist_ok=True)
        # 入口 SSRF 校验（#140 复扫 B1）：与 download() 同口径
        assert_public_http_url(video_url)
        logger.debug("video_url=%s", sanitize_url(video_url))
        video_id=extract_video_id(video_url, "bilibili")
        if not video_id:
            raise ValueError(f"无法从链接提取 B 站视频 ID: {video_url}")
        # 多 P 视频 yt-dlp 的 id 是 {BV}_pN（缓存名 {BV}_pN.mp4），N 与 URL 的 p 参数一致。
        # 旧前缀 glob（{BV}*.mp4）会误配别的视频（BV12345_p1.mp4 命中 BV1234 的查询）；
        # #121 B5 改 `{BV}_p*.mp4` 仍跨 P 通配——?p=2 请求可能命中 BV_p1.mp4（拿错集视频）。
        # 精确匹配（#122 B2）：带 p → {BV}_p{p}.mp4；无 p → 单集 {BV}.mp4 或分 P 第 1 集
        # {BV}_p1.mp4（都无通配，绝不误配其它视频/其它 P）
        p_num = extract_bilibili_p_number(video_url)
        if p_num:
            existing = _glob.glob(os.path.join(output_dir, f"{video_id}_p{p_num}.mp4"))
        else:
            existing = _glob.glob(os.path.join(output_dir, f"{video_id}.mp4")) or _glob.glob(
                os.path.join(output_dir, f"{video_id}_p1.mp4")
            )
        # 复用前校验非空（docs/05 第 16 轮 B11）：0 字节/半截残留（上次中断）
        # 删掉重下，不把损坏文件当成功产物（kuaishou mp3 同款守卫 #124 B1）
        valid = [p for p in existing if os.path.getsize(p) > 0]
        for stale in existing:
            if os.path.getsize(stale) == 0:
                try:
                    os.unlink(stale)
                except OSError:
                    pass
        if valid:
            return valid[0]

        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bv*[ext=mp4]/bestvideo+bestaudio/best',
            'outtmpl': output_path,
            'http_headers': {'Referer': 'https://www.bilibili.com'},
            'noplaylist': True,
            'quiet': False,
            'merge_output_format': 'mp4',  # 确保合并成 mp4
            'progress_hooks': [ytdlp_cancel_hook(cancel_event)],
        }
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ytdlp_retry(ydl.extract_info, video_url, download=True)
            video_id = info.get("id")
            video_path = os.path.join(output_dir, f"{video_id}.mp4")

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频文件未找到: {video_path}")

        return video_path

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: List[str] = None) -> Optional[TranscriptResult]:
        """
        尝试获取B站视频字幕

        :param video_url: 视频链接
        :param output_dir: 输出路径
        :param langs: 优先语言列表
        :return: TranscriptResult 或 None
        """
        # B 站 AI 字幕需要登录态（SESSDATA cookie）：没配 cookie 时 API 返回空列表，
        # 只能走语音识别。提示用户配置后可跳过转写。
        if not CookieConfigManager().get("bilibili"):
            logger.info(
                "未配置 B 站 SESSDATA cookie：AI 字幕拿不到，将走语音识别。"
                "配置请走 CLI：`! videonote login bilibili`（扫码）或 `videonote setup` 向导；"
                "MCP 工具不收 cookie（安全红线）"
            )
        # 1) 优先走 B 站官方 player API（直拉，无需下视频；AI 字幕需 SESSDATA cookie）
        try:
            result = BilibiliSubtitleFetcher().fetch_subtitles(video_url)
            if result and result.segments:
                return result
        except Exception as e:
            logger.warning(f"player API 直拉字幕异常，回退到 yt-dlp: {e}")

        # 2) Fallback：原 yt-dlp 路径（更脆弱，遇到签名/Cookie 问题失败概率较高）。
        # 调用方没给 output_dir 时落专属临时目录、解析后整体清理——yt-dlp 字幕文件
        # 曾是数据根目录的常驻垃圾（每次回退都留下 {BV}.{lang}.{ext}，从不删除，#121 B6）
        owned_tmpdir = None
        if output_dir is None:
            owned_tmpdir = tempfile.mkdtemp(prefix="videonote_subs_")
            output_dir = owned_tmpdir
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        if langs is None:
            langs = ['zh-Hans', 'zh', 'zh-CN', 'ai-zh', 'en', 'en-US']

        video_id = extract_video_id(video_url, "bilibili")

        ydl_opts = {
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': langs,
            'subtitlesformat': 'srt/json3/best',  # 支持多种格式
            'skip_download': True,
            'outtmpl': os.path.join(output_dir, f'{video_id}.%(ext)s'),
            'quiet': True,
        }

        # 通过 CookieConfigManager 注入 B站 Cookie（Netscape cookiefile）
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile
            ydl_opts['http_headers'] = {'Referer': 'https://www.bilibili.com'}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ytdlp_retry(ydl.extract_info, video_url, download=True)

                # 查找下载的字幕文件
                subtitles = info.get('requested_subtitles') or {}
                if not subtitles:
                    logger.info(f"B站视频 {video_id} 没有可用字幕")
                    return None

                # 按优先级查找字幕
                detected_lang = None
                sub_info = None
                for lang in langs:
                    if lang in subtitles:
                        detected_lang = lang
                        sub_info = subtitles[lang]
                        break

                # 如果按优先级没找到，取第一个可用的（排除弹幕）
                if not detected_lang:
                    for lang, info_item in subtitles.items():
                        if lang != 'danmaku':  # 排除弹幕
                            detected_lang = lang
                            sub_info = info_item
                            break

                if not sub_info:
                    logger.info(f"B站视频 {video_id} 没有可用字幕（排除弹幕）")
                    return None

                # 检查是否有内嵌数据（yt-dlp 有时直接返回字幕内容）
                if 'data' in sub_info and sub_info['data']:
                    logger.info(f"直接从返回数据解析字幕: {detected_lang}")
                    return self._parse_srt_content(sub_info['data'], detected_lang)

                # 查找字幕文件
                ext = sub_info.get('ext', 'srt')
                subtitle_file = os.path.join(output_dir, f"{video_id}.{detected_lang}.{ext}")

                if not os.path.exists(subtitle_file):
                    logger.info(f"字幕文件不存在: {subtitle_file}")
                    return None

                # 根据格式解析字幕文件
                if ext == 'json3':
                    return self._parse_json3_subtitle(subtitle_file, detected_lang)
                else:
                    with open(subtitle_file, 'r', encoding='utf-8') as f:
                        return self._parse_srt_content(f.read(), detected_lang)

        except Exception as e:
            logger.warning(f"获取B站字幕失败: {e}")
            return None
        finally:
            # 自建临时目录：成功（字幕已解析进内存）/失败都清掉，不留垃圾
            if owned_tmpdir:
                shutil.rmtree(owned_tmpdir, ignore_errors=True)

    def _parse_srt_content(self, srt_content: str, language: str) -> Optional[TranscriptResult]:
        """
        解析 SRT 格式字幕内容

        :param srt_content: SRT 字幕文本内容
        :param language: 语言代码
        :return: TranscriptResult
        """
        import re
        try:
            segments = []
            # 部分工具产出的 SRT 是 CRLF 行尾，正则按 \n 匹配会整段失配
            srt_content = srt_content.replace("\r\n", "\n")
            # SRT 格式: 序号\n时间戳\n文本\n\n
            pattern = r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\n\d+\n|$)'
            matches = re.findall(pattern, srt_content, re.DOTALL)

            for match in matches:
                idx, start_time, end_time, text = match
                text = text.strip()
                if not text:
                    continue

                # 转换时间格式 00:00:00,000 -> 秒
                def time_to_seconds(t):
                    parts = t.replace(',', '.').split(':')
                    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

                segments.append(TranscriptSegment(
                    start=time_to_seconds(start_time),
                    end=time_to_seconds(end_time),
                    text=text
                ))

            if not segments:
                return None

            full_text = ' '.join(seg.text for seg in segments)
            logger.info(f"成功解析B站SRT字幕，共 {len(segments)} 段")
            return TranscriptResult(
                language=language,
                full_text=full_text,
                segments=segments,
                raw={'source': 'bilibili_subtitle', 'format': 'srt'}
            )

        except Exception as e:
            logger.warning(f"解析SRT字幕失败: {e}")
            return None

    def _parse_json3_subtitle(self, subtitle_file: str, language: str) -> Optional[TranscriptResult]:
        """
        解析 json3 格式字幕文件

        :param subtitle_file: 字幕文件路径
        :param language: 语言代码
        :return: TranscriptResult
        """
        try:
            with open(subtitle_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            segments = []
            events = data.get('events', [])

            for event in events:
                # json3 格式中时间单位是毫秒
                start_ms = event.get('tStartMs', 0)
                duration_ms = event.get('dDurationMs', 0)

                # 提取文本
                segs = event.get('segs', [])
                text = ''.join(seg.get('utf8', '') for seg in segs).strip()

                if text:  # 只添加非空文本
                    segments.append(TranscriptSegment(
                        start=start_ms / 1000.0,
                        end=(start_ms + duration_ms) / 1000.0,
                        text=text
                    ))

            if not segments:
                return None

            full_text = ' '.join(seg.text for seg in segments)

            logger.info(f"成功解析B站字幕，共 {len(segments)} 段")
            return TranscriptResult(
                language=language,
                full_text=full_text,
                segments=segments,
                raw={'source': 'bilibili_subtitle', 'file': subtitle_file}
            )

        except Exception as e:
            logger.warning(f"解析字幕文件失败: {e}")
            return None