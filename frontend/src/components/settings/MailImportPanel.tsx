import { useEffect, useMemo, useState } from 'react'
import { App, Alert, Button, Card, Form, Input, InputNumber, Popconfirm, Select, Space, Switch, Table, Tag, Typography } from 'antd'
import type { FormInstance } from 'antd'

import { normalizeMailImportSource, useStoredMailImportSource, type MailImportSource } from '@/lib/mailImport'
import { PLATFORM_FILTER_OPTIONS, getPlatformLabel, getPlatformTagColor } from '@/lib/platforms'
import { poolStatusMeta } from '@/lib/poolStatus'
import { apiFetch } from '@/lib/utils'

type MailImportProviderType = 'microsoft'
type MailImportSelectionType = MailImportSource

interface MailImportPanelProps {
  form: FormInstance
  /**
   * 锁定到某个 provider：隐藏顶部的视图切换栏，只显示这一个。
   *
   * 邮箱服务现在是一级菜单，iCloud 本地 / Outlook 本地各自是独立页面 ——
   * 页面本身已经表达了范围，再给一个「切换到别的 provider」的开关会让
   * 用户以为这个页面能管所有邮箱池。
   */
  lockToProvider?: MailImportSelectionType
}

interface MailImportProviderDescriptor {
  type: MailImportProviderType
  label: string
  description: string
  content_placeholder: string
  helper_text: string
  supports_filename: boolean
  filename_label: string
  filename_placeholder: string
  preview_empty_text: string
}

interface MailImportDisplayProvider extends Omit<MailImportProviderDescriptor, 'type'> {
  type: MailImportSelectionType
  apiType: MailImportProviderType
}

interface MailImportSnapshotItem {
  index: number
  /** 号池里的行 id，用于「导入邮箱池」把勾选的行映射回账号。 */
  id?: number | null
  email: string
  mailbox: string
  enabled?: boolean | null
  status?: string
  has_oauth?: boolean | null
  account_type?: 'microsoft_oauth' | 'mailapi_url' | null
  /** 被哪些平台消耗过（`,grok,chatgpt,` 形式）。 */
  used_platforms?: string
  /** `accounts` 表里的权威注册证据（跨库查出来）。 */
  registered_platforms?: string[]
}

interface MailImportSnapshot {
  type: MailImportProviderType
  label: string
  count: number
  items: MailImportSnapshotItem[]
  truncated: boolean
  filename: string
  path: string
  pool_dir: string
}

interface MailImportSummary {
  total: number
  success: number
  failed: number
}

interface MailImportResult {
  type: MailImportProviderType
  summary: MailImportSummary
  snapshot: MailImportSnapshot
  errors: string[]
  meta: Record<string, unknown>
}

/** 后端唯一支持的类型（applemail 删除后只剩微软号池）。 */
const IMPORT_API_TYPE: MailImportProviderType = 'microsoft'

function isSupportedImportType(value: string): value is MailImportProviderType {
  return value === IMPORT_API_TYPE
}

/**
 * 选中哪一栏只看已保存的 `mail_import_source`。以前这里按 mail_provider 加
 * luckmail 域名反推，反推路径里没有 MailAPI URL 这个分支，于是选完再回来必然
 * 变回 Outlook。
 *
 * 配置还没加载回来时返回 null：这时候什么都反推不出来，别急着把用户拽到 Outlook。
 */
function resolvePreferredImportType(
  currentMailProvider: string,
  mailImportSource: string,
): MailImportSelectionType | null {
  if (!mailImportSource) return null
  return normalizeMailImportSource(mailImportSource, currentMailProvider)
}

function buildDisplayProviders(providers: MailImportProviderDescriptor[]) {
  const items: MailImportDisplayProvider[] = []

  for (const provider of providers) {
    items.push(
      {
        ...provider,
        type: 'outlook',
        apiType: 'microsoft',
        label: 'Outlook',
        description: '导入 Outlook 本地号池；选中后注册取号只取 OAuth 账号（走 Graph/IMAP 收码）。',
        helper_text: '自动识别 OAuth 与 MailAPI URL 两种行格式；本视图只显示 @outlook 账号。',
        content_placeholder: 'example@outlook.com----password----client_id----refresh_token',
        preview_empty_text: '当前还没有可预览的 Outlook 已导入账号。',
      },
      {
        ...provider,
        type: 'hotmail',
        apiType: 'microsoft',
        label: 'Hotmail',
        description: '导入 Hotmail 本地号池；选中后注册取号只取 OAuth 账号（走 Graph/IMAP 收码）。',
        helper_text: '自动识别 OAuth 与 MailAPI URL 两种行格式；本视图只显示 @hotmail 账号。',
        content_placeholder: 'example@hotmail.com----password----client_id----refresh_token',
        preview_empty_text: '当前还没有可预览的 Hotmail 已导入账号。',
      },
      {
        ...provider,
        type: 'mailapi',
        apiType: 'microsoft',
        label: 'MailAPI URL',
        description: '导入 MailAPI URL 账号池（邮箱----mailapi_url）；选中后注册取号只取 mailapi_url 类型的账号。',
        helper_text: '当前视图仅展示 account_type=mailapi_url 的账号。',
        content_placeholder: 'example@hotmail.com----https://mailapi.icu/key?type=html&orderNo=xxxxxxxx',
        preview_empty_text: '当前还没有可预览的 MailAPI URL 已导入账号。',
      },
    )
  }

  return items
}

function matchesSelectionType(
  selectionType: MailImportSelectionType,
  email: string,
  accountType?: string | null,
) {
  const domain = String(email.split('@')[1] || '').trim().toLowerCase()
  const normalizedType = String(accountType || 'microsoft_oauth').trim().toLowerCase()
  if (selectionType === 'mailapi') return normalizedType === 'mailapi_url'
  if (selectionType === 'hotmail') return normalizedType !== 'mailapi_url' && domain.includes('hotmail')
  if (selectionType === 'outlook') return normalizedType !== 'mailapi_url' && domain.includes('outlook')
  return true
}

/** `,chatgpt,grok,` → ['chatgpt','grok']（大小写归一）。 */
function parsePlatformField(field?: string | null): string[] {
  return String(field || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
}

/** 该地址已注册过的全部平台（池记账 ∪ `accounts` 表证据，去重排序）。 */
function registeredPlatformsOf(item: MailImportSnapshotItem): string[] {
  const names = new Set(parsePlatformField(item.used_platforms))
  for (const name of item.registered_platforms || []) {
    const value = String(name || '').trim().toLowerCase()
    if (value) names.add(value)
  }
  return Array.from(names).sort()
}

/** 该地址在指定平台是否已注册（记账 ∪ 证据）。 */
function registeredOn(item: MailImportSnapshotItem, platform: string): boolean {
  const name = String(platform || '').trim().toLowerCase()
  if (!name) return false
  return registeredPlatformsOf(item).includes(name)
}

function filterSnapshotBySelection(
  snapshot: MailImportSnapshot | null,
  selectionType: MailImportSelectionType,
  platform: string = '',
) {
  if (!snapshot || snapshot.type !== 'microsoft') {
    return snapshot
  }

  return {
    ...snapshot,
    items: snapshot.items
      .filter((item) => matchesSelectionType(selectionType, item.email, item.account_type))
      // 平台筛选：只留在这个平台注册过的地址。
      .filter((item) => !platform || registeredOn(item, platform)),
  }
}

function buildResultMessage(result: MailImportResult) {
  return `导入完成：成功 ${result.summary.success} / 失败 ${result.summary.failed}`
}

export default function MailImportPanel({ form, lockToProvider }: MailImportPanelProps) {
  const { message } = App.useApp()
  const currentMailProvider = String(Form.useWatch('mail_provider', form) || '')
  const storedMailImportSource = useStoredMailImportSource(form)

  const [providers, setProviders] = useState<MailImportDisplayProvider[]>([])
  const [selectedType, setSelectedType] = useState<MailImportSelectionType>(lockToProvider ?? 'outlook')
  const [content, setContent] = useState('')
  const [importing, setImporting] = useState(false)
  const [deletingEmail, setDeletingEmail] = useState('')
  const [batchDeleting, setBatchDeleting] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])
  const [loadingProviders, setLoadingProviders] = useState(false)
  const [loadingSnapshot, setLoadingSnapshot] = useState(false)
  const [rawSnapshot, setRawSnapshot] = useState<MailImportSnapshot | null>(null)
  const [result, setResult] = useState<MailImportResult | null>(null)
  // 平台筛选：选中后预览表只留「在这个平台注册过」的地址。空 = 显示全部。
  const [filterPlatform, setFilterPlatform] = useState<string>('')
  const [aliasSplitEnabled, setAliasSplitEnabled] = useState(false)
  const [aliasSplitCount, setAliasSplitCount] = useState(5)
  const [aliasIncludeOriginal, setAliasIncludeOriginal] = useState(false)
  const [poolImporting, setPoolImporting] = useState(false)

  const providerMap = useMemo(
    () => new Map(providers.map((provider) => [provider.type, provider])),
    [providers],
  )
  const selectedProvider = providerMap.get(selectedType) ?? null
  const selectedApiType = selectedProvider?.apiType ?? IMPORT_API_TYPE
  const supportsAliasSplit = selectedApiType === 'microsoft'
  const preferredImportType = useMemo(
    () => resolvePreferredImportType(currentMailProvider, storedMailImportSource),
    [storedMailImportSource, currentMailProvider],
  )
  const snapshot = useMemo(
    () => filterSnapshotBySelection(rawSnapshot, selectedType, filterPlatform),
    [rawSnapshot, selectedType, filterPlatform],
  )
  const tableData = useMemo(
    () => (snapshot?.items || []).map((item) => ({
      ...item,
      key: `${item.email}::${item.mailbox || ''}`,
    })),
    [snapshot],
  )

  /**
   * 视图选择立刻落库，不等整页「保存」。用户在这里选完 MailAPI URL 就去别的页面
   * 是常态，只留在表单里等于没选。
   */
  const persistImportSource = async (value: MailImportSelectionType) => {
    try {
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({ data: { mail_import_source: value } }),
      })
    } catch {
      // 表单里那份还在，点「保存配置」还能补上；但得说一声，否则刷新回来又变 Outlook
      // 会看起来像界面自己乱跳
      message.warning('这一栏没能保存，刷新后可能变回 Outlook，点一下「保存配置」再试')
    }
  }

  const loadProviders = async () => {
    setLoadingProviders(true)
    try {
      const data = await apiFetch('/mail-imports/providers') as { items?: MailImportProviderDescriptor[] }
      const items = Array.isArray(data.items) ? data.items.filter((item) => isSupportedImportType(item.type)) : []
      const displayProviders = buildDisplayProviders(items)
      setProviders(displayProviders)

      const available = new Set(displayProviders.map((item) => item.type))
      setSelectedType((current) => {
        // 页面锁定了 provider 就别再自动跳
        if (lockToProvider && available.has(lockToProvider)) return lockToProvider
        if (preferredImportType && available.has(preferredImportType)) return preferredImportType
        if (available.has(current)) return current
        return displayProviders[0]?.type ?? current
      })
    } catch (error) {
      const detail = error instanceof Error ? error.message : '加载邮箱导入配置失败'
      message.error(detail)
    } finally {
      setLoadingProviders(false)
    }
  }

  const loadSnapshot = async () => {
    setLoadingSnapshot(true)
    try {
      const params = new URLSearchParams({ type: IMPORT_API_TYPE })
      const nextSnapshot = await apiFetch(`/mail-imports/snapshot?${params.toString()}`) as MailImportSnapshot
      setRawSnapshot(nextSnapshot)
    } catch {
      setRawSnapshot(null)
    } finally {
      setLoadingSnapshot(false)
    }
  }

  useEffect(() => {
    void loadProviders()
  }, [])

  useEffect(() => {
    if (lockToProvider) return
    if (preferredImportType && providerMap.has(preferredImportType)) {
      setSelectedType(preferredImportType)
    }
  }, [preferredImportType, providerMap, lockToProvider])

  useEffect(() => {
    if (!selectedProvider) return
    void loadSnapshot()
  }, [selectedProvider, selectedType])

  useEffect(() => {
    setSelectedRowKeys([])
  }, [selectedType, rawSnapshot, filterPlatform])

  const handleImport = async () => {
    const payload = content.trim()
    if (!payload) {
      message.error('请输入导入内容')
      return
    }

    setImporting(true)
    try {
      const apiType = IMPORT_API_TYPE
      const body: Record<string, unknown> = {
        type: apiType,
        content: payload,
        enabled: true,
        bind_to_config: true,
        alias_split_enabled: aliasSplitEnabled,
        alias_split_count: aliasSplitCount,
        alias_include_original: aliasIncludeOriginal,
      }

      const response = await apiFetch('/mail-imports', {
        method: 'POST',
        body: JSON.stringify(body),
      }) as MailImportResult

      setResult(response)
      setRawSnapshot(response.snapshot)
      setContent('')

      form.setFieldsValue({
        mail_provider: 'mail_import',
        mail_import_source: selectedType,
      })
      void persistImportSource(selectedType)

      message.success(buildResultMessage(response))
    } catch (error) {
      const detail = error instanceof Error ? error.message : '邮箱导入失败'
      message.error(detail)
    } finally {
      setImporting(false)
    }
  }

  const handleTypeChange = (value: MailImportSelectionType) => {
    setSelectedType(value)
    form.setFieldsValue({
      mail_provider: 'mail_import',
      mail_import_source: value,
    })
    void persistImportSource(value)
  }

  /**
   * 把预览表里勾选的邮箱从「未入池」转成可领取。
   *
   * 导入（粘贴）与入池是两步：粘贴只是把地址存进池子，注册任务取号时只认
   * `available`。用户明确要求「账号邮箱要选择导入才能导入邮箱池，不是全部导入」，
   * 所以入池必须由这里显式触发。
   */
  const handleImportToPool = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选要导入邮箱池的邮箱')
      return
    }

    const ids = tableData
      .filter((item) => selectedRowKeys.includes(item.key))
      .map((item) => Number(item.id))
      .filter((id) => Number.isFinite(id) && id > 0)

    if (!ids.length) {
      message.warning('勾选的记录里没有可入池的账号')
      return
    }

    setPoolImporting(true)
    try {
      const result = await apiFetch('/outlook/pool-status/import', {
        method: 'POST',
        body: JSON.stringify({ ids }),
      }) as { changed?: number; skipped?: number; remaining_unpooled?: number }

      const changed = Number(result?.changed || 0)
      const skipped = Number(result?.skipped || 0)
      if (changed) {
        message.success(`已导入邮箱池 ${changed} 个${skipped ? `（${skipped} 个已在池中，跳过）` : ''}`)
      } else {
        message.info(
          skipped
            ? `这 ${skipped} 个邮箱已经在池里了，不需要重复导入`
            : '没有可入池的邮箱',
        )
      }
      setSelectedRowKeys([])
      await loadSnapshot()
    } catch (error) {
      const detail = error instanceof Error ? error.message : '导入邮箱池失败'
      message.error(detail)
    } finally {
      setPoolImporting(false)
    }
  }

  const handleDelete = async (item: MailImportSnapshotItem) => {
    const apiType = IMPORT_API_TYPE
    const email = String(item.email || '').trim()
    if (!email) return

    setDeletingEmail(email)
    try {
      const body: Record<string, unknown> = {
        type: apiType,
        email,
      }

      const response = await apiFetch('/mail-imports/delete', {
        method: 'POST',
        body: JSON.stringify(body),
      }) as MailImportResult

      setResult(response)
      setRawSnapshot(response.snapshot)
      setSelectedRowKeys([])
      message.success(`已删除 ${email}`)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '删除失败'
      message.error(detail)
    } finally {
      setDeletingEmail('')
    }
  }

  const handleBatchDelete = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选要删除的邮箱')
      return
    }

    const selectedItems = tableData.filter((item) => selectedRowKeys.includes(item.key))
    if (!selectedItems.length) {
      message.warning('未找到要删除的邮箱')
      return
    }

    const apiType = IMPORT_API_TYPE
    setBatchDeleting(true)
    try {
      const body: Record<string, unknown> = {
        type: apiType,
        items: selectedItems.map((item) => ({
          email: item.email,
          mailbox: item.mailbox || '',
        })),
      }

      const response = await apiFetch('/mail-imports/batch-delete', {
        method: 'POST',
        body: JSON.stringify(body),
      }) as MailImportResult

      setResult(response)
      setRawSnapshot(response.snapshot)
      setSelectedRowKeys([])
      message.success(`批量删除完成：成功 ${response.summary.success} / 失败 ${response.summary.failed}`)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '批量删除失败'
      const shouldFallbackToSingleDelete = /405|404|Method Not Allowed|Not Found/i.test(detail)

      if (!shouldFallbackToSingleDelete) {
        message.error(detail)
        return
      }

      let success = 0
      let failed = 0
      const errors: string[] = []

      for (const item of selectedItems) {
        try {
          const body: Record<string, unknown> = {
            type: apiType,
            email: item.email,
          }

          const response = await apiFetch('/mail-imports/delete', {
            method: 'POST',
            body: JSON.stringify(body),
          }) as MailImportResult

          setResult(response)
          setRawSnapshot(response.snapshot)
          success += 1
        } catch (singleError) {
          failed += 1
          errors.push(singleError instanceof Error ? singleError.message : `删除失败: ${item.email}`)
        }
      }

      setSelectedRowKeys([])
      if (errors.length) {
        message.warning(`批量删除已回退单条删除：成功 ${success} / 失败 ${failed}`)
        setResult((prev) => prev ? {
          ...prev,
          errors,
          summary: { total: success + failed, success, failed },
        } : prev)
      } else {
        message.success(`批量删除已回退单条删除：成功 ${success} / 失败 ${failed}`)
      }
    } finally {
      setBatchDeleting(false)
    }
  }

  const columns = useMemo(() => {
    const baseColumns = [
      {
        title: '#',
        dataIndex: 'index',
        key: 'index',
        width: 72,
      },
      {
        title: '邮箱',
        dataIndex: 'email',
        key: 'email',
      },
    ]

    baseColumns.push(
      {
        title: '类型',
        dataIndex: 'account_type',
        key: 'account_type',
        width: 120,
        render: (value: string | null | undefined) => {
          const isMailApi = String(value || '').trim().toLowerCase() === 'mailapi_url'
          return <Tag color={isMailApi ? 'purple' : 'blue'}>{isMailApi ? 'MailAPI URL' : 'OAuth'}</Tag>
        },
      } as never,
      {
        title: '状态',
        dataIndex: 'status',
        key: 'status',
        width: 110,
        render: (value: string | null | undefined) => {
          const item = poolStatusMeta(value)
          return <Tag color={item.color}>{item.text}</Tag>
        },
      } as never,
      {
        // 这个地址注册过哪些平台（池记账 ∪ accounts 表证据）。
        title: '已注册平台',
        dataIndex: 'used_platforms',
        key: 'registered_platforms',
        width: 170,
        render: (_: unknown, item: MailImportSnapshotItem) => {
          const names = registeredPlatformsOf(item)
          if (names.length === 0) return <Typography.Text type="secondary">-</Typography.Text>
          return (
            <Space size={4} wrap>
              {names.map((name) => (
                <Tag key={name} color={getPlatformTagColor(name)}>
                  {getPlatformLabel(name)}
                </Tag>
              ))}
            </Space>
          )
        },
      } as never,
      {
        title: '启用',
        dataIndex: 'enabled',
        key: 'enabled',
        width: 80,
        render: (value: boolean | null | undefined) => (
          <Tag color={value ? 'default' : 'default'}>{value ? '是' : '否'}</Tag>
        ),
      } as never,
      {
        title: '认证',
        dataIndex: 'has_oauth',
        key: 'has_oauth',
        width: 100,
        render: (value: boolean | null | undefined) => (
          <Tag color={value ? 'blue' : 'default'}>{value ? 'OAuth' : '密码'}</Tag>
        ),
      } as never,
    )

    baseColumns.push({
      title: '操作',
      key: 'action',
      width: 90,
      render: (_: unknown, item: MailImportSnapshotItem) => (
        <Popconfirm
          title="确认删除这个邮箱吗？"
          description={item.email}
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true, loading: deletingEmail === item.email }}
          onConfirm={() => void handleDelete(item)}
        >
          <Button
            danger
            type="link"
            size="small"
            loading={deletingEmail === item.email}
            style={{ paddingInline: 0 }}
          >
            删除
          </Button>
        </Popconfirm>
      ),
    } as never)

    return baseColumns
  }, [deletingEmail, selectedType, tableData])

  return (
    <Card
      title="邮箱导入"
      extra={
        lockToProvider ? (
          // 锁定模式：页面本身就是这个 provider 的范围，不给切换入口
          <Tag>{providers.find((p) => p.type === lockToProvider)?.label || ''}</Tag>
        ) : (
          <Select
            value={selectedType}
            onChange={handleTypeChange}
            loading={loadingProviders}
            style={{ width: 240 }}
            options={providers.map((provider) => ({
              label: provider.label,
              value: provider.type,
            }))}
          />
        )
      }
      style={{ marginBottom: 16 }}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <Typography.Text type="secondary">
          {selectedProvider?.description || '通过统一导入接口，将内容导入到对应邮箱账号池。'}
        </Typography.Text>
        {/* 两步流程必须写清楚：用户粘贴完一屏邮箱就以为可以开任务了，
            结果注册报「池里没有可用账号」——那是导入成功但没入池。 */}
        <Alert
          type="info"
          showIcon
          message="导入后还要勾选入池"
          description="粘贴只是存进号池（状态「未入池」，注册不会取用）；勾选后点「导入邮箱池」才可被领取。"
        />
        {selectedProvider?.helper_text ? (
          <Typography.Text type="secondary">{selectedProvider.helper_text}</Typography.Text>
        ) : null}

        {supportsAliasSplit ? (
          <div
            style={{
              border: '1px dashed var(--border-strong)',
              borderRadius: 8,
              padding: 12,
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
            }}
          >
            <Space align="center">
              <Typography.Text strong>邮箱裂变（别名）</Typography.Text>
              <Switch checked={aliasSplitEnabled} onChange={setAliasSplitEnabled} />
              <Typography.Text type="secondary">
                默认关闭；开启后每个原邮箱生成随机 6 位英文别名
              </Typography.Text>
            </Space>
            {aliasSplitEnabled ? (
              <Space align="center" wrap>
                <Typography.Text>每个原邮箱裂变数量</Typography.Text>
                <InputNumber
                  min={1}
                  max={5}
                  value={aliasSplitCount}
                  onChange={(value) => setAliasSplitCount(Math.max(1, Math.min(5, Number(value || 5))))}
                />
                <Typography.Text type="secondary">（1~5）</Typography.Text>
                <Typography.Text style={{ marginLeft: 16 }}>包含原邮箱</Typography.Text>
                <Switch checked={aliasIncludeOriginal} onChange={setAliasIncludeOriginal} />
              </Space>
            ) : null}
          </div>
        ) : null}

        <Input.TextArea
          value={content}
          onChange={(event) => setContent(event.target.value)}
          rows={10}
          placeholder={selectedProvider?.content_placeholder || ''}
          style={{ fontFamily: 'monospace' }}
        />

        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Button
            danger
            onClick={() => {
              setContent('')
              setResult(null)
            }}
          >
            清空
          </Button>
          <Space>
            <Button onClick={() => void loadSnapshot()} loading={loadingSnapshot}>
              刷新预览
            </Button>
            <Button type="primary" onClick={handleImport} loading={importing}>
              确认导入
            </Button>
          </Space>
        </Space>

        {result ? (
          <Alert
            type={result.summary.failed ? 'warning' : 'success'}
            showIcon
            message={buildResultMessage(result)}
            description={result.errors.length ? (
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{result.errors.join('\n')}</pre>
            ) : undefined}
          />
        ) : null}

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Tag color="blue">
            {`当前预览匹配: ${snapshot?.items.length || 0}${rawSnapshot?.truncated ? ` / 总池 ${rawSnapshot?.count || 0}` : ''}`}
          </Tag>
          {/* 平台筛选：选中后只留「在这个平台注册过」的地址。 */}
          <Select
            allowClear
            placeholder="全部平台"
            style={{ width: 140 }}
            value={filterPlatform || undefined}
            onChange={(value) => setFilterPlatform(value || '')}
            options={PLATFORM_FILTER_OPTIONS.filter((item) => item.value)}
          />
          {filterPlatform ? (
            <Typography.Text type="secondary">
              只显示在 {getPlatformLabel(filterPlatform)} 注册过的地址
            </Typography.Text>
          ) : null}
          {snapshot?.items?.length ? (
            <>
              {/* 入池与删除是并列的两个动作：入池让勾选的号可被注册取用，
                  删除是把导入记录整个抹掉。放在一起是因为它们都作用在
                  「当前勾选」上，分开摆反而要来回找。 */}
              <Button
                type="primary"
                onClick={() => void handleImportToPool()}
                loading={poolImporting}
                disabled={!selectedRowKeys.length}
              >
                {selectedRowKeys.length ? `导入邮箱池 ${selectedRowKeys.length} 个` : '导入邮箱池'}
              </Button>
              <Popconfirm
                title={`确认删除已勾选的 ${selectedRowKeys.length} 个邮箱吗？`}
                okText="批量删除"
                cancelText="取消"
                okButtonProps={{ danger: true, loading: batchDeleting }}
                onConfirm={() => void handleBatchDelete()}
                disabled={!selectedRowKeys.length}
              >
                <Button danger disabled={!selectedRowKeys.length} loading={batchDeleting}>
                  批量删除
                </Button>
              </Popconfirm>
            </>
          ) : null}
        </div>
        {snapshot?.items?.length ? (
          <Table
            rowSelection={{
              selectedRowKeys,
              onChange: setSelectedRowKeys,
            }}
            columns={columns}
            dataSource={tableData}
            size="small"
            pagination={false}
            scroll={{ y: 320 }}
          />
        ) : (
          <div
            style={{
              border: '1px solid var(--border)',
              borderRadius: 8,
              padding: 12,
              background: 'var(--bg-subtle)',
              minHeight: 88,
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <Typography.Text type="secondary">
              {selectedProvider?.preview_empty_text || '当前还没有可预览的导入内容。'}
            </Typography.Text>
          </div>
        )}

        {snapshot?.truncated ? (
          <Typography.Text type="secondary">预览只展示前 100 条记录，完整内容以实际存储为准。</Typography.Text>
        ) : null}
      </Space>
    </Card>
  )
}
