"""阈值管理：状态链、版本快照、阈值命中、告警恢复与事务一致性的回归测试。

只用标准库 unittest，运行方式：
    cd backend && PYTHONPATH=. .venv/bin/python -m unittest tests.test_threshold -v
"""
from __future__ import annotations

import copy
import threading
import unittest

from app.seed import SEED_ROWS
from app.services.threshold import (
    ThresholdConflict,
    ThresholdService,
    condition_holds,
    parse_effective_range,
)
from app.store import store

VALID_RULE = {
    "规则编号": "THRE-T",
    "关联指标": "REAL-0001",
    "阈值下限": "95",
    "阈值上限": "100",
    "触发条件": "低于下限",
    "持续周期": "3",
    "告警等级": "P2",
    "生效时间范围": "2026-09-01 ~ 2026-09-30",
}


class ThresholdTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从干净的种子数据开始
        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        self.svc = ThresholdService()

    def test_save_validation(self) -> None:
        # 缺字段不允许保存
        _, problems = self.svc.create_entry({"规则编号": "X", "关联指标": "REAL-0001"})
        self.assertTrue(any("阈值上限" in p for p in problems))
        # 低于下限必须提供下限
        bad = dict(VALID_RULE, 规则编号="T1", 触发条件="低于下限")
        del bad["阈值下限"]
        _, problems = self.svc.create_entry(bad)
        self.assertTrue(any("阈值下限" in p for p in problems))
        # 持续周期必须为正整数
        _, problems = self.svc.create_entry(dict(VALID_RULE, 规则编号="T2", 持续周期="0"))
        self.assertTrue(any("持续周期" in p for p in problems))
        # 下限必须小于上限
        _, problems = self.svc.create_entry(dict(VALID_RULE, 规则编号="T3", 阈值下限="100", 阈值上限="90"))
        self.assertTrue(any("小于" in p for p in problems))
        # 生效时间范围必须可解析且起止有序
        _, problems = self.svc.create_entry(dict(VALID_RULE, 规则编号="T4", 生效时间范围="2026-10-01"))
        self.assertTrue(any("结束" in p for p in problems))
        # 关联指标必须存在
        _, problems = self.svc.create_entry(dict(VALID_RULE, 规则编号="T5", 关联指标="NOPE"))
        self.assertTrue(any("不存在" in p for p in problems))
        # 规则编号唯一
        self.svc.create_entry(dict(VALID_RULE))
        _, problems = self.svc.create_entry(dict(VALID_RULE))
        self.assertTrue(any("已存在" in p for p in problems))

    def test_effective_range_end_of_day(self) -> None:
        start, end, err = parse_effective_range("2026-09-01 ~ 2026-09-30")
        self.assertEqual(err, "")
        self.assertEqual(end.hour, 23)
        self.assertEqual(end.minute, 59)

    def test_state_chain_and_snapshot_freeze(self) -> None:
        entry, _ = self.svc.create_entry(dict(VALID_RULE))
        # 草稿不能直接生效
        _, msg = self.svc.run_action(entry["id"], "生效规则")
        self.assertIn("单向状态链", msg)
        self.svc.run_action(entry["id"], "提交规则评审")
        activated, msg = self.svc.run_action(entry["id"], "生效规则")
        self.assertEqual(activated["status"], "已生效")
        self.assertEqual(activated["冻结指标版本"], "v1")
        self.assertEqual(activated["版本快照"]["持续周期"], 3)
        self.assertEqual(activated["版本快照"]["指标版本"], "v1")
        # 已关闭是终态
        self.svc.run_action(entry["id"], "关闭规则")
        _, msg = self.svc.run_action(entry["id"], "生效规则")
        self.assertIn("不允许", msg)

    def test_condition_predicate(self) -> None:
        snap = {"触发条件": "高于上限", "阈值上限": 800.0, "阈值下限": 0.0}
        self.assertTrue(condition_holds(900.0, snap))
        self.assertFalse(condition_holds(700.0, snap))
        snap2 = {"触发条件": "低于下限", "阈值上限": 100.0, "阈值下限": 95.0}
        self.assertTrue(condition_holds(94.0, snap2))

    def test_evaluate_trigger_version_conflict_and_recovery(self) -> None:
        report = self.svc.evaluate()
        # 种子里 THRE-0003、THRE-0004 两条已生效规则各产生一条命中
        self.assertEqual(len(report["triggered"]), 2)
        reasons = [s["原因"] for s in report["skipped"]]
        self.assertTrue(any("版本冲突" in r for r in reasons))  # v2 窗口跳过
        self.assertTrue(any("生效时间范围之外" for r in reasons))  # 范围外跳过

        hits = store.rows("thresholdhit")
        # THRE-0003：连续 3 窗口低于 95 后，WIN-0005 回升触发恢复，时长 2 小时
        h3 = next(h for h in hits if h["规则编号"] == "THRE-0003")
        self.assertEqual(h3["状态"], "已恢复")
        self.assertEqual(h3["持续时长秒"], 7200)
        self.assertEqual(h3["触发窗口序列"], ["WIN-0001", "WIN-0002", "WIN-0003"])
        self.assertEqual(h3["规则版本"], "v1")
        self.assertEqual(h3["指标版本"], "v1")  # 一次命中只引用一组可追溯版本
        # THRE-0004：始终高于上限，保持命中中
        h4 = next(h for h in hits if h["规则编号"] == "THRE-0004")
        self.assertEqual(h4["状态"], "命中中")
        self.assertIsNone(h4["恢复时间"])

    def test_idempotent_repeated_evaluation(self) -> None:
        self.svc.evaluate()
        before = len(store.rows("thresholdhit"))
        for _ in range(3):
            self.svc.evaluate()
        after = store.rows("thresholdhit")
        self.assertEqual(len(after), before)
        # 同一业务键 + 触发时刻唯一
        keys = {(h["业务键"], h["触发时间"]) for h in after}
        self.assertEqual(len(keys), len(after))

    def test_alarm_synced_and_recovered_on_same_record(self) -> None:
        self.svc.evaluate()
        hit = next(h for h in store.rows("thresholdhit") if h["规则编号"] == "THRE-0003")
        alarms = [a for a in store.rows("alarmhistory") if a.get("关联命中编号") == hit["命中编号"]]
        self.assertEqual(len(alarms), 1)  # 一次命中一条告警
        alarm = alarms[0]
        self.assertEqual(alarm["处置状态"], "已恢复")
        self.assertEqual(alarm["恢复时间"], hit["恢复时间"])
        self.assertEqual(alarm["持续时长"], "2小时")
        self.assertEqual(alarm["关联指标版本"], "v1")

    def test_concurrent_activation_only_one_succeeds(self) -> None:
        outcomes: list[str] = []

        def activate() -> None:
            _, msg = self.svc.run_action(2, "生效规则")
            outcomes.append(msg)

        threads = [threading.Thread(target=activate) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum("已生效规则" in m for m in outcomes), 1)
        self.assertEqual(store.find("threshold", 2)["status"], "已生效")

    def test_failed_recovery_rolls_back_and_retry_repairs(self) -> None:
        entry, _ = self.svc.create_entry({
            "规则编号": "THRE-RB", "关联指标": "REAL-0002", "阈值下限": "0", "阈值上限": "800",
            "触发条件": "高于上限", "持续周期": "3", "告警等级": "P2",
            "生效时间范围": "2026-09-01 ~ 2026-09-30",
        })
        self.svc.run_action(entry["id"], "提交规则评审")
        self.svc.run_action(entry["id"], "生效规则")
        self.svc.evaluate()
        hit = next(h for h in store.rows("thresholdhit") if h["规则编号"] == "THRE-RB")
        linked = next(a for a in store.rows("alarmhistory") if a.get("关联命中编号") == hit["命中编号"])
        store.rows("alarmhistory").remove(linked)  # 模拟告警写入中断
        store.rows("metricwindow").append({
            "id": 99, "status": "已聚合", "pending": False, "abnormal": False,
            "窗口编号": "WIN-RB", "指标编号": "REAL-0002", "指标版本": "v1",
            "窗口开始": "2026-09-11T10:30:00", "窗口结束": "2026-09-11T11:00:00",
            "聚合值": "300", "样本数": "1",
        })
        snapshot = copy.deepcopy(store._tables)
        with self.assertRaises(ThresholdConflict):
            self.svc.evaluate()
        self.assertEqual(store._tables, snapshot)  # 事务整体回滚，无半成品

        result = self.svc.retry_recoveries()
        self.assertEqual(len(result["repaired"]), 1)
        hit2 = next(h for h in store.rows("thresholdhit") if h["规则编号"] == "THRE-RB")
        self.assertEqual(hit2["状态"], "已恢复")
        self.assertTrue(any(
            a.get("关联命中编号") == hit2["命中编号"]
            for a in store.rows("alarmhistory")
        ))

    def test_close_rule_forces_recovery(self) -> None:
        self.svc.evaluate()
        _, msg = self.svc.run_action(4, "关闭规则")  # THRE-0004 处于命中中
        self.assertIn("关闭", msg)
        hit = next(h for h in store.rows("thresholdhit") if h["规则编号"] == "THRE-0004")
        self.assertEqual(hit["状态"], "已恢复")
        self.assertEqual(hit["恢复原因"], "规则关闭触发的强制恢复")


if __name__ == "__main__":
    unittest.main()
