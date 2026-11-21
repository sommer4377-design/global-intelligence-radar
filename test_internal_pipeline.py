"""Rehearsal state machine tests. Synthetic inputs are never public output."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import closing

from internal_pipeline import BEIJING, Pipeline, slot_id
from internal_pipeline_adapters import Adapters, rollback, verify_archive


class InternalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def pipeline(self, batch="2026-11-22-08", mode="mock", capture=None):
        return Pipeline(self.root, batch, mode,
                        dict(dry_run=True, limit=2, capture=capture, models=None, model=None, runtime=None))

    def report(self, p):
        return json.loads((p.root/"runs"/p.batch/"report.json").read_text())

    def test_beijing_schedule(self):
        for time, expected in [(7,"2026-11-21-20"),(8,"2026-11-22-08"),
                               (19,"2026-11-22-08"),(20,"2026-11-22-20")]:
            self.assertEqual(slot_id(datetime(2026,11,22,time,tzinfo=BEIJING)),expected)

    def test_normal_mock_archive_and_dedup_on_rerun(self):
        p = self.pipeline()
        self.assertEqual(p.execute(Adapters()), 0)
        before = self.report(p)
        self.assertEqual(before["technical"], "COMPLETED")
        self.assertEqual(before["quality"], "SYNTHETIC_PASS")
        self.assertFalse(before["publication_ready"])
        self.assertFalse(before["deploy"]["public_verified"])
        self.assertEqual(before["notification"], "SIMULATED_SENT")
        self.assertEqual(verify_archive(self.root/before["archive"])["batch"], p.batch)
        self.assertEqual(p.execute(Adapters(), resume=True), 0)
        self.assertEqual(before,self.report(p))
        with closing(p.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pipeline_notice").fetchone()[0],1)

    def test_no_qualified_news_blocks_publish(self):
        capture = self.root/"input.json"
        capture.write_text(json.dumps({"results":[["IT之家", [{
            "title":"受控结构测试","published":"2026-11-19 19:00 北京时间",
            "url":"https://www.ithome.com/example"}],""]]}))
        p = self.pipeline(mode="real",capture=str(capture))
        self.assertEqual(p.execute(Adapters()),0)
        report = self.report(p)
        self.assertEqual(report["technical"],"COMPLETED")
        self.assertEqual(report["quality"],"BLOCKED_NO_QUALIFIED_STORIES")
        self.assertFalse(report["publication_ready"])
        self.assertEqual(report["notification"],"NOT_SENT")
        self.assertTrue((self.root/report["archive"]/"manifest.json").exists())
        self.assertFalse((self.root/"simulation"/"deployment.json").exists())

    def test_real_empty_collection_is_failure(self):
        capture = self.root/"empty.json"
        capture.write_text(json.dumps({"results":[["IT之家",[],"测试超时"]]}))
        p = self.pipeline(mode="real",capture=str(capture))
        self.assertEqual(p.execute(Adapters()),1)
        self.assertEqual(self.report(p)["attempts"][0]["stage"],"collect")
        self.assertIn("所有来源", self.report(p)["attempts"][0]["error"])

    def test_stage_failures_stop_and_resume(self):
        for stage in ("collect","verify","write","render","archive","deploy","notify"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as subroot:
                p = Pipeline(Path(subroot),"2026-11-22-08","mock",
                             dict(dry_run=True,limit=2,capture=None,models=None,model=None,runtime=None))
                self.assertEqual(p.execute(Adapters(stage)),1)
                report = self.report(p)
                self.assertEqual(report["notification"],"NOT_SENT")
                self.assertEqual(report["technical"],"FAILED")
                self.assertEqual(p.execute(Adapters()),1)
                self.assertEqual(p.execute(Adapters(),resume=True),0)
                attempts = [row for row in self.report(p)["attempts"] if row["stage"] == stage]
                self.assertEqual([row["status"] for row in attempts],["FAILED","COMPLETED"])

    def test_single_stage_and_interruption(self):
        class Interrupt(Adapters):
            def execute(self, p, stage):
                if stage == "identify":
                    raise KeyboardInterrupt()
                return super().execute(p, stage)
        p = self.pipeline()
        self.assertEqual(p.execute(Adapters(),only="identify"),2)
        self.assertEqual(p.execute(Adapters(),only="collect"),0)
        with self.assertRaises(KeyboardInterrupt):
            p.execute(Interrupt(),only="identify")
        self.assertEqual(self.report(p)["technical"],"INTERRUPTED")
        self.assertEqual(p.execute(Adapters(),resume=True),0)
        retry = [a for a in self.report(p)["attempts"] if a["stage"]=="identify"]
        self.assertEqual([a["status"] for a in retry],["FAILED","COMPLETED"])

    def test_archive_tampering_blocks_preparation(self):
        p = self.pipeline()
        self.assertEqual(p.execute(Adapters("prepare")),1)
        archive = self.root/self.report(p)["archive"]
        (archive/"index.html").write_text("changed")
        self.assertEqual(p.execute(Adapters(),resume=True),1)
        self.assertFalse((self.root/"simulation"/"deployment.json").exists())

    def test_deploy_rollback_and_notification_failure_isolation(self):
        first = self.pipeline("2026-11-22-08")
        second = self.pipeline("2026-11-22-20")
        pointer = self.root/"simulation"/"deployment.json"
        self.assertEqual(first.execute(Adapters()),0)
        baseline = pointer.read_bytes()
        self.assertEqual(second.execute(Adapters("deploy")),1)
        self.assertEqual(pointer.read_bytes(),baseline)
        self.assertEqual(second.execute(Adapters("notify"),resume=True),1)
        self.assertEqual(json.loads(pointer.read_text())["current"],"archive/2026-11-22-20")
        rollback(second)
        self.assertEqual(json.loads(pointer.read_text())["current"],"archive/2026-11-22-08")
