"""第 22 轮扫描 #146：并发、状态与批量边界回归。"""
import json
import shutil
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest import mock

from app.utils import task_manifest
from videonote_mcp import server


class TranscriberRebuildFailureTest(unittest.TestCase):
    def test_failed_rebuild_keeps_old_instance_usable(self):
        from app.transcriber import transcriber_provider as provider

        key = provider.TranscriberType.FAST_WHISPER
        old = mock.Mock()
        old.model_size = "tiny"
        saved = provider._transcribers[key]
        provider._transcribers[key] = old
        new_cls = mock.Mock(side_effect=RuntimeError("model download failed"))
        new_cls.__name__ = "FakeTranscriber"
        try:
            with self.assertRaises(RuntimeError):
                provider._get_or_build_transcriber(key, new_cls, model_size="small")
            self.assertIs(provider._transcribers[key], old)
            old.close.assert_not_called()
        finally:
            provider._transcribers[key] = saved


class TaskSubmitRegistrationTest(unittest.TestCase):
    class _ProbePool:
        def __init__(self):
            self.submit_lock_held = []

        def submit(self, fn, *args, **kwargs):
            self.submit_lock_held.append(server._tasks_lock.locked())
            future = Future()
            future.set_result(None)
            return future

    def tearDown(self):
        with server._tasks_lock:
            server._task_futures.clear()
            server._task_events.clear()

    def test_generate_and_material_submit_under_registry_lock(self):
        pool = self._ProbePool()
        common = [
            mock.patch.object(server, "_pool", pool),
            mock.patch.object(server, "_guard_remote_url"),
            mock.patch.object(server, "_index_step_task"),
            mock.patch.object(server, "_write_status"),
        ]
        with mock.patch.object(server, "_resolve_default_provider_id", return_value="p"), \
             mock.patch.object(server, "get_models_by_provider", return_value=[{"model_name": "m"}]), \
             mock.patch.object(server, "get_app_config", return_value={}), \
             common[0], common[1], common[2], common[3]:
            server.generate_note("https://example.com/video")
            server.prepare_note_material("https://example.com/video")
        self.assertEqual(pool.submit_lock_held, [True, True])


class ConcurrencyAdmissionTest(unittest.TestCase):
    """普通提交的容量检查与 Future 登记必须是一个不可分割的临界区。"""

    def setUp(self):
        with server._tasks_lock:
            self._old_futures = dict(server._task_futures)
            self._old_events = dict(server._task_events)
            server._task_futures.clear()
            server._task_events.clear()
        self._old_bypass = getattr(server._batch_ctx, "bypass_guard", False)
        server._batch_ctx.bypass_guard = False

    def tearDown(self):
        if self._old_bypass:
            server._batch_ctx.bypass_guard = True
        else:
            try:
                del server._batch_ctx.bypass_guard
            except AttributeError:
                pass
        with server._tasks_lock:
            server._task_futures.clear()
            server._task_events.clear()
            server._task_futures.update(self._old_futures)
            server._task_events.update(self._old_events)

    def test_normal_submission_reserves_before_next_check(self):
        submitted_under_lock = []

        class Pool:
            def submit(self, fn, *args, **kwargs):
                submitted_under_lock.append(server._tasks_lock.locked())
                return Future()

        with mock.patch.object(server, "_pool", Pool()), mock.patch.object(
            server, "_MAX_WORKERS", 1
        ):
            first = server._submit_registered_task("admission-1", threading.Event())
            with self.assertRaises(ValueError):
                server._submit_registered_task("admission-2", threading.Event())

        self.assertEqual(submitted_under_lock, [True])
        self.assertIs(getattr(first, server._CONCURRENCY_RESERVED_ATTR), True)

    def test_batch_submission_bypasses_reservation_but_not_registry(self):
        class Pool:
            def submit(self, fn, *args, **kwargs):
                return Future()

        with mock.patch.object(server, "_pool", Pool()), mock.patch.object(
            server, "_MAX_WORKERS", 1
        ):
            normal = server._submit_registered_task("admission-normal", threading.Event())
            server._batch_ctx.bypass_guard = True
            batch = server._submit_registered_task("admission-batch", threading.Event())

        self.assertIs(getattr(normal, server._CONCURRENCY_RESERVED_ATTR), True)
        self.assertIs(getattr(batch, server._CONCURRENCY_RESERVED_ATTR), False)
        self.assertIs(getattr(batch, server._CONCURRENCY_BATCH_ATTR), True)
        self.assertIn("admission-batch", server._task_futures)

    def test_running_batch_future_does_not_block_ordinary_admission(self):
        batch = mock.Mock()
        batch.running.return_value = True
        setattr(batch, server._CONCURRENCY_BATCH_ATTR, True)
        with server._tasks_lock:
            server._task_futures["running-batch"] = batch
        with mock.patch.object(server, "_MAX_WORKERS", 1):
            server._guard_concurrency()

    def test_running_unmarked_future_still_counts_for_compatibility(self):
        legacy = mock.Mock()
        legacy.running.return_value = True
        with server._tasks_lock:
            server._task_futures["legacy-running"] = legacy
        with mock.patch.object(server, "_MAX_WORKERS", 1), self.assertRaises(ValueError):
            server._guard_concurrency()


class ManifestConcurrencyTest(unittest.TestCase):
    def test_concurrent_recorders_preserve_disjoint_union(self):
        task_id = f"manifest-union-{id(self):x}"
        original_read = task_manifest._read_manifest
        barrier = threading.Barrier(2)

        def coordinated_read(tid):
            data = original_read(tid)
            try:
                barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                # A correct same-process lock serializes the reads, so the first
                # reader times out and the second proceeds after it releases.
                pass
            return data

        try:
            with mock.patch.object(task_manifest, "_read_manifest", side_effect=coordinated_read):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(task_manifest.record_task_paths, task_id, [f"path-{i}"])
                        for i in range(2)
                    ]
                    self.assertTrue(all(f.result(timeout=2) for f in futures))
            self.assertEqual(set(task_manifest.get_task_paths(task_id)), {"path-0", "path-1"})
        finally:
            shutil.rmtree(task_manifest.task_dir(task_id), ignore_errors=True)


class CorruptTerminalStatusTest(unittest.TestCase):
    def setUp(self):
        self.task_id = f"corrupt-{id(self):x}"
        self.task_dir = server.NOTE_OUTPUT_DIR / self.task_id

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._status_memory.pop(self.task_id, None)

    def test_corrupt_terminal_status_is_unknown_not_pending(self):
        server._write_status(self.task_id, "SUCCESS", message="完成")
        (self.task_dir / "status.json").write_text("{", encoding="utf-8")
        payload = json.loads(server.task(self.task_id))
        self.assertEqual(payload["status"], "UNKNOWN")
        self.assertEqual(payload["stage"], "状态未知")
        self.assertIn("无法确认", payload["message"])


class BatchSingleMaxEntriesZeroTest(unittest.TestCase):
    def test_single_zero_does_not_submit(self):
        with mock.patch(
            "app.services.inspect.inspect_video",
            return_value={
                "ok": True,
                "platform": "generic",
                "kind": "single",
                "title": "single",
                "total": 1,
                "entries": [],
            },
        ), mock.patch.object(server, "generate_note") as generate:
            payload = json.loads(
                server.batch_generate_notes("https://example.com/video", max_entries=0)
            )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["submitted"], 0)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["remaining"], 1)
        self.assertEqual(payload["tasks"], [])
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
