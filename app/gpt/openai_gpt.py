from typing import List
from app.gpt.base import GPT
from app.gpt.prompt import BASE_PROMPT, AI_SUM, SCREENSHOT, LINK
from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider
from app.models.gpt_model import GPTSource
from app.models.transcriber_model import TranscriptSegment


class OpenaiGPT(GPT):
    def __init__(self):
        from os import getenv
        self.api_key = getenv("OPENAI_API_KEY")
        self.base_url = getenv("OPENAI_API_BASE_URL")
        self.model=getenv('OPENAI_MODEL')
        print(self.model)
        self.client = OpenAICompatibleProvider(api_key=self.api_key, base_url=self.base_url)
        self.screenshot = False
        self.link=False

    def _format_time(self, seconds: float) -> str:
        # ≥1h 保留小时位，<1h 输出 MM:SS（如 03:15）
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _build_segment_text(self, segments: List[TranscriptSegment]) -> str:
        return "\n".join(
            f"{self._format_time(seg.start)} - {seg.text.strip()}"
            for seg in segments
        )

    def ensure_segments_type(self, segments) -> List[TranscriptSegment]:
        return [
            TranscriptSegment(**seg) if isinstance(seg, dict) else seg
            for seg in segments
        ]

    def create_messages(self, segments: List[TranscriptSegment], title: str,tags:str):
        content = BASE_PROMPT.format(
            video_title=title,
            segment_text=self._build_segment_text(segments),
            tags=tags
        )
        if self.screenshot:
            print(":需要截图")
            content += SCREENSHOT
        if self.link:
            print(":需要链接")
            content += LINK

        print(content)
        return [{"role": "user", "content": content + AI_SUM}]
    def list_models(self):
        return self.client.list_models()
    def summarize(self, source: GPTSource) -> str:
        self.screenshot = source.screenshot
        self.link = source.link
        source.segment = self.ensure_segments_type(source.segment)
        messages = self.create_messages(source.segment, source.title,source.tags)
        response = self.client.chat(
            model=self.model,
            messages=messages,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()

if __name__ == '__main__':
    gpt = OpenaiGPT()
    print(gpt.list_models())
