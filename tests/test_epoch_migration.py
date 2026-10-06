"""纪元迁移状态机的单元测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import ApiError, Store, new_id, utcnow  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.store = Store(self.db_path, page_ttl_seconds=45)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ 辅助

    def make_ws(self, name="样地A", record_count=0):
        ws = self.store.create_workspace(name)
        page = self.store.open_page(ws["id"])
        for i in range(record_count):
            self.store.add_record(ws["id"], page["page_id"], f"观测记录-{i + 1}")
        return ws, page["page_id"]

    def drive_to(self, ws_id, page_id, target_phase, version="v2", batch=1, start=True):
        """把迁移推进到指定阶段。start=False 表示迁移已发起，直接续推。"""
        if start:
            self.store.start_migration(ws_id, page_id, version)
        if target_phase == "copying":
            return
        while True:
            s = self.store.copy_batch(ws_id, page_id, batch)
            if s["migration"]["phase"] == "validating":
                break
        if target_phase == "validating":
            return
        self.store.validate_migration(ws_id, page_id)
        if target_phase == "publishing":
            return
        self.store.publish_migration(ws_id, page_id)

    def assert_api_error(self, status, code, fn, *args, **kwargs):
        with self.assertRaises(ApiError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    # ------------------------------------------------------------ 基础

    def test_workspace_starts_at_epoch_one(self):
        ws, _ = self.make_ws()
        self.assertEqual(ws["current_epoch"]["number"], 1)
        self.assertEqual(ws["current_epoch"]["version"], "v1")
        self.assertEqual(ws["records"], [])

    def test_add_and_read_records(self):
        ws, page = self.make_ws(record_count=3)
        s = self.store.get_state(ws["id"])
        self.assertEqual([r["content"] for r in s["records"]],
                         ["观测记录-1", "观测记录-2", "观测记录-3"])
        self.assertEqual([r["seq"] for r in s["records"]], [1, 2, 3])

    def test_write_requires_active_page(self):
        ws, page = self.make_ws()
        self.store.close_page(ws["id"], page)
        self.assert_api_error(409, "page_not_active",
                              self.store.add_record, ws["id"], page, "迟到记录")

    # ------------------------------------------------------------ 迁移主流程

    def test_full_migration_and_stale_write_rejected(self):
        """两个页面打开同一工作区：迁移完成后，另一页的迟到保存被拒绝。"""
        ws, page_a = self.make_ws(record_count=4)
        page_b = self.store.open_page(ws["id"])["page_id"]

        self.store.start_migration(ws["id"], page_a, "v2")
        # 发布前：迁移进行中，另一页的保存被拒绝
        self.assert_api_error(409, "migration_in_progress",
                              self.store.add_record, ws["id"], page_b, "迟到记录")
        # 复制 -> 校验 -> 发布
        self.drive_to(ws["id"], page_a, "publishing", start=False)
        self.store.publish_migration(ws["id"], page_a)

        s = self.store.get_state(ws["id"])
        self.assertEqual(s["current_epoch"]["number"], 2)
        self.assertEqual(s["current_epoch"]["version"], "v2")
        self.assertEqual([r["content"] for r in s["records"]],
                         [f"观测记录-{i}" for i in range(1, 5)])
        # 两个旧页面均已失效
        states = {p["id"]: p["state"] for p in s["pages"]}
        self.assertEqual(states[page_a], "invalidated")
        self.assertEqual(states[page_b], "invalidated")
        # 发布后：旧页面的迟到保存仍被拒绝并提示重新载入
        err = self.assert_api_error(409, "page_not_active",
                                    self.store.add_record, ws["id"], page_b, "迟到记录")
        self.assertIn("重新载入", err.message)
        # 重开页面后只能读到新纪元，且可继续写入
        page_c = self.store.open_page(ws["id"])["page_id"]
        self.store.add_record(ws["id"], page_c, "新纪元记录")
        s = self.store.get_state(ws["id"])
        self.assertEqual(len(s["records"]), 5)
        self.assertEqual(s["records"][-1]["content"], "新纪元记录")

    def test_concurrent_migration_creates_no_second_candidate(self):
        ws, page_a = self.make_ws(record_count=2)
        page_b = self.store.open_page(ws["id"])["page_id"]
        self.store.start_migration(ws["id"], page_a, "v2")
        self.assert_api_error(409, "migration_active",
                              self.store.start_migration, ws["id"], page_b, "v2b")
        cand_count = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM epochs WHERE kind='candidate'").fetchone()["c"]
        self.assertEqual(cand_count, 1)

    def test_reads_never_come_from_candidate(self):
        """复制进行中读取到的仍是完整旧纪元，候选的部分数据不可见。"""
        ws, page_a = self.make_ws(record_count=5)
        self.store.start_migration(ws["id"], page_a, "v2")
        self.store.copy_batch(ws["id"], page_a, 2)  # 只复制 2/5
        s = self.store.get_state(ws["id"])
        self.assertEqual(s["current_epoch"]["number"], 1)
        self.assertEqual(len(s["records"]), 5)
        self.assertEqual(s["migration"]["copied"], 2)

    # ------------------------------------------------------------ 中断恢复

    def test_copy_interruption_recycles_candidate(self):
        """复制阶段页面关闭：候选被安全回收，绝不展示部分复制数据。"""
        ws, page_a = self.make_ws(record_count=5)
        self.store.start_migration(ws["id"], page_a, "v2")
        self.store.copy_batch(ws["id"], page_a, 2)
        self.store.close_page(ws["id"], page_a)  # 模拟页面在复制之间关闭

        s = self.store.get_state(ws["id"])
        self.assertEqual(s["migration"]["phase"], "aborted")
        self.assertIsNone(s["migration"]["candidate_epoch"])
        self.assertEqual(s["current_epoch"]["number"], 1)
        self.assertEqual(len(s["records"]), 5)  # 完整旧纪元
        cand_count = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM epochs WHERE kind='candidate'").fetchone()["c"]
        self.assertEqual(cand_count, 0)

    def test_validating_resumes_same_candidate(self):
        """校验阶段页面关闭：后来页面续用同一候选并完成迁移。"""
        ws, page_a = self.make_ws(record_count=3)
        self.drive_to(ws["id"], page_a, "validating")
        cand_before = self.store.get_state(ws["id"])["migration"]["candidate_epoch"]
        self.store.close_page(ws["id"], page_a)

        s = self.store.get_state(ws["id"])
        self.assertEqual(s["migration"]["phase"], "validating")
        self.assertEqual(s["migration"]["candidate_epoch"], cand_before)  # 同一候选

        page_b = self.store.open_page(ws["id"])["page_id"]
        self.store.validate_migration(ws["id"], page_b)
        self.store.publish_migration(ws["id"], page_b)
        s = self.store.get_state(ws["id"])
        self.assertEqual(s["current_epoch"]["number"], 2)
        self.assertEqual(len(s["records"]), 3)

    def test_publishing_completes_after_owner_close(self):
        """发布阶段页面关闭：恢复时把原子发布补齐。"""
        ws, page_a = self.make_ws(record_count=3)
        self.drive_to(ws["id"], page_a, "publishing")
        self.store.close_page(ws["id"], page_a)

        s = self.store.get_state(ws["id"])
        self.assertEqual(s["migration"]["phase"], "published")
        self.assertEqual(s["current_epoch"]["number"], 2)
        self.assertEqual(len(s["records"]), 3)

    def test_crash_recovery_on_reopen_store(self):
        """模拟进程在复制中途崩溃：重开存储后候选被回收，数据不残缺。"""
        ws, page_a = self.make_ws(record_count=4)
        self.store.start_migration(ws["id"], page_a, "v2")
        self.store.copy_batch(ws["id"], page_a, 1)
        self.store.close()  # 模拟进程崩溃（事务已提交到 copying 阶段）

        self.store = Store(self.db_path, page_ttl_seconds=45)
        s = self.store.get_state(ws["id"])
        self.assertEqual(s["migration"]["phase"], "aborted")
        self.assertEqual(s["current_epoch"]["number"], 1)
        self.assertEqual(len(s["records"]), 4)

    # ------------------------------------------------------------ 校验与持久化

    def test_validate_mismatch_marks_failed_and_allows_retry(self):
        ws, page_a = self.make_ws(record_count=3)
        self.drive_to(ws["id"], page_a, "validating")
        cand = self.store.get_state(ws["id"])["migration"]["candidate_epoch"]["id"]
        # 人为破坏候选内容
        self.store.conn.execute(
            "UPDATE records SET content='被篡改' WHERE epoch_id=? AND seq=1", (cand,))
        s = self.store.validate_migration(ws["id"], page_a)
        self.assertEqual(s["migration"]["phase"], "failed")
        # 失败后可重新发起：旧候选被回收，新候选唯一
        self.store.start_migration(ws["id"], page_a, "v2")
        cand_count = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM epochs WHERE kind='candidate'").fetchone()["c"]
        self.assertEqual(cand_count, 1)
        self.drive_to(ws["id"], page_a, "published", start=False)
        s = self.store.get_state(ws["id"])
        self.assertEqual(s["current_epoch"]["number"], 2)
        self.assertEqual(len(s["records"]), 3)

    def test_persistence_after_publish(self):
        """发布后重开存储：纪元、记录、页面失效状态一致。"""
        ws, page_a = self.make_ws(record_count=4)
        page_b = self.store.open_page(ws["id"])["page_id"]
        self.drive_to(ws["id"], page_a, "published")
        before = self.store.get_state(ws["id"])
        self.store.close()

        self.store = Store(self.db_path, page_ttl_seconds=45)
        after = self.store.get_state(ws["id"])
        self.assertEqual(after["current_epoch"], before["current_epoch"])
        self.assertEqual([r["content"] for r in after["records"]],
                         [r["content"] for r in before["records"]])
        states = {p["id"]: p["state"] for p in after["pages"]}
        self.assertEqual(states[page_a], "invalidated")
        self.assertEqual(states[page_b], "invalidated")
        self.assertEqual(after["migration"]["phase"], "published")

    # ---------------------------------------------------- 重复正文逐条保留

    def test_identical_content_records_all_survive_cross_batch_migration(self):
        """两条正文完全相同的观测是独立记录：跨批复制、校验、发布、重开全部保留。"""
        ws, page_a = self.make_ws()
        wid = ws["id"]
        self.store.add_record(wid, page_a, "降雨 10mm")
        self.store.add_record(wid, page_a, "降雨 10mm")  # 与上一条正文完全相同
        self.store.add_record(wid, page_a, "晴天")
        before = self.store.get_state(wid)["records"]
        original_times = [r["created_at"] for r in before]
        self.assertEqual([r["seq"] for r in before], [1, 2, 3])

        # 故意每批只复制 1 条，让相同正文跨批次出现
        self.store.start_migration(wid, page_a, "v2")
        phases = []
        for _ in range(5):
            s = self.store.copy_batch(wid, page_a, 1)
            phases.append(s["migration"]["phase"])
            if s["migration"]["phase"] == "validating":
                break
        self.assertIn("validating", phases)
        s = self.store.validate_migration(wid, page_a)
        self.assertEqual(s["migration"]["phase"], "publishing")  # 校验必须通过
        s = self.store.publish_migration(wid, page_a)
        self.assertEqual(s["migration"]["phase"], "published")

        after = s["records"]
        self.assertEqual(len(after), 3, f"记录数减少: {after}")
        self.assertEqual([r["seq"] for r in after], [1, 2, 3])
        self.assertEqual([r["content"] for r in after],
                         ["降雨 10mm", "降雨 10mm", "晴天"])
        # 各自的创建时间完整保留（独立记录，不合并）
        self.assertEqual([r["created_at"] for r in after], original_times)
        self.assertEqual(after[0]["id"] != after[1]["id"], True)

        # 关闭后重新打开页面：仍是完整三条，顺序不变
        self.store.close_page(wid, page_a)
        page_b = self.store.open_page(wid)["page_id"]
        s = self.store.get_state(wid)
        self.assertEqual(s["current_epoch"]["number"], 2)
        self.assertEqual([(r["seq"], r["content"]) for r in s["records"]],
                         [(1, "降雨 10mm"), (2, "降雨 10mm"), (3, "晴天")])

    def test_case_and_whitespace_similar_records_are_distinct(self):
        """大小写/空白形式相近也不能被折叠为同一条观测。"""
        ws, page_a = self.make_ws()
        wid = ws["id"]
        contents = ["RAIN", "rain", "rain  fall", "rain fall"]
        for c in contents:
            self.store.add_record(wid, page_a, c)
        self.drive_to(wid, page_a, "published", batch=1)
        s = self.store.get_state(wid)
        self.assertEqual(len(s["records"]), 4)
        self.assertEqual([r["content"] for r in s["records"]], contents)
        self.assertEqual([r["seq"] for r in s["records"]], [1, 2, 3, 4])

        # 即使绕过录入直接放入首尾带空白的记录，复制/校验也不得做 TRIM 归一化
        page_b = self.store.open_page(wid)["page_id"]
        ws2 = self.store.create_workspace("空白样地")
        w2 = ws2["id"]
        p2 = self.store.open_page(w2)["page_id"]
        ep = ws2["current_epoch"]["id"]
        now = utcnow()
        with self.store._tx():
            self.store.conn.execute(
                "INSERT INTO records(id,workspace_id,epoch_id,seq,content,created_at) "
                "VALUES(?,?,?,?,?,?)", (new_id("rec"), w2, ep, 1, "x", now))
            self.store.conn.execute(
                "INSERT INTO records(id,workspace_id,epoch_id,seq,content,created_at) "
                "VALUES(?,?,?,?,?,?)", (new_id("rec"), w2, ep, 2, "  x  ", now))
        self.drive_to(w2, p2, "published", batch=1)
        got = self.store.get_state(w2)["records"]
        self.assertEqual([r["content"] for r in got], ["x", "  x  "])

    def _make_legacy_dedup_published(self):
        """复刻旧缺陷已发布的工作区：候选按归一化正文去重，吞掉重复正文记录。"""
        ws = self.store.create_workspace("旧缺陷站")
        wid = ws["id"]
        old_page = self.store.open_page(wid)["page_id"]
        self.store.add_record(wid, old_page, "降雨")
        self.store.add_record(wid, old_page, "降雨")  # 将被旧逻辑吞掉
        self.store.add_record(wid, old_page, "晴天")
        ep1 = ws["current_epoch"]["id"]
        src = self.store.conn.execute(
            "SELECT seq,content,created_at FROM records WHERE epoch_id=? ORDER BY seq",
            (ep1,)).fetchall()
        with self.store._tx():
            ep2 = new_id("ep")
            now = utcnow()
            self.store.conn.execute(
                "INSERT INTO epochs(id,workspace_id,number,version,kind,created_at) "
                "VALUES(?,?,?,?,'candidate',?)", (ep2, wid, 2, "v2", now))
            for r in src:
                if r["seq"] != 2:  # 旧 bug：第二条“降雨”被去重
                    self.store.conn.execute(
                        "INSERT INTO records(id,workspace_id,epoch_id,seq,content,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (new_id("rec"), wid, ep2, r["seq"], r["content"], r["created_at"]))
            self.store.conn.execute(
                "UPDATE epochs SET kind='superseded' WHERE id=?", (ep1,))
            self.store.conn.execute(
                "UPDATE epochs SET kind='published' WHERE id=?", (ep2,))
            self.store.conn.execute(
                "UPDATE pages SET state='invalidated', invalidated_at=? "
                "WHERE workspace_id=? AND epoch_id=?", (now, wid, ep1))
            self.store.conn.execute(
                "UPDATE workspaces SET current_epoch_id=?, migration_phase='published', "
                "migration_target_version='v2', migration_candidate_epoch_id=NULL, "
                "migration_source_epoch_id=?, migration_owner_page_id=NULL, "
                "migration_started_at=?, migration_updated_at=? WHERE id=?",
                (ep2, ep1, now, now, wid))
        return wid, old_page, ep1, [dict(r) for r in src]

    def test_legacy_affected_workspace_heals_safely_on_reopen(self):
        """已被旧缺陷发布掉记录的工作区，重开存储时安全收敛为完整记录。"""
        wid, old_page, ep1, src = self._make_legacy_dedup_published()
        # 旧进程在受影响纪元上又新增一条（绝不能被覆盖）
        new_page = self.store.open_page(wid)["page_id"]
        self.store.add_record(wid, new_page, "迁移后新增")
        self.store.close()

        self.store = Store(self.db_path, page_ttl_seconds=45)
        s = self.store.get_state(wid)
        # 当前纪元不回退，仍是 #2/v2
        self.assertEqual(s["current_epoch"]["number"], 2)
        self.assertEqual(s["current_epoch"]["version"], "v2")
        self.assertEqual(s["migration"]["phase"], "published")
        # 记录按原顺序完整：补回的 seq=2 排在新增 seq=4 之前
        self.assertEqual([(r["seq"], r["content"]) for r in s["records"]],
                         [(1, "降雨"), (2, "降雨"), (3, "晴天"), (4, "迁移后新增")])
        restored = next(r for r in s["records"] if r["seq"] == 2)
        self.assertEqual(restored["created_at"], src[1]["created_at"])  # 沿用原时间
        # 旧页面保持失效，不会重新获得写入资格
        self.assert_api_error(409, "page_not_active",
                              self.store.add_record, wid, old_page, "旧页偷写")
        # 新页面写入序号顺延
        page = self.store.open_page(wid)["page_id"]
        r = self.store.add_record(wid, page, "再补一条")
        self.assertEqual(r["seq"], 5)

        # 再次重启：收敛幂等，不重复补、不丢新增
        self.store.close()
        self.store = Store(self.db_path, page_ttl_seconds=45)
        s = self.store.get_state(wid)
        self.assertEqual([r["seq"] for r in s["records"]], [1, 2, 3, 4, 5])

    def test_legacy_affected_workspace_heals_on_page_reopen_without_restart(self):
        """不重启进程，关闭后重新打开页面也应触发安全收敛。"""
        wid, old_page, ep1, src = self._make_legacy_dedup_published()
        # 构造后尚未经过任何维护入口：直接读底层表确认受影响状态确实缺记录
        cur_ep = self.store.conn.execute(
            "SELECT current_epoch_id FROM workspaces WHERE id=?", (wid,)).fetchone()[0]
        seqs = [r[0] for r in self.store.conn.execute(
            "SELECT seq FROM records WHERE epoch_id=? ORDER BY seq", (cur_ep,)).fetchall()]
        self.assertEqual(seqs, [1, 3], "构造的受影响状态应先缺记录")

        # 重新打开页面即触发惰性安全收敛
        page = self.store.open_page(wid)["page_id"]
        s = self.store.get_state(wid)
        self.assertEqual(s["current_epoch"]["number"], 2)
        self.assertEqual([r["content"] for r in s["records"]],
                         ["降雨", "降雨", "晴天"])
        states = {p["id"]: p["state"] for p in s["pages"]}
        self.assertEqual(states[old_page], "invalidated")
        # 收敛后仍可正常写入
        r = self.store.add_record(wid, page, "后续观测")
        self.assertEqual(r["seq"], 4)


if __name__ == "__main__":
    unittest.main()
