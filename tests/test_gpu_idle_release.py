"""GPU 空闲释放 + CUDA 判据的回归测试（2026-09-14 高烈度评审修掉的缺陷）。

覆盖的缺陷：

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
        from app.utils.env_checker import env_int

        # staticmethod：否则 self.env_int(...) 会把 self 绑成第一个位置参数
        cls.env_int = staticmethod(env_int)

    def _read(self, raw):
        with mock.patch.dict(os.environ, {"VN_TEST_ENV_INT": raw}):
            return self.env_int("VN_TEST_ENV_INT", 180, lo=0, hi=86400)

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
            self.assertEqual(self.env_int("VN_TEST_ENV_INT", 180, lo=0, hi=86400), 180)

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
                        # 旧实现只以 torch.cuda.is_available() 为唯一判据
                        # （torch_installed 只影响日志，ct2 当时根本没被探测）
                        old = torch_cuda
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


class _BuildableCudaFake:
    """provider 构造/替换路径用的替身：接受 model_size/device，device=cuda。"""

    def __init__(self, model_size=None, device="cuda"):
        self.device = "cuda"
        self.model_size = model_size
        self.model = object()
        self._lock = threading.Lock()
        self._retired = False

    def close(self):
        self.model = None

    def retire(self):
        self._retired = True
        self.model = None


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

    def _stub_transcriber(self, device="cuda", model_size="tiny"):
        inst = _FakeTranscriber(device=device)
        inst.model_size = model_size
        return inst

    def test_schedule_cancels_previous_and_rearms(self):
        """`schedule_gpu_idle_release` 先 cancel 旧计时器再挂新的：批量连续任务期间
        旧计时器不会残留、也就不会在一批任务中途卸载模型。"""
        old = mock.Mock()
        self.tp._idle_timer = old
        self.tp._gpu_last_touch = 0.0
        with mock.patch.object(self.tp, "_arm_idle_timer") as arm:
            self.tp.schedule_gpu_idle_release()
        old.cancel.assert_called_once()
        arm.assert_called_once_with(self.tp._GPU_IDLE_RELEASE_SEC)
        self.assertGreater(self.tp._gpu_last_touch, 0.0, "取用/结束时必须刷新空闲基准")
        self.assertIs(self.tp._idle_timer, old, "本测试打桩了 _arm_idle_timer，引用不变")

    def test_schedule_noop_when_disabled(self):
        """`VIDEONOTE_GPU_IDLE_RELEASE_SEC=0` 时不得挂计时器，也不改空闲基准。"""
        self.tp._gpu_last_touch = 12345.0
        with mock.patch.object(self.tp, "_GPU_IDLE_RELEASE_SEC", 0), \
             mock.patch.object(self.tp, "_arm_idle_timer") as arm:
            self.tp.schedule_gpu_idle_release()
        arm.assert_not_called()
        self.assertEqual(self.tp._gpu_last_touch, 12345.0)

    def test_hand_out_touches_gpu_instance(self):
        """`_get_or_build_transcriber` 取用即刷新空闲基准（覆盖「已取走但还没进
        transcript()」的窗口，如预处理模式下中间隔一次 ffmpeg 归一化）。"""
        inst = self._stub_transcriber(device="cuda")
        self.tp._transcribers[self.K] = inst
        self.tp._gpu_last_touch = 0.0
        got = self.tp._get_or_build_transcriber(self.K, _FakeTranscriber, model_size="tiny")
        self.assertIs(got, inst)
        self.assertGreater(self.tp._gpu_last_touch, 0.0)

    def test_hand_out_does_not_touch_cpu_instance(self):
        inst = self._stub_transcriber(device="cpu")
        self.tp._transcribers[self.K] = inst
        self.tp._gpu_last_touch = 0.0
        self.tp._get_or_build_transcriber(self.K, _FakeTranscriber, model_size="tiny")
        self.assertEqual(self.tp._gpu_last_touch, 0.0, "CPU 无显存可释放，不应挂计时器基准")

    def test_new_gpu_instance_arms_idle_timer(self):
        """R4：模型首次加载成功即挂计时器。此前只有 transcript() 的 finally 才挂，
        若任务在加载后、进入 transcript() 前被取消（或预处理在 ASR 前失败），模型已
        驻留显存却永远没有回调释放它。"""
        self.tp._transcribers[self.K] = None
        with mock.patch.object(self.tp, "schedule_gpu_idle_release") as sched:
            self.tp._get_or_build_transcriber(self.K, _BuildableCudaFake, model_size="tiny")
        sched.assert_called_once()

    def test_cpu_instance_build_does_not_arm_timer(self):
        class _CpuBuildable(_BuildableCudaFake):
            def __init__(self, model_size=None, device="cpu"):
                super().__init__(model_size=model_size, device=device)
                self.device = "cpu"

        self.tp._transcribers[self.K] = None
        with mock.patch.object(self.tp, "schedule_gpu_idle_release") as sched:
            self.tp._get_or_build_transcriber(self.K, _CpuBuildable, model_size="tiny")
        sched.assert_not_called()

    def test_size_switch_retires_old_instance(self):
        """R2：尺寸切换必须 retire() 旧实例（区别于空闲卸载），否则旧任务下一块会按
        旧尺寸自愈重建出脱离注册表的游离模型。"""
        old = _BuildableCudaFake(model_size="small")
        self.tp._transcribers[self.K] = old
        new = self.tp._get_or_build_transcriber(self.K, _BuildableCudaFake, model_size="tiny")
        self.assertIsNot(new, old)
        self.assertTrue(old._retired, "旧实例必须被标记退役")
        self.assertIsNone(old.model)
        self.assertIs(self.tp._transcribers[self.K], new)

    def test_release_rechecks_touch_after_waiting_for_cache_lock(self):
        """S1/R5：复检通过后、真正 close 前若发生新取用，必须作废释放。

        旧实现先复检再等 `_cache_lock`；等待期间的新取用不会被看到，模型会被关掉。
        新实现把「复检 + close」收进同一把 `_idle_lock`，复检在取锁之后进行。
        """
        inst = _FakeTranscriber()
        gen = self._install(inst)                      # 基准推到已超时
        result = {}

        def run():
            self.tp._release_idle_gpu(gen)
            result["done"] = True

        with self.tp._cache_lock:                      # 卡住 release 的第一次取锁
            th = threading.Thread(target=run, daemon=True)
            th.start()
            time.sleep(0.05)
            with self.tp._idle_lock:
                self.tp._touch_gpu_use_locked()        # 等待期间的新取用
        th.join(5)
        self.assertTrue(result.get("done"), "release 必须在 cache 锁释放后继续")
        self.assertIsNotNone(inst.model, "新取用后不得关闭刚使用的模型")
        self.assertEqual(inst.closed, 0)
        self.assertIsNotNone(self.tp._idle_timer, "作废后必须补挂计时器")

    def test_release_rechecks_generation_after_waiting_for_cache_lock(self):
        """S1/R5：等待 `_cache_lock` 期间发生的计时器换代同样必须作废释放。"""
        inst = _FakeTranscriber()
        gen = self._install(inst)
        result = {}

        def run():
            self.tp._release_idle_gpu(gen)
            result["done"] = True

        with self.tp._cache_lock:
            th = threading.Thread(target=run, daemon=True)
            th.start()
            time.sleep(0.05)
            with self.tp._idle_lock:                   # 换代：旧回调已成孤儿
                self.tp._idle_gen += 1
                self.tp._idle_timer = None
        th.join(5)
        self.assertTrue(result.get("done"))
        self.assertIsNotNone(inst.model, "换代后旧回调不得关闭模型")
        self.assertEqual(inst.closed, 0)


class Ct2ProbeTest(unittest.TestCase):
    """ctranslate2 CUDA 探测有两层要求：

    1. cuBLAS 与 cuBLASLt 缺一不可（否则「探测说能用、首次 transcribe 才 FAILED」）；
    2. 探测的那一代必须**匹配已安装 ctranslate2 的构建目标** —— 官方 wheel 的 CUDA
       主版本编译期写死，只装另一代运行库的机器必须回落 CPU，不能误判 GPU 可用。
    """

    @classmethod
    def setUpClass(cls):
        import app.utils.env_checker as ec

        cls.ec = ec
        cls.libs = (
            ec._CT2_CUDA_LIBS_WIN if sys.platform == "win32" else ec._CT2_CUDA_LIBS_POSIX
        )

    def _loadable(self, names, major):
        with mock.patch.object(self.ec, "_ct2_cuda_major", return_value=major), \
             mock.patch.object(
                 self.ec, "_load_shared_library", side_effect=lambda n: n in names
             ):
            return self.ec._ct2_cuda_libs_loadable()

    def test_requires_both_cublas_and_cublaslt(self):
        pair = self.libs[12]
        # 只装到 cuBLAS（缺 cuBLASLt）→ 判不可用
        self.assertFalse(self._loadable({pair[0]}, 12))
        # 一对齐全 → 可用
        self.assertTrue(self._loadable(set(pair), 12))

    def test_other_cuda_generation_is_rejected(self):
        """负例（原测试固化了错误行为）：目标是 CUDA 12，机器只装了 CUDA 11 那对
        时必须是 False —— ctranslate2 wheel 不会跨代回退 ABI，实际 GPU 路径仍缺
        CUDA 12 库，误判会让「CPU 能跑」变成「GPU 一跑就 FAILED」。

        同一对库在目标确为 CUDA 11 时仍应判可用（不同 ctranslate2 版本目标不同）。
        """
        if 11 not in self.libs:
            self.skipTest("当前平台无 CUDA 11 候选库名")
        self.assertFalse(self._loadable(set(self.libs[11]), 12))
        self.assertTrue(self._loadable(set(self.libs[11]), 11))

    def test_major_from_installed_ct2_version(self):
        """构建目标按已安装 ctranslate2 大版本推断：4.x → CUDA 12，3.x 及更早 → 11。"""
        for ver, expected in {
            "4.8.1": 12,
            "4.0.0": 12,
            "3.24.0": 11,
            "2.0.0": 11,
        }.items():
            with self.subTest(version=ver):
                fake = mock.Mock()
                fake.__version__ = ver
                with mock.patch.dict(sys.modules, {"ctranslate2": fake}):
                    self.assertEqual(self.ec._ct2_cuda_major(), expected)

    def test_major_defaults_when_version_unknown(self):
        """拿不到版本号时按当前目标（CUDA 12）处理：探测不到就回落 CPU，不误报。"""

        class _NoVersion:
            pass

        with mock.patch.dict(sys.modules, {"ctranslate2": _NoVersion()}):
            self.assertEqual(
                self.ec._ct2_cuda_major(), self.ec._CT2_DEFAULT_CUDA_MAJOR
            )

    def test_no_device_falls_back(self):
        fake = mock.Mock()
        fake.get_cuda_device_count.return_value = 0
        with mock.patch.dict(sys.modules, {"ctranslate2": fake}):
            self.assertFalse(self.ec.is_ct2_cuda_available())

    def test_missing_libs_falls_back(self):
        fake = mock.Mock()
        fake.get_cuda_device_count.return_value = 1
        with mock.patch.dict(sys.modules, {"ctranslate2": fake}), \
             mock.patch.object(self.ec, "_ct2_cuda_libs_loadable", return_value=False):
            self.assertFalse(self.ec.is_ct2_cuda_available())

    def test_available_returns_true(self):
        fake = mock.Mock()
        fake.get_cuda_device_count.return_value = 1
        with mock.patch.dict(sys.modules, {"ctranslate2": fake}), \
             mock.patch.object(self.ec, "_ct2_cuda_libs_loadable", return_value=True):
            self.assertTrue(self.ec.is_ct2_cuda_available())


class IsCudaProbeGuardTest(unittest.TestCase):
    """`is_cuda()` 的 torch 兜底分支：torch 安装损坏（抛 OSError 而非 ImportError）时
    不得让探测崩溃，应视为不可用并回落 CPU。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber.whisper import WhisperTranscriber

        cls.W = WhisperTranscriber

    def test_broken_torch_probe_does_not_raise(self):
        with mock.patch("app.transcriber.whisper.is_ct2_cuda_available", return_value=False), \
             mock.patch("app.transcriber.whisper.is_cuda_available",
                        side_effect=OSError("DLL load failed while importing torch")):
            self.assertFalse(self.W.is_cuda())


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

    def test_self_heal_purges_on_corrupt_cache(self):
        """自愈重建与 __init__ 共用同一加载路径：cache 损坏时同样 purge + 重下，
        而不是直接抛错（否则被空闲释放过的模型遇到损坏 cache 就永久失败）。"""
        tr = self._bare_instance()
        tr.model = None
        fake = self._fake_model()
        calls = {"n": 0}

        def build(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("truncated cache")
            return fake

        with mock.patch.object(self.W, "_build_model", side_effect=build), \
             mock.patch.object(self.W, "_purge_cache") as purge, \
             mock.patch("app.transcriber.whisper.get_model_dir", return_value="/tmp/x"):
            result = tr.transcript("whatever.mp3")
        purge.assert_called_once()
        self.assertEqual(result.full_text, "你好")
        self.assertIs(tr.model, fake)

    def test_instance_without_device_attribute_does_not_raise(self):
        """回归守卫：`finally` 里裸读 `self.device` 曾让 2 个既有单测失败。

        `finally` 抛出的 AttributeError 会顶掉 `return`，把「返回结果」变成「抛异常」。
        """
        tr = self._bare_instance(with_device=False)
        tr.model = self._fake_model()
        result = tr.transcript("whatever.mp3")
        self.assertEqual(result.full_text, "你好")


class RetireAndCancelTest(unittest.TestCase):
    """尺寸切换退役（R2）与重建后取消（S3）。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber.whisper import WhisperTranscriber

        cls.W = WhisperTranscriber

    def _bare_instance(self, *, retired=False):
        tr = object.__new__(self.W)
        tr._lock = threading.Lock()
        tr.model_size = "tiny"
        tr.device = "cuda"
        tr.compute_type = "float16"
        tr.model = None
        tr._retired = retired
        return tr

    def _fake_model(self):
        seg = mock.Mock()
        seg.text = "  你好  "
        seg.start, seg.end = 0.0, 1.0
        m = mock.Mock()
        m.transcribe = mock.Mock(return_value=(iter([seg]), mock.Mock(language="zh")))
        return m

    def test_retired_instance_delegates_same_size_instead_of_rebuilding(self):
        """退役实例不得按旧尺寸重建游离模型；同尺寸的受管实例可安全转交。"""
        tr = self._bare_instance(retired=True)
        delegate = mock.Mock()
        delegate.model_size = "tiny"                     # 与任务请求尺寸一致
        delegate.transcript = mock.Mock(return_value="delegated")
        with mock.patch(
            "app.transcriber.transcriber_provider.get_current_whisper_transcriber",
            return_value=delegate,
        ), mock.patch.object(self.W, "_build_model") as build:
            result = tr.transcript("chunk.mp3")
        self.assertEqual(result, "delegated")
        delegate.transcript.assert_called_once_with("chunk.mp3", None)
        build.assert_not_called()
        self.assertIsNone(tr.model, "退役实例不得自己重建模型")

    def test_retired_instance_different_size_registered_raises(self):
        """Spec 1：退化时不得静默改用**不同尺寸**的注册实例（会污染原尺寸缓存键）。"""
        from app.transcriber.whisper import TranscriberRetiredError

        tr = self._bare_instance(retired=True)           # 任务请求 tiny
        other = mock.Mock()
        other.model_size = "large-v3"                    # 注册表已是别的尺寸
        with mock.patch(
            "app.transcriber.transcriber_provider.get_current_whisper_transcriber",
            return_value=other,
        ), mock.patch.object(self.W, "_build_model") as build:
            with self.assertRaises(TranscriberRetiredError):
                tr.transcript("chunk.mp3")
        other.transcript.assert_not_called()
        build.assert_not_called()

    def test_retired_instance_no_current_raises_not_rebuild(self):
        """Spec 2：注册表没有可转交实例时不得自愈重建（否则发布前窗口会留下游离模型）。"""
        from app.transcriber.whisper import TranscriberRetiredError

        tr = self._bare_instance(retired=True)
        with mock.patch(
            "app.transcriber.transcriber_provider.get_current_whisper_transcriber",
            return_value=None,
        ), mock.patch.object(self.W, "_build_model") as build:
            with self.assertRaises(TranscriberRetiredError):
                tr.transcript("chunk.mp3")
        build.assert_not_called()
        self.assertIsNone(tr.model)

    def test_retired_instance_self_still_registered_raises(self):
        """Spec 2：退役/发布交接窗口里 `current is self` 时必须报错，不得落入重建兜底。"""
        from app.transcriber.whisper import TranscriberRetiredError

        tr = self._bare_instance(retired=True)
        with mock.patch(
            "app.transcriber.transcriber_provider.get_current_whisper_transcriber",
            return_value=tr,
        ), mock.patch.object(self.W, "_build_model") as build:
            with self.assertRaises(TranscriberRetiredError):
                tr.transcript("chunk.mp3")
        build.assert_not_called()

    def test_cancel_during_rebuild_skips_asr_and_rearms_timer(self):
        """S3 + Standards 1：重建期间取消 → 不执行 ASR，且仍要在 finally 里安排回收。"""
        from app.exceptions.task import TaskCancelledError

        tr = self._bare_instance()
        fake = self._fake_model()
        cancel = threading.Event()

        def build(*_a, **_k):
            cancel.set()          # 模拟重建期间用户取消
            return fake

        with mock.patch.object(self.W, "_build_model", side_effect=build), \
             mock.patch("app.transcriber.whisper.get_model_dir", return_value="/tmp/x"), \
             mock.patch(
                 "app.transcriber.transcriber_provider.schedule_gpu_idle_release"
             ) as schedule:
            with self.assertRaises(TaskCancelledError):
                tr.transcript("chunk.mp3", cancel_event=cancel)
        fake.transcribe.assert_not_called()
        schedule.assert_called_once()


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
