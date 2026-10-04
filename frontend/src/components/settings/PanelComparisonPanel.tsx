import { useCallback, useEffect, useMemo, useState } from 'react'
import { App, Button, Empty, Popconfirm, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CloudDownloadOutlined,
  CloudUploadOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { formatLocalTime, localTimezoneLabel } from '@/lib/time'

/**
 * 选中面板后的本地管理面板：本地账号 ↔ 远端账号的对比 + **面板操作**。
 *
 * 数据来自 `GET /api/integrations/panels/{key}/comparison`（带缓存，`refresh=1`
 * 绕过）。对比口径在服务端（`services/panel_comparison.py`）：**按凭证本体**
 * 判定是否同步 —— AT/RT/session 这些全相同就是「已同步」，有任一不同就是
 * 「凭证不同」；时间只作辅助信息说明是哪边动的（按小时）。
 *
 * 操作区（用户要求「CPA 未上传 / Sub2API 未上传 / 同步 CLIProxyAPI 状态这类
 * 都集成到面板管理里面去」）：对比结果已经知道**哪些本地账号没传上去**，
 * 动作直接在这个页面上发 —— 不用再跳到账号列表去筛一遍。
 * 上传/同步走 `POST /api/actions/{platform}/{action_id}/batch`，
 * 账号 ID 由对比行的 `local_id` 提供。
 */

interface ComparisonRow {
  email: string
  /** 这一行属于哪个平台（CPA 同时托管 ChatGPT 与 Grok） */
  platform: string
  state: string
  label: string
  local_id: number | null
  local_status: string
  local_updated_at: string
  local_updated_hour: string
  local_has_refresh_token: boolean
  remote_id: string
  remote_status: string
  remote_updated_at: string
  remote_updated_at_raw: string
  remote_updated_hour: string
  remote_extra: Record<string, unknown>
  differences: string[]
  /** 凭证比对的差异字段（access_token / refresh_token …） */
  credential_differences: string[]
  /** 凭证实际比了几个字段（0 = 比不了） */
  credential_compared: number
  /** 谁更新（按小时）：local_newer / remote_newer / time_synced / '' */
  time_relation: string
}

interface ComparisonPayload {
  panel: string
  fetched_at: string
  cached: boolean
  summary: Record<string, number>
  /** 状态名 → 中文标签（后端 `services/panel_comparison.py` 的 STATE_LABELS） */
  labels: Record<string, string>
  rows: ComparisonRow[]
  remote_error: string
  local_count: number
  remote_count: number
}

/** 状态 → 颜色。红/黄表示需要动作，绿表示已同步。 */
const STATE_COLORS: Record<string, string> = {
  local_only: 'warning',
  remote_only: 'processing',
  credential_diff: 'error',
  unknown_credential: 'default',
  synced: 'success',
  unknown_time: 'default',
}

/** 平台名 → 展示文案（CPA 这类多平台面板的对比行要标出平台）。 */
const PLATFORM_LABELS: Record<string, string> = {
  chatgpt: 'ChatGPT',
  grok: 'Grok',
}

/**
 * 状态筛选条的展示顺序（标签文案来自后端 `labels`，不在这里再抄一份）。
 *
 * **不含 `unknown_time`**：它是「时间比不了」，而现在时间只是辅助信息
 * （`time_relation`），行状态由凭证比对决定 —— `unknown_time` 永远不会是某行的
 * `state`，摆成筛选项的话那个标签会恒为 0，看着像坏了。
 */
const STATE_ORDER = [
  'local_only',
  'remote_only',
  'credential_diff',
  'unknown_credential',
  'synced',
]

/** 面板上存的时间串 → 浏览器本地时区显示（带时区标注，见 `@/lib/time`）。
 *
 * 为什么不再直接截断 ISO 串：远端面板用 `+08:00` 写时间、本地库写 UTC，
 * 后端归一成 UTC 后直接显示，对 +08:00 的用户每个时间都差 8 小时且无标注。
 * 现在转成**浏览器本地时区**显示，界面上另有 `UTC+8` 这样的标注说明口径。
 */
function shortTime(value: string): string {
  return formatLocalTime(value)
}

/**
 * 批量平台动作的单次账号数上限 —— 与后端 `api/actions.py` 的
 * `_resolve_batch_accounts` 保持一致（超过 1000 个 ID 会被 400 拒掉）。
 * 超出时由 `runBatch` 分批串行发。
 */
const BATCH_ACTION_LIMIT = 1000

/**
 * 可键盘操作的状态筛选标签。
 *
 * antd 的 `Tag.CheckableTag` 只绑了 `onClick` —— 渲染出的 `<span>` 既没有
 * `tabIndex` 也没有 `role`，键盘用户完全用不了这排筛选（Tab 遍历会整组跳过，
 * Enter/Space 也没有任何反应）。`CheckableTag` 会把 restProps 透传到 span 上，
 * 所以这里补上 `role` / `tabIndex` 与 Enter/Space 处理即可，不用换组件。
 *
 * 类型上要放宽：`CheckableTagProps` 没声明 HTML 属性（虽然运行时会透传），
 * 直接用会报 "Property 'role' does not exist"。
 */
const CheckableTag = Tag.CheckableTag as React.ComponentType<
  React.ComponentProps<typeof Tag.CheckableTag> & React.HTMLAttributes<HTMLSpanElement>
>

function FilterTag({
  label,
  checked,
  onSelect,
}: {
  label: string
  checked: boolean
  onSelect: () => void
}) {
  return (
    <CheckableTag
      checked={checked}
      onChange={onSelect}
      role="button"
      tabIndex={0}
      aria-pressed={checked}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect()
        }
      }}
    >
      {label}
    </CheckableTag>
  )
}

/**
 * 表格列定义。
 *
 * 放模块级而不是组件里：这些 render 只用模块级的 `shortTime` / `STATE_COLORS`
 * 与 antd 组件，不读任何 props/state —— 摆在组件体内会让每次渲染都重建整份
 * 列数组与所有闭包（对比面板本身有 7 个筛选项 + 分页，一次交互就多轮渲染）。
 */
const COLUMNS: ColumnsType<ComparisonRow> = [
  {
    title: '平台',
    dataIndex: 'platform',
    width: 90,
    render: (value: string) =>
      value ? (
        // 用已有的 accent / purple token，不引新色板：
        // antd 的 `geekblue` 在暗色下是 rgb(82,115,224)，压在自己的浅底上
        // 只有 4.16:1（低于 AA 4.5，对比度门禁实测抓到）。token 是
        // 全站统一调过对比度的那套（见 `index.css` 的 tag 覆盖段）。
        <Tag color={value === 'grok' ? 'purple' : 'processing'} style={{ marginInlineEnd: 0 }}>
          {PLATFORM_LABELS[value] || value}
        </Tag>
      ) : (
        <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      ),
  },
  {
    title: '邮箱',
    dataIndex: 'email',
    width: 240,
    render: (value: string, row) => (
      <Space direction="vertical" size={0}>
        <Typography.Text ellipsis style={{ maxWidth: 220 }}>{value}</Typography.Text>
        <Space size={4}>
          {row.local_id ? (
            <Tooltip title={`本地账号 id=${row.local_id}，状态 ${row.local_status || '未知'}`}>
              <Tag color={row.local_status === 'banned' ? 'error' : 'default'} style={{ marginInlineEnd: 0 }}>
                本地
              </Tag>
            </Tooltip>
          ) : null}
          {row.remote_id ? (
            <Tooltip title={`远端标识 ${row.remote_id}${row.remote_status ? `，状态 ${row.remote_status}` : ''}`}>
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>远端</Tag>
            </Tooltip>
          ) : null}
          {row.local_id && !row.local_has_refresh_token ? (
            <Tooltip title="本地没有 refresh_token：远端刷新时可能换不到新凭证">
              <Tag color="warning" style={{ marginInlineEnd: 0 }}>无RT</Tag>
            </Tooltip>
          ) : null}
        </Space>
      </Space>
    ),
  },
  {
    title: '对比',
    dataIndex: 'state',
    width: 130,
    render: (value: string, row) => (
      <Tooltip
        title={
          row.state === 'synced'
            ? `凭证一致（比对了 ${row.credential_compared} 个字段）`
            : row.state === 'credential_diff'
              ? `不同的凭证字段：${(row.credential_differences || []).join('、') || '—'}`
              : row.state === 'unknown_credential'
                ? '远端接口不返回凭证（AT/RT），无法比对'
                : row.state === 'unknown_time'
                  ? '至少一边没有可用的更新时间'
                  : `按小时比较（不管分秒）：本地 ${row.local_updated_hour || '—'} / 远端 ${row.remote_updated_hour || '—'}`
        }
      >
        <Tag color={STATE_COLORS[value] || 'default'}>{row.label}</Tag>
      </Tooltip>
    ),
  },
  {
    title: '本地更新时间',
    dataIndex: 'local_updated_at',
    width: 150,
    render: (value: string, row) => (
      <Tooltip
        title={`本地库写的是 UTC，这里按浏览器本地时区（${localTimezoneLabel()}）显示${
          row.local_updated_hour ? `；小时档位 ${row.local_updated_hour}` : ''
        }`}
      >
        <span style={{ fontSize: 12 }}>{shortTime(value)}</span>
      </Tooltip>
    ),
  },
  {
    title: '远端更新时间',
    dataIndex: 'remote_updated_at',
    width: 150,
    render: (value: string, row) => (
      <Tooltip
        title={`远端面板服务器时区可能不同，已归一；这里按浏览器本地时区（${localTimezoneLabel()}）显示${
          row.remote_updated_at_raw ? `；远端原始值：${row.remote_updated_at_raw}` : ''
        }`}
      >
        <span style={{ fontSize: 12 }}>{shortTime(value)}</span>
      </Tooltip>
    ),
  },
  {
    title: '远端信息',
    key: 'remote_extra',
    width: 220,
    render: (_: unknown, row) => {
      const extra = row.remote_extra || {}
      const plan = String(extra.plan_type || '')
      const until = String(extra.chatgpt_subscription_active_until || '')
      const disabled = Boolean(extra.disabled)
      const bits = [
        plan ? `套餐 ${plan}` : '',
        until ? `订阅至 ${shortTime(until)}` : '',
        disabled ? '已禁用' : '',
        row.remote_status ? `状态 ${row.remote_status}` : '',
      ].filter(Boolean)
      if (!bits.length) return <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      return <span style={{ fontSize: 12 }}>{bits.join(' · ')}</span>
    },
  },
  {
    title: '凭证',
    key: 'credentials',
    width: 120,
    render: (_: unknown, row) => {
      // 凭证比对的结果直说：比了几个字段、哪几个不同
      if (row.state === 'credential_diff') {
        return (
          <Tooltip title={`不同的字段：${(row.credential_differences || []).join('、')}`}>
            <Tag color="error">{(row.credential_differences || []).length} 项不同</Tag>
          </Tooltip>
        )
      }
      if (row.state === 'synced') {
        return (
          <Tooltip title={`比对了 ${row.credential_compared} 个凭证字段，全部相同`}>
            <span style={{ color: 'var(--success, #389e0d)', fontSize: 12 }}>
              {row.credential_compared} 项相同
            </span>
          </Tooltip>
        )
      }
      if (row.state === 'unknown_credential') {
        return (
          <Tooltip title="远端接口不返回凭证（AT/RT），无法比对 —— 这不等于已同步">
            <Tag>比不了</Tag>
          </Tooltip>
        )
      }
      return <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
    },
  },
  {
    title: '时间',
    dataIndex: 'time_relation',
    width: 110,
    render: (value: string) => {
      const labels: Record<string, string> = {
        local_newer: '本地较新',
        remote_newer: '远端较新',
        time_synced: '同小时',
      }
      const text = labels[value] || ''
      if (!text) return <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      return (
        <Tooltip title="按小时比较（不管分秒），辅助判断是哪边动的">
          <span style={{ fontSize: 12 }}>{text}</span>
        </Tooltip>
      )
    },
  },
  {
    title: '差异',
    dataIndex: 'differences',
    width: 130,
    render: (value: string[]) =>
      value?.length ? (
        <Tooltip title={value.join('、')}>
          <Tag color="warning">{value.length} 项不一致</Tag>
        </Tooltip>
      ) : (
        <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      ),
  },
]

export function PanelComparisonPanel({
  panelKey,
  panelLabel,
  platform = '',
  platformActions = {},
  uploadAction = '',
  syncAction = '',
}: {
  panelKey: string
  panelLabel: string
  /** 这个面板对应的平台（来自注册表），批量动作要用 */
  platform?: string
  /**
   * 平台 → 动作 id 的映射（CPA 这类多平台面板用）。
   *
   * 面板同时服务多个平台时（CPA 托管 ChatGPT + Grok），同一个动作在两个平台上
   * 是两份实现、两条接口 —— 批量时必须按**行自己的平台**分发，不能全用第一个。
   * 单平台面板走 `platform` + `uploadAction` 那两个参数即可。
   */
  platformActions?: { upload?: Record<string, string>; sync?: Record<string, string> }
  /** 上传动作 id（如 `upload_cpa`）；空串表示这个面板没有上传动作 */
  uploadAction?: string
  /** 拉远端状态的动作 id（如 `sync_cliproxyapi_status`）；空串表示没有 */
  syncAction?: string
}) {
  const { message } = App.useApp()
  const [payload, setPayload] = useState<ComparisonPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [stateFilter, setStateFilter] = useState('')
  // 操作进行中的标记：`upload:<scope>` / `sync:<scope>`
  const [running, setRunning] = useState('')

  const load = useCallback(
    async (refresh: boolean) => {
      if (refresh) setSyncing(true)
      else setLoading(true)
      try {
        const query = refresh ? '?refresh=1' : ''
        const data = (await apiFetch(
          `/integrations/panels/${panelKey}/comparison${query}`,
        )) as ComparisonPayload
        setPayload(data)
        if (refresh) message.success('已同步到最新')
      } catch (e: unknown) {
        message.error(e instanceof Error ? e.message : '加载对比数据失败')
      } finally {
        setLoading(false)
        setSyncing(false)
      }
    },
    [panelKey, message],
  )

  useEffect(() => {
    setPayload(null)
    setStateFilter('')
    void load(false)
  }, [load])

  const rows = useMemo(() => {
    const all = payload?.rows || []
    if (!stateFilter) return all
    return all.filter((row) => row.state === stateFilter)
  }, [payload, stateFilter])

  const summary = payload?.summary || {}
  const stateLabels = payload?.labels || {}
  const fetchTime = shortTime(payload?.fetched_at || '')
  /**
   * 远端读取失败。
   *
   * 此时所有本地账号都会退化成 `local_only`（对比拿不到远端那一侧），
   * 「上传未上传」按钮上的数字会变成**全部本地账号** —— 用户以为在补传几个，
   * 实际会把全部重传一遍。所以这个状态下按钮改成危险样式并在确认框里说清楚。
   */
  const remoteUnavailable = Boolean(payload?.remote_error)

  // 未上传的本地账号（对比已经算出来了，不用再查一遍）
  const unuploadedIds = useMemo(
    () =>
      (payload?.rows || [])
        .filter((row) => row.state === 'local_only' && row.local_id)
        .map((row) => row.local_id as number),
    [payload],
  )
  // 凭证不同的本地账号（上传能把这批刷新到远端）
  const staleIds = useMemo(
    () =>
      (payload?.rows || [])
        .filter((row) => row.state === 'credential_diff' && row.local_id)
        .map((row) => row.local_id as number),
    [payload],
  )
  /**
   * 远端较新的本地账号（拉回能把本地刷新到最新）。
   *
   * 只挑 `time_relation === 'remote_newer'` 的：同小时/本地较新的不动 ——
   * 那些拉回来等于用更旧的凭证覆盖本地（后端 `plan_sync` 也是同一口径，
   * 这里只用来显示计数，实际方向由后端判定）。
   */
  const remoteNewerIds = useMemo(
    () =>
      (payload?.rows || [])
        .filter(
          (row) =>
            row.state === 'credential_diff' &&
            row.local_id &&
            row.time_relation === 'remote_newer',
        )
        .map((row) => row.local_id as number),
    [payload],
  )

  /**
   * 跑一个批量平台动作（账号 ID 由对比结果给）。
   *
   * `kind` 决定用哪个平台的接口：面板可能同时服务多个平台（CPA 托管
   * ChatGPT + Grok），必须按**每个账号自己的平台**分发 —— 用第一个平台的
   * 接口去传另一个平台的账号会报「账号不存在」。
   */
  const runBatch = useCallback(
    async (
      kind: 'upload' | 'sync',
      fallbackActionId: string,
      actionLabel: string,
      accountIds: number[],
      key: string,
    ) => {
      if (!accountIds.length) {
        message.info(`没有需要${actionLabel}的账号`)
        return
      }
      // 平台 → 动作 id：优先按平台的映射，没有就退回面板级动作 id
      const actionFor = (plat: string) =>
        platformActions[kind]?.[plat] || (plat === platform ? fallbackActionId : '')
      // 行上的平台 → 该平台的账号 id（顺序稳定，便于分批）
      const idsByPlatform = new Map<string, number[]>()
      for (const row of payload?.rows || []) {
        if (!row.local_id || !accountIds.includes(row.local_id)) continue
        const plat = row.platform || platform
        if (!actionFor(plat)) continue
        const list = idsByPlatform.get(plat) || []
        list.push(row.local_id)
        idsByPlatform.set(plat, list)
      }
      if (!idsByPlatform.size) return

      setRunning(key)
      const toastKey = `panel-action:${key}`
      message.loading({ content: `${actionLabel}进行中（${accountIds.length} 个）...`, key: toastKey, duration: 0 })
      try {
        let total = 0, success = 0, failed = 0
        for (const [plat, ids] of idsByPlatform) {
          const actionId = actionFor(plat)
          // 后端单次上限 1000（`api/actions.py` 的 `_resolve_batch_accounts`）——
          // 本地账号超过 1000 时直接发会整批 400，一个都处理不了。分批串行发。
          const chunks: number[][] = []
          for (let i = 0; i < ids.length; i += BATCH_ACTION_LIMIT) {
            chunks.push(ids.slice(i, i + BATCH_ACTION_LIMIT))
          }
          for (let i = 0; i < chunks.length; i++) {
            if (chunks.length > 1) {
              message.loading({
                content: `${actionLabel}进行中（${plat} 第 ${i + 1}/${chunks.length} 批，共 ${ids.length} 个）...`,
                key: toastKey,
                duration: 0,
              })
            }
            const result = (await apiFetch(`/actions/${plat}/${actionId}/batch`, {
              method: 'POST',
              body: JSON.stringify({ account_ids: chunks[i], params: {} }),
            })) as { total: number; success: number; failed: number }
            total += result.total
            success += result.success
            failed += result.failed
          }
        }
        if (!failed) {
          message.success({ content: `${actionLabel}完成：成功 ${success} / ${total}`, key: toastKey })
        } else if (!success) {
          message.error({ content: `${actionLabel}失败：成功 0 / ${total}`, key: toastKey })
        } else {
          message.warning({ content: `${actionLabel}部分完成：成功 ${success} / ${total}`, key: toastKey })
        }
        // 动作改的是账号/远端状态，重拉对比才能反映出来
        await load(true)
      } catch (e: unknown) {
        message.error({ content: `${actionLabel}失败：${e instanceof Error ? e.message : e}`, key: toastKey })
      } finally {
        setRunning('')
      }
    },
    [platform, platformActions, payload, message, load],
  )

  /**
   * 「同步到最新」：把远端较新的凭证拉回本地。
   *
   * 与 `runBatch` 不同 —— 这不是按账号发平台动作，而是**整面板**的一次调用
   * （`POST /integrations/panels/{key}/sync`）：方向判定（谁较新）与写库都在
   * 后端做，前端只展示汇总。原因：凭证比对的结果（哪些行 remote_newer）
   * 后端手里才有完整上下文，逐账号发动作会 N 次拉远端。
   */
  const runRemoteSync = useCallback(async () => {
    setRunning('sync-from-remote')
    const toastKey = 'panel-sync-from-remote'
    message.loading({ content: '同步中（拉取远端较新的凭证）...', key: toastKey, duration: 0 })
    try {
      const result = (await apiFetch(`/integrations/panels/${panelKey}/sync`, {
        method: 'POST',
      })) as { pulled: number; skipped: number; total: number; remote_error?: string }
      if (result.remote_error) {
        message.error({ content: `同步失败：${result.remote_error}`, key: toastKey })
      } else if (result.pulled) {
        message.success({
          content: `已从远端拉回 ${result.pulled} 个账号的凭证（其余 ${result.skipped} 个无需同步）`,
          key: toastKey,
        })
      } else {
        message.info({
          content: `没有需要拉回的账号（${result.skipped} 个无需同步）`,
          key: toastKey,
        })
      }
      // 拉回改的是本地账号，重拉对比才能反映出来
      await load(true)
    } catch (e: unknown) {
      message.error({ content: `同步失败：${e instanceof Error ? e.message : e}`, key: toastKey })
    } finally {
      setRunning('')
    }
  }, [panelKey, message, load])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
        <Space wrap>
          <Typography.Text strong>{panelLabel} · 本地 ↔ 远程对比</Typography.Text>
          {fetchTime ? (
            <Tooltip title={`数据拉取时间：${payload?.fetched_at}${payload?.cached ? '（来自缓存）' : ''}`}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                同步时间 {fetchTime}
              </Typography.Text>
            </Tooltip>
          ) : null}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            本地 {payload?.local_count ?? 0} · 远端 {payload?.remote_count ?? 0}
          </Typography.Text>
          {/* 时区标注：本地库写 UTC、远端面板写 +08:00（实测），后端已归一，
              时间列统一按**浏览器本地时区**显示 —— 不标出时区的话用户会把
              显示值按自己的钟面读，对不上时误以为同步出了问题。 */}
          <Tooltip title="所有时间列均按浏览器本地时区显示（后端已把本地 UTC 与远端面板时间归一后比较）">
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              时区 {localTimezoneLabel()}
            </Typography.Text>
          </Tooltip>
        </Space>
        {/* 按钮组要允许换行：窄屏（实测 390px）下三个按钮一排有 503px，
            不 wrap 会被容器裁掉 —— 「重新拉取对比」实测 right=519 > 视口 390，
            只有 15px 可见、点不到。 */}
        <Space wrap>
          {uploadAction || Object.keys(platformActions.upload || {}).length ? (
            <>
              <Popconfirm
                title={
                  remoteUnavailable
                    ? `远端读取失败，无法判断哪些账号没上传 —— 仍要上传全部 ${unuploadedIds.length} 个本地账号？`
                    : `把 ${unuploadedIds.length} 个未上传的账号传到 ${panelLabel}？`
                }
                description={
                  remoteUnavailable
                    ? '对比结果里所有本地账号都成了「未上传」，直接上传等于把全部重传一遍。'
                    : '只处理对比结果里「未上传」的那些账号。'
                }
                okText="上传"
                cancelText="取消"
                disabled={!unuploadedIds.length || Boolean(running)}
                onConfirm={() =>
                  void runBatch('upload', uploadAction, `上传 ${panelLabel}`, unuploadedIds, 'upload-unuploaded')
                }
              >
                <Button
                  type={remoteUnavailable ? 'default' : 'primary'}
                  danger={remoteUnavailable}
                  icon={<CloudUploadOutlined />}
                  loading={running === 'upload-unuploaded'}
                  disabled={!unuploadedIds.length}
                  data-hermes-action="upload-unuploaded"
                >
                  上传未上传 ({unuploadedIds.length})
                </Button>
              </Popconfirm>
              {staleIds.length ? (
                <Popconfirm
                  title={`把 ${staleIds.length} 个「凭证不同」的账号重新传到 ${panelLabel}？`}
                  description="本地凭证比远端新，重传可让远端跟上。"
                  okText="上传"
                  cancelText="取消"
                  disabled={Boolean(running)}
                  onConfirm={() =>
                    void runBatch('upload', uploadAction, `上传 ${panelLabel}`, staleIds, 'upload-stale')
                  }
                >
                  <Button
                    icon={<CloudUploadOutlined />}
                    loading={running === 'upload-stale'}
                    data-hermes-action="upload-stale"
                  >
                    上传凭证不同 ({staleIds.length})
                  </Button>
                </Popconfirm>
              ) : null}
            </>
          ) : null}
          {syncAction || Object.keys(platformActions.sync || {}).length ? (
            <Tooltip title="读远端状态回写本地账号（含封禁/失效判定）—— 会修改本地账号记录">
              <Button
                icon={<SyncOutlined />}
                loading={running === 'sync-remote'}
                disabled={Boolean(running)}
                onClick={() => {
                  // 全部本地账号都要同步 —— 这不是"只处理某一档"的动作
                  const ids = (payload?.rows || [])
                    .filter((row) => row.local_id)
                    .map((row) => row.local_id as number)
                  void runBatch('sync', syncAction, '同步远端状态', ids, 'sync-remote')
                }}
                data-hermes-action="sync-remote-status"
              >
                同步远端状态到本地
              </Button>
            </Tooltip>
          ) : null}
          {/* 「同步到最新」：把远端**较新**的凭证拉回本地（覆盖 AT/RT）。
              与上面的「同步远端状态」不同 —— 那个读的是远端账号的**状态**
              （封禁/失效），这个拉的是**凭证本体**。远端面板刷新过 token 后
              本地存的就是死值（x.ai 的 RT 轮换），不拉回本地就换不出新 token。 */}
          <Tooltip title="把远端面板里较新的凭证（AT/RT）拉回本地 —— 会覆盖本地账号的凭证字段；只处理「远端较新」的账号">
            <Popconfirm
              title={
                remoteNewerIds.length
                  ? `把 ${remoteNewerIds.length} 个「远端较新」账号的凭证拉回本地？`
                  : '按对比结果同步：只拉回「远端较新」的账号'
              }
              description="会覆盖本地账号的 access_token / refresh_token 等凭证字段（其它字段保留）。"
              okText="同步"
              cancelText="取消"
              disabled={Boolean(running) || remoteUnavailable}
              onConfirm={() => void runRemoteSync()}
            >
              <Button
                icon={<CloudDownloadOutlined />}
                loading={running === 'sync-from-remote'}
                disabled={Boolean(running) || remoteUnavailable}
                data-hermes-action="sync-from-remote"
              >
                同步到最新{remoteNewerIds.length ? ` (${remoteNewerIds.length})` : ''}
              </Button>
            </Popconfirm>
          </Tooltip>
          {/* 「同步到最新」与「刷新」只重拉**对比表**，不碰账号数据。两者的唯一
              区别是绕不绕服务端缓存 —— 对用户来说结果一样，所以只留一个按钮，
              并在 tooltip 里说明它会强制重拉。 */}
          <Tooltip title="重新拉取本地与远端的账号清单（强制绕过缓存，不修改任何账号）">
            <Button
              icon={<SyncOutlined />}
              loading={syncing}
              onClick={() => void load(true)}
              data-hermes-action="sync-latest"
            >
              重新拉取对比
            </Button>
          </Tooltip>
        </Space>
      </div>

      {payload?.remote_error ? (
        <div
          style={{
            padding: '8px 12px',
            borderRadius: 8,
            fontSize: 12,
            background: 'var(--bg-subtle)',
            color: 'var(--text-muted)',
          }}
        >
          远端读取失败：{payload.remote_error} —— 下方只显示本地账号
        </div>
      ) : null}

      <Space wrap size={6}>
        <FilterTag
          label={`全部 ${summary.total ?? 0}`}
          checked={stateFilter === ''}
          onSelect={() => setStateFilter('')}
        />
        {STATE_ORDER.map((state) => (
          <FilterTag
            key={state}
            label={`${stateLabels[state] || state} ${summary[state] ?? 0}`}
            checked={stateFilter === state}
            onSelect={() => setStateFilter(state)}
          />
        ))}
      </Space>

      <Spin spinning={loading}>
        {rows.length ? (
          <Table
            rowKey={(row) => row.email}
            columns={COLUMNS}
            dataSource={rows}
            size="small"
            pagination={{ pageSize: 20, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'] }}
            // scroll.x 必须 ≥ 各列宽度之和，否则 antd 会等比压缩所有列 ——
            // 实测 1000 < 列宽和时，唯一的无宽度列（远端信息）被挤到 30px，
            // 表头 4 个字竖排成 94px 高、整行被撑到 62px。
            // 各列：平台 90 + 邮箱 240 + 对比 130 + 本地 150 + 远端 150 +
            // 远端信息 220 + 凭证 120 + 时间 110 + 差异 130 = 1340。
            // 改任一列宽时把这个数一起改。
            scroll={{ x: 1340 }}
          />
        ) : (
          <Empty
            description={
              payload
                ? '没有符合条件的账号'
                : '用左侧「面板管理」下的子项切换面板查看对比'
            }
          />
        )}
      </Spin>
    </div>
  )
}

export default PanelComparisonPanel
