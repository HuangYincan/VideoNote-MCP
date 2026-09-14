"""GPU 空闲释放 + CUDA 判据的回归测试（2026-09-14 高烈度评审修掉的缺陷）。

覆盖的缺陷（逐条对应评审发现，详见 memory/gpu-patch-review-findings）：

1. **env 解析容错** —— 裸 `int(os.environ.get(...))` 在 **import 期** 抛 ValueError，
   使 `transcriber_provider` 无法导入 → `videonote_mcp.server` 起不来 → MCP 全部工具
   不可用（连 health_check 都没了）。用子进程做真实的导入验证。
2. **`is_cuda()` 判据顺序** —— 把 `is_torch_installed()` 排在 ctranslate2 之前时，
   装了 CPU 版 torch（Windows 上 `pip install torch` 的默认产物，且项目 funasr /
   diarization 两个 extra 都会装）的用户永远走不到 ct2 分支 → GPU 静默不启用。
   断言：新顺序是旧顺序的**超集**（只修正误判，绝不把 True 变 False）。
3. **空闲释放的 5 条安全性质**：代次守卫（孤儿回调作废）、空闲复检（刚取用则作废并
   补挂）、忙时非阻塞跳过、无 `_lock` 时 fail-closed、只释放 whisper。
4. **释放后单例保留 + `transcript()` 自愈** —— 摘掉单例槽位会让自愈重建的模型成为
   「游离模型」（释放扫描看不到 → 永不归还；新任务再建一个 → 双份显存）。
5. **`object.__new__` 实例调 `transcript()` 不抛** —— `finally` 里裸读 `self.device`
   会在无该属性的实例上抛 AttributeError 并顶掉 `return`（曾让 2 个既有单测回归）。
6. **哨兵日志不影响构造** —— torch 安装损坏时 `import torch` 抛 `OSError`
   （`is_torch_installed` 只捕 ImportError），会把「显式 device='cpu'」变成构造失败。

不碰真实模型 / GPU / 网络：全部 mock。释放逻辑直接调用 `_release_idle_gpu` 驱动，
不走真实计时器（避免测试等待 180s）。
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO_ROOT = Path(__file__).resolve().parents[1]


class EnvIntTest(unittest.TestCase):
    """env 整数解析必须容错：非法值回默认、越界夹取，绝不抛异常。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber import transcriber_provider as tp

        cls.tp = tp

    def _read(self, raw):
        with mock.patch.dict(os.environ, {"VN_TEST_ENV_INT": raw}):
            return self.tp._env_int("VN_TEST_ENV_INT", 180, lo=0, hi=86400)

    def test_valid_passthrough(self):
        self.assertEqual(self._read("300"), 300)

    def test_whitespace_tolerated(self):
        self.assertEqual(self._read(" 300 "), 300)

    def test_non_numeric_falls_back(self):
        # 旧实现：int('abc') → ValueError → 模块 import 崩 → MCP 起不来
        self.assertEqual(self._read("abc"), 180)

    def test_empty_falls_back(self):
        # 部分客户端对未填项传空串而非省略
        self.assertEqual(self._read(""), 180)

    def test_float_string_falls_back(self):
        self.assertEqual(self._read("180.5"), 180)

    def test_unset_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VN_TEST_ENV_INT", None)
            self.assertEqual(self.tp._env_int("VN_TEST_ENV_INT", 180, lo=0, hi=86400), 180)

    def test_clamped_low(self):
        self.assertEqual(self._read("-5"), 0)

    def test_clamped_high(self):
        # 旧实现：threading.Timer(9.2e9+) 会让计时器线程 OverflowError 立刻死掉
        # → 等于静默禁用释放 + 每次转写往 stderr 刷 traceback
        self.assertEqual(self._read("99999999999"), 86400)


class ModuleImportSurvivesBadEnvTest(unittest.TestCase):
    """真实回归点：非法 env 值下 `videonote_mcp.server` 仍必须能导入。

    这是「配错一个环境变量 → 整个 MCP 起不来」的端到端守卫。用子进程跑，
    因为 ValueError 发生在 import 期，同进程无法恢复。
    """

    def _import_server(self, value):
        env = dict(os.environ)
        env["VIDEONOTE_GPU_IDLE_RELEASE_SEC"] = value
        env["PYTHONPATH"] = str(_REPO_ROOT)
        # 必须显式指定 encoding：子进程 stderr 里有 UTF-8 中文日志，若按 locale
        # （Windows 中文环境是 GBK）解码会在读取线程里 UnicodeDecodeError。
        return subprocess.run(
            [sys.executable, "-c", "import app.transcriber.transcriber_provider"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(_REPO_ROOT), timeout=120,
        )

    def test_bad_values_do_not_break_import(self):
        for bad in ("abc", "", "180.5", "99999999999", "-1"):
            with self.subTest(value=bad):
                r = self._import_server(bad)
                self.assertEqual(
                    r.returncode, 0,
                    f"env={bad!r} 导致导入失败：{r.stderr[-400:]}",
                )


class IsCudaOrderTest(unittest.TestCase):
    """`is_cuda()` 必须问「跑推理的引擎」而非「torch 装没装」，且是旧行为的超集。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber.whisper import WhisperTranscriber

        cls.W = WhisperTranscriber

    def _is_cuda(self, torch_cuda, torch_installed, ct2_ok):
        with mock.patch("app.transcriber.whisper.is_cuda_available", return_value=torch_cuda), \
             mock.patch("app.transcriber.whisper.is_torch_installed", return_value=torch_installed), \
             mock.patch("app.transcriber.whisper.is_ct2_cuda_available", return_value=ct2_ok):
            return self.W.is_cuda()

    def test_cpu_only_torch_does_not_block_ct2(self):
        """核心回归：装了 CPU 版 torch（Windows 上 pip 的默认产物）时，
        ctranslate2 的 CUDA 后端可用就必须用 GPU —— 旧实现的 elif 顺序会返回 False。"""
        self.assertTrue(self._is_cuda(False, True, True))

    def test_ct2_failure_falls_back_to_cpu(self):
        self.assertFalse(self._is_cuda(False, False, False))

    def test_torch_cuda_still_honored(self):
        """torch 自带 CUDA 但 ct2 探测失败时仍返回 True（保留旧行为，不改坏）。"""
        self.assertTrue(self._is_cuda(True, True, False))

    def test_superset_of_old_implementation(self):
        """不变量：任何组合下，新实现都不允许把旧的 True 判成 False。"""
        for torch_cuda in (False, True):
            for torch_installed in (False, True):
                for ct2_ok in (False, True):
                    with self.subTest(t=torch_cuda, ti=torch_installed, c=ct2_ok):
                        new = self._is_cuda(torch_cuda, torch_installed, ct2_ok)
                        # 旧实现（原 elif 顺序）的等价判定
                        old = torch_cuda or (False if torch_installed else ct2_ok)
                        self.assertTrue(
                            new or not old,
                            f"新实现把旧的 True 变成了 False：torch_cuda={torch_cuda} "
                            f"torch_installed={torch_installed} ct2={ct2_ok}",
                        )


class _FakeTranscriber:
    """释放扫描的目标替身：只需 device / _lock / model / close()。"""

    def __init__(self, device="cuda", with_lock=True, has_model=True):
        self.device = device
        self.model = object() if has_model else None
        self._lock = threading.Lock() if with_lock else None
        self.closed = 0

    def close(self):
        self.model = None
        self.closed += 1


class IdleReleaseTest(unittest.TestCase):
    """空闲释放的 5 条安全性质。直接调 `_release_idle_gpu` 驱动，不等真实计时器。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber import transcriber_provider as tp

        cls.tp = tp
        cls.K = tp.TranscriberType.FAST_WHISPER
        cls.K_FUNASR = tp.TranscriberType.FUNASR

    def setUp(self):
        self._saved = (
            dict(self.tp._transcribers), self.tp._idle_timer,
            self.tp._idle_gen, self.tp._gpu_last_touch,
        )
        self.tp._idle_timer = None

    def tearDown(self):
        transcribers, timer, gen, touch = self._saved
        if self.tp._idle_timer is not None:
            try:
                self.tp._idle_timer.cancel()
            except Exception:
                pass
        self.tp._transcribers.clear()
        self.tp._transcribers.update(transcribers)
        self.tp._idle_timer, self.tp._idle_gen, self.tp._gpu_last_touch = timer, gen, touch

    def _install(self, inst, idle=True):
        """把替身放进单例，并把空闲基准推到「已超时」。"""
        self.tp._transcribers[self.K] = inst
        self.tp._idle_gen += 1000                       # 与历史代次解耦
        self.tp._gpu_last_touch = (
            time.monotonic() - (self.tp._GPU_IDLE_RELEASE_SEC + 100) if idle
            else time.monotonic()
        )
        return self.tp._idle_gen

    def test_releases_when_idle(self):
        inst = _FakeTranscriber()
        gen = self._install(inst)
        self.tp._release_idle_gpu(gen)
        self.assertIsNone(inst.model)
        self.assertEqual(inst.closed, 1)

    def test_stale_generation_is_ignored(self):
        """孤儿回调：Timer.cancel() 对已进入回调的计时器无效，它必须认出自己被取代。

        否则它会把 **新** 计时器的引用置空 → 新计时器失去引用、再也 cancel 不到
        → 可能在一批任务中途卸载模型。
        """
        inst = _FakeTranscriber()
        gen = self._install(inst)
        self.tp._release_idle_gpu(gen - 1)              # 过期代次
        self.assertIsNotNone(inst.model)
        self.assertEqual(inst.closed, 0)

    def test_recent_touch_revokes_release_and_rearms(self):
        """实例刚被取走（还没进 transcript()）时不得释放。

        实例 _lock 只能证明「没有正在进行的 transcript()」，不能证明「没有任务持有
        实例引用」—— 那个窗口靠 _gpu_last_touch 复检覆盖。
        """
        inst = _FakeTranscriber()
        gen = self._install(inst, idle=False)           # 刚取用
        self.tp._release_idle_gpu(gen)
        self.assertIsNotNone(inst.model)
        self.assertEqual(inst.closed, 0)
        self.assertIsNotNone(self.tp._idle_timer, "作废后必须补挂计时器")

    def test_busy_lock_skips_without_blocking(self):
        """忙时跳过，且**不阻塞**（阻塞会造成危险交错）。"""
        inst = _FakeTranscriber()
        gen = self._install(inst)
        ready, release = threading.Event(), threading.Event()

        def holder():
            with inst._lock:
                ready.set()
                release.wait(10)

        th = threading.Thread(target=holder, daemon=True)
        th.start()
        self.assertTrue(ready.wait(10))
        try:
            t0 = time.monotonic()
            self.tp._release_idle_gpu(gen)
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            th.join(5)
        self.assertIsNotNone(inst.model, "正在转写时不得关闭模型")
        self.assertEqual(inst.closed, 0)
        self.assertLess(elapsed, 1.0, "释放路径不得等待实例锁")

    def test_missing_lock_fails_closed(self):
        """无法确认空闲时不得释放（fail-closed）。旧写法会无条件 close。"""
        inst = _FakeTranscriber(with_lock=False)
        gen = self._install(inst)
        self.tp._release_idle_gpu(gen)
        self.assertIsNotNone(inst.model)
        self.assertEqual(inst.closed, 0)

    def test_cpu_instance_not_released(self):
        inst = _FakeTranscriber(device="cpu")
        gen = self._install(inst)
        self.tp._release_idle_gpu(gen)
        self.assertEqual(inst.closed, 0)

    def test_only_whisper_is_released(self):
        """funasr 从未挂计时器；顺手释放它只会白付一次分钟级模型重载。"""
        inst = _FakeTranscriber()
        funasr = _FakeTranscriber()
        gen = self._install(inst)
        self.tp._transcribers[self.K_FUNASR] = funasr
        self.tp._release_idle_gpu(gen)
        self.assertIsNone(inst.model)
        self.assertIsNotNone(funasr.model, "不得跨引擎释放 funasr")
        self.assertEqual(funasr.closed, 0)

    def test_release_keeps_singleton_registered(self):
        """释放只关模型、**不摘单例**。

        摘掉槽位会让 transcript() 自愈重建出的模型成为「游离模型」：
        ① 释放扫描看不到它 → 永不归还显存；② 新任务见槽位为 None 会再建一个 → 双份显存。
        """
        inst = _FakeTranscriber()
        gen = self._install(inst)
        self.tp._release_idle_gpu(gen)
        self.assertIs(self.tp._transcribers[self.K], inst, "实例必须留在单例里")
        self.assertIsNone(inst.model)

    def test_already_released_is_skipped(self):
        """已释放的实例（model 为空）不再重复 close。"""
        inst = _FakeTranscriber(has_model=False)
        gen = self._install(inst)
        self.tp._release_idle_gpu(gen)
        self.assertEqual(inst.closed, 0)


class SelfHealTest(unittest.TestCase):
    """`transcript()` 在模型被释放后按原尺寸重建，而不是抛误导性 AttributeError。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber.whisper import WhisperTranscriber

        cls.W = WhisperTranscriber

    def _bare_instance(self, *, with_device=True):
        """模拟不经 __init__ 构造的实例（测试与反序列化路径都会这样）。"""
        tr = object.__new__(self.W)
        tr._lock = threading.Lock()
        tr.model_size = "tiny"
        if with_device:
            tr.device = "cuda"
            tr.compute_type = "float16"
        return tr

    def _fake_model(self):
        seg = mock.Mock()
        seg.text = "  你好  "
        seg.start, seg.end = 0.0, 1.0
        m = mock.Mock()
        m.transcribe = mock.Mock(return_value=(iter([seg]), mock.Mock(language="zh")))
        return m

    def test_rebuilds_model_when_released(self):
        tr = self._bare_instance()
        tr.model = None                                  # 被空闲释放关掉
        fake = self._fake_model()
        with mock.patch.object(self.W, "_build_model", return_value=fake) as build, \
             mock.patch("app.transcriber.whisper.get_model_dir", return_value="/tmp/x"):
            result = tr.transcript("whatever.mp3")
        self.assertEqual(build.call_count, 1, "必须按原尺寸重建")
        self.assertEqual(result.full_text, "你好")
        self.assertIs(tr.model, fake)

    def test_no_rebuild_when_model_present(self):
        tr = self._bare_instance()
        tr.model = self._fake_model()
        with mock.patch.object(self.W, "_build_model") as build, \
             mock.patch("app.transcriber.whisper.get_model_dir", return_value="/tmp/x"):
            tr.transcript("whatever.mp3")
        self.assertEqual(build.call_count, 0, "模型在时不重建")

    def test_instance_without_device_attribute_does_not_raise(self):
        """回归守卫：`finally` 里裸读 `self.device` 曾让 2 个既有单测失败。

        `finally` 抛出的 AttributeError 会顶掉 `return`，把「返回结果」变成「抛异常」。
        """
        tr = self._bare_instance(with_device=False)
        tr.model = self._fake_model()
        result = tr.transcript("whatever.mp3")
        self.assertEqual(result.full_text, "你好")


class SentinelLogTest(unittest.TestCase):
    """设备哨兵日志是诊断信息，绝不能影响构造。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber.whisper import WhisperTranscriber

        cls.W = WhisperTranscriber

    def test_cpu_construction_survives_broken_torch(self):
        """torch 安装损坏时 `import torch` 抛 OSError（is_torch_installed 只捕
        ImportError）。显式 device='cpu' 这条路径按设计不碰 torch，不得因此构造失败。"""
        with mock.patch.object(self.W, "_build_model", return_value=mock.Mock()), \
             mock.patch("app.transcriber.whisper.get_model_dir", return_value="/tmp/x"), \
             mock.patch("app.transcriber.whisper.is_torch_installed",
                        side_effect=OSError("DLL load failed while importing torch")):
            tr = self.W(model_size="tiny", device="cpu")
        self.assertEqual(tr.device, "cpu")
        self.assertEqual(tr.compute_type, "int8")


if __name__ == "__main__":
    unittest.main()
