import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Grid,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  ImportOutlined,
  KeyOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { PageHeader } from '@/components/PageHeader'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { DetailGroup, DetailRow, SecretLine } from '@/components/account/AccountDetailParts'
import { accountStatusMeta, formatCreatedAt } from '@/lib/accountFormat'
import { withDataLabels } from '@/lib/tableCards'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

/**
 * 授权账号 = AWS Builder ID。
 *
 * 它不再是「平台管理」里的一个平台：Kiro 的凭据是**用它换出来的**，
 * 所以这里把它当授权根来管（列表、派生链、测活、刷新、导入）。
 * 平台的注册入口仍然保留 —— 顶部「注册 AWS 账号」直接建号。
 */

interface AccountRow {
  id: string
  email: string
  status: string
  region?: string
  created_at?: string
  has_token?: boolean
  extra_json?: string
}

interface Derived {
  id: string
  email: string
  status: string
  plan: string
}

interface AccountList {
  total: number
  items: AccountRow[]
}

function errMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function parseExtra(raw?: string): Record<string, unknown> {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw) as unknown
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

/** Kiro 账号按 `source_email` 归到 AWS 邮箱下（大小写无关）。 */
function buildDerived(items: AccountRow[]): Record<string, Derived[]> {
  const map: Record<string, Derived[]> = {}
  for (const item of items) {
    const extra = parseExtra(item.extra_json)
    const source = String(extra.source_email || extra.account_id || '').trim().toLowerCase()
    if (!source) continue
    const entry: Derived = {
      id: item.id,
      email: item.email,
      status: item.status,
      plan: String(extra.plan || ''),
    }
    map[source] = [...(map[source] || []), entry]
  }
  return map
}

/** 授权链：根（AWS）与派生（Kiro）之间的一条细线。 */
function ChainCell({ count }: { count: number }) {
  return (
    <span className="auth-chain" title={count ? `${count} 个 Kiro 账号由它换取凭据` : '还没有派生 Kiro 账号'}>
      <span className="auth-chain__node auth-chain__node--root" />
      <span className={`auth-chain__link${count ? '' : ' auth-chain__link--idle'}`} />
      <span className={`auth-chain__node ${count ? 'auth-chain__node--derived' : 'auth-chain__node--idle'}`} />
      <span className="auth-chain__label">{count ? `Kiro ${count}` : '未派生'}</span>
    </span>
  )
}

/** 表格内展开的派生行：与抽屉里的授权树同一套「脊柱」语言。 */
function DerivedList({ items }: { items: Derived[] }) {
  if (!items.length) return null
  return (
    <div className="auth-lineage" role="list" aria-label="派生 Kiro 账号">
      {items.map((item) => (
        <div className="auth-lineage__row" role="listitem" key={item.id}>
          <span className="auth-lineage__node" />
          <div className="auth-lineage__body">
            <span className="auth-lineage__email">{item.email}</span>
            <span className="auth-lineage__meta">
              {item.plan || 'Kiro'}
              {item.status ? `，${accountStatusMeta(item.status).label}` : ''}
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}

/** 授权树：抽屉里的纵向链（根在上，Kiro 派生在下）。 */
function AuthTree({ status, derived }: { status: string; derived: Derived[] }) {
  const meta = accountStatusMeta(status)
  return (
    <div className="auth-tree">
      <div className="auth-tree__root">
        <span className="auth-tree__node auth-tree__node--root" />
        <div className="auth-tree__body">
          {/* 邮箱在抽屉标题里，这里只标身份类型，避免同一屏重复两遍 */}
          <div className="auth-tree__email">AWS Builder ID</div>
          <div className="auth-tree__meta">
            授权根
            <Tag color={meta.color} style={{ marginInlineStart: 8 }}>{meta.label}</Tag>
          </div>
        </div>
      </div>
      <div className="auth-tree__branches">
        {derived.length === 0 ? (
          <div className="auth-tree__empty">
            还没有 Kiro 账号用它换取凭据。到「注册任务」里注册一个 Kiro 账号，就会在它的来源里记下这个邮箱。
          </div>
        ) : (
          derived.map((item) => (
            <div className="auth-tree__branch" key={item.id}>
              <span className="auth-tree__node auth-tree__node--derived" />
              <div className="auth-tree__body">
                <div className="auth-tree__email">{item.email}</div>
                <div className="auth-tree__meta">
                  {item.plan || 'Kiro'}
                  {item.status ? `，${accountStatusMeta(item.status).label}` : ''}
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

export default function AuthorizedAccounts() {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [email, setEmail] = useState('')
  const [status, setStatus] = useState('')
  const [derivedMap, setDerivedMap] = useState<Record<string, Derived[]>>({})
  const loadSeq = useRef(0)
  const detailId = useRef<string>('')
  const screens = Grid.useBreakpoint()
  const narrow = !screens.md

  const [detail, setDetail] = useState<AccountRow | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)

  const [registerOpen, setRegisterOpen] = useState(false)
  const [registerForm] = Form.useForm()
  const [registerTaskId, setRegisterTaskId] = useState<string | null>(null)
  const [registering, setRegistering] = useState(false)

  const [actionTask, setActionTask] = useState<{
    id: string
    title: string
    kind: 'batch_action' | 'register'
  } | null>(null)

  const [importOpen, setImportOpen] = useState(false)
  const [importFormat, setImportFormat] = useState<'json' | 'text'>('json')
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    setLoading(true)
    try {
      const params = new URLSearchParams({
        platform: 'aws',
        page: String(page),
        page_size: String(pageSize),
      })
      if (email.trim()) params.set('email', email.trim())
      if (status) params.set('status', status)
      // Kiro 派生数据失败不该让整页变空：分开 settle，AWS 列表照常显示。
      const [awsResult, kiroResult] = await Promise.allSettled([
        apiFetch(`/accounts?${params.toString()}`) as Promise<AccountList>,
        apiFetch('/accounts?platform=kiro&page=1&page_size=200') as Promise<AccountList>,
      ])
      // 过期响应守卫：快速翻页/改筛选时，先发的慢响应不能覆盖后发的
      if (seq !== loadSeq.current) return
      if (awsResult.status === 'rejected') throw awsResult.reason
      setRows(awsResult.value.items || [])
      setTotal(awsResult.value.total || 0)
      setLoadFailed(false)
      if (kiroResult.status === 'fulfilled') {
        setDerivedMap(buildDerived(kiroResult.value.items || []))
      } else {
        setDerivedMap({})
        message.warning('Kiro 派生数据加载失败，授权链暂不可用（可点刷新重试）')
      }
    } catch (error) {
      if (seq === loadSeq.current) {
        setLoadFailed(true)
        message.error(`读取授权账号失败：${errMessage(error)}`)
      }
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [page, pageSize, email, status])

  useEffect(() => {
    void load()
  }, [load])

  const derivedTotal = useMemo(() => Object.keys(derivedMap).length, [derivedMap])

  const openDetail = async (row: AccountRow) => {
    detailId.current = row.id
    setDetail(row)
    setDetailOpen(true)
    setDetailLoading(true)
    try {
      const full = (await apiFetch(
        `/accounts/${row.id}?platform=aws`,
      )) as AccountRow
      // 连点两行时只认最后一次打开的那一行
      if (full && typeof full === 'object' && detailId.current === row.id) setDetail(full)
    } catch (error) {
      if (detailId.current === row.id) {
        message.error(`读取详情失败：${errMessage(error)}（先显示列表里的字段）`)
      }
    } finally {
      if (detailId.current === row.id) setDetailLoading(false)
    }
  }

  const runAction = async (actionId: 'probe' | 'refresh_token', row: AccountRow) => {
    try {
      const payload = {
        platform: 'aws',
        action_id: actionId,
        action_label: actionId === 'probe' ? '测活' : '刷新凭据',
        account_ids: [row.id],
        concurrency: 1,
      }
      const result = (await apiFetch('/tasks/batch-action', {
        method: 'POST',
        body: JSON.stringify(payload),
      })) as { task_id: string }
      setActionTask({ id: result.task_id, title: `${actionId === 'probe' ? '测活' : '刷新凭据'} · ${row.email}`, kind: 'batch_action' })
    } catch (error) {
      message.error(`任务启动失败：${errMessage(error)}`)
    }
  }

  const runKiroReuse = async (row: AccountRow) => {
    try {
      const cfg = (await apiFetch('/config')) as Record<string, unknown>
      const payload = {
        platform: 'kiro',
        count: 1,
        concurrency: 1,
        executor_type: 'protocol',
        captcha_solver: String(cfg.default_captcha_solver || 'yescaptcha'),
        extra: {
          mail_provider: String(cfg.mail_provider || ''),
          mail_import_source: String(cfg.mail_import_source || ''),
          kiro_source: 'manual',
          kiro_aws_email: row.email,
          kiro_auto_register: '0',
        },
      }
      const result = (await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify(payload),
      })) as { task_id: string }
      setActionTask({ id: result.task_id, title: `补 Kiro · ${row.email}`, kind: 'register' })
    } catch (error) {
      message.error(`补 Kiro 任务启动失败：${errMessage(error)}`)
    }
  }

  const remove = async (row: AccountRow) => {
    try {
      await apiFetch(`/accounts/${row.id}?platform=aws`, { method: 'DELETE' })
      message.success('已删除')
      if (detail?.id === row.id) setDetailOpen(false)
      void load()
    } catch (error) {
      message.error(`删除失败：${errMessage(error)}`)
    }
  }

  const submitRegister = async () => {
    const values = await registerForm.validateFields()
    setRegistering(true)
    try {
      const cfg = (await apiFetch('/config')) as Record<string, unknown>
      const result = (await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'aws',
          count: Number(values.count || 1),
          concurrency: Number(values.concurrency || 1),
          register_retry_times: Number(values.retry || 0),
          register_delay_seconds: 0,
          executor_type: 'protocol',
          captcha_solver: String(cfg.default_captcha_solver || 'yescaptcha'),
          proxy: null,
          extra: {
            mail_provider: String(cfg.mail_provider || ''),
            mail_import_source: String(cfg.mail_import_source || ''),
            aws_full_name: String(cfg.aws_full_name || ''),
          },
        }),
      })) as { task_id: string }
      setRegisterTaskId(result.task_id)
    } catch (error) {
      message.error(`注册任务启动失败：${errMessage(error)}`)
    } finally {
      setRegistering(false)
    }
  }

  const closeRegister = () => {
    setRegisterOpen(false)
    setRegisterTaskId(null)
    registerForm.resetFields()
  }

  const submitImport = async () => {
    const text = importText.trim()
    if (!text) {
      message.warning('先粘贴要导入的账号')
      return
    }
    setImporting(true)
    try {
      const result = (await apiFetch('/accounts/import', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'aws',
          lines: text.split('\n'),
          format: importFormat,
          source_format: 'email_pw',
        }),
      })) as { created: number; updated: number; skipped: number }
      message.success(`导入完成：新增 ${result.created}，更新 ${result.updated}，跳过 ${result.skipped}`)
      setImportOpen(false)
      setImportText('')
      void load()
    } catch (error) {
      message.error(`导入失败：${errMessage(error)}`)
    } finally {
      setImporting(false)
    }
  }

  const [exporting, setExporting] = useState(false)

  const doExport = async () => {
    setExporting(true)
    try {
      const result = (await apiFetch('/accounts/export-text', {
        method: 'POST',
        body: JSON.stringify({ format: 'json', platform: 'aws', status, email }),
      })) as { content: string; filename: string }
      const blob = new Blob([result.content], { type: 'application/json;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = result.filename || 'aws_accounts.json'
      link.click()
      // Safari/旧 Firefox 在 click 后立刻 revoke 会取消下载
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      message.success(`已导出 ${total} 个账号（含明文凭据，请妥善保管）`)
    } catch (error) {
      message.error(`导出失败：${errMessage(error)}`)
    } finally {
      setExporting(false)
    }
  }

  const exportJson = () => {
    Modal.confirm({
      title: '导出授权账号？',
      content: '文件包含明文密码与 refresh_token，下载后请妥善保管，不要放进仓库或聊天记录。',
      okText: '导出',
      cancelText: '取消',
      onOk: () => doExport(),
    })
  }

  const actionMenu = (row: AccountRow): MenuProps['items'] => {
    const hasKiro = (derivedMap[row.email.trim().toLowerCase()] || []).length > 0
    return [
      { key: 'detail', label: '详情' },
      { key: 'probe', label: '测活' },
      { key: 'refresh', label: '刷新凭据' },
      ...(hasKiro ? [] : [{ key: 'kiro', label: '补 Kiro（复用登录）' }]),
      { type: 'divider' },
      { key: 'delete', label: '删除', danger: true, icon: <DeleteOutlined /> },
    ]
  }

  const onActionClick = (row: AccountRow, key: string) => {
    if (key === 'detail') void openDetail(row)
    else if (key === 'probe') void runAction('probe', row)
    else if (key === 'refresh') void runAction('refresh_token', row)
    else if (key === 'kiro') void runKiroReuse(row)
    else if (key === 'delete') {
      Modal.confirm({
        title: `删除 ${row.email}？`,
        content: '只删除本地记录；AWS 那边的 Builder ID 不会消失。',
        okText: '删除',
        okButtonProps: { danger: true },
        cancelText: '取消',
        onOk: () => remove(row),
      })
    }
  }

  const columns: ColumnsType<AccountRow> = [
    {
      title: '邮箱',
      dataIndex: 'email',
      width: 340,
      render: (_value, row) => {
        const extra = parseExtra(row.extra_json)
        const clientId = String(extra.client_id || '')
        return (
          <div className="auth-account">
            <span className="auth-account__email">{row.email}</span>
            <span className="auth-account__meta">
              {clientId ? `client ${clientId}` : '未记录 OIDC 客户端'}
            </span>
          </div>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 96,
      render: (value: string, row) => {
        const meta = accountStatusMeta(value)
        const needsSso = Boolean(parseExtra(row.extra_json).needs_sso)
        return (
          <Space size={4} wrap>
            <Tag color={meta.color}>{meta.label}</Tag>
            {needsSso ? <Tag color="warning">待补凭据</Tag> : null}
          </Space>
        )
      },
    },
    {
      title: '已注册平台',
      key: 'registered_platforms',
      width: 140,
      render: (_value, row) => {
        const derived = derivedMap[row.email.trim().toLowerCase()] || []
        if (!derived.length) return <Text type="secondary">—</Text>
        return (
          <Tag color="geekblue">
            Kiro{derived.length > 1 ? ` ×${derived.length}` : ''}
          </Tag>
        )
      },
    },
    {
      title: '授权链',
      key: 'chain',
      width: 150,
      render: (_value, row) => (
        <ChainCell count={(derivedMap[row.email.trim().toLowerCase()] || []).length} />
      ),
    },
    {
      title: '地区',
      key: 'region',
      render: (_value, row) => {
        const extra = parseExtra(row.extra_json)
        return (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {row.region || String(extra.region || '') || '—'}
          </Text>
        )
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      render: (value: string) => {
        const created = formatCreatedAt(value)
        return (
          <div className="auth-time">
            {created.date}
            {created.time ? <span>{created.time}</span> : null}
          </div>
        )
      },
    },
    {
      title: '',
      key: 'actions',
      width: 56,
      render: (_value, row) => (
        <Dropdown
          trigger={['click']}
          menu={{ items: actionMenu(row), onClick: ({ key }) => onActionClick(row, key) }}
        >
          <Button type="text" icon={<MoreOutlined />} aria-label={`操作 ${row.email}`} />
        </Dropdown>
      ),
    },
  ]

  const detailExtra = parseExtra(detail?.extra_json)
  const detailDerived = detail ? derivedMap[detail.email.trim().toLowerCase()] || [] : []

  return (
    <div className="auth-page">
      <PageHeader
        title="AWS"
        subtitle={
          <span className="auth-summary">
            AWS Builder ID 是 Kiro 的授权根。<strong>{total}</strong> 个账号；Kiro 侧{' '}
            <strong>{derivedTotal}</strong> 个来源邮箱已换取凭据（按前 200 条 Kiro 记录统计）。
          </span>
        }
        actions={
          <Space wrap>
            <Button icon={<DownloadOutlined />} loading={exporting} onClick={exportJson}>
              导出
            </Button>
            <Button icon={<ImportOutlined />} onClick={() => setImportOpen(true)}>
              导入
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setRegisterOpen(true)}
            >
              注册 AWS 账号
            </Button>
          </Space>
        }
      />

      <div className="auth-toolbar">
        <Input.Search
          allowClear
          value={email}
          placeholder="按邮箱筛选"
          aria-label="按邮箱筛选授权账号"
          style={{ width: 240 }}
          onChange={(event) => {
            setPage(1)
            setEmail(event.target.value)
          }}
          onSearch={(value) => {
            setPage(1)
            setEmail(value)
          }}
        />
        <Select
          allowClear
          placeholder="全部状态"
          aria-label="按状态筛选授权账号"
          style={{ width: 132 }}
          value={status || undefined}
          onChange={(value) => {
            setPage(1)
            setStatus(value || '')
          }}
          options={[
            { value: 'registered', label: '正常' },
            { value: 'invalid', label: '失效' },
            { value: 'banned', label: '禁用' },
            { value: 'expired', label: '过期' },
          ]}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
      </div>

      <Table<AccountRow>
        rowKey="id"
        className="table-cards"
        columns={withDataLabels(columns)}
        dataSource={rows}
        loading={loading}
        expandable={
          // 授权链在行内展开：这页的组织原则是「根 → 派生」，不是一张扁平的
          // 账号表。窄屏走卡片模式，展开行会被 AntD 渲染成跨列块、撑破卡片，
          // 所以窄屏不提供行内展开 —— 抽屉里的授权树仍在。
          narrow
            ? undefined
            : {
                rowExpandable: (row) =>
                  (derivedMap[row.email.trim().toLowerCase()] || []).length > 0,
                expandedRowRender: (row) => (
                  <DerivedList items={derivedMap[row.email.trim().toLowerCase()] || []} />
                ),
                expandRowByClick: false,
              }
        }
        scroll={narrow ? undefined : { x: 1000 }}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                loadFailed
                  ? '加载失败，不是没有数据。点「重试」再拉一次。'
                  : email || status
                    ? '没有匹配的授权账号。换个筛选条件，或清除筛选看全部。'
                    : '还没有授权账号。注册一个 AWS Builder ID，或从 JSON 导入一批。'
              }
            >
              {loadFailed ? (
                <Button icon={<ReloadOutlined />} onClick={() => void load()}>
                  重试
                </Button>
              ) : email || status ? (
                <Button
                  onClick={() => {
                    setEmail('')
                    setStatus('')
                    setPage(1)
                  }}
                >
                  清除筛选
                </Button>
              ) : (
              <Space>
                <Button type="primary" icon={<PlusOutlined />} onClick={() => setRegisterOpen(true)}>
                  注册 AWS 账号
                </Button>
                <Button icon={<ImportOutlined />} onClick={() => setImportOpen(true)}>
                  导入
                </Button>
              </Space>
              )}
            </Empty>
          ),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: (value) => `共 ${value} 个`,
          onChange: (nextPage, nextSize) => {
            setPage(nextPage)
            setPageSize(nextSize)
          },
        }}
      />

      <p className="auth-footnote">
        Kiro 凭据在「平台管理 → Kiro」。删除本地记录不会注销 AWS 号；测活与刷新凭据走后台任务，日志在弹窗里。
      </p>

      <Drawer
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={narrow ? '100%' : 460}
        title={detail?.email || '授权账号'}
      >
        {detail ? (
          <Space direction="vertical" size={18} style={{ width: '100%' }}>
            <AuthTree status={detail.status} derived={detailDerived} />
            <DetailGroup title="凭据" hint="这些是 AWS Builder ID 的 OIDC 凭据，Kiro 靠它换授权。">
              <DetailRow label="Client ID">
                <Text style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
                  {String(detailExtra.client_id || '') || '—'}
                </Text>
              </DetailRow>
              <DetailRow label="Refresh Token">
                <SecretLine
                  value={String(detailExtra.refresh_token || '')}
                  empty={
                    detailLoading
                      ? '加载中…'
                      : detailExtra.has_refresh_token
                        ? '已配置（详情未加载，重开抽屉可查看）'
                        : '无'
                  }
                />
              </DetailRow>
              <DetailRow label="地区">
                <Text>{detail.region || String(detailExtra.region || '') || '—'}</Text>
              </DetailRow>
              <DetailRow label="创建时间">
                <Text>
                  {formatCreatedAt(detail.created_at).date} {formatCreatedAt(detail.created_at).time}
                </Text>
              </DetailRow>
            </DetailGroup>
            <Space wrap>
              <Button icon={<KeyOutlined />} onClick={() => void runAction('refresh_token', detail)}>
                刷新凭据
              </Button>
              <Popconfirm
                title="删除这条本地记录？"
                description="AWS 那边的 Builder ID 不会消失。"
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => void remove(detail)}
              >
                <Button danger icon={<DeleteOutlined />}>删除</Button>
              </Popconfirm>
            </Space>
          </Space>
        ) : null}
      </Drawer>

      <Modal
        title="注册 AWS 账号"
        open={registerOpen}
        onCancel={registerTaskId ? undefined : closeRegister}
        closable={!registerTaskId}
        keyboard={!registerTaskId}
        maskClosable={false}
        footer={null}
        width={registerTaskId ? 720 : 480}
      >
        {registerTaskId ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <TaskLogPanel
              taskId={registerTaskId}
              kind="register"
              onDone={() => {
                void load()
              }}
            />
            <Button onClick={closeRegister}>关闭</Button>
          </Space>
        ) : (
          <Form
            form={registerForm}
            layout="vertical"
            initialValues={{ count: 1, concurrency: 1, retry: 0 }}
            onFinish={() => void submitRegister()}
          >
            <Form.Item name="count" label="数量" rules={[{ required: true }]}>
              <InputNumber min={1} max={50} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发" rules={[{ required: true }]}>
              <InputNumber min={1} max={5} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="retry" label="失败重试轮数">
              <InputNumber min={0} max={5} style={{ width: '100%' }} />
            </Form.Item>
            <Alert
              type="warning"
              showIcon
              message="一个邮箱一次注册；验证码发到邮箱后约 3 分钟才到，请留足时间。"
              style={{ marginBottom: 12 }}
            />
            <Space>
              <Button type="primary" htmlType="submit" loading={registering}>
                开始注册
              </Button>
              <Button onClick={closeRegister}>取消</Button>
            </Space>
          </Form>
        )}
      </Modal>

      <Modal
        title="导入授权账号"
        open={importOpen}
        onOk={() => void submitImport()}
        confirmLoading={importing}
        onCancel={() => {
          setImportOpen(false)
          setImportText('')
          setImportFormat('json')
        }}
        okText="导入"
        cancelText="取消"
        width={560}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Select
            value={importFormat}
            onChange={(value) => setImportFormat(value)}
            options={[
              { value: 'json', label: 'JSON（含 client_id / refresh_token）' },
              { value: 'text', label: '文本（每行邮箱----密码）' },
            ]}
          />
          <Input.TextArea
            rows={8}
            value={importText}
            onChange={(event) => setImportText(event.target.value)}
            placeholder={
              importFormat === 'json'
                ? '[{"email":"a@x.ai","password":"...","client_id":"...","client_secret":"...","refresh_token":"..."}]'
                : 'a@x.ai----password'
            }
          />
        </Space>
      </Modal>

      <Modal
        title={actionTask?.title || '任务'}
        open={Boolean(actionTask)}
        onCancel={() => setActionTask(null)}
        footer={<Button onClick={() => setActionTask(null)}>关闭</Button>}
        width={720}
        maskClosable={false}
      >
        {actionTask ? (
          <TaskLogPanel
            taskId={actionTask.id}
            kind={actionTask.kind}
            onDone={() => {
              void load()
            }}
          />
        ) : null}
      </Modal>
    </div>
  )
}
