# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 / 阈值命中 | 规则编号、关联指标、规则版本、冻结指标版本、持续周期、命中状态、持续时长 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 阈值命中与告警恢复

阈值规则按「草稿 → 规则评审中 → 已生效 → 已关闭」单向流转，相关逻辑都在
`app/services/threshold.py`：

- **保存校验**：生效条件（高于上限/低于下限/超出范围）、阈值上限、生效时间范围、
  持续周期（连续窗口数）、告警等级、关联指标缺一不允许保存，且下限必须小于上限。
- **生效冻结版本快照**：规则生效时冻结关联指标的当前版本、触发条件、上下限、持续周期
  与生效时间范围。业务取舍——一次阈值命中只引用一组可追溯的规则版本 + 指标版本；
  指标后续升级出的新版本窗口按「指标版本冲突」跳过，绝不混版本判定。
- **阈值命中判定**：`POST /api/threshold/evaluate` 读取 `metricwindow` 指标窗口聚合记录，
  连续满足持续周期才生成一条 `thresholdhit` 命中记录，并在同一事务内向告警历史模块
  写入一条告警；窗口不再满足条件时回写同一条告警并计算持续时长。
- **幂等与并发**：命中按「业务键 + 触发时间」去重，重复上报、重复判定、并发生效都不会
  产生多条告警；恢复只回写同一条告警，不覆盖无关记录。
- **事务一致性**：跨表写入走 `store.transaction()`，任何一步失败整体回滚，不残留半成品。
- **中断恢复重试**：`POST /api/threshold/retry-recoveries` 修复缺失的告警关联并补做恢复，
  命中列表空态与重试结果都给出可解释说明。

后端回归测试（仅标准库）：

```bash
cd backend && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

领域：数据工程
子方向：数据分析与数据应用
