import threading
from abc import ABC
from typing import Optional

from app.models.gpt_model import GPTSource


class GPT(ABC):
    def summarize(
        self,
        source: GPTSource,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        '''

        :param source: 
        :return:
        '''
        pass
    def create_messages(self, segments:list,**kwargs)->list:
        pass