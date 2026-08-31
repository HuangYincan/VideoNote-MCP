import datetime
import json
import os
import re
import threading
from typing import Optional, Union
from urllib.parse import quote, urlencode

import httpx
import requests
from pydantic import BaseModel

from app.downloaders.base import Downloader
from app.downloaders.common import stream_download
from app.downloaders.douyin_helper.abogus import ABogus
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir
from app.utils.url_safety import public_head, sanitize_error_text, sanitize_url

logger = get_logger(__name__)
from dotenv import load_dotenv

if not os.environ.get("VIDEONOTE_DATA_DIR"):
    load_dotenv()
DOUYIN_DOMAIN = "https://www.douyin.com"

cfm = None  # 惰性单例（B13）：import 不构造 CookieConfigManager，避免落空 downloader.json


def _get_cfm():
    global cfm
    if cfm is None:
        cfm = CookieConfigManager()
    return cfm


def get_timestamp(unit: str = "milli"):
    """
    根据给定的单位获取当前时间 (Get the current time based on the given unit)

    Args:
        unit (str): 时间单位，可以是 "milli"、"sec"、"min" 等
            (The time unit, which can be "milli", "sec", "min", etc.)

    Returns:
        int: 根据给定单位的当前时间 (The current time based on the given unit)
    """

    now = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    if unit == "milli":
        return int(now.total_seconds() * 1000)
    elif unit == "sec":
        return int(now.total_seconds())
    elif unit == "min":
        return int(now.total_seconds() / 60)
    else:
        raise ValueError("Unsupported time unit")


class DouyinConfig:
    HEADERS = {
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36",
        "Referer": "https://www.douyin.com/",
        "Cookie": None
    }

    PROXIES = {
        "http": None,
        "https": None,
    }

    MS_TOKEN = {
        "url": "https://mssdk.bytedance.com/web/report",
        "magic": 538969122,
        "version": 1,
        "dataType": 8,
        "strData": "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",
        "User-Agent": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36 Edg/117.0.2045.47"
    }

    TTWID = {
        "url": "https://ttwid.bytedance.com/ttwid/union/register/",
        "data": '{"region":"cn","aid":1768,"needFid":false,"service":"www.ixigua.com","migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https","union":true}'
    }


class BaseRequestModel(BaseModel):
    device_platform: str = "webapp"
    aid: str = "6383"
    channel: str = "channel_pc_web"
    pc_client_type: int = 1
    version_code: str = "290100"
    version_name: str = "29.1.0"
    cookie_enabled: str = "true"
    screen_width: int = 1920
    screen_height: int = 1080
    browser_language: str = "zh-CN"
    browser_platform: str = "Win32"
    browser_name: str = "Chrome"
    browser_version: str = "130.0.0.0"
    browser_online: str = "true"
    engine_name: str = "Blink"
    engine_version: str = "130.0.0.0"
    os_name: str = "Windows"
    os_version: str = "10"
    cpu_core_num: int = 12
    device_memory: int = 8
    platform: str = "PC"
    downlink: str = "10"
    effective_type: str = "4g"
    from_user_page: str = "1"
    locate_query: str = "false"
    need_time_list: str = "1"
    pc_libra_divert: str = "Windows"
    publish_video_strategy_type: str = "2"
    round_trip_time: str = "0"
    show_live_replay_strategy: str = "1"
    time_list_query: str = "0"
    whale_cut_token: str = ""
    update_version_code: str = "170400"
    msToken: str = None


class DouyinDownloader(Downloader):
    def __init__(self, cookie=None):
        super().__init__()
        self.headers_config = DouyinConfig.HEADERS.copy()
        self.headers_config["Cookie"] = _get_cfm().get('douyin')
        # 不要 print headers：Cookie 会进 mcp_stderr.log / 会话日志
        self.proxies_config = DouyinConfig.PROXIES.copy()
        self.ttwid_config = DouyinConfig.TTWID.copy()
        self.ms_token_config = DouyinConfig.MS_TOKEN.copy()
        # C2（docs/05 第 16 轮）：同一任务内 download_video + download(skip_download)
        # 各调一次 fetch_video_info——memo 按 aweme_id 复用，省 1 次 msToken POST +
        # aweme GET + 签名（msToken 分钟级有效，任务内复用安全）
        self._info_cache: dict = {}
        self._ms_token: Optional[str] = None
        self._bogus = ABogus()

    @staticmethod
    def find_url(string: str) -> list:
        url = re.findall('http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', string)
        return url

    def extract_video_id(self, url: str) -> str:
        video_url = DouyinDownloader.find_url(url)

        if len(video_url):
            video_url = video_url[0]
            try:
                # public_head 逐跳校验（#140）：入口 URL 公网后重定向到内网的
                # Location 在发出前拦截（入口校验覆盖不到 redirect 目标）
                response = public_head(video_url, timeout=(5, 10))
                url = response.url
            except Exception:
                return ""
        patterns = [
            r'video/(\d+)',
            r'aweme_id=(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return ""

    def gen_real_msToken(self) -> str:
        # msToken 是分钟级有效会话 cookie（docs/05 第 16 轮 C2）：任务内复用，
        # 不再每次 fetch_video_info 都 POST mssdk.bytedance.com
        if self._ms_token:
            return self._ms_token
        try:
            payload = json.dumps(
                {
                    "magic": self.ms_token_config["magic"],
                    "version": self.ms_token_config["version"],
                    "dataType": self.ms_token_config["dataType"],
                    "strData": self.ms_token_config["strData"],
                    "tspFromClient": get_timestamp(),
                }
            )
            headers = {
                "User-Agent": self.headers_config["User-Agent"],
                "Content-Type": "application/json",
            }
            transport = httpx.HTTPTransport(retries=5)
            with httpx.Client(transport=transport) as client:
                try:
                    response = client.post(
                        self.ms_token_config["url"], content=payload, headers=headers
                    )
                    response.raise_for_status()

                    msToken = str(httpx.Cookies(response.cookies).get("msToken"))
                    if len(msToken) not in [120, 128]:
                        raise ValueError("响应内容：{0}， Douyin msToken API 的响应内容不符合要求。".format(msToken))

                    self._ms_token = msToken
                    return msToken
                except Exception as e:
                    raise ValueError("Douyin msToken API 请求失败：%s" % sanitize_error_text(e)) from e
        except Exception as e:
            raise ValueError("Douyin msToken API%s" % sanitize_error_text(e)) from e

    def fetch_video_info(self, video_url: str) -> json:
        # memo（docs/05 第 16 轮 C2）：download_video 与 download(skip) 同任务双调时复用
        aweme_id = self.extract_video_id(video_url)
        if aweme_id and aweme_id in self._info_cache:
            return self._info_cache[aweme_id]
        try:
            kwargs = self.headers_config
            base_params = BaseRequestModel().model_dump()
            base_params["msToken"] = self.gen_real_msToken()

            base_params["aweme_id"] = aweme_id
            ab_value = self._bogus.get_value(base_params)
            a_bogus = quote(ab_value, safe='')
            logger.debug("a_bogus 签名已生成")
            query_str = urlencode(base_params)
            full_url = f"{DOUYIN_DOMAIN}/aweme/v1/web/aweme/detail/?{query_str}&a_bogus={a_bogus}"

            logger.debug("抖音 API 请求 URL 已构造")

            response = requests.get(full_url, headers=kwargs, timeout=(5, 10))

            result = response.json()
            if aweme_id:
                self._info_cache[aweme_id] = result
            return result
        except Exception as e:
            logger.warning("抖音视频信息请求失败: %s", sanitize_error_text(e))
            # 旧写法 ValueError("请求失败:", e) 是元组参数——str() 输出
            # ('请求失败:', <异常>)，且无 from e 丢失原始链（#124 B4）
            raise ValueError(f"请求失败: {sanitize_error_text(e)}") from e
        # print(kwargs)

    def download(
            self,
            video_url: str,
            output_dir: Union[str, None] = None,
            quality: DownloadQuality = "fast",
            need_video: Optional[bool] = False,
            skip_download: bool = False,
            cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult:
        # 日志只打脱敏 URL（可能含签名 token，docs/05 第 16 轮 A5）
        logger.info("正在下载视频: %s，保存路径: %s，质量: %s", sanitize_url(video_url), output_dir, quality)
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        video_data = self.fetch_video_info(video_url)
        detail = video_data.get('aweme_detail') or {}
        aweme_id = detail.get('aweme_id') or ''
        if not aweme_id:
            raise ValueError(f"抖音接口未返回 aweme_id: {sanitize_url(video_url)}")
        title = detail.get('item_title') or '抖音视频'
        # douyin aweme_detail.video.duration 单位是**毫秒**（如 15.3s 视频返回 15300），
        # 与 bilibili/youtube 的秒口径不一致——归一为秒（#124 B6）
        duration = int((detail.get('video') or {}).get('duration', 0)) / 1000.0
        tags = [t.get('tag_name') for t in (detail.get('video_tag') or []) if t.get('tag_name')]

        output_path = os.path.join(output_dir, f"{aweme_id}.mp3")

        if not skip_download:
            # play_url 的 uri 是播放键不是完整 URL；用 url_list[0] 兜底 uri
            music = (detail.get('music') or {}).get('play_url') or {}
            url = (music.get('url_list') or [None])[0] or music.get('uri')
            if not url:
                raise RuntimeError("抖音接口未返回音频播放地址")
            # 用 self.headers_config（已注入 cookie）而非类级 DOUYIN HEADERS——
            # 类属性 Cookie 恒 None，用户配置的 cookie 对音频请求不生效（#124 B5）
            # 连接/读分离超时 + 退避重试 + 取消检查（docs/05 第 16 轮 B4/B1）
            stream_download(
                url, output_path, headers=self.headers_config, cancel_event=cancel_event
            )

        # 封面：优先 cover_original_scale → cover → dynamic_cover；
        # 旧代码的 else 分支引用了不存在的顶层 video_data['video']，会 KeyError
        video_info = detail.get('video') or {}
        cover_url = ""
        for key in ("cover_original_scale", "cover", "dynamic_cover"):
            u = (video_info.get(key) or {}).get("url_list") or []
            if u:
                cover_url = u[0]
                break

        return AudioDownloadResult(
            file_path=output_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="douyin",
            video_id=aweme_id,
            raw_info={
                'tags': (detail.get('caption') or '') + ''.join(tags),
            },
            video_path=None  # ❗音频下载不包含视频路径
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:

        try:

            if output_dir is None:
                output_dir = get_data_dir()
            if not output_dir:
                output_dir = self.cache_data
            os.makedirs(output_dir, exist_ok=True)

            video_id = self.extract_video_id(video_url)
            video_path = os.path.join(output_dir, f"{video_id}.mp4")
            if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                return video_path
            # 0 字节/半截残留（上次中断，docs/05 第 16 轮 B11）：删掉重下
            if os.path.exists(video_path):
                try:
                    os.unlink(video_path)
                except OSError:
                    pass

            # 直接 Path 拼接（#123 B10）：旧实现 output_path % {...} 对整个字符串做
            # %-格式化——output_dir 含字面 %（如 /tmp/100%off/）→ ValueError 下载失败。
            video_data = self.fetch_video_info(video_url)
            detail = video_data.get('aweme_detail') or {}
            aweme_id = detail.get('aweme_id') or ''
            if not aweme_id:
                raise ValueError(f"抖音接口未返回 aweme_id: {sanitize_url(video_url)}")
            output_path = os.path.join(output_dir, f"{aweme_id}.mp4")

            # 与 download() 同口径：.get() 链 + 显式错误（#127 B6），
            # 截图/视频理解路径上 API 异常不再多层裸索引天书
            url_list = ((detail.get('video') or {}).get('download_addr') or {}).get('url_list') or []
            if not url_list:
                raise RuntimeError("抖音接口未返回视频下载地址")
            url = url_list[0]
            # 连接/读分离超时 + 退避重试 + 取消检查（docs/05 第 16 轮 B4/B1）
            stream_download(
                url, output_path, headers=self.headers_config, cancel_event=cancel_event
            )

            return output_path
        except Exception as e:
            logger.warning("抖音下载请求失败: %s", sanitize_error_text(e))
            raise ValueError(f"抖音下载请求失败: {sanitize_error_text(e)}") from e



if __name__ == '__main__':
    dy = DouyinDownloader(
        cookie='')

    dy.download(
        '7.43 11/16 gba:/ j@P.xS 以“马成钢”的视角打开《抓娃娃》笼中鸟，何时飞 # 独白 # 人物故事  https://v.douyin.com/0pcFVdG_lx4/ 复制此链接，打开Dou音搜索，直接观看视频！'
    )
