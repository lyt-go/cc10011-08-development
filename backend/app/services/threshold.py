"""阈值管理业务规则。

这里收口四件事，路由层不做业务判断：

1. 阈值规则从「草稿 → 规则评审中 → 已生效 → 已关闭」的单向状态链；
2. 规则生效时冻结规则版本快照（关联指标版本、触发条件、上下限、持续周期、生效时间范围）；
3. 读取指标窗口聚合记录，按持续周期判定阈值命中，生成阈值命中数据记录，并在同一事务内
   向告警历史模块写入/回写同一条告警记录（触发、恢复、持续时长）；
4. 中断恢复失败后的重试与可解释空态。

版本冲突的业务取舍（显式约定）：
- 规则生效那一刻冻结关联指标的“当前版本”，之后只认该版本的指标窗口；
- 指标后来升级出的新版本窗口一律跳过并记为“指标版本冲突”，一次阈值命中只引用一组
  可追溯的规则版本 + 指标版本，绝不混版本判定。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "threshold"
METRIC_MODULE = "realtimemetric"
WINDOW_MODULE = "metricwindow"
HIT_MODULE = "thresholdhit"
ALARM_MODULE = "alarmhistory"

# 规则状态链（单向，只能沿 ALLOWED_NEXT 给出的边迁移）
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已关闭"]
ALLOWED_NEXT: dict[str, list[str]] = {
    "草稿": ["规则评审中"],
    "规则评审中": ["已生效", "已关闭"],
    "已生效": ["已关闭"],
    "已关闭": [],
}
ACTION_TARGET = {
    "提交规则评审": "规则评审中",
    "生效规则": "已生效",
    "关闭规则": "已关闭",
}

# 保存规则时必须核对的口径：生效条件、上限、范围、持续标准
VALID_CONDITIONS = {"高于上限", "低于下限", "超出范围"}
SAVE_REQUIRED = ["规则编号", "关联指标", "阈值上限", "触发条件", "持续周期", "告警等级", "生效时间范围"]

HIT_OPEN = "命中中"
HIT_RECOVERED = "已恢复"


class ThresholdConflict(Exception):
    """事务内抛出的业务冲突；由 store.transaction 负责整体回滚。"""


def _to_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_positive_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_datetime(value: Any) -> datetime | None:
    """兼容 ``2026-09-01`` 与 ``2026-09-01T10:00:00`` 两种写法。"""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "T")
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def parse_effective_range(raw: Any) -> tuple[datetime | None, datetime | None, str]:
    """解析生效时间范围，返回 (开始, 结束, 错误说明)。"""
    text = str(raw or "").strip()
    if not text:
        return None, None, "生效时间范围为空"
    separator = ""
    for candidate in ("~", "至"):
        if candidate in text:
            separator = candidate
            break
    if not separator and text.count("-") >= 4:  # ISO 日期范围形如 2026-09-01-2026-09-30
        # 找第二个日期起点（第 3 个“-”之后）
        parts_at_dash = text.split("-")
        if len(parts_at_dash) >= 6:
            second_date = "-".join(parts_at_dash[3:6])
            first_date = "-".join(parts_at_dash[0:3])
            separator = "-"
            text = f"{first_date}~{second_date}"
    if not separator:
        return None, None, "生效时间范围需包含开始与结束时间，例如 2026-09-01 ~ 2026-09-30"
    parts = [part.strip() for part in text.split(separator, 1)]
    start = parse_datetime(parts[0])
    end = parse_datetime(parts[1]) if len(parts) > 1 else None
    if start is None:
        return None, None, "生效时间范围的开始时间无法解析"
    if end is None:
        return None, None, "生效时间范围的结束时间无法解析"
    # 只写日期的结束时间按当天 23:59:59 处理，避免把结束当天整天排除在生效范围外
    if "T" not in parts[1].replace(" ", "T") and ":" not in parts[1]:
        end = end.replace(hour=23, minute=59, second=59)
    if start >= end:
        return None, None, "生效时间范围的开始时间必须早于结束时间"
    return start, end, ""


def format_duration(seconds: int) -> str:
    minutes, _ = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时"
    if minutes:
        return f"{minutes}分钟"
    return f"{max(seconds, 0)}秒"


def condition_holds(value: float, snapshot: dict[str, Any]) -> bool:
    """按生效时冻结的触发条件与上下限判定单个聚合值是否命中。"""
    condition = snapshot["触发条件"]
    lower = snapshot.get("阈值下限")
    upper = snapshot.get("阈值上限")
    if condition == "高于上限":
        return upper is not None and value > upper
    if condition == "低于下限":
        return lower is not None and value < lower
    if condition == "超出范围":
        return (upper is not None and value > upper) or (lower is not None and value < lower)
    return False


class ThresholdService:
    # ------------------------------------------------------------------ 列表/详情
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_windows(self) -> list[dict[str, Any]]:
        """指标窗口聚合记录（阈值判定的输入），按指标与窗口时间排序。"""
        rows = list(store.rows(WINDOW_MODULE))
        return sorted(rows, key=lambda row: (str(row.get("指标编号")), str(row.get("窗口开始"))))

    # ------------------------------------------------------------------ 保存校验
    def validate_rule(self, values: dict[str, Any]) -> list[str]:
        """核对生效条件、上限、范围与持续标准；缺任何一项都不允许保存。"""
        problems: list[str] = []
        for field in SAVE_REQUIRED:
            if not str(values.get(field) or "").strip():
                problems.append(field)
        if problems:
            return [f"缺少必填字段：{name}" for name in problems]

        condition = str(values["触发条件"]).strip()
        if condition not in VALID_CONDITIONS:
            problems.append(f"触发条件「{condition}」不支持，仅支持：{'、'.join(sorted(VALID_CONDITIONS))}")

        upper = _to_float(values.get("阈值上限"))
        lower = _to_float(values.get("阈值下限"))
        if upper is None:
            problems.append("阈值上限必须是数字")
        if condition in {"低于下限", "超出范围"} and lower is None:
            problems.append(f"触发条件为「{condition}」时必须提供可解析的阈值下限")
        if lower is not None and upper is not None and lower >= upper:
            problems.append("阈值下限必须小于阈值上限")

        if _to_positive_int(values.get("持续周期")) is None:
            problems.append("持续周期必须是大于 0 的整数（连续满足的窗口数）")

        start, end, range_error = parse_effective_range(values.get("生效时间范围"))
        if range_error:
            problems.append(range_error)

        metric_code = str(values["关联指标"]).strip()
        if not any(str(row.get("指标编号")) == metric_code for row in store.rows(METRIC_MODULE)):
            problems.append(f"关联指标「{metric_code}」在实时指标模块中不存在")

        code = str(values["规则编号"]).strip()
        if any(str(row.get("规则编号")) == code for row in store.rows(MODULE)):
            problems.append(f"规则编号「{code}」已存在，规则编号必须唯一")
        return problems

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        problems = self.validate_rule(values)
        if problems:
            return None, problems
        with store.transaction():
            entry = {"id": store.next_id(MODULE)}
            entry.update({
                "规则编号": str(values["规则编号"]).strip(),
                "关联指标": str(values["关联指标"]).strip(),
                "阈值下限": str(values.get("阈值下限") or "").strip(),
                "阈值上限": str(values["阈值上限"]).strip(),
                "触发条件": str(values["触发条件"]).strip(),
                "持续周期": str(_to_positive_int(values["持续周期"])),
                "告警等级": str(values["告警等级"]).strip(),
                "生效时间范围": str(values["生效时间范围"]).strip(),
                "规则版本": "v1",
                "冻结指标版本": None,
                "版本快照": None,
                "生效时间": None,
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
            })
            store.rows(MODULE).append(entry)
            return entry, []

    # ------------------------------------------------------------------ 状态链
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_TARGET:
            return None, f"动作「{action}」不属于阈值管理可执行范围"
        target = ACTION_TARGET[action]
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"阈值规则 {entry_id} 不存在或已归档"
            current = str(entry.get("status"))
            if target not in ALLOWED_NEXT.get(current, []):
                chain = " → ".join(STATUS_ORDER)
                return None, (
                    f"规则当前为「{current}」，不允许直接执行「{action}」；"
                    f"阈值规则只能沿单向状态链迁移：{chain}"
                )
            if action == "生效规则":
                message = self._activate(entry)
                if message:
                    raise ThresholdConflict(message)
            elif action == "关闭规则":
                message = self._close(entry)
                if message:
                    raise ThresholdConflict(message)
            else:
                entry["status"] = target
                entry["pending"] = True
            return entry, f"阈值规则已{action}"

    def _current_metric_version(self, metric_code: str) -> tuple[dict[str, Any] | None, str]:
        """取关联指标当前生效版本；业务取舍：生效时冻结，后续新版本窗口不参与判定。"""
        candidates = [
            row for row in store.rows(METRIC_MODULE)
            if str(row.get("指标编号")) == metric_code
        ]
        if not candidates:
            return None, f"关联指标「{metric_code}」不存在，无法冻结指标版本"
        current = [row for row in candidates if row.get("当前版本")]
        chosen = current[0] if current else sorted(candidates, key=lambda row: str(row.get("指标版本")))[-1]
        return chosen, ""

    def _activate(self, entry: dict[str, Any]) -> str:
        """生效规则：再校验一次口径，并冻结规则版本快照。"""
        problems = self.validate_rule(entry)
        # 编辑场景下规则编号已存在是预期情况，剔除这条“自我冲突”
        problems = [problem for problem in problems if not problem.startswith("规则编号「")]
        if problems:
            return "规则不满足生效条件，已阻止生效：" + "；".join(problems)
        metric, error = self._current_metric_version(str(entry["关联指标"]))
        if error:
            return error
        start, end, range_error = parse_effective_range(entry.get("生效时间范围"))
        if range_error:
            return range_error
        snapshot = {
            "规则版本": str(entry.get("规则版本") or "v1"),
            "关联指标": str(entry["关联指标"]),
            "指标版本": str(metric["指标版本"]),
            "阈值下限": _to_float(entry.get("阈值下限")),
            "阈值上限": _to_float(entry.get("阈值上限")),
            "触发条件": str(entry["触发条件"]),
            "持续周期": _to_positive_int(entry.get("持续周期")),
            "告警等级": str(entry["告警等级"]),
            "生效开始": start.isoformat() if start else None,
            "生效结束": end.isoformat() if end else None,
        }
        entry["冻结指标版本"] = snapshot["指标版本"]
        entry["版本快照"] = snapshot
        entry["生效时间"] = datetime.now().replace(microsecond=0).isoformat()
        entry["status"] = "已生效"
        entry["pending"] = False
        entry["abnormal"] = False
        return ""

    def _close(self, entry: dict[str, Any]) -> str:
        """关闭规则：先把仍在命中中的告警恢复掉（回写同一条记录），再落关闭状态。"""
        open_hits = [
            hit for hit in store.rows(HIT_MODULE)
            if str(hit.get("规则编号")) == str(entry["规则编号"]) and hit.get("状态") == HIT_OPEN
        ]
        boundary = datetime.now().replace(microsecond=0)
        for hit in open_hits:
            error = self._recover_hit(hit, boundary, reason="规则关闭触发的强制恢复")
            if error:
                return error
        entry["status"] = "已关闭"
        entry["pending"] = False
        return ""

    # ------------------------------------------------------------------ 命中判定
    @staticmethod
    def business_key(rule: dict[str, Any]) -> str:
        return f"{rule['规则编号']}|{rule.get('规则版本', 'v1')}|{rule.get('冻结指标版本')}"

    def _find_open_hit(self, key: str) -> dict[str, Any] | None:
        for hit in store.rows(HIT_MODULE):
            if hit.get("业务键") == key and hit.get("状态") == HIT_OPEN:
                return hit
        return None

    def _hit_exists(self, key: str, trigger_time: datetime) -> dict[str, Any] | None:
        """同业务键 + 同触发时刻的命中是否已存在（含已恢复），用于确定性幂等。"""
        stamp = trigger_time.isoformat()
        for hit in store.rows(HIT_MODULE):
            if hit.get("业务键") == key and hit.get("触发时间") == stamp:
                return hit
        return None

    def _find_alarm_for_hit(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        for alarm in store.rows(ALARM_MODULE):
            if alarm.get("关联命中编号") == hit["命中编号"] or (
                alarm.get("业务键") == hit["业务键"] and alarm.get("处置状态") != "已恢复"
            ):
                return alarm
        return None

    def list_hits(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, str]:
        """阈值命中记录列表；空态给出可解释说明，而不是一个空白表格。"""
        rows = list(store.rows(HIT_MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("业务键", "")) or keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows.sort(key=lambda row: str(row.get("触发时间")), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        if total == 0:
            message = (
                "暂无阈值命中记录：当前没有「已生效」规则，或生效规则对应的指标窗口"
                "尚未连续满足持续周期；指标版本与规则冻结版本不一致的窗口会按版本冲突跳过。"
            )
        else:
            message = ""
        return rows[start:start + size], total, message

    def evaluate(self, rule_id: int | None = None) -> dict[str, Any]:
        """按持续周期校验指标窗口并计算阈值命中；触发与恢复在同一事务内落库。"""
        report: dict[str, Any] = {
            "ok": True,
            "evaluated": [],
            "triggered": [],
            "recovered": [],
            "skipped": [],
            "message": "",
        }
        with store.transaction():
            rules = [row for row in store.rows(MODULE) if row.get("status") == "已生效"]
            if rule_id is not None:
                target = store.find(MODULE, rule_id)
                if target is None:
                    raise ThresholdConflict(f"阈值规则 {rule_id} 不存在或已归档")
                if target.get("status") != "已生效":
                    report["skipped"].append({
                        "规则编号": target.get("规则编号"),
                        "原因": f"规则状态为「{target.get('status')}」，仅「已生效」规则参与阈值判定",
                    })
                    rules = []
                else:
                    rules = [target]
            if not rules:
                report["message"] = "没有可判定的「已生效」阈值规则，未生成阈值命中记录。"
                return report

            for rule in rules:
                self._evaluate_rule(rule, report)

            if not report["triggered"] and not report["recovered"]:
                report["message"] = (
                    "本轮判定没有产生新的阈值命中或恢复：窗口未连续满足持续周期，"
                    "或相关窗口因指标版本冲突/超出生效时间范围被跳过。"
                )
            else:
                report["message"] = (
                    f"判定完成：新触发 {len(report['triggered'])} 条，恢复 {len(report['recovered'])} 条，"
                    f"跳过窗口 {len(report['skipped'])} 个。"
                )
            return report

    def _evaluate_rule(self, rule: dict[str, Any], report: dict[str, Any]) -> None:
        snapshot = rule.get("版本快照")
        key = self.business_key(rule)
        if not isinstance(snapshot, dict):
            report["skipped"].append({"规则编号": rule.get("规则编号"), "原因": "规则缺少生效时冻结的版本快照"})
            return
        report["evaluated"].append(rule.get("规则编号"))

        start_dt = parse_datetime(snapshot["生效开始"])
        end_dt = parse_datetime(snapshot["生效结束"])
        streak: list[dict[str, Any]] = []
        for window in self.list_windows():
            if str(window.get("指标编号")) != snapshot["关联指标"]:
                continue
            window_start = parse_datetime(window.get("窗口开始"))
            if window_start is None:
                report["skipped"].append({"规则编号": rule.get("规则编号"), "窗口编号": window.get("窗口编号"),
                                          "原因": "窗口开始时间无法解析"})
                continue
            if str(window.get("指标版本")) != snapshot["指标版本"]:
                report["skipped"].append({
                    "规则编号": rule.get("规则编号"), "窗口编号": window.get("窗口编号"),
                    "原因": f"指标版本冲突：规则冻结版本 {snapshot['指标版本']}，窗口版本 {window.get('指标版本')}，该窗口不参与判定",
                })
                continue
            if start_dt and (window_start < start_dt or window_start > end_dt):
                report["skipped"].append({
                    "规则编号": rule.get("规则编号"), "窗口编号": window.get("窗口编号"),
                    "原因": "窗口落在规则生效时间范围之外",
                })
                continue

            value = _to_float(window.get("聚合值"))
            if value is None:
                report["skipped"].append({"规则编号": rule.get("规则编号"), "窗口编号": window.get("窗口编号"),
                                          "原因": "聚合值无法解析为数字"})
                continue

            if condition_holds(value, snapshot):
                streak.append(window)
                # 仅在连续窗口数“首次达到”持续周期的那一刻触发一次；触发后清空计数，
                # 只有先经历恢复（条件中断）再重新攒满持续周期，才会产生下一轮告警
                if len(streak) == int(snapshot["持续周期"]):
                    self._trigger_hit(rule, snapshot, key, streak, value, report)
                    streak = []
            else:
                open_hit = self._find_open_hit(key)
                if open_hit is not None:
                    error = self._recover_hit(open_hit, window_start, reason="连续窗口不再满足触发条件")
                    if error:
                        # 事务回滚后由上层统一提示重试，保证不残留半成品
                        raise ThresholdConflict(error)
                # 条件中断，连续命中计数清零；后续再次满足需重新攒满持续周期才会再触发
                streak = []

    def _trigger_hit(
        self,
        rule: dict[str, Any],
        snapshot: dict[str, Any],
        key: str,
        streak: list[dict[str, Any]],
        latest_value: float,
        report: dict[str, Any],
    ) -> None:
        """生成阈值命中记录，并在同一事务写入告警历史（一次命中只写一条告警）。"""
        trigger_window = streak[0]
        trigger_time = parse_datetime(trigger_window["窗口开始"])
        # 幂等：同一业务键在同一触发时刻只允许一条命中（无论命中中还是已恢复），
        # 保证重复上报、重复判定、并发判定都不会产生多条告警
        if self._find_open_hit(key) is not None or self._hit_exists(key, trigger_time):
            return
        hit_id = store.next_id(HIT_MODULE)
        alarm_id = store.next_id(ALARM_MODULE)
        hit_code = f"HIT-{hit_id:04d}"
        alarm_code = f"ALAR-{alarm_id:04d}"
        threshold_compare = self._threshold_text(snapshot)
        hit = {
            "id": hit_id,
            "status": HIT_OPEN,
            "状态": HIT_OPEN,
            "pending": True,
            "abnormal": True,
            "命中编号": hit_code,
            "业务键": key,
            "规则编号": rule["规则编号"],
            "规则版本": snapshot["规则版本"],
            "关联指标": snapshot["关联指标"],
            "指标版本": snapshot["指标版本"],
            "触发时间": trigger_time.isoformat(),
            "恢复时间": None,
            "持续时长秒": None,
            "持续时长说明": None,
            "触发窗口序列": [str(window.get("窗口编号")) for window in streak],
            "命中时聚合值": latest_value,
            "阈值下限": snapshot.get("阈值下限"),
            "阈值上限": snapshot.get("阈值上限"),
            "触发条件": snapshot["触发条件"],
            "持续周期": snapshot["持续周期"],
            "告警等级": snapshot["告警等级"],
            "关联告警编号": alarm_code,
            "版本快照": dict(snapshot),
        }
        alarm = {
            "id": alarm_id,
            "status": "已触发",
            "pending": True,
            "abnormal": True,
            "记录编号": alarm_code,
            "告警规则": rule["规则编号"],
            "触发时间": trigger_time.isoformat(),
            "当前值": str(latest_value),
            "阈值对比": threshold_compare,
            "恢复时间": None,
            "持续时长": None,
            "处置状态": "已触发",
            "业务键": key,
            "关联规则版本": snapshot["规则版本"],
            "关联指标版本": snapshot["指标版本"],
            "关联命中编号": hit_code,
        }
        store.rows(HIT_MODULE).append(hit)
        store.rows(ALARM_MODULE).append(alarm)  # 沿数据流在这里同步写入告警历史模块
        report["triggered"].append({"命中编号": hit_code, "业务键": key, "告警编号": alarm_code,
                                    "触发时间": hit["触发时间"]})

    def _recover_hit(self, hit: dict[str, Any], boundary: datetime, *, reason: str) -> str:
        """恢复时回写同一条告警记录并计算持续时长；找不到对应告警即视为中断失败。"""
        alarm = self._find_alarm_for_hit(hit)
        if alarm is None:
            return (
                f"业务键 {hit['业务键']} 的命中记录找不到关联告警 {hit.get('关联告警编号')}，"
                "恢复中断，本次变更已回滚；请调用「重试中断恢复」接口修复后再判定。"
            )
        trigger_time = parse_datetime(hit["触发时间"])
        seconds = int((boundary - trigger_time).total_seconds())
        hit["恢复时间"] = boundary.isoformat()
        hit["持续时长秒"] = seconds
        hit["持续时长说明"] = format_duration(seconds)
        hit["状态"] = HIT_RECOVERED
        hit["status"] = HIT_RECOVERED
        hit["pending"] = False
        hit["abnormal"] = False
        hit["恢复原因"] = reason
        # 只回写同一条告警，绝不覆盖无关记录
        alarm["恢复时间"] = boundary.isoformat()
        alarm["持续时长"] = format_duration(seconds)
        alarm["持续时长秒"] = seconds
        alarm["处置状态"] = "已恢复"
        alarm["status"] = "已恢复"
        alarm["pending"] = False
        alarm["abnormal"] = False
        return ""

    def retry_recoveries(self) -> dict[str, Any]:
        """中断恢复失败后的重试：修复缺失的告警关联，并对仍在命中中的记录补做恢复。"""
        result: dict[str, Any] = {"ok": True, "repaired": [], "recovered": [], "pending_hits": [], "message": ""}
        with store.transaction():
            open_hits = [hit for hit in store.rows(HIT_MODULE) if hit.get("状态") == HIT_OPEN]
            if not open_hits:
                result["message"] = "没有处于「命中中」的阈值命中记录，无需重试恢复。"
                return result
            for hit in open_hits:
                alarm = self._find_alarm_for_hit(hit)
                if alarm is None:
                    # 半成品修复：依据命中记录里冻结的可追溯快照补建同一条告警
                    alarm_id = store.next_id(ALARM_MODULE)
                    alarm_code = hit.get("关联告警编号") or f"ALAR-{alarm_id:04d}"
                    alarm = {
                        "id": alarm_id,
                        "status": "已触发",
                        "pending": True,
                        "abnormal": True,
                        "记录编号": alarm_code,
                        "告警规则": hit["规则编号"],
                        "触发时间": hit["触发时间"],
                        "当前值": str(hit.get("命中时聚合值")),
                        "阈值对比": self._threshold_text(hit["版本快照"]),
                        "恢复时间": None,
                        "持续时长": None,
                        "处置状态": "已触发",
                        "业务键": hit["业务键"],
                        "关联规则版本": hit["规则版本"],
                        "关联指标版本": hit["指标版本"],
                        "关联命中编号": hit["命中编号"],
                    }
                    store.rows(ALARM_MODULE).append(alarm)
                    hit["关联告警编号"] = alarm_code
                    result["repaired"].append({"业务键": hit["业务键"], "补建告警": alarm_code})

                snapshot = hit.get("版本快照") or {}
                boundary: datetime | None = None
                for window in self.list_windows():
                    if str(window.get("指标编号")) != str(hit.get("关联指标")):
                        continue
                    if str(window.get("指标版本")) != str(hit.get("指标版本")):
                        continue
                    window_start = parse_datetime(window.get("窗口开始"))
                    if window_start is None or window_start <= parse_datetime(hit["触发时间"]):
                        continue
                    value = _to_float(window.get("聚合值"))
                    if value is not None and not condition_holds(value, snapshot):
                        boundary = window_start
                        break
                if boundary is None:
                    result["pending_hits"].append(hit["命中编号"])
                    continue
                error = self._recover_hit(hit, boundary, reason="中断恢复重试")
                if error:
                    raise ThresholdConflict(error)
                result["recovered"].append({"业务键": hit["业务键"], "恢复时间": boundary.isoformat()})
            if not result["message"]:
                if result["pending_hits"]:
                    result["message"] = (
                        f"重试完成：修复告警关联 {len(result['repaired'])} 条；"
                        f"另有 {len(result['pending_hits'])} 条命中的最新窗口仍满足触发条件，"
                        "告警保持「命中中」，待出现不满足条件的窗口后再恢复。"
                    )
                else:
                    result["message"] = (
                        f"重试完成：修复告警关联 {len(result['repaired'])} 条，恢复命中 {len(result['recovered'])} 条。"
                    )
            return result

    @staticmethod
    def _threshold_text(snapshot: dict[str, Any]) -> str:
        lower = snapshot.get("阈值下限")
        upper = snapshot.get("阈值上限")
        return f"{snapshot.get('触发条件')}（下限 {lower if lower is not None else '—'} / 上限 {upper if upper is not None else '—'}）"
