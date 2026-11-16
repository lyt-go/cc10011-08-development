"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

事务约定：
- 任何跨表/多步状态变更都必须走 ``transaction()``：进入时对全量数据做深拷贝快照，
  过程中抛异常就整体回滚，保证“数据库事务与状态记录是同一份”，失败不残留半成品。
- 所有写操作都在同一把可重入锁内完成，模拟数据库行锁/唯一约束，避免并发产生
  重复告警、重复生效等问题。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """跨表写入的原子边界：异常时回滚全部变更，成功后一次性提交。"""
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except Exception:
                # 整体回滚：阈值命中、告警记录、规则状态始终保持同一份
                self._tables = snapshot
                raise

    def lock(self) -> threading.RLock:
        """只读+写回组合动作（如幂等判定）需要持锁时使用。"""
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """在调用方持锁/事务内生成下一个自增主键。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
