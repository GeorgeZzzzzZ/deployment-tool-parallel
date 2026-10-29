import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from main import DeployTool, DockerImageProgress


class TransferProgressTests(unittest.TestCase):
    def run_output(self, lines, images, exit_code=0, fragment=False):
        self.assertIn("on_image_progress", inspect.signature(DeployTool._remote_exec).parameters,
                      "Transfer progress must identify the image owning each update")
        chunks = [(line + "\n").encode() for line in lines]
        if fragment:
            chunks = [bytes([value]) for value in b"".join(chunks)]
        channel = SimpleNamespace(
            exit_status_ready=lambda: not chunks,
            recv_ready=lambda: bool(chunks),
            recv=lambda _size: chunks.pop(0),
            recv_stderr_ready=lambda: False,
            recv_exit_status=lambda: exit_code,
        )
        client = Mock()
        client.exec_command.return_value = (None, SimpleNamespace(channel=channel), None)
        tool = SimpleNamespace(emit=Mock())
        progress = Mock()
        with patch("main.time.sleep"):
            DeployTool._remote_exec(tool, client, "test-command", progress_range=(15, 78),
                                    transfer_images=images, on_image_progress=progress)
        self.assertFalse(any(call.args[0] == "deploy_progress" for call in tool.emit.call_args_list))
        return [call.args for call in progress.call_args_list]

    def test_serial_images_advance_independently_and_wait_for_push_receipts(self):
        images = [f"registry.example/industry/{product}backend:1" for product in ("etm", "pqm", "mms")]
        digest = "1: digest: sha256:" + "a" * 64 + " size: 1234"
        lines = []
        for image in images:
            lines.extend([
                "处理镜像: " + image.rsplit("/", 1)[-1],
                "1: Pulling from industry/" + image.rsplit("/", 1)[-1].split(":")[0],
                "layer: Pulling fs layer",
                "layer: Downloading 50MB/100MB",
                "layer: Downloading 100MB/100MB",
                "The push refers to repository [" + image.rsplit(":", 1)[0] + "]",
                "layer: Pushing 50MB/100MB", "layer: Pushing 100MB/100MB", digest,
            ])
        updates = self.run_output(lines, images)
        self.assertEqual(list(dict.fromkeys(image for image, _percent, _text in updates)), images)
        for image in images:
            own = [(percent, text) for target, percent, text in updates if target == image]
            values = [percent for percent, _text in own]
            self.assertEqual(values, sorted(values))
            self.assertTrue(any(15 < percent < 46 and "拉取" in text and "50%" in text
                                for percent, text in own))
            self.assertTrue(any(46 < percent < 78 and "推送" in text for percent, text in own))
            self.assertEqual(values[-1], 78)
            self.assertTrue(all(percent < 78 for percent in values[:-1]))

    def test_pull_percentage_sums_layers_instead_of_using_the_largest_layer(self):
        image = "registry.example/industry/etmbackend:1"
        updates = self.run_output([
            "1: Pulling from industry/etmbackend",
            "a: Pulling fs layer", "b: Pulling fs layer",
            "a: Downloading 100MB/100MB", "b: Downloading 0MB/100MB",
            "b: Downloading 25MB/100MB",
        ], [image])
        self.assertEqual(updates[-1][0], image)
        self.assertEqual(updates[-1][1], 34)
        self.assertIn("62%", updates[-1][2])
        self.assertTrue(all(percent < 46 for _image, percent, _text in updates))

    def test_unrelated_and_duplicate_push_receipts_do_not_finish_other_images(self):
        digest = "1: digest: sha256:" + "a" * 64 + " size: 1234"
        images = ["registry.example/etmbackend:1", "registry.example/pqmbackend:1"]
        updates = self.run_output([
            "The push refers to repository [other.example/etmbackend]", digest,
            "The push refers to repository [registry.example/etmbackend]", digest, digest,
        ], images)
        self.assertTrue(any(image == images[0] and percent == 78 for image, percent, _text in updates))
        self.assertFalse(any(image == images[1] for image, _percent, _text in updates))
        self.assertEqual(sum(percent == 78 for _image, percent, _text in updates), 1)

    def test_cached_pull_is_confirmed_without_layer_bytes(self):
        image = "registry.example/industry/etmbackend:1"
        updates = self.run_output([
            "处理镜像: etmbackend:1",
            "1: Pulling from industry/etmbackend", "a: Already exists",
            "Status: Image is up to date for source.example/industry/etmbackend:1",
        ], [image])
        self.assertEqual(updates[-1][1], 46)
        self.assertIn("已拉取", updates[-1][2])

    def test_unknown_or_ambiguous_image_does_not_update_any_task(self):
        images = ["registry.example/one/app:1", "registry.example/two/app:1"]
        self.assertEqual(self.run_output([
            "处理镜像: app:1", "a: Downloading 50MB/100MB",
            "The push refers to repository [other.example/app]",
            "1: digest: sha256:" + "a" * 64 + " size: 1234",
        ], images), [])

    def test_two_tags_for_one_repository_use_the_current_package(self):
        images = ["registry.example/industry/app:1", "registry.example/industry/app:2"]
        updates = self.run_output([
            "处理镜像: app:2", "2: Pulling from industry/app",
            "The push refers to repository [registry.example/industry/app]",
            "a: Pushing 25MB/100MB",
            "2: digest: sha256:" + "b" * 64 + " size: 1234",
        ], images)
        self.assertTrue(updates)
        self.assertTrue(all(image == images[1] for image, _percent, _text in updates))

    def test_unrelated_pull_header_clears_previous_image_attribution(self):
        image = "registry.example/industry/etmbackend:1"
        updates = self.run_output([
            "1: Pulling from industry/etmbackend", "a: Downloading 10MB/100MB",
            "1: Pulling from industry/unrelated", "a: Downloading 100MB/100MB",
        ], [image])
        self.assertFalse(any("99%" in text for _image, _percent, text in updates))

    def test_fragmented_unicode_and_terminal_redraw_keep_image_attribution(self):
        images = ["registry.example/etmbackend:1", "registry.example/pqmbackend:1"]
        updates = self.run_output([
            "处理镜像: pqmbackend:1",
            "\x1b[2Ka: Downloading 10MiB/100MiB\r\x1b[2Ka: Downloading 25MiB/100MiB\x1b[0m",
        ], images, fragment=True)
        self.assertTrue(all(image == images[1] for image, _percent, _text in updates))
        self.assertEqual(updates[-1][1], 22)
        self.assertIn("25%", updates[-1][2])

    def test_heartbeat_keeps_current_image_and_percentage_without_advancing(self):
        images = ["registry.example/etmbackend:1", "registry.example/pqmbackend:1"]
        progress = DockerImageProgress(images, (15, 78))
        self.assertIsNone(progress.heartbeat(10))
        progress.feed("处理镜像: etmbackend:1")
        update = progress.feed("a: Downloading 50MB/100MB")
        heartbeat = progress.heartbeat(30)
        self.assertEqual(heartbeat[:2], update[:2])
        self.assertIn("等待远端输出", heartbeat[2])
        progress.feed("The push refers to repository [registry.example/etmbackend]")
        progress.feed("1: digest: sha256:" + "a" * 64 + " size: 1234")
        self.assertIsNone(progress.heartbeat(40))

    def test_layer_completion_does_not_confirm_pull_or_push_without_receipt(self):
        image = "registry.example/etmbackend:1"
        updates = self.run_output([
            "处理镜像: etmbackend:1", "a: Pull complete",
            "The push refers to repository [registry.example/etmbackend]", "a: Pushed",
        ], [image])
        self.assertTrue(all(percent < 78 for _image, percent, _text in updates))
        self.assertTrue(all(percent < 46 for _image, percent, text in updates if "拉取" in text))


class TransferLogTests(unittest.TestCase):
    def run_transfer(self, schedule, images=None, exit_code=0, route_logs=False):
        clock = [0.0]
        steps = iter(schedule)
        pending = [b""]
        events, updates, image_logs = [], [], []

        def next_step():
            try:
                clock[0], pending[0] = next(steps)
                return False
            except StopIteration:
                return True

        def receive(_size):
            data, pending[0] = pending[0], b""
            return data

        channel = SimpleNamespace(
            exit_status_ready=next_step, recv_ready=lambda: bool(pending[0]),
            recv=receive, recv_stderr_ready=lambda: False,
            recv_exit_status=lambda: exit_code,
        )
        client = Mock()
        client.exec_command.return_value = (None, SimpleNamespace(channel=channel), None)
        owner = SimpleNamespace(emit=lambda kind, value: events.append((clock[0], kind, value)))
        kwargs = {}
        if images:
            kwargs["on_image_progress"] = lambda *update: updates.append(update)
        if route_logs:
            kwargs["on_image_log"] = lambda image, line: image_logs.append((image, line))
        with patch("main.time.monotonic", side_effect=lambda: clock[0]), patch("main.time.sleep"):
            if exit_code:
                with self.assertRaisesRegex(RuntimeError, "命令退出码"):
                    DeployTool._remote_exec(owner, client, "fixture", progress_range=(15, 78),
                                            transfer_images=images, **kwargs)
            else:
                DeployTool._remote_exec(owner, client, "fixture", progress_range=(15, 78),
                                        transfer_images=images, **kwargs)
        return events, updates, image_logs

    def test_real_download_extract_and_push_lines_survive_terminal_redraws(self):
        output = (
            "a: Downloading [=====>     ] 50MB/100MB\r"
            "a: Download complete\n"
            "\x1b[2Ka: Extracting [==>        ] 25MB/100MB\x1b[0m\r"
            "a: Pull complete\n"
            "The push refers to repository [registry.example/app]\n"
            "a: Pushing [=======>   ] 75MB/100MB\r"
        ).encode()
        for images in (None, ["registry.example/app:1"]):
            with self.subTest(images=images):
                events, _, _ = self.run_transfer([(0, output)], images)
                logs = [value for _, kind, value in events if kind == "deploy_log"]
                self.assertIn("a: Downloading [=====>     ] 50MB/100MB", logs)
                self.assertIn("a: Extracting [==>        ] 25MB/100MB", logs)
                self.assertIn("a: Pushing [=======>   ] 75MB/100MB", logs)
                self.assertLess(logs.index("a: Downloading [=====>     ] 50MB/100MB"),
                                logs.index("a: Download complete"))
                self.assertFalse(any("\x1b" in line for line in logs))

    def test_fast_redraws_coalesce_but_latest_bytes_are_flushed_even_on_failure(self):
        output = "".join(f"a: Downloading {i}MB/1000MB\r" for i in range(1, 501)).encode()
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                events, _, _ = self.run_transfer([(0, output)], ["registry.example/app:1"], exit_code)
                logs = [value for _, kind, value in events if kind == "deploy_log" and "Downloading" in value]
                self.assertIn("a: Downloading 500MB/1000MB", logs)
                self.assertLessEqual(len(logs), 2)

    def test_pending_progress_is_flushed_while_remote_has_no_more_output(self):
        events, _, _ = self.run_transfer([
            (0, b"a: Downloading 5MB/100MB\r"),
            (0.2, b"a: Downloading 10MB/100MB\r"),
            (1.2, b""), (2.2, b""),
        ], ["registry.example/app:1"])
        self.assertIn((1.2, "deploy_log", "a: Downloading 10MB/100MB"), events)

    def test_silence_reports_time_since_remote_output_without_inventing_progress(self):
        events, updates, _ = self.run_transfer([
            (0, b"a: Extracting 25MB/100MB\r"), (10, b""), (20, b""),
        ], ["registry.example/app:1"])
        logs = [value for _, kind, value in events if kind == "deploy_log"]
        self.assertTrue(any("20 秒未收到远端输出" in line for line in logs), logs)
        self.assertTrue(any("20 秒无进度或状态变化" in line for line in logs), logs)
        self.assertTrue(any("无法确认" in line for line in logs), logs)
        self.assertEqual(len({percent for _, percent, _ in updates}), 1)

    def test_identical_redraws_do_not_count_as_progress_and_new_bytes_reset_wait(self):
        line = b"a: Downloading 50MB/100MB\r"
        events, _, _ = self.run_transfer([
            (0, line), (10, line), (20, line),
            (21, b"a: Downloading 60MB/100MB\r"), (25, b""), (31, b""),
        ], ["registry.example/app:1"])
        warnings = [(at, value) for at, kind, value in events
                    if kind == "deploy_log" and "无进度或状态变化" in value]
        self.assertTrue(any(at == 20 and "20 秒无进度或状态变化" in value and
                            "0 秒未收到远端输出" in value for at, value in warnings), warnings)
        self.assertFalse(any(at == 25 for at, _ in warnings))
        self.assertTrue(any(at == 31 and "10 秒无进度或状态变化" in value for at, value in warnings))

    def test_image_logs_follow_current_owner_and_do_not_go_to_other_batch_tasks(self):
        self.assertIn("on_image_log", inspect.signature(DeployTool._remote_exec).parameters)
        images = ["registry.example/first:1", "registry.example/second:1"]
        events, _, image_logs = self.run_transfer([
            (0, b"1: Pulling from first\na: Downloading 10MB/100MB\r"),
            (1, b"1: Pulling from second\na: Extracting 20MB/100MB\r"),
            (11, b""),
        ], images, route_logs=True)
        self.assertIn((images[0], "a: Downloading 10MB/100MB"), image_logs)
        self.assertIn((images[1], "a: Extracting 20MB/100MB"), image_logs)
        self.assertTrue(any(image == images[1] and "未收到远端输出" in line for image, line in image_logs))
        self.assertFalse(any(image == images[0] and "Extracting" in line for image, line in image_logs))
        self.assertFalse(any(kind == "deploy_log" and "Downloading" in value for _, kind, value in events))

    def test_new_extraction_bytes_clear_stale_waiting_status_at_same_overall_percent(self):
        _, updates, _ = self.run_transfer([
            (0, b"a: Extracting 25MB/100MB\r"), (10, b""),
            (11, b"a: Extracting 50MB/100MB\r"),
        ], ["registry.example/app:1"])
        self.assertEqual(len({percent for _, percent, _ in updates}), 1)
        self.assertIn("多层汇总", updates[-1][2])

    def test_each_layer_flushes_its_last_bytes_before_its_completion_status(self):
        events, _, _ = self.run_transfer([(0, (
            "a: Downloading 5MB/100MB\ra: Downloading 100MB/100MB\r"
            "b: Downloading 5MB/100MB\rb: Downloading 80MB/100MB\r"
            "a: Download complete\nb: Download complete\n"
        ).encode())], ["registry.example/app:1"])
        logs = [value for _, kind, value in events if kind == "deploy_log"]
        self.assertLess(logs.index("a: Downloading 100MB/100MB"), logs.index("a: Download complete"))
        self.assertLess(logs.index("b: Downloading 80MB/100MB"), logs.index("b: Download complete"))


if __name__ == "__main__":
    unittest.main()
