import threading
from abc import ABC, abstractmethod
from typing import Optional

from app.models.transcriber_model import TranscriptResult


class Transcriber(ABC):
    @abstractmethod
    def transcript(
        self,
        file_path: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> TranscriptResult:
        '''

        :param file_path:音频路径
        :param cancel_event:可选取消事件
        :return: 返回一个 TranscriptResult 类
        '''
        pass