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


if __name__ == "__main__":
    unittest.main()
