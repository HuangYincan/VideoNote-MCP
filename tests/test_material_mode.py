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


def test_skip_download_media_cache_miss_clears_audio_path():
    """skip_download 时媒体缓存 miss：悬空 file_path 置 None（#119）。

    字幕路径的 audio.file_path 是拼出来的假路径（文件不存在）；跨任务媒体缓存
    miss 是常态（字幕路径从不写媒体缓存）。此前原样透传给素材包 → audio_path
    指向不存在的文件，Agent Read 失败；现在置 None + info 留痕。
    """
    gen = NoteGenerator()
    task_id = "media_miss_task"
    task_dir = NOTE_OUTPUT_DIR / task_id
    cache_file = task_dir / "gen" / "audio.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    fake_downloader = mock.Mock()
    fake_downloader.download.return_value = AudioDownloadResult(
        file_path=str(task_dir / "raw" / "BV1xx.mp3"),  # 悬空路径（文件不存在）
        title="测试视频",
        duration=10.0,
        cover_url=None,
        platform="bilibili",
        video_id="BV1xx411c7mD",
        raw_info={},
    )
    with mock.patch("app.services.note.note_cache.lookup_media", return_value=None), \
         mock.patch.object(gen, "_update_status"):
        audio = gen._download_media(
            downloader=fake_downloader,
            video_url="https://www.bilibili.com/video/BV1xx411c7mD",
            quality="fast",
            audio_cache_file=cache_file,
            status_phase="DOWNLOADING",
            platform="bilibili",
            output_path=None,
            screenshot=False,
            video_understanding=False,
            video_interval=None,
            grid_size=None,
            skip_download=True,
        )
    # 悬空路径被清掉：素材包 audio_path 得到 None 而非指向不存在文件的假路径
    assert audio.file_path is None
    assert audio.duration == 10.0  # 元信息仍在
    # audio.json 落盘的是 None（诚实契约），下次缓存加载分支也一致
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["file_path"] is None
    shutil.rmtree(task_dir, ignore_errors=True)


def test_skip_download_keeps_real_local_path():
    """#122 B1：skip_download 时本地文件返回的是真实源路径，必须保留。

    #119 的修复缺存在性检查：`not audio.file_path`（falsy）把 skip_download 下
    LocalDownloader 直接回的真实 video_url 也吞成 None——二次跑本地文件素材包
    audio_path 恒空。is_file() 通过的真实路径应原样保留。
    """
    import tempfile

    gen = NoteGenerator()
    task_id = "media_local_task"
    task_dir = NOTE_OUTPUT_DIR / task_id
    cache_file = task_dir / "gen" / "audio.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    # 真实存在的本地视频文件：LocalDownloader.download(skip_download=True) 原样返回
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as nf:
        local_path = nf.name
    fake_downloader = mock.Mock()
    fake_downloader.download.return_value = AudioDownloadResult(
        file_path=local_path,  # 存在 → 必须保留
        title="本地视频",
        duration=5.0,
        cover_url=None,
        platform="local",
        video_id=None,
        raw_info={},
    )
    with mock.patch("app.services.note.note_cache.lookup_media", return_value=None), \
         mock.patch.object(gen, "_update_status"):
        audio = gen._download_media(
            downloader=fake_downloader,
            video_url=local_path,
            quality="fast",
            audio_cache_file=cache_file,
            status_phase="DOWNLOADING",
            platform="local",
            output_path=None,
            screenshot=False,
            video_understanding=False,
            video_interval=None,
            grid_size=None,
            skip_download=True,
        )
    try:
        # 真实存在的本地路径不被吞掉（#122 B1 修复点）
        assert audio.file_path == local_path
    finally:
        Path(local_path).unlink(missing_ok=True)
        shutil.rmtree(task_dir, ignore_errors=True)


def test_skip_download_media_cache_hit_reuses_real_file():
    """媒体缓存命中：file_path 指向复制的真实文件（不为 None）。"""
    gen = NoteGenerator()
    task_id = "media_hit_task"
    task_dir = NOTE_OUTPUT_DIR / task_id
    cache_file = task_dir / "gen" / "audio.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    fake_downloader = mock.Mock()
    fake_downloader.download.return_value = AudioDownloadResult(
        file_path=str(task_dir / "raw" / "BV1xx.mp3"),
        title="测试视频",
        duration=10.0,
        cover_url=None,
        platform="bilibili",
        video_id="BV1xx411c7mD",
        raw_info={},
    )
    with mock.patch(
        "app.services.note.note_cache.lookup_media", return_value="/cache/real.mp3"
    ), mock.patch.object(gen, "_update_status"):
        audio = gen._download_media(
            downloader=fake_downloader,
            video_url="https://www.bilibili.com/video/BV1xx411c7mD",
            quality="fast",
            audio_cache_file=cache_file,
            status_phase="DOWNLOADING",
            platform="bilibili",
            output_path=None,
            screenshot=False,
            video_understanding=False,
            video_interval=None,
            grid_size=None,
            skip_download=True,
        )
    assert audio.file_path == "/cache/real.mp3"
    shutil.rmtree(task_dir, ignore_errors=True)


def test_audio_cache_none_file_path_treated_as_stale():
    """audio.json 的 file_path=None（#119 置空路径）在需要真实音频时视为缓存失效。

    此前 falsy 判断把 None 当「缓存有效」直接返回 → 转写时 Path(None) 抛误导性
    TypeError（切换转写引擎后重跑同视频的回归链，#120）。
    """
    gen = NoteGenerator()
    task_id = "media_stale_task"
    task_dir = NOTE_OUTPUT_DIR / task_id
    cache_file = task_dir / "gen" / "audio.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "file_path": None,
                "title": "测试视频",
                "duration": 10.0,
                "platform": "bilibili",
                "video_id": "BV1xx411c7mD",
                "raw_info": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_downloader = mock.Mock()
    fake_downloader.download.return_value = AudioDownloadResult(
        file_path=str(task_dir / "raw" / "BV1xx.mp3"),
        title="测试视频",
        duration=10.0,
        cover_url=None,
        platform="bilibili",
        video_id="BV1xx411c7mD",
        raw_info={},
    )
    with mock.patch.object(gen, "_update_status"):
        audio = gen._download_media(
            downloader=fake_downloader,
            video_url="https://www.bilibili.com/video/BV1xx411c7mD",
            quality="fast",
            audio_cache_file=cache_file,
            status_phase="DOWNLOADING",
            platform="bilibili",
            output_path=None,
            screenshot=False,
            video_understanding=False,
            video_interval=None,
            grid_size=None,
            skip_download=False,
        )
    # 缓存未直接返回：走重新下载，拿到真实路径
    assert audio.file_path == str(task_dir / "raw" / "BV1xx.mp3")
    fake_downloader.download.assert_called()
    shutil.rmtree(task_dir, ignore_errors=True)


def test_format_screenshot_forces_video_download():
    """format 直接声明 "screenshot"（screenshot 布尔 False）时仍下载视频。

    否则 prompt 注入的标记指令让 LLM 输出 *Screenshot-[mm:ss]，但 video_path=None
    时替换被跳过 → 标记残留（#120）。
    """
    import app.services.note as note_mod

    gen = NoteGenerator()
    task_id = "fmt_shot_task"
    task_dir = NOTE_OUTPUT_DIR / task_id
    (task_dir / "gen").mkdir(parents=True, exist_ok=True)

    fake_dl = mock.Mock()
    fake_dl.download_subtitles.return_value = TranscriptResult(
        language="zh",
        full_text="hello world",
        segments=[TranscriptSegment(start=0, end=5, text="hello")],
    )
    video_file = task_dir / "raw" / "v.mp4"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"fake-video")
    fake_dl.download_video.return_value = str(video_file)
    fake_dl.download.return_value = AudioDownloadResult(
        file_path=str(task_dir / "raw" / "a.mp3"),
        title="测试视频",
        duration=10.0,
        cover_url=None,
        platform="bilibili",
        video_id="BV1xx411c7mD",
        raw_info={},
    )
    with mock.patch.object(gen, "_update_status"), \
         mock.patch.object(gen, "_get_downloader", return_value=fake_dl), \
         mock.patch.object(note_mod, "VideoReader") as m_reader, \
         mock.patch.object(note_mod, "_extract_audio_from_video",
                           return_value=str(task_dir / "raw" / "a.mp3")), \
         mock.patch.object(note_mod.note_cache, "promote_transcript"), \
         mock.patch.object(note_mod.note_cache, "promote_media"):
        m_reader.return_value.run.return_value = []
        gen.generate(
            video_url="https://www.bilibili.com/video/BV1xx411c7mD",
            platform="bilibili",
            quality="fast",
            task_id=task_id,
            screenshot=False,
            _format=["screenshot"],  # format 直接声明截图
            material_only=True,
        )
    fake_dl.download_video.assert_called_once()
    assert gen.video_path == video_file
    shutil.rmtree(task_dir, ignore_errors=True)


def test_screenshot_insert_failure_logs_exception_detail():
    """截图插入失败此前只记「跳过该步骤」无异常详情（同文件 link 分支带 {e}）→ 补上（#120）。"""
    import app.services.note as note_mod

    gen = NoteGenerator()
    gen._insert_screenshots = mock.Mock(side_effect=RuntimeError("ffmpeg 不存在"))
    with mock.patch.object(note_mod.logger, "warning") as m_warn:
        out = gen._post_process_markdown(
            markdown="*Screenshot-[01:23]",
            video_path=Path("/tmp/fake.mp4"),
            formats=["screenshot"],
            audio_meta=None,
            platform="bilibili",
        )
    assert out == "*Screenshot-[01:23]"  # 失败跳过，标记保留（不裸抛）
    assert any("ffmpeg 不存在" in str(c) for c in m_warn.call_args_list)


class TestPortableDirReserve:
    """便携笔记目录原子占用（#123 B7）：`exists()` 预检是 TOCTOU，两个并发任务
    （同 notes_dir + 同标题）都选同一目录会互相 rmtree 对方 Assets/。
    改 `mkdir(exist_ok=False)` 原子占用，冲突回退后缀。
    """

    def test_normal_title(self, tmp_path):
        from app.services.note import _reserve_portable_dir

        out = _reserve_portable_dir("我的标题", "t1", tmp_path)
        assert out == tmp_path / "我的标题"
        assert out.is_dir()

    def test_conflict_appends_task_suffix(self, tmp_path):
        from app.services.note import _reserve_portable_dir

        (tmp_path / "同标题").mkdir()
        out = _reserve_portable_dir("同标题", "abcdef", tmp_path)
        assert out == tmp_path / "同标题-abcdef"
        assert out.is_dir()

    def test_concurrent_same_title_never_share_dir(self, tmp_path):
        """模拟并发：两次分配同标题，绝不能返回同一目录（原子占用保证）。"""
        from app.services.note import _reserve_portable_dir

        a = _reserve_portable_dir("并发标题", "aaa111", tmp_path)
        b = _reserve_portable_dir("并发标题", "bbb222", tmp_path)
        assert a != b
        assert a.is_dir() and b.is_dir()

    def test_title_suffix_and_task_id_all_taken_falls_back_to_random(self, tmp_path):
        from app.services.note import _reserve_portable_dir

        (tmp_path / "标题").mkdir()
        (tmp_path / "标题-abc123").mkdir()
        (tmp_path / "abc123").mkdir()
        out = _reserve_portable_dir("标题", "abc123", tmp_path)
        assert out.is_dir()
        assert out.name.startswith("abc123-")  # 极端兜底：task_id + 随机段

    def test_empty_title_uses_task_id(self, tmp_path):
        from app.services.note import _reserve_portable_dir

        out = _reserve_portable_dir(None, "t999", tmp_path)
        assert out == tmp_path / "t999"


if __name__ == "__main__":
    test_prepare_note_material_no_provider_needed()
    test_build_note_material_persists_frames()
    test_run_note_task_writes_material_payload()
    test_skip_download_media_cache_miss_clears_audio_path()
    test_skip_download_media_cache_hit_reuses_real_file()
    test_audio_cache_none_file_path_treated_as_stale()
    test_format_screenshot_forces_video_download()
    test_screenshot_insert_failure_logs_exception_detail()
    print("ALL_OK")
