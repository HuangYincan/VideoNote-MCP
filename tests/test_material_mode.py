"""material_only 素材包模式的验证脚本。

兼容两种运行方式：
- 直接运行：python tests/test_material_mode.py
- pytest 运行（环境装了 pytest 时）：pytest tests/test_material_mode.py

覆盖点：
1. prepare_note_material：mock 掉 _run_note_task 后直接调用工具，验证返回
   {task_id, status: PENDING, kind: material}，且不需要 provider/model（不触发任何
   provider 相关校验；提交参数里没有 provider_id / model_name）。
2. _build_note_material：用假 base64 帧验证帧落盘 + file:// 绝对路径，非 data URI
   的条目被跳过不落盘。
"""
import base64
import json
import shutil
import sys
from pathlib import Path
from unittest import mock

# 确保能 import videonote_mcp.server（vendored app.* 在其内部 import）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须先 import server：其模块顶层 setup_environment() 会把 NOTE_OUTPUT_DIR 等
# 环境变量设为数据目录的绝对路径，之后的 app.services.note 才会拿到绝对输出目录
import videonote_mcp.server as server

from app.models.audio_model import AudioDownloadResult
from app.models.notes_model import NoteResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.note import NOTE_OUTPUT_DIR, NoteGenerator


class _FakeFuture:
    """最小 Future 替身：mock 掉线程池后 _task_futures 仍可正常写入。"""

    def __init__(self):
        self._done = False

    def done(self):
        return self._done

    def cancel(self):
        self._done = True
        return True


def _cleanup_registry():
    for tid in list(server._task_futures.keys()):
        server._task_futures.pop(tid, None)
        server._task_events.pop(tid, None)


def test_prepare_note_material_no_provider_needed():
    _cleanup_registry()
    with mock.patch.object(server, "_run_note_task"), mock.patch.object(server, "_pool") as m_pool:
        m_pool.submit.return_value = _FakeFuture()
        resp = json.loads(
            server.prepare_note_material(
                video_url="https://www.bilibili.com/video/BV1xx411c7mD",
                video_understanding=True,
                grid_size=[3, 3],
                include_comments=True,
            )
        )
    assert resp["status"] == "PENDING"
    assert resp["kind"] == "material"
    assert resp["task_id"]
    # 提交给后台的 params 里必须是 material_only=True，且不携带 provider_id / model_name
    _args, kwargs = m_pool.submit.call_args
    assert kwargs.get("material_only") is True
    assert "provider_id" not in kwargs
    assert "model_name" not in kwargs
    _cleanup_registry()


def test_build_note_material_persists_frames():
    gen = NoteGenerator()
    task_id = "material_test_task"
    fake_bytes = b"fake-jpeg-bytes-12345"
    fake_jpg = base64.b64encode(fake_bytes).decode()
    gen.video_img_urls = [f"data:image/jpeg;base64,{fake_jpg}", "not-a-data-uri"]
    gen.video_path = Path("/tmp/fake_video.mp4")
    audio_meta = AudioDownloadResult(
        file_path="/tmp/fake_audio.mp3",
        title="测试视频",
        duration=10.0,
        cover_url=None,
        platform="bilibili",
        video_id="BV1xx411c7mD",
        raw_info={},
    )
    transcript = TranscriptResult(
        language="zh",
        full_text="你好世界",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="你好世界")],
    )
    material = gen._build_note_material(task_id, audio_meta, transcript, comments_danmaku="【弹幕】测试")

    assert material["title"] == "测试视频"
    assert material["audio_path"] == "/tmp/fake_audio.mp3"
    assert material["video_path"] == "/tmp/fake_video.mp4"
    assert material["comments_danmaku"] == "【弹幕】测试"
    assert material["transcript"]["language"] == "zh"
    assert material["transcript"]["full_text"] == "你好世界"
    assert material["transcript"]["segments"][0]["text"] == "你好世界"

    frames_dir = NOTE_OUTPUT_DIR / task_id / "gen" / "frames"
    frame_file = frames_dir / "frame_1.jpg"
    assert frame_file.exists(), f"帧文件应落盘: {frame_file}"
    assert frame_file.read_bytes() == fake_bytes
    assert material["frames"] == [frame_file.as_uri()]
    assert material["frames"][0].startswith("file://")
    # 第二条不是 data URI，应被跳过，不产生 frame_2.jpg
    assert not (frames_dir / "frame_2.jpg").exists()

    # 空帧 → frames=[]（material_only 且没开抽帧的常见路径）
    gen.video_img_urls = []
    empty_material = gen._build_note_material(task_id, audio_meta, transcript, None)
    assert empty_material["frames"] == []

    shutil.rmtree(frames_dir, ignore_errors=True)


def test_run_note_task_writes_material_payload():
    """_run_note_task 收到带 material 的 NoteResult 时，{task_id}.json 应写素材包 payload。"""
    task_id = "material_run_task"
    transcript = TranscriptResult(
        language="zh",
        full_text="你好世界",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="你好世界")],
    )
    audio_meta = AudioDownloadResult(
        file_path="/tmp/fake_audio.mp3",
        title="测试视频",
        duration=10.0,
        cover_url=None,
        platform="bilibili",
        video_id="BV1xx411c7mD",
        raw_info={},
    )
    note_result = NoteResult(
        markdown="",
        transcript=transcript,
        audio_meta=audio_meta,
        material={
            "title": "测试视频",
            "transcript": {
                "language": "zh",
                "full_text": "你好世界",
                "segments": [{"start": 0.0, "end": 1.0, "text": "你好世界"}],
            },
            "frames": ["file:///tmp/fake_frame.jpg"],
            "comments_danmaku": "【弹幕】测试",
            "video_path": "/tmp/fake_video.mp4",
            "audio_path": "/tmp/fake_audio.mp3",
        },
    )
    with mock.patch.object(server, "NoteGenerator") as m_gen:
        m_gen.return_value.generate.return_value = note_result
        server._run_note_task(task_id, None)

    out_file = server.NOTE_OUTPUT_DIR / task_id / "result.json"
    assert out_file.exists(), f"结果文件应落盘: {out_file}"
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["kind"] == "material"
    assert data["title"] == "测试视频"
    assert data["transcript"]["full_text"] == "你好世界"
    assert data["frames"] == ["file:///tmp/fake_frame.jpg"]
    assert data["comments_danmaku"] == "【弹幕】测试"
    assert data["video_path"] == "/tmp/fake_video.mp4"
    assert data["audio_path"] == "/tmp/fake_audio.mp3"
    import shutil as _sh

    _sh.rmtree(server.NOTE_OUTPUT_DIR / task_id, ignore_errors=True)


if __name__ == "__main__":
    test_prepare_note_material_no_provider_needed()
    test_build_note_material_persists_frames()
    test_run_note_task_writes_material_payload()
    print("ALL_OK")
