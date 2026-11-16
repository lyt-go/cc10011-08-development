"""阈值管理接口。

除规则维护与单向状态流转外，还提供：
- GET  /metric-windows            指标窗口聚合记录（判定输入）
- GET  /hits                      阈值命中数据记录（带可解释空态）
- POST /evaluate                  按持续周期计算阈值命中并同步写告警历史
- POST /retry-recoveries          中断恢复失败后的重试
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.threshold import ThresholdConflict, ThresholdService

router = APIRouter(prefix="/api/threshold", tags=["阈值管理"])

service = ThresholdService()

LIST_FIELDS = ["规则编号", "关联指标", "阈值下限", "阈值上限", "触发条件", "持续周期", "告警等级", "生效时间范围"]
STATUSES = ["草稿", "规则评审中", "已生效", "已关闭"]
HIT_FIELDS = [
    "命中编号", "业务键", "规则编号", "规则版本", "指标版本", "状态",
    "触发时间", "恢复时间", "持续时长说明", "关联告警编号",
]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤阈值管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/metric-windows")
def list_metric_windows() -> dict[str, Any]:
    """指标窗口聚合记录：阈值命中判定读取的输入数据，按指标与窗口时间排序。"""
    items = service.list_windows()
    return {"module": "metricwindow", "total": len(items), "items": items}


@router.get("/hits")
def list_hits(
    keyword: str | None = Query(default=None, description="按规则编号或业务键检索"),
    status: str | None = Query(default=None, description="命中中、已恢复"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """阈值命中数据记录；空态附带可解释说明，告诉调用方为什么没有数据。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total, message = service.list_hits(keyword=keyword, status=status, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size, "message": message}


@router.post("/evaluate")
def evaluate(payload: EntryPayload | None = None) -> dict[str, Any]:
    """读取指标窗口与阈值规则，按持续周期校验并生成阈值命中；失败整体回滚。"""
    rule_id_raw = payload.values.get("rule_id") if payload and payload.values else None
    rule_id: int | None = None
    if rule_id_raw not in (None, ""):
        try:
            rule_id = int(rule_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="rule_id 必须是整数")
    try:
        return service.evaluate(rule_id)
    except ThresholdConflict as exc:
        # 事务已回滚：规则、命中、告警不会残留半成品
        return {"ok": False, "evaluated": [], "triggered": [], "recovered": [],
                "skipped": [], "message": str(exc)}


@router.post("/retry-recoveries")
def retry_recoveries() -> dict[str, Any]:
    """中断恢复失败后的重试入口：修复缺失告警关联并补做恢复。"""
    try:
        return service.retry_recoveries()
    except ThresholdConflict as exc:
        return {"ok": False, "repaired": [], "recovered": [], "message": str(exc)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出阈值管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "threshold", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条阈值规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"阈值规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条阈值规则；生效条件、上限、范围与持续标准缺任何一项都不允许保存。"""
    entry, problems = service.create_entry(payload.values)
    if problems:
        return ActionResult(ok=False, message="；".join(problems))
    return ActionResult(ok=True, message="阈值规则已登记（草稿）", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条阈值规则执行提交规则评审、生效规则、关闭规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action)
    except ThresholdConflict as exc:
        return ActionResult(ok=False, message=str(exc))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
