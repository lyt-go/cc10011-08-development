import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/threshold'
const ruleColumns = [
  '规则编号',
  '关联指标',
  '阈值下限',
  '阈值上限',
  '触发条件',
  '持续周期',
  '告警等级',
  '生效时间范围',
  '冻结指标版本',
  '生效时间'
]
const hitColumns = [
  '命中编号',
  '业务键',
  '规则版本',
  '指标版本',
  '状态',
  '触发时间',
  '恢复时间',
  '持续时长说明',
  '关联告警编号'
]
const windowColumns = [
  '窗口编号',
  '指标编号',
  '指标版本',
  '窗口开始',
  '窗口结束',
  '聚合值',
  '样本数'
]
const STATUS_CHAIN = ['草稿', '规则评审中', '已生效', '已关闭']
const NEXT_ACTION: Record<string, string> = {
  草稿: '提交规则评审',
  规则评审中: '生效规则',
  已生效: '关闭规则'
}
const CONDITIONS = ['高于上限', '低于下限', '超出范围']
const LEVELS = ['P1', 'P2', 'P3']

type Row = Record<string, string | number | null>
type TabKey = 'rules' | 'hits' | 'windows'
type FormState = Record<string, string>

const EMPTY_FORM: FormState = {
  规则编号: '',
  关联指标: '',
  阈值下限: '',
  阈值上限: '',
  触发条件: '高于上限',
  持续周期: '3',
  告警等级: 'P2',
  生效时间范围: '2026-09-01 ~ 2026-09-30'
}

function badgeClass(status: string): string {
  if (status === '已生效' || status === '命中中') return 'badge open'
  if (status === '已关闭' || status === '已恢复') return 'badge done'
  return 'badge review'
}

export default function ThresholdView() {
  const [tab, setTab] = useState<TabKey>('rules')
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [hitEmptyMessage, setHitEmptyMessage] = useState('')
  const [report, setReport] = useState<Record<string, unknown> | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<FormState>({ ...EMPTY_FORM })
  const [busy, setBusy] = useState(false)

  const filterFields = useMemo(() => ruleColumns.slice(0, 3), [])

  const loadRules = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('阈值规则列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值管理列表读取失败')
    }
  }, [filters])

  const loadHits = useCallback(async () => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/hits`)
      if (!response.ok) throw new Error('阈值命中记录读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setHitEmptyMessage(payload.message ?? '')
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值命中记录读取失败')
    }
  }, [])

  const loadWindows = useCallback(async () => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/metric-windows`)
      if (!response.ok) throw new Error('指标窗口聚合记录读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setHitEmptyMessage('')
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '指标窗口聚合记录读取失败')
    }
  }, [])

  useEffect(() => {
    setReport(null)
    if (tab === 'rules') void loadRules()
    if (tab === 'hits') void loadHits()
    if (tab === 'windows') void loadWindows()
  }, [tab, loadRules, loadHits, loadWindows])

  const refreshCurrent = useCallback(() => {
    if (tab === 'rules') return loadRules()
    if (tab === 'hits') return loadHits()
    return loadWindows()
  }, [tab, loadRules, loadHits, loadWindows])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '阈值管理动作未生效')
      setNotice(payload.message)
      await loadRules()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值管理操作失败')
    }
  }

  async function evaluate() {
    setBusy(true)
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/evaluate`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      const payload = await response.json()
      setReport(payload)
      if (payload.ok === false) setErrorMessage(payload.message)
      else setNotice(payload.message)
      await refreshCurrent()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值判定失败，事务已回滚')
    } finally {
      setBusy(false)
    }
  }

  async function retryRecoveries() {
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/retry-recoveries`, { method: 'POST', body: JSON.stringify({}) })
      const payload = await response.json()
      setReport(payload)
      if (payload.ok === false) setErrorMessage(payload.message)
      else setNotice(payload.message)
      await refreshCurrent()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '中断恢复重试失败')
    } finally {
      setBusy(false)
    }
  }

  async function submitForm() {
    setErrorMessage('')
    try {
      const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: form }) })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '阈值规则保存失败')
      setShowForm(false)
      setForm({ ...EMPTY_FORM })
      setNotice(payload.message)
      setTab('rules')
      await loadRules()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值规则保存失败')
    }
  }

  const activeColumns = tab === 'hits' ? hitColumns : tab === 'windows' ? windowColumns : ruleColumns
  const stats = useMemo(() => {
    if (tab !== 'rules') return []
    return [
      { label: '阈值规则总数', value: total },
      { label: '已生效规则', value: rows.filter((row) => row.status === '已生效').length },
      { label: '评审中规则', value: rows.filter((row) => row.status === '规则评审中').length }
    ]
  }, [tab, rows, total])

  return (
    <section className="page" data-module="threshold">
      <header className="page-head">
        <div>
          <h2>阈值管理管理</h2>
          <p className="page-desc">
            规则按「草稿 → 规则评审中 → 已生效 → 已关闭」单向流转，生效时冻结指标版本与触发口径；
            连续窗口满足持续周期后生成阈值命中并同步告警，恢复时回写同一条告警并计算持续时长。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => { setShowForm(true); setErrorMessage('') }}>
            登记阈值规则
          </button>
          <button className="btn" type="button" disabled={busy} onClick={() => void evaluate()}>
            计算阈值命中
          </button>
          <button className="btn" type="button" disabled={busy} onClick={() => void retryRecoveries()}>
            重试中断恢复
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出规则清单
          </button>
        </div>
      </header>

      <div className="tab-bar">
        {([['rules', '阈值规则'], ['hits', '阈值命中数据'], ['windows', '指标窗口聚合']] as [TabKey, string][]).map(
          ([key, label]) => (
            <button
              key={key}
              type="button"
              className={`tab-btn ${tab === key ? 'active' : ''}`}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          )
        )}
      </div>

      {stats.length > 0 && (
        <div className="stat-row">
          {stats.map((item) => (
            <article key={item.label} className="stat-card">
              <span className="stat-label">{item.label}</span>
              <strong className="stat-value">{item.value}</strong>
            </article>
          ))}
        </div>
      )}

      {notice && <div className="report-box success-text">{notice}</div>}
      {report && (
        <div className="report-box">
          <strong>最近一次判定/重试结果</strong>
          <ul>
            <li>新触发：{String((report.triggered as unknown[])?.length ?? 0)} 条</li>
            <li>恢复：{String((report.recovered as unknown[])?.length ?? 0)} 条</li>
            <li>跳过窗口：{String((report.skipped as unknown[])?.length ?? 0)} 个（版本冲突或超出生效范围）</li>
            {Array.isArray(report.repaired) && report.repaired.length > 0 && (
              <li>修复告警关联：{String((report.repaired as unknown[]).length)} 条</li>
            )}
          </ul>
        </div>
      )}

      {tab === 'rules' && (
        <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void loadRules() }}>
          {filterFields.map((field) => (
            <label key={field} className="filter-item">
              <span>{field}</span>
              <input
                value={filters[field] ?? ''}
                placeholder={`按${field}检索`}
                onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
              />
            </label>
          ))}
          <button className="btn" type="submit">查询</button>
          <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
        </form>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {activeColumns.map((column) => <th key={column}>{column}</th>)}
            {tab === 'rules' && <th>可执行动作（状态链：{STATUS_CHAIN.join(' → ')}）</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {activeColumns.map((column) => (
                <td key={column}>
                  {column === '状态' || column === 'status' ? (
                    <span className={badgeClass(String(row[column] ?? ''))}>{String(row[column] ?? '—')}</span>
                  ) : column === '业务键' ? (
                    <span className="mono">{String(row[column] ?? '—')}</span>
                  ) : (
                    String(row[column] ?? '—')
                  )}
                </td>
              ))}
              {tab === 'rules' && (
                <td className="row-actions">
                  {(() => {
                    const action = NEXT_ACTION[String(row.status)]
                    return action ? (
                      <button className="link" type="button" onClick={() => void runAction(action, row)}>
                        {action}
                      </button>
                    ) : <span>终态，无动作</span>
                  })()}
                </td>
              )}
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={activeColumns.length + 1}>
                {tab === 'hits' && hitEmptyMessage
                  ? hitEmptyMessage
                  : tab === 'windows'
                    ? '暂无指标窗口聚合记录'
                    : '暂无阈值规则数据，可先登记阈值规则'}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {showForm && (
        <div className="modal-mask" onClick={(event) => { if (event.target === event.currentTarget) setShowForm(false) }}>
          <div className="modal-card">
            <h3 className="modal-title">登记阈值规则（保存前核对生效条件、上限、范围与持续标准）</h3>
            <div className="form-grid">
              <label>
                规则编号
                <input value={form.规则编号} onChange={(e) => setForm({ ...form, 规则编号: e.target.value })} placeholder="如 THRE-0100" />
              </label>
              <label>
                关联指标（指标编号）
                <input value={form.关联指标} onChange={(e) => setForm({ ...form, 关联指标: e.target.value })} placeholder="如 REAL-0001" />
              </label>
              <label>
                阈值下限
                <input value={form.阈值下限} onChange={(e) => setForm({ ...form, 阈值下限: e.target.value })} placeholder="数字，低于下限/超出范围时必填" />
              </label>
              <label>
                阈值上限
                <input value={form.阈值上限} onChange={(e) => setForm({ ...form, 阈值上限: e.target.value })} placeholder="数字，必填" />
              </label>
              <label>
                触发条件
                <select value={form.触发条件} onChange={(e) => setForm({ ...form, 触发条件: e.target.value })}>
                  {CONDITIONS.map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
              </label>
              <label>
                持续周期（连续窗口数）
                <input value={form.持续周期} onChange={(e) => setForm({ ...form, 持续周期: e.target.value })} placeholder="大于 0 的整数" />
              </label>
              <label>
                告警等级
                <select value={form.告警等级} onChange={(e) => setForm({ ...form, 告警等级: e.target.value })}>
                  {LEVELS.map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
              </label>
              <label className="full">
                生效时间范围
                <input value={form.生效时间范围} onChange={(e) => setForm({ ...form, 生效时间范围: e.target.value })} placeholder="2026-09-01 ~ 2026-09-30" />
              </label>
            </div>
            <p className="page-desc">规则保存为草稿，经「提交规则评审 → 生效规则」后生效；生效时会冻结关联指标的当前版本。</p>
            <div className="modal-actions">
              <button className="btn ghost" type="button" onClick={() => setShowForm(false)}>取消</button>
              <button className="btn primary" type="button" onClick={() => void submitForm()}>保存为草稿</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
