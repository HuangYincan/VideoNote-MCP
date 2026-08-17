from abc import ABC, abstractmethod

from app.models.transcriber_model import TranscriptResult


class Transcriber(ABC):
    @abstractmethod
    def transcript(self,file_path:str)->TranscriptResult:
        '''

        :param file_path:音频路径
        :return: 返回一个 TranscriptResult 类
        '''
        pass