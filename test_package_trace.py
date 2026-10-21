import threading
import unittest
from unittest.mock import Mock

from package_trace import (
    JenkinsHistorySource, TraceCancelled, TraceLogParser, build_revisions, build_time, parse_packages,
    format_trace_match, trace_packages,
)


PACKAGE = "pqmbackend-5.2.0:5.2.0.1001.11047"
FULL = "registry.example/industry/" + PACKAGE
SHA = "a" * 40
MOBILE_PACKAGE = "pqmmobile-bmgd-2.1.0-4.0.0.1000.10007"
MOBILE_IMAGE = "pqmmobile-bmgd-2.1.0:4.0.0.1000.10007"
JOB = {"path": ["cloud", "22-PQM", "3-SERVICE"], "label": "云应用 / PQM / SERVICE"}


class TraceParsingTests(unittest.TestCase):
    def test_time_uses_fixed_china_offset_independent_of_machine_timezone(self):
        self.assertEqual(build_time(1779426641000), "2026-05-22 13:10:41")
        self.assertEqual(build_time(None), "时间未知")

    def test_batch_input_preserves_registry_and_deduplicates(self):
        self.assertEqual(parse_packages(f" {PACKAGE}\n{FULL}\n{PACKAGE}\n"), [PACKAGE, FULL])
        for value in ("", "pqmbackend-5.2.0", "https://" + FULL):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_packages(value)

    def test_app_package_accepts_hyphen_version_and_preserves_original_input(self):
        self.assertEqual(parse_packages(MOBILE_PACKAGE), [MOBILE_PACKAGE])
        self.assertEqual(parse_packages(MOBILE_IMAGE), [MOBILE_IMAGE])
        for invalid in ("pqmmobile-bmgd-2.1.0-invalid", "pqmmobile-bmgd-2.1.0-"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_packages(invalid)

    def test_hyphen_package_matches_colon_image_without_conflating_customer_or_tag(self):
        parser = TraceLogParser([MOBILE_PACKAGE])
        for image in (MOBILE_IMAGE + "9", MOBILE_IMAGE.replace("bmgd", "other"),
                      MOBILE_IMAGE.replace("pqmmobile", "etmmobile")):
            parser.feed("Successfully tagged registry.example/industry/" + image)
        self.assertEqual(parser.matches, [])
        parser.feed("Successfully tagged registry.example/industry/" + MOBILE_IMAGE)
        self.assertEqual(parser.matches[0]["package"], MOBILE_PACKAGE)
        self.assertEqual(parser.matches[0]["image"], "registry.example/industry/" + MOBILE_IMAGE)

    def test_full_hyphen_package_keeps_registry_restriction(self):
        full = "registry.example:5000/industry/" + MOBILE_PACKAGE
        self.assertEqual(parse_packages(full), [full])
        parser = TraceLogParser([full])
        parser.feed("Successfully tagged registry.other:5000/industry/" + MOBILE_IMAGE)
        self.assertEqual(parser.matches, [])
        parser.feed("Successfully tagged registry.example:5000/industry/" + MOBILE_IMAGE)
        self.assertEqual(len(parser.matches), 1)

    def test_only_output_evidence_matches_exact_package(self):
        parser = TraceLogParser([PACKAGE])
        for line in (
            f"FROM {FULL}", f"+ docker pull {FULL}",
            f"+ echo 'naming to {FULL} done'",
            f"#10 naming to {FULL}9 done",
        ):
            parser.feed(line)
        self.assertEqual(parser.matches, [])
        parser.feed(f"\x1b[32m#12 naming to {FULL} done\x1b[0m")
        self.assertEqual(parser.matches[0]["image"], FULL)
        self.assertEqual(parser.matches[0]["kind"], "镜像生成记录")

    def test_full_reference_does_not_match_another_registry_or_path(self):
        parser = TraceLogParser([FULL])
        parser.feed("Successfully tagged other.example/industry/" + PACKAGE)
        parser.feed("Successfully tagged registry.example/other/" + PACKAGE)
        self.assertEqual(parser.matches, [])
        parser.feed("Successfully tagged " + FULL)
        self.assertEqual(len(parser.matches), 1)

    def test_push_requires_digest_and_is_labeled_as_push_only(self):
        parser = TraceLogParser([PACKAGE])
        parser.feed("The push refers to repository [registry.example/industry/pqmbackend-5.2.0]")
        self.assertEqual(parser.matches, [])
        parser.feed("5.2.0.1001.11047: digest: sha256:" + "b" * 64 + " size: 1234")
        self.assertEqual(parser.matches[0]["kind"], "仅推送记录")
        self.assertTrue(parser.matches[0]["digest"].startswith("sha256:"))

    def test_buildkit_naming_with_elapsed_seconds_matches_both_input_formats(self):
        for package in (MOBILE_IMAGE, MOBILE_PACKAGE):
            for prefix, suffix in (("#13 ", " 0.0s done"), ("#13 ", " 12.3s done"),
                                   ("#13 0.2s ", " done")):
                with self.subTest(package=package, prefix=prefix, suffix=suffix):
                    parser = TraceLogParser([package])
                    parser.feed(prefix + "naming to registry.example/industry/" + MOBILE_IMAGE + suffix)
                    self.assertEqual(len(parser.matches), 1)
                    self.assertEqual(parser.matches[0]["kind"], "镜像生成记录")

    def test_dockerbuild_arrow_prefix_keeps_push_receipt_and_build_evidence(self):
        parser = TraceLogParser([MOBILE_IMAGE])
        parser.feed("---> The push refers to repository [registry.example/industry/pqmmobile-bmgd-2.1.0]")
        self.assertEqual(parser.matches, [])
        parser.feed("---> 4.0.0.1000.10007: digest: sha256:" + "b" * 64 + " size: 3442")
        self.assertEqual(parser.matches[0]["kind"], "仅推送记录")
        parser.feed("#13 naming to registry.example/industry/" + MOBILE_IMAGE + " 0.0s done")
        self.assertEqual(len(parser.matches), 1)
        self.assertEqual(parser.matches[0]["kind"], "镜像生成记录")
        self.assertEqual(parser.matches[0]["digest"], "sha256:" + "b" * 64)

    def test_wrapped_output_still_rejects_echo_and_nearby_customer_or_version(self):
        parser = TraceLogParser([MOBILE_IMAGE])
        for line in (
            "+ echo '#13 naming to registry.example/industry/" + MOBILE_IMAGE + " 0.0s done'",
            "---> + echo 'Successfully tagged " + MOBILE_IMAGE + "'",
            "#13 naming to registry.example/industry/" + MOBILE_IMAGE + "9 0.0s done",
            "#13 naming to registry.example/industry/" + MOBILE_IMAGE.replace("bmgd", "other") + " 0.0s done",
            "+ FILENAME=" + MOBILE_PACKAGE,
        ):
            parser.feed(line)
        self.assertEqual(parser.matches, [])

    def test_template_repository_resolves_only_from_matching_checkout(self):
        data = {"actions": [{"remoteUrls": ["https://git.example/custom-${CUSTOM_NAME}/pqm/app.git"],
                            "lastBuiltRevision": {"SHA1": SHA, "branch": [{"name": "origin/f-case"}]}}]}
        checkout = {"repository": "https://git.example/custom-bmgd/pqm/app.git", "branch": "origin/f-case",
                    "commit": SHA, "source": "控制台 checkout 记录"}
        self.assertEqual(build_revisions(data, [checkout]), [checkout])
        for change in ({"commit": "c" * 40}, {"branch": "origin/other"},
                       {"repository": "https://git.example/custom-bmgd/etm/app.git"}):
            with self.subTest(change=change):
                rows = build_revisions(data, [{**checkout, **change}])
                self.assertEqual(len(rows), 2)
                self.assertIn("${CUSTOM_NAME}", rows[0]["repository"])

    def test_actual_checkout_revisions_keep_multiple_repositories_and_remove_url_credentials(self):
        parser = TraceLogParser([PACKAGE])
        parser.feed("Fetching upstream changes from https://user:secret@git.example/pqm.git?token=private")
        parser.feed(f"Checking out Revision {SHA} (origin/master-5.48.2.1.0)")
        parser.feed("Cloning repository git@git.example:custom/pqm.git")
        parser.feed(f"Checking out Revision {'b' * 40} (origin/f-case)")
        self.assertEqual(len(parser.revisions), 2)
        self.assertEqual(parser.revisions[0]["repository"], "https://git.example/pqm.git")
        self.assertEqual(parser.revisions[1]["branch"], "origin/f-case")
        self.assertNotIn("secret", str(parser.revisions))


class MemorySource:
    def __init__(self):
        self.cancel = threading.Event()
        self.pages = {0: [self.build(3), self.build(2)], 2: [self.build(1)]}
        self.logs = {1: "Successfully tagged " + FULL, 2: "FROM " + FULL,
                     3: "Successfully tagged " + FULL}
        self.requests = []

    @staticmethod
    def build(number):
        return {"number": number, "timestamp": 1779426641000 + number,
                "result": "SUCCESS", "building": False}

    def jobs(self, packages, scope, selected, progress, warnings):
        return [JOB]

    def builds(self, job, offset, size):
        self.requests.append(offset)
        return self.pages.get(offset, [])

    def console(self, job, number, parser):
        for line in self.logs[number].splitlines():
            parser.feed(line)
        return True

    def revision_data(self, job, number):
        return {"actions": [{"causes": [{"userName": "打包用户", "userId": "builder"}]},
                            {"remoteUrls": ["https://git.example/pqm.git"],
                             "lastBuiltRevision": {"SHA1": SHA, "branch": [{"name": "origin/master"}]}}]}

    def build_url(self, job, number):
        return f"https://jenkins.example/job/pqm/{number}/"


class TraceSearchTests(unittest.TestCase):
    def test_mobile_build_log_and_template_metadata_return_the_five_fields(self):
        source = MemorySource()
        source.pages = {0: [{**source.build(10), "timestamp": 1785392100620}]}
        source.logs[10] = "\n".join([
            "Started by user 打包用户",
            "Cloning repository https://git.example/custom-bmgd/pqm/app.git",
            f"Checking out Revision {SHA} (origin/f-case375检验通知)",
            "#13 naming to registry.example/industry/" + MOBILE_IMAGE + " 0.0s done",
            "---> The push refers to repository [registry.example/industry/pqmmobile-bmgd-2.1.0]",
            "---> 4.0.0.1000.10007: digest: sha256:" + "b" * 64 + " size: 3442",
            "Finished: SUCCESS",
        ])
        source.revision_data = Mock(return_value={"actions": [
            {"causes": [{"userName": "打包用户"}]},
            {"remoteUrls": ["https://git.example/custom-${CUSTOM_NAME}/pqm/app.git"],
             "lastBuiltRevision": {"SHA1": SHA, "branch": [{"name": "origin/f-case375检验通知"}]}}
        ]})
        report = trace_packages(source, [MOBILE_IMAGE])
        self.assertTrue(report["complete"])
        self.assertEqual(report["unmatched"], [])
        self.assertEqual(len(report["matches"]), 1)
        self.assertEqual(format_trace_match(report["matches"][0]), "\n".join([
            "包号：" + MOBILE_IMAGE, "打包人：打包用户", "打包分支：origin/f-case375检验通知",
            "打包时间：2026-07-30 14:15:00", "代码仓库：https://git.example/custom-bmgd/pqm/app.git",
        ]))

    def test_result_has_exactly_the_five_requested_fields(self):
        result = trace_packages(MemorySource(), [PACKAGE], limit=1)
        row = result["matches"][0]
        self.assertEqual(row.get("builder"), "打包用户")
        self.assertEqual(format_trace_match(row), "\n".join([
            "包号：" + PACKAGE,
            "打包人：打包用户",
            "打包分支：origin/master",
            "打包时间：2026-05-22 13:10:41",
            "代码仓库：https://git.example/pqm.git",
        ]))

    def test_builder_falls_back_to_id_then_started_by_log_not_commit_author(self):
        for metadata, log, expected in (
            ({"actions": [{"causes": [{"userId": "builder-id"}]}]}, "", "builder-id"),
            ({"actions": []}, "Started by user 张三\n", "张三"),
            ({"actions": [{"causes": [{"shortDescription": "Started by timer"}]}],
              "changeSet": {"items": [{"author": {"fullName": "代码作者"}}]}}, "", "未记录"),
        ):
            with self.subTest(expected=expected):
                source = MemorySource()
                source.revision_data = Mock(return_value=metadata)
                source.logs[3] = log + source.logs[3]
                row = trace_packages(source, [PACKAGE], limit=1)["matches"][0]
                self.assertEqual(row.get("builder"), expected)

    def test_push_only_result_does_not_claim_the_push_user_or_time_as_build_origin(self):
        source = MemorySource()
        source.logs[3] = ("The push refers to repository [registry.example/industry/pqmbackend-5.2.0]\n"
                          "5.2.0.1001.11047: digest: sha256:" + "b" * 64 + " size: 123")
        row = trace_packages(source, [PACKAGE], limit=1)["matches"][0]
        self.assertEqual(format_trace_match(row).splitlines(), [
            "包号：" + PACKAGE, "打包人：未确认", "打包分支：未确认",
            "打包时间：未确认", "代码仓库：未确认",
        ])

    def test_multiple_repositories_keep_branch_correspondence_within_five_lines(self):
        source = MemorySource()
        source.logs[3] = ("Cloning repository https://git.example/custom.git\n"
                          f"Checking out Revision {'b' * 40} (origin/f-case)\n" + source.logs[3])
        row = trace_packages(source, [PACKAGE], limit=1)["matches"][0]
        lines = format_trace_match(row).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[2], "打包分支：[1] origin/master；[2] origin/f-case")
        self.assertEqual(lines[4], "代码仓库：[1] https://git.example/pqm.git；[2] https://git.example/custom.git")

    def test_finds_old_history_and_keeps_duplicate_builds(self):
        source = MemorySource()
        emitted = []
        result = trace_packages(source, [PACKAGE], limit=10, page_size=2, on_match=emitted.append)
        self.assertEqual([row["number"] for row in result["matches"]], [3, 1])
        self.assertEqual(source.requests, [0, 2])
        self.assertEqual(len(emitted), 2)
        self.assertEqual(result["unmatched"], [])
        self.assertEqual(result["matches"][0]["revisions"][0]["commit"], SHA)

    def test_limit_and_partial_failures_are_not_reported_as_exhaustive(self):
        source = MemorySource()
        source.console = Mock(side_effect=RuntimeError("HTTP 403"))
        result = trace_packages(source, [PACKAGE], limit=1, page_size=2)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unmatched"], [PACKAGE])
        self.assertTrue(result["warnings"])

    def test_missing_scm_does_not_discard_a_match_or_fabricate_commit(self):
        source = MemorySource()
        source.revision_data = Mock(side_effect=RuntimeError("HTTP 404"))
        result = trace_packages(source, [PACKAGE], limit=1)
        self.assertEqual(result["matches"][0]["revisions"], [])
        self.assertIn("代码来源", " ".join(result["warnings"]))

    def test_cancel_keeps_completed_matches(self):
        source = MemorySource()
        def match(_row):
            source.cancel.set()
        result = trace_packages(source, [PACKAGE], limit=10, on_match=match)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(result["matches"]), 1)

    def test_partial_logs_and_running_builds_are_visible(self):
        source = MemorySource()
        source.pages = {0: [{**source.build(3), "building": True, "result": None}]}
        source.console = Mock(return_value=False)
        result = trace_packages(source, [PACKAGE], limit=10)
        self.assertFalse(result["complete"])
        self.assertIn("日志", " ".join(result["warnings"]))

    def test_one_removed_log_does_not_hide_other_build_matches(self):
        source = MemorySource()
        original = source.console
        def console(job, number, parser):
            if number == 2:
                raise RuntimeError("HTTP 404")
            return original(job, number, parser)
        source.console = console
        result = trace_packages(source, [PACKAGE], limit=10, page_size=2)
        self.assertEqual([row["number"] for row in result["matches"]], [3, 1])
        self.assertFalse(result["complete"])

    def test_shortcode_keeps_distinct_repositories_visible(self):
        source = MemorySource()
        source.logs[3] += "\nSuccessfully tagged other.example/industry/" + PACKAGE
        result = trace_packages(source, [PACKAGE], limit=1)
        self.assertEqual({row["image"] for row in result["matches"]}, {
            FULL, "other.example/industry/" + PACKAGE})


class HistorySourceTests(unittest.TestCase):
    def source(self, response):
        client = Mock(url="https://jenkins.example/jenkins")
        client.session.get.return_value = response
        return JenkinsHistorySource(client, threading.Event()), client

    def response(self, data=None, status=200):
        response = Mock(status_code=status)
        response.json.return_value = data
        return response

    def test_history_uses_allbuilds_pagination_and_never_requests_parameters(self):
        source, client = self.source(self.response({"allBuilds": []}))
        source.builds(JOB, 100, 50)
        kwargs = client.session.get.call_args.kwargs
        self.assertIn("allBuilds[", kwargs["params"]["tree"])
        self.assertIn("{100,150}", kwargs["params"]["tree"])
        self.assertNotIn("parameters", kwargs["params"]["tree"])
        self.assertFalse(kwargs["allow_redirects"])

    def test_build_metadata_requests_trigger_user_without_parameters_or_environment(self):
        source, client = self.source(self.response({"actions": []}))
        source.revision_data(JOB, 3)
        tree = client.session.get.call_args.kwargs["params"]["tree"]
        self.assertIn("causes[userName,userId]", tree)
        self.assertNotIn("parameters", tree)
        self.assertNotIn("environment", tree)

    def service_source(self):
        source, _ = self.source(self.response())
        folder = "com.cloudbees.hudson.plugins.folder.Folder"
        source._json = Mock(side_effect=lambda url, **kwargs: {
            "jobs": ([{"name": code, "displayName": f"16-{code}-模块", "_class": folder}
                      for code in ("ETM", "PQM")]
                     if url.endswith("/jenkins/api/json") else
                     [{"name": name, "_class": "hudson.model.FreeStyleProject"}
                      for name in ("1-WEB", "2-APP", "3-SERVICE", "6-SERVICE-5.3.0", "unknown")])
        })
        return source

    def test_backend_queries_only_same_product_service_jobs(self):
        source = self.service_source()
        jobs = source.jobs(["etmbackend-5.3.0:5.3.0.1001.10916"], "auto", None, lambda _: None, [])
        self.assertEqual([job["path"] for job in jobs], [
            ["ETM", "3-SERVICE"], ["ETM", "6-SERVICE-5.3.0"]])
        self.assertFalse(any("/PQM/" in call.args[0] for call in source._json.call_args_list))

    def test_frontend_and_mobile_do_not_query_each_other(self):
        for prefix, job in (("etmfrontend", "1-WEB"), ("etmmobilefrontend", "2-APP")):
            with self.subTest(prefix=prefix):
                result = self.service_source().jobs([prefix + "-5.3.0:1"], "auto", None, lambda _: None, [])
                self.assertEqual([row["path"] for row in result], [["ETM", job]])

    def test_mobile_package_prefix_routes_only_to_app_for_both_version_formats(self):
        for package in (MOBILE_PACKAGE, MOBILE_IMAGE, "registry.example/industry/" + MOBILE_PACKAGE):
            with self.subTest(package=package):
                result = self.service_source().jobs([package], "auto", None, lambda _: None, [])
                self.assertEqual([row["path"] for row in result], [["PQM", "2-APP"]])

    def test_multibranch_leaf_inherits_service_from_its_folder(self):
        source, _ = self.source(self.response())
        folder = "com.cloudbees.hudson.plugins.folder.Folder"
        def jobs(url, **kwargs):
            if url.endswith("/jenkins/api/json"):
                return {"jobs": [{"name": "ETM", "displayName": "16-ETM-模块", "_class": folder}]}
            if url.endswith("/ETM/api/json"):
                return {"jobs": [{"name": name, "_class": "WorkflowMultiBranchProject"}
                                 for name in ("1-WEB", "3-SERVICE")]}
            return {"jobs": [{"name": "master", "_class": "WorkflowJob"}]}
        source._json = jobs
        result = source.jobs(["etmbackend-5.3.0:1"], "auto", None, lambda _: None, [])
        self.assertEqual([row["path"] for row in result], [["ETM", "3-SERVICE", "master"]])

    def test_batch_filters_product_service_pairs_without_cross_product_combinations(self):
        jobs = self.service_source().jobs(["etmbackend-5.3.0:1", "pqmfrontend-5.2.0:2"],
                                          "auto", None, lambda _: None, [])
        self.assertEqual([job["path"] for job in jobs], [
            ["ETM", "3-SERVICE"], ["ETM", "6-SERVICE-5.3.0"], ["PQM", "1-WEB"]])

    def test_explicit_all_or_selected_scope_is_preserved(self):
        source = self.service_source()
        self.assertEqual(len(source.jobs([PACKAGE], "all", None, lambda _: None, [])), 10)
        jobs = source.jobs([PACKAGE], "selected", ["custom", "arbitrary"], lambda _: None, [])
        self.assertEqual(jobs[0]["path"], ["custom", "arbitrary"])

    def test_http_failure_does_not_return_response_body_or_auth_data(self):
        response = self.response(status=403)
        response.text = "password=supersecret"
        source, _ = self.source(response)
        with self.assertRaisesRegex(RuntimeError, "403") as context:
            source.builds(JOB, 0, 20)
        self.assertNotIn("supersecret", str(context.exception))

    def test_cancel_prevents_http_requests(self):
        source, client = self.source(self.response())
        source.cancel.set()
        with self.assertRaises(TraceCancelled):
            source.builds(JOB, 0, 20)
        client.session.get.assert_not_called()

    def test_expired_login_stops_scan_instead_of_requesting_every_log(self):
        source, client = self.source(self.response(status=403))
        source.jobs = Mock(return_value=[JOB])
        source.builds = Mock(return_value=[MemorySource.build(number) for number in (3, 2, 1)])
        report = trace_packages(source, [PACKAGE], limit=10)
        self.assertEqual(report["scanned"], 1)
        self.assertFalse(report["complete"])
        client.session.get.assert_called_once()

    def test_log_is_streamed_bounded_and_closed(self):
        response = self.response()
        response.iter_content.return_value = [b"Successfully tagged " + FULL.encode() + b"\n", b"x" * 400]
        source, client = self.source(response)
        source.log_limit = 200
        parser = TraceLogParser([PACKAGE])
        self.assertFalse(source.console(JOB, 1, parser))
        self.assertEqual(len(parser.matches), 1)
        self.assertTrue(client.session.get.call_args.kwargs["stream"])
        response.close.assert_called_once()

    def test_utf8_branch_split_between_network_chunks_is_preserved(self):
        response = self.response()
        raw = (f"Checking out Revision {SHA} (origin/湖北兴欣)\nSuccessfully tagged {FULL}").encode()
        response.iter_content.return_value = [raw[start:start + 2] for start in range(0, len(raw), 2)]
        source, _ = self.source(response)
        parser = TraceLogParser([PACKAGE])
        self.assertTrue(source.console(JOB, 1, parser))
        self.assertEqual(parser.revisions[0]["branch"], "origin/湖北兴欣")
        self.assertEqual(parser.matches[0]["image"], FULL)

    def test_cancelling_during_log_stream_closes_response(self):
        response = self.response()
        source, _ = self.source(response)
        def chunks(**kwargs):
            source.cancel.set()
            yield b"anything"
        response.iter_content.side_effect = chunks
        with self.assertRaises(TraceCancelled):
            source.console(JOB, 1, TraceLogParser([PACKAGE]))
        response.close.assert_called_once()

    def test_discovery_includes_cloud_and_custom_and_skips_other_products(self):
        source, client = self.source(self.response())
        folder = "com.cloudbees.hudson.plugins.folder.Folder"
        def jobs(path, **kwargs):
            if path.endswith("/jenkins/api/json"):
                return {"jobs": [{"name": "cloud", "_class": folder}, {"name": "custom", "_class": folder}]}
            if path.endswith("/cloud/api/json") or path.endswith("/custom/api/json"):
                return {"jobs": [{"name": "22-PQM", "_class": folder}, {"name": "23-ETM", "_class": folder}]}
            return {"jobs": [{"name": "3-SERVICE", "_class": "hudson.model.FreeStyleProject"}]}
        source._json = jobs
        warnings = []
        result = source.jobs([PACKAGE], "auto", None, lambda _: None, warnings)
        self.assertEqual([row["path"] for row in result], [
            ["cloud", "22-PQM", "3-SERVICE"], ["custom", "22-PQM", "3-SERVICE"]])


if __name__ == "__main__":
    unittest.main()
