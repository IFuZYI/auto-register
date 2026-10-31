import { useEffect, useMemo, useState, type Key } from 'react'
import {
  Card,
  Table,
  Button,
  Input,
  Tag,
  Space,
  Popconfirm,
  message,
  Modal,
  Tooltip,
  Switch,
  InputNumber,
  Alert,
} from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SwapRightOutlined,
  SwapLeftOutlined,
  ApartmentOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'
import { withDataLabels } from '@/lib/tableCards'

/** 两个标签的展示口径，与后端 `core/models/proxy.py` 的 TAG_* 一一对应。 */
const TAG_META: Record<string, { label: string; color: string; hint: string }> = {
  register: {
    label: '注册',
    color: 'blue',
    hint: '可用于注册链路：新号优先从带这个标签的出口出去',
  },
  bind: {
    label: '绑定',
    color: 'green',
    hint: '可绑定给账号：账号的代理只会落在这类代理上',
  },
}

type BalanceRow = { total: number; platforms: Record<string, number> }

interface ProxyRow {
  id: number
  url: string
  region: string
  tags: string[]
  success_count: number
  fail_count: number
  is_active: boolean
  last_checked?: string | null
}

interface BalanceItem {
  url: string
  bound_total: number
  bound_by_platform: Record<string, number>
  bindable: boolean
}

interface BalanceResponse {
  items: BalanceItem[]
  bindable_count: number
  bound_accounts: number
  orphan_accounts: number
}

interface BalanceRunResult {
  dry_run: boolean
  changed: number
  skipped: number
  reason: string
  items: Array<{ email: string; from: string; to: string; platform: string }>
}

/** `catch` 的 `e` 是 unknown，统一取出可读消息。 */
function errMsg(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export default function Proxies() {
  const [proxies, setProxies] = useState<ProxyRow[]>([])
  const [newProxy, setNewProxy] = useState('')
  const [region, setRegion] = useState('')
  const [newTags, setNewTags] = useState<string[]>([])
  const [checking, setChecking] = useState(false)
  const [loading, setLoading] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([])
  const [balanceMap, setBalanceMap] = useState<Record<string, BalanceRow>>({})
  const [balanceInfo, setBalanceInfo] = useState({
    bindable_count: 0,
    bound_accounts: 0,
    orphan_accounts: 0,
  })
  const [balanceEnabled, setBalanceEnabled] = useState(true)
  const [balanceMax, setBalanceMax] = useState(0)
  const [balancing, setBalancing] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const [rows, balance, cfg] = await Promise.all([
        apiFetch('/proxies') as Promise<ProxyRow[]>,
        apiFetch('/proxies/balance') as Promise<BalanceResponse>,
        apiFetch('/config') as Promise<Record<string, unknown>>,
      ])
      setProxies(rows || [])
      const map: Record<string, BalanceRow> = {}
      for (const item of balance?.items || []) {
        map[item.url] = { total: item.bound_total, platforms: item.bound_by_platform || {} }
      }
      setBalanceMap(map)
      setBalanceInfo({
        bindable_count: balance?.bindable_count || 0,
        bound_accounts: balance?.bound_accounts || 0,
        orphan_accounts: balance?.orphan_accounts || 0,
      })
      const conf = cfg || {}
      const enabledRaw = String(conf.proxy_balance_enabled ?? '1').toLowerCase()
      setBalanceEnabled(!['0', 'false', 'off', 'no'].includes(enabledRaw))
      setBalanceMax(Number(conf.proxy_balance_max_per_proxy || 0) || 0)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const add = async () => {
    if (!newProxy.trim()) return
    const lines = newProxy.trim().split('\n').map((l) => l.trim()).filter(Boolean)
    try {
      if (lines.length > 1) {
        await apiFetch('/proxies/bulk', {
          method: 'POST',
          body: JSON.stringify({ proxies: lines, region, tags: newTags }),
        })
      } else {
        await apiFetch('/proxies', {
          method: 'POST',
          body: JSON.stringify({ url: lines[0], region, tags: newTags }),
        })
      }
      message.success('添加成功')
      setNewProxy('')
      setRegion('')
      load()
    } catch (e: unknown) {
      message.error(`添加失败: ${errMsg(e)}`)
    }
  }

  const del = async (id: number) => {
    try {
      await apiFetch(`/proxies/${id}`, { method: 'DELETE' })
      message.success('删除成功')
      setSelectedRowKeys((prev) => prev.filter((key) => key !== id))
      load()
    } catch (e: unknown) {
      message.error(`删除失败: ${errMsg(e) || '未知错误'}`)
    }
  }

  const batchDel = async () => {
    if (selectedRowKeys.length === 0) return
    const ids = selectedRowKeys.map((key) => Number(key)).filter((v) => Number.isFinite(v))
    try {
      const result = await apiFetch('/proxies/batch-delete', {
        method: 'POST',
        body: JSON.stringify({ ids }),
      }) as { deleted: number; not_found?: number[]; total_requested?: number }
      setSelectedRowKeys([])
      load()

      const notFound = (result.not_found || []) as number[]
      Modal.success({
        title: '批量删除结果',
        okText: '知道了',
        content: (
          <div>
            <div>请求删除：{result.total_requested ?? ids.length} 条</div>
            <div>成功删除：{result.deleted ?? 0} 条</div>
            <div>未找到：{notFound.length} 条</div>
            {notFound.length > 0 && (
              <div style={{ marginTop: 8, maxHeight: 120, overflow: 'auto', fontFamily: 'monospace' }}>
                {notFound.join(', ')}
              </div>
            )}
          </div>
        ),
      })
    } catch (e: unknown) {
      message.error(`批量删除失败: ${errMsg(e) || '未知错误'}`)
    }
  }

  const toggle = async (id: number) => {
    await apiFetch(`/proxies/${id}/toggle`, { method: 'PATCH' })
    load()
  }

  const check = async () => {
    setChecking(true)
    await apiFetch('/proxies/check', { method: 'POST' })
    setTimeout(() => {
      load()
      setChecking(false)
    }, 3000)
  }

  /** 点标签即切换（PATCH 全量标签列表，后端只认 register / bind）。 */
  const toggleTag = async (record: ProxyRow, tag: string) => {
    const current: string[] = Array.isArray(record.tags) ? record.tags : []
    const next = current.includes(tag) ? current.filter((t) => t !== tag) : [...current, tag]
    try {
      await apiFetch(`/proxies/${record.id}/tags`, {
        method: 'PATCH',
        body: JSON.stringify({ tags: next }),
      })
      setProxies((prev) =>
        prev.map((row) => (row.id === record.id ? { ...row, tags: next } : row)),
      )
      // 绑定池变了，汇总数字也要重算
      const balance = (await apiFetch('/proxies/balance')) as BalanceResponse
      setBalanceInfo({
        bindable_count: balance?.bindable_count || 0,
        bound_accounts: balance?.bound_accounts || 0,
        orphan_accounts: balance?.orphan_accounts || 0,
      })
    } catch (e: unknown) {
      message.error(`改标签失败: ${errMsg(e) || '未知错误'}`)
    }
  }

  /** 均衡：先 dry-run 看会动多少号，确认后再执行。 */
  const runBalance = async (dryRun: boolean) => {
    setBalancing(true)
    try {
      const res = (await apiFetch('/proxies/balance', {
        method: 'POST',
        body: JSON.stringify({ dry_run: dryRun }),
      })) as BalanceRunResult
      if (res.reason) {
        Modal.warning({ title: '无法均衡', content: res.reason, okText: '知道了' })
        return
      }
      if (dryRun) {
        if (!res.changed) {
          Modal.success({
            title: '不需要均衡',
            content: '所有账号都已绑定在带「绑定」标签的代理上。',
            okText: '知道了',
          })
          return
        }
        Modal.confirm({
          title: `将调整 ${res.changed} 个账号的绑定`,
          okText: '执行均衡',
          cancelText: '取消',
          content: (
            <div style={{ maxHeight: 260, overflow: 'auto' }}>
              <div style={{ marginBottom: 8, color: 'var(--text-muted)' }}>
                已合规 {res.skipped} 个不动。预览前 {res.items.length} 条：
              </div>
              <div style={{ fontFamily: 'monospace', fontSize: 12, lineHeight: 1.7 }}>
                {res.items.map((item) => (
                  <div key={item.email}>
                    {item.email} → {String(item.to).slice(0, 42)}…
                  </div>
                ))}
              </div>
            </div>
          ),
          onOk: () => runBalance(false),
        })
      } else {
        message.success(`已均衡 ${res.changed} 个账号`)
        load()
      }
    } catch (e: unknown) {
      message.error(`均衡失败: ${errMsg(e) || '未知错误'}`)
    } finally {
      setBalancing(false)
    }
  }

  const saveBalanceCfg = async (enabled: boolean, max: number) => {
    try {
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({
          data: {
            proxy_balance_enabled: enabled ? '1' : '0',
            proxy_balance_max_per_proxy: String(max || 0),
          },
        }),
      })
      setBalanceEnabled(enabled)
      setBalanceMax(max)
      message.success('均衡配置已保存')
    } catch (e: unknown) {
      message.error(`保存失败: ${errMsg(e) || '未知错误'}`)
    }
  }

  const columns = [
    {
      title: '代理地址',
      dataIndex: 'url',
      key: 'url',
      render: (text: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{text}</span>,
    },
    {
      title: '地区',
      dataIndex: 'region',
      key: 'region',
      render: (text: string) => text || '-',
    },
    {
      title: '标签',
      key: 'tags',
      width: 150,
      render: (_: unknown, record: ProxyRow) => {
        const tags: string[] = Array.isArray(record.tags) ? record.tags : []
        return (
          <Space size={4}>
            {Object.entries(TAG_META).map(([key, meta]) => (
              <Tooltip key={key} title={`${meta.hint}（点击切换）`}>
                <Tag
                  color={tags.includes(key) ? meta.color : undefined}
                  style={{ cursor: 'pointer', opacity: tags.includes(key) ? 1 : 0.45 }}
                  onClick={() => toggleTag(record, key)}
                >
                  {meta.label}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        )
      },
    },
    {
      title: '已绑定账号',
      key: 'bound',
      width: 130,
      render: (_: unknown, record: ProxyRow) => {
        const stat = balanceMap[record.url]
        const total = stat?.total || 0
        const platforms = stat?.platforms || {}
        const detail = Object.entries(platforms)
          .map(([platform, count]) => `${platform}: ${count}`)
          .join('\n')
        const bindable = Array.isArray(record.tags) && record.tags.includes('bind') && record.is_active
        return (
          <Tooltip
            title={<span style={{ whiteSpace: 'pre' }}>{detail || '还没有账号绑定到这个代理'}</span>}
          >
            <Space size={4}>
              <Tag color={total ? 'processing' : undefined}>{total}</Tag>
              {!bindable && <Tag color="warning">不可绑</Tag>}
            </Space>
          </Tooltip>
        )
      },
    },
    {
      title: '成功/失败',
      key: 'stats',
      render: (_: unknown, record: ProxyRow) => (
        <Space>
          <Tag color="success">{record.success_count}</Tag>
          <span>/</span>
          <Tag color="error">{record.fail_count}</Tag>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      render: (active: boolean) => (
        <Tag color={active ? 'success' : 'error'} icon={active ? <CheckCircleOutlined /> : <CloseCircleOutlined />}>
          {active ? '活跃' : '禁用'}
        </Tag>
      ),
    },
    {
      title: '操作',
      key: 'action',
      render: (_: unknown, record: ProxyRow) => (
        <Space>
          <Button
            type="text"
            size="small"
            icon={record.is_active ? <SwapLeftOutlined /> : <SwapRightOutlined />}
            onClick={() => toggle(record.id)}
          />
          <Popconfirm
            title="确认删除该代理吗？"
            onConfirm={() => del(record.id)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const summary = useMemo(
    () =>
      `共 ${proxies.length} 个代理 · 可绑定 ${balanceInfo.bindable_count} 个 · ` +
      `已绑定 ${balanceInfo.bound_accounts} 个账号` +
      (balanceInfo.orphan_accounts ? ` · ${balanceInfo.orphan_accounts} 个账号的代理已删除` : ''),
    [proxies.length, balanceInfo],
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <PageHeader
        title="代理管理"
        subtitle={summary}
        actions={
          <Space>
            <Button
              icon={<ApartmentOutlined />}
              onClick={() => runBalance(true)}
              loading={balancing}
              disabled={!balanceInfo.bindable_count}
            >
              均衡绑定
            </Button>
            <Button icon={<ReloadOutlined spin={checking} />} onClick={check} loading={checking}>
              检测全部
            </Button>
          </Space>
        }
      />

      <Card className="app-card" title="均衡配置">
        <Space size={24} wrap>
          <Space size={8}>
            <span style={{ color: 'var(--text-muted)' }}>按负载均衡</span>
            <Tooltip title="开：给账号挑绑定代理时选当前挂得最少的；关：随机挑">
              <Switch
                checked={balanceEnabled}
                onChange={(checked) => saveBalanceCfg(checked, balanceMax)}
              />
            </Tooltip>
          </Space>
          <Space size={8}>
            <span style={{ color: 'var(--text-muted)' }}>单代理上限</span>
            <Tooltip title="0 = 不限。超过上限的代理不再接收新绑定（全部超限时忽略上限）">
              <InputNumber
                min={0}
                value={balanceMax}
                onChange={(value) => saveBalanceCfg(balanceEnabled, Number(value) || 0)}
                style={{ width: 90 }}
              />
            </Tooltip>
          </Space>
        </Space>
        {!balanceInfo.bindable_count && (
          <Alert
            style={{ marginTop: 12 }}
            type="warning"
            showIcon
            message="还没有「绑定」标签的代理"
            description="账号不会被自动绑定。给至少一个代理打上「绑定」标签后，点「均衡绑定」即可把账号分配过去。"
          />
        )}
      </Card>

      <Card title="添加代理（每行一个）" className="app-card">
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Input.TextArea
            value={newProxy}
            onChange={(e) => setNewProxy(e.target.value)}
            placeholder="http://user:pass@host:port"
            rows={3}
            style={{
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              fontSize: 13,
              // 代理串很长但没必要占满整行 —— 一屏放得下 3 行、扫读也不费力。
              maxWidth: 'var(--w-field-lg)',
            }}
          />
          <Space wrap>
            <Input
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              placeholder="地区标签 (如 US, SG)"
              style={{ width: 'var(--w-field)' }}
            />
            <Space size={4}>
              {Object.entries(TAG_META).map(([key, meta]) => (
                <Tag.CheckableTag
                  key={key}
                  checked={newTags.includes(key)}
                  onChange={(checked) =>
                    setNewTags((prev) => (checked ? [...prev, key] : prev.filter((t) => t !== key)))
                  }
                >
                  {meta.label}
                </Tag.CheckableTag>
              ))}
            </Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={add}>
              添加
            </Button>
          </Space>
        </Space>
      </Card>

      <Card className="app-card">
        <div style={{ marginBottom: 14, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>
            已选中 {selectedRowKeys.length} 条
          </div>
          <Popconfirm
            title={`确认删除选中的 ${selectedRowKeys.length} 条代理？`}
            onConfirm={batchDel}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            disabled={selectedRowKeys.length === 0}
          >
            <Button danger icon={<DeleteOutlined />} disabled={selectedRowKeys.length === 0}>
              批量删除
            </Button>
          </Popconfirm>
        </div>
        <Table
          className="table-cards"
          rowKey="id"
          columns={withDataLabels(columns)}
          dataSource={proxies}
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
          pagination={false}
        />
      </Card>
    </div>
  )
}
