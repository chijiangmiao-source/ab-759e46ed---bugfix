"""纪元迁移状态机的单元测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import ApiError, Store  # noqa: E402


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


    # ---------------------------------------------------- 重复正文：逐条保留

    def test_duplicate_content_records_preserved_across_migration(self):
        """两条正文完全相同的观测是独立记录：跨批复制、校验、发布后逐条保留。"""
        ws = self.store.create_workspace("重复正文")
        page = self.store.open_page(ws["id"])["page_id"]
        self.store.add_record(ws["id"], page, "雨后土壤湿度偏高")
        self.store.add_record(ws["id"], page, "雨后土壤湿度偏高")
        self.store.add_record(ws["id"], page, "风速 3 级")
        before = self.store.get_state(ws["id"])["records"]
        self.assertEqual([r["seq"] for r in before], [1, 2, 3])

        # batch_size=1 强制三条记录跨三个复制批次，检验跨批复制不再按正文归并
        self.store.start_migration(ws["id"], page, "v2")
        phases = []
        while True:
            s = self.store.copy_batch(ws["id"], page, 1)
            phases.append(s["migration"]["phase"])
            if s["migration"]["phase"] == "validating":
                break
        self.assertIn("copying", phases)
        s = self.store.validate_migration(ws["id"], page)
        self.assertEqual(s["migration"]["phase"], "publishing")  # 校验通过
        s = self.store.publish_migration(ws["id"], page)
        self.assertEqual(s["migration"]["phase"], "published")

        after = s["records"]
        self.assertEqual(len(after), 3)                                   # 记录数未减少
        self.assertEqual([r["seq"] for r in after], [1, 2, 3])            # 序号完整有序
        self.assertEqual([r["content"] for r in after],
                         ["雨后土壤湿度偏高", "雨后土壤湿度偏高", "风速 3 级"])
        self.assertEqual([r["created_at"] for r in after],
                         [r["created_at"] for r in before])               # 创建时间一致
        # 关闭后重开页面：新纪元仍是完整三条
        self.store.close_page(ws["id"], page)
        fresh = self.store.open_page(ws["id"])
        s = self.store.get_state(ws["id"])
        self.assertEqual(fresh["epoch_number"], 2)
        self.assertEqual(len(s["records"]), 3)

    def test_case_and_whitespace_variants_are_distinct_records(self):
        """大小写或空白形式相近但不同的观测不能被视为同一条。"""
        ws = self.store.create_workspace("相近正文")
        page = self.store.open_page(ws["id"])["page_id"]
        self.store.add_record(ws["id"], page, "Alpha")
        self.store.add_record(ws["id"], page, "alpha")
        # 含首尾空白的历史记录（写入 API 会规整，这里直接落库代表既有差异形态）
        epoch = self.store.get_state(ws["id"])["current_epoch"]["id"]
        self.store.conn.execute(
            "INSERT INTO records(id,workspace_id,epoch_id,seq,content,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("rec_ws_variant", ws["id"], epoch, 3, "  Alpha\t",
             "2026-01-01T00:00:00+00:00"))
        self.drive_to(ws["id"], page, "published")
        contents = [r["content"] for r in self.store.get_state(ws["id"])["records"]]
        self.assertEqual(contents, ["Alpha", "alpha", "  Alpha\t"])

    def test_duplicate_content_validation_catches_real_loss(self):
        """候选确实丢记录时校验必须失败，不能靠内容归并蒙混通过。"""
        ws = self.store.create_workspace("严格校验")
        page = self.store.open_page(ws["id"])["page_id"]
        for content in ("a", "a", "b"):
            self.store.add_record(ws["id"], page, content)
        self.store.start_migration(ws["id"], page, "v2")
        while self.store.copy_batch(ws["id"], page, 1)["migration"]["phase"] == "copying":
            pass
        cand = self.store.get_state(ws["id"])["migration"]["candidate_epoch"]["id"]
        self.store.conn.execute(
            "DELETE FROM records WHERE epoch_id=? AND seq=2", (cand,))
        s = self.store.validate_migration(ws["id"], page)
        self.assertEqual(s["migration"]["phase"], "failed")

    # -------------------------------------------- 已发布受损工作区的安全收敛

    def _seed_legacy_damaged_published(self):
        """构造「旧版错误归并代码」发布后的受损现场：当前纪元 #2 缺少一条重复正文。"""
        from app.db import new_id
        ws = self.store.create_workspace("历史受损")
        page = self.store.open_page(ws["id"])["page_id"]
        self.store.add_record(ws["id"], page, "雨后湿度")
        self.store.add_record(ws["id"], page, "雨后湿度")
        self.store.add_record(ws["id"], page, "风速3级")
        ep1 = self.store.get_state(ws["id"])["current_epoch"]["id"]
        t1, _, t3 = [r["created_at"] for r in self.store.conn.execute(
            "SELECT created_at FROM records WHERE epoch_id=? ORDER BY seq", (ep1,))]
        ep2 = new_id("ep")
        published_at = "2026-05-01T00:00:00.000+00:00"
        self.store.conn.execute(
            "INSERT INTO epochs(id,workspace_id,number,version,kind,created_at,converged_at) "
            "VALUES(?,?,?,?,'published',?,NULL)",
            (ep2, ws["id"], 2, "v2", published_at))
        self.store.conn.execute(
            "UPDATE epochs SET kind='superseded', converged_at=NULL WHERE id=?", (ep1,))
        for seq, content, ts in ((1, "雨后湿度", t1), (3, "风速3级", t3)):
            self.store.conn.execute(
                "INSERT INTO records(id,workspace_id,epoch_id,seq,content,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (new_id("rec"), ws["id"], ep2, seq, content, ts))
        new_ts = "2026-06-01T00:00:00.000+00:00"
        new_rec = new_id("rec")
        self.store.conn.execute(
            "INSERT INTO records(id,workspace_id,epoch_id,seq,content,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (new_rec, ws["id"], ep2, 4, "迁移后新增", new_ts))
        self.store.conn.execute(
            "UPDATE workspaces SET current_epoch_id=?, migration_phase='published' WHERE id=?",
            (ep2, ws["id"]))
        return ws["id"], (new_rec, new_ts)

    def test_damaged_published_epoch_converges_on_reopen(self):
        ws_id, (new_rec, new_ts) = self._seed_legacy_damaged_published()
        self.store.close()
        self.store = Store(self.db_path)  # 重开触发收敛
        s = self.store.get_state(ws_id)
        self.assertEqual(s["current_epoch"]["number"], 2)      # 不回退当前纪元
        self.assertEqual(s["current_epoch"]["version"], "v2")
        self.assertEqual([(r["seq"], r["content"]) for r in s["records"]],
                         [(1, "雨后湿度"), (2, "雨后湿度"), (3, "风速3级"),
                          (4, "迁移后新增")])
        row = self.store.conn.execute(
            "SELECT seq, created_at FROM records WHERE id=?", (new_rec,)).fetchone()
        self.assertEqual((row["seq"], row["created_at"]), (4, new_ts))  # 新增记录未被覆盖

    def test_convergence_idempotent_and_preserves_write_fencing(self):
        ws_id, _ = self._seed_legacy_damaged_published()
        self.store.close()
        self.store = Store(self.db_path)
        first = [(r["seq"], r["content"], r["created_at"])
                 for r in self.store.get_state(ws_id)["records"]]
        self.store.close()
        self.store = Store(self.db_path)  # 再次重开：幂等
        second = [(r["seq"], r["content"], r["created_at"])
                  for r in self.store.get_state(ws_id)["records"]]
        self.assertEqual(first, second)
        # 收敛后新页面可正常写入，旧页面不会重新获得写资格
        page = self.store.open_page(ws_id)["page_id"]
        self.store.add_record(ws_id, page, "收敛后再写")
        s = self.store.get_state(ws_id)
        self.assertEqual(len(s["records"]), 5)
        self.assertEqual(s["records"][-1]["content"], "收敛后再写")


if __name__ == "__main__":
    unittest.main()
