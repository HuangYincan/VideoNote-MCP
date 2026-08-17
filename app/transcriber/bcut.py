import json
import os
import time
from typing import List, Optional

import requests

from app.decorators.timeit import timeit
from app.events import transcription_finished
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.utils.logger import get_logger

__version__ = "0.0.3"

API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"

# 申请上传
API_REQ_UPLOAD = API_BASE_URL + "/resource/create"

# 提交上传
API_COMMIT_UPLOAD = API_BASE_URL + "/resource/create/complete"

# 创建任务
API_CREATE_TASK = API_BASE_URL + "/task"

# 查询结果
API_QUERY_RESULT = API_BASE_URL + "/task/result"

# 语音识别模型 id：创建任务与查询结果必须一致（旧代码 create 用 "8"、query 用 7，
# 不一致会导致查询永远查不到建的任务）
_BCUT_MODEL_ID = 7

logger = get_logger(__name__)

class BcutTranscriber(Transcriber):
    """必剪 语音识别接口"""
    headers = {
        'User-Agent': 'Bilibili/1.0.0 (https://www.bilibili.com)',
        'Content-Type': 'application/json'
    }

    def __init__(self):
        self.session = requests.Session()
        self.task_id = None
        self.__etags = []

        self.__in_boss_key: Optional[str] = None
        self.__resource_id: Optional[str] = None
        self.__upload_id: Optional[str] = None
        self.__upload_urls: List[str] = []
        self.__per_size: Optional[int] = None
        self.__clips: Optional[int] = None

        self.__etags: List[str] = []
        self.__download_url: Optional[str] = None
        self.task_id: Optional[str] = None

    def close(self) -> None:
        """显式释放 requests.Session（连接池/打开 fd）。"""
        if getattr(self, "session", None) is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None

    def __del__(self):
        # 每任务新建实例；实例被 GC 时兜底释放连接（#123 B11）
        try:
            self.close()
        except Exception:
            pass
        
    def _upload(self, file_path: str) -> None:
        """申请上传"""
        size = os.path.getsize(file_path)
        if not size:
            raise ValueError("无法读取文件数据")

        payload = json.dumps({
            "type": 2,
            "name": "audio.mp3",
            "size": size,
            "ResourceFileType": "mp3",
            "model_id": "8",
        })

        resp = self.session.post(
            API_REQ_UPLOAD,
            data=payload,
            headers=self.headers,
            timeout=(10, 30)
        )
        resp.raise_for_status()
        resp = resp.json()
        # 业务层 code 检查：接口失败时返回 {code: 非0, message}，直接取 data 会
        # KeyError 裸崩（调用方只看到天书般的 traceback）（#121 B8）
        if resp.get("code") != 0:
            msg = resp.get("message") or resp.get("msg") or "未知错误"
            raise RuntimeError(f"必剪申请上传失败: code={resp.get('code')}, {msg}")
        resp_data = resp["data"]

        self.__in_boss_key = resp_data["in_boss_key"]
        self.__resource_id = resp_data["resource_id"]
        self.__upload_id = resp_data["upload_id"]
        self.__upload_urls = resp_data["upload_urls"]
        self.__per_size = resp_data["per_size"]
        self.__clips = len(resp_data["upload_urls"])

        logger.info(
            f"申请上传成功, 总计大小{resp_data['size'] // 1024}KB, {self.__clips}分片, 分片大小{resp_data['per_size'] // 1024}KB: {self.__in_boss_key}"
        )
        self.__upload_part(file_path)
        self.__commit_upload()

    def __upload_part(self, file_path: str) -> None:
        """上传音频数据（按分片从文件分段读，#125 B15：不再整文件载入 + 切片复制，
        多 GB 音频峰值内存从 ~2× 文件大小降到 per_size）"""
        if not self.__per_size or not self.__clips:
            # per_size=0 时 f.read(0) 上传空块，报错是晦涩的 HTTP 400（#126 B5）
            raise RuntimeError(
                f"必剪返回异常分片参数（per_size={self.__per_size}, clips={self.__clips}），无法上传"
            )
        with open(file_path, "rb") as f:
            for clip in range(self.__clips):
                start_range = clip * self.__per_size
                f.seek(start_range)
                chunk = f.read(self.__per_size)
                logger.info(f"开始上传分片{clip}: {start_range}-{start_range + len(chunk)}")
                resp = self.session.put(
                    self.__upload_urls[clip],
                    data=chunk,
                    headers={'Content-Type': 'application/octet-stream'},
                    timeout=(10, 120)
                )
                resp.raise_for_status()
                # header 存在但值为 None（异常网关响应）时 `.strip` 抛 AttributeError——
                # 分片上传失败信息会变成天书（#124 B10）
                etag = (resp.headers.get("Etag") or "").strip('"')
                self.__etags.append(etag)
                logger.info(f"分片{clip}上传成功: {etag}")

    def __commit_upload(self) -> None:
        """提交上传数据"""
        data = json.dumps({
            "InBossKey": self.__in_boss_key,
            "ResourceId": self.__resource_id,
            "Etags": ",".join(self.__etags),
            "UploadId": self.__upload_id,
            "model_id": "8",
        })
        resp = self.session.post(
            API_COMMIT_UPLOAD,
            data=data,
            headers=self.headers,
            timeout=(10, 30)
        )
        resp.raise_for_status()
        resp = resp.json()

        if resp.get("code") != 0:
            error_msg = f"上传提交失败: {resp.get('message', '未知错误')}"
            logger.error(error_msg)
            raise Exception(error_msg)
            
        self.__download_url = resp["data"]["download_url"]
        logger.info(f"提交成功，下载链接: {self.__download_url}")

    def _create_task(self) -> str:
        """开始创建转换任务"""
        resp = self.session.post(
            API_CREATE_TASK, json={"resource": self.__download_url, "model_id": _BCUT_MODEL_ID}, headers=self.headers,
            timeout=(10, 30)
        )
        resp.raise_for_status()
        resp = resp.json()
        if resp.get("code") != 0:
            error_msg = f"创建任务失败: {resp.get('message', '未知错误')}"
            logger.error(error_msg)
            raise Exception(error_msg)
            
        self.task_id = resp["data"]["task_id"]
        logger.info(f"任务已创建: {self.task_id}")
        return self.task_id

    def _query_result(self) -> dict:
        """查询转换结果"""
        resp = self.session.get(
            API_QUERY_RESULT,
            params={"model_id": _BCUT_MODEL_ID, "task_id": self.task_id},
            headers=self.headers,
            timeout=(5, 10)
        )
        resp.raise_for_status()
        resp = resp.json()
        if resp.get("code") != 0:
            error_msg = f"查询结果失败: {resp.get('message', '未知错误')}"
            logger.error(error_msg)
            raise Exception(error_msg)
            
        return resp["data"]

    @timeit
    def transcript(self, file_path: str) -> TranscriptResult:
        """执行识别过程，符合 Transcriber 接口"""
        try:
            logger.info(f"开始处理文件: {file_path}")
            
            # 上传文件
            logger.info("正在上传文件...")
            self._upload(file_path)
            
            # 创建任务
            logger.info("提交转录任务...")
            self._create_task()
            
            # 轮询检查任务状态
            logger.info("等待转录结果...")
            task_resp = None
            max_retries = 500
            for i in range(max_retries):
                task_resp = self._query_result()
                
                if task_resp["state"] == 4:  # 完成状态
                    break
                elif task_resp["state"] == 3:  # 失败状态
                    error_msg = f"B站ASR任务失败，状态码: {task_resp['state']}"
                    logger.error(error_msg)
                    raise Exception(error_msg)
                    
                # 每隔一段时间打印进度
                if i % 10 == 0:
                    logger.info(f"转录进行中... {i}/{max_retries}")
                    
                # 指数退避轮询 1→2→4→5s 封顶(B站 ASR 常需数十秒,不空转)
                time.sleep(min(1 << i, 5))
                
            if not task_resp or task_resp["state"] != 4:
                error_msg = f"B站ASR任务未能完成，状态: {task_resp.get('state') if task_resp else 'Unknown'}"
                logger.error(error_msg)
                raise Exception(error_msg)
                
            # 解析结果
            logger.info("转录成功，处理结果...")
            result_json = json.loads(task_resp["result"])
            
            # 提取分段数据
            segments = []
            
            for u in result_json.get("utterances", []):
                text = (u.get("transcript") or "").strip()  # API 返回 null 不裸崩（#126 B5）
                # B站ASR返回的时间戳是毫秒，需要转换为秒
                start_time = float(u.get("start_time", 0)) / 1000.0
                end_time = float(u.get("end_time", 0)) / 1000.0
                
                segments.append(TranscriptSegment(
                    start=start_time,
                    end=end_time,
                    text=text
                ))
            
            # 创建结果对象
            result = TranscriptResult(
                language=result_json.get("language", "zh"),
full_text=" ".join(seg.text for seg in segments).strip(),
                segments=segments,
                raw=result_json
            )
            
            # 触发完成事件
            # self.on_finish(file_path, result)
            
            return result
            
        except Exception as e:
            logger.error(f"B站ASR处理失败: {str(e)}")
            raise

    def on_finish(self, video_path: str, result: TranscriptResult) -> None:
        """转录完成的回调"""
        logger.info(f"B站ASR转写完成: {video_path}")
        transcription_finished.send({
            "file_path": video_path,
        })