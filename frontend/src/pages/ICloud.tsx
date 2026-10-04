import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Dropdown,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  DeleteOutlined,
  DownOutlined,
  DownloadOutlined,
  ImportOutlined,
  InboxOutlined,
  LinkOutlined,
  LoginOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import {
  batchDeleteICloudAliases,
  deleteICloudAccount,
  deleteICloudAlias,
  EMPTY_ALIAS_POOL,
  importICloudAliasesToPool,
  listICloudAccounts,
  listICloudAliases,
  setICloudAccountEnabled,
  setICloudAliasActive,
  setICloudAliasPoolStatus,
  syncICloudAccount,
  unpoolICloudAliases,
  type ICloudAccount,
  type ICloudAlias,
  type ICloudAliasPool,
} from '@/api/icloud'
import { ICloudLoginModal } from '@/components/icloud/ICloudLoginModal'
import { AliasInboxDrawer } from '@/components/icloud/AliasInboxDrawer'
import { CookieImportModal } from '@/components/icloud/CookieImportModal'
import { GenerateAliasModal } from '@/components/icloud/GenerateAliasModal'
import { PageHeader } from '@/components/PageHeader'
import {
  ICLOUD_HOURLY_ALIAS_LIMIT,
  ALIAS_EXPORT_FILENAME,
  PLATFORM_STATUS_META,
  aliasMailUrl,
  aliasPlatformStatus,
  aliasPlatformSummary,
  aliasRegisteredPlatforms,
  countExportableAliases,
  downloadTextFile,
  filterAliases,
  formatAliasExport,
  formatDateTime,
  getICloudRegionLabel,
  hasAliasFilter,
  type AliasExportMode,
  type AliasPlatformStatus,
} from '@/lib/icloud'
import { PLATFORM_FILTER_OPTIONS, getPlatformLabel, getPlatformTagColor } from '@/lib/platforms'
import { POOL_SUMMARY_ORDER, poolStatusMeta } from '@/lib/poolStatus'

const { Text } = Typography

/** 平台口径统计条的固定顺序（与 `PLATFORM_STATUS_META` 对应）。 */
const PLATFORM_SUMMARY_ORDER: AliasPlatformStatus[] = ['unpooled', 'available', 'in_use', 'registered']

export default function ICloudPage() {
  const { message } = App.useApp()
  const [accounts, setAccounts] = useState<ICloudAccount[]>([])
  const [aliases, setAliases] = useState<ICloudAlias[]>([])
  const [loading, setLoading] = useState(false)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [cookieOpen, setCookieOpen] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [filterAccountId, setFilterAccountId] = useState<number | undefined>()
  // 隐私邮箱列表的筛选条件。客户端过滤而非服务端：列表本来就是一次性拉全量
  // （号池计数也依赖全量），几百条在浏览器里筛没有可感延迟，加服务端参数
  // 只会让「改筛选 = 重新请求」多一次往返。
  const [searchText, setSearchText] = useState('')
  const [filterPoolStatus, setFilterPoolStatus] = useState<string | undefined>()
  const [filterAliasStatus, setFilterAliasStatus] = useState<string | undefined>()
  // 平台筛选：选了之后号池状态与顶部统计换成「该平台注册过没有」的口径。
  const [filterPlatform, setFilterPlatform] = useState<string | undefined>()
  const [inboxAlias, setInboxAlias] = useState<ICloudAlias | null>(null)
  const [selectedAliasIds, setSelectedAliasIds] = useState<React.Key[]>([])
  const [batchDeleting, setBatchDeleting] = useState(false)
  const [poolChanging, setPoolChanging] = useState(false)
  // 号池计数（未使用 / 使用中 / 已使用），列表接口一起返回
  const [pool, setPool] = useState<ICloudAliasPool>(EMPTY_ALIAS_POOL)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [nextAccounts, nextAliases] = await Promise.all([
        listICloudAccounts(),
        listICloudAliases(filterAccountId),
      ])
      setAccounts(nextAccounts)
      setAliases(nextAliases.items)
      setPool(nextAliases.pool)
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setLoading(false)
    }
  }, [filterAccountId, message])

  useEffect(() => {
    load()
  }, [load])

  // 用带类型前缀的复合键，不用裸 id：账号与别名是两套独立自增 id，裸数字会
  // 撞上 —— 账号 #1 在请求时，别名 #1 的按钮也会跟着转圈（实测同一 busyId
  // 被两处读）。前缀把它们分开。
  const withBusy = async (key: string, action: () => Promise<unknown>, successText: string) => {
    setBusyKey(key)
    try {
      await action()
      message.success(successText)
      await load()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusyKey(null)
    }
  }

  const accountOptions = useMemo(
    () => accounts.map((account) => ({ value: account.id, label: account.email })),
    [accounts],
  )

  // 筛选条件变化后，原来选中的行可能已经不在可见列表里了 —— 留着会让
  // 「删除 N 个」的 N 与用户眼前看到的对不上（而且删的是看不见的行）。
  // 主号、搜索词、号池状态、平台、启用状态任一变化都清空选择。
  useEffect(() => {
    setSelectedAliasIds([])
  }, [filterAccountId, searchText, filterPoolStatus, filterAliasStatus, filterPlatform])

  // 筛选后的别名列表 —— **表格与导出都读它**，不是原始 aliases。
  // 「导出没勾选时导出当前筛选结果」的既有口径要求这里就是筛选后的集合，
  // 否则用户筛出 3 条再点导出会拿到全部。
  // 规则本体在 `@/lib/icloud` 的 `filterAliases`（纯函数，可单测）。
  const visibleAliases = useMemo(
    () =>
      filterAliases(aliases, {
        keyword: searchText,
        poolStatus: filterPoolStatus,
        status: filterAliasStatus,
        platform: filterPlatform,
      }),
    [aliases, searchText, filterPoolStatus, filterAliasStatus, filterPlatform],
  )

  const aliasFilterActive = hasAliasFilter({
    keyword: searchText,
    poolStatus: filterPoolStatus,
    status: filterAliasStatus,
    platform: filterPlatform,
  })

  // 平台口径统计（顶部统计条在选了平台时改读它）。
  const platformSummary = useMemo(
    () => (filterPlatform ? aliasPlatformSummary(aliases, filterPlatform) : null),
    [aliases, filterPlatform],
  )

  const clearAliasFilters = () => {
    setSearchText('')
    setFilterPoolStatus(undefined)
    setFilterAliasStatus(undefined)
    setFilterPlatform(undefined)
  }

  // 没勾选就按当前筛选出来的全部算，跟账号页导出的口径一致
  const targetAliases = useMemo(() => {
    if (selectedAliasIds.length === 0) return visibleAliases
    const picked = new Set(selectedAliasIds.map(Number))
    return visibleAliases.filter((alias) => picked.has(alias.id))
  }, [visibleAliases, selectedAliasIds])

  const handleBatchDelete = async () => {
    const ids = selectedAliasIds.map(Number).filter((id) => Number.isInteger(id) && id > 0)
    if (ids.length === 0) return
    setBatchDeleting(true)
    try {
      const result = await batchDeleteICloudAliases(ids)
      if (result.failed.length === 0) {
        message.success(`已删除 ${result.deleted.length} 个隐私邮箱`)
      } else {
        message.warning(
          `删除 ${result.deleted.length} 个，失败 ${result.failed.length} 个：${result.failed[0].message}`,
        )
      }
      setSelectedAliasIds(result.failed.map((item) => item.alias_id))
      await load()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBatchDeleting(false)
    }
  }

  /**
   * 批量入池 / 出池。
   *
   * 后端只动「处于预期状态」的那些（入池只认未入池、出池只认未使用），
   * 所以这里必须把 skipped 说出来：用户勾了一批混合状态的号，闷头报「成功
   * 12 个」会让他以为全进了池，下次注册取不到号时完全摸不着头脑。
   */
  const handlePoolChange = async (direction: 'import' | 'unpool') => {
    const ids = selectedAliasIds.map(Number).filter((id) => Number.isInteger(id) && id > 0)
    if (ids.length === 0) return
    setPoolChanging(true)
    try {
      const result =
        direction === 'import'
          ? await importICloudAliasesToPool(ids)
          : await unpoolICloudAliases(ids)
      const verb = direction === 'import' ? '导入号池' : '移出号池'
      if (result.skipped.length === 0) {
        message.success(`已${verb} ${result.changed.length} 个`)
      } else {
        message.warning(
          `已${verb} ${result.changed.length} 个，跳过 ${result.skipped.length} 个` +
            (direction === 'import'
              ? '（只有「未入池」的能导入；使用中/已用过的请单独处理）'
              : '（只有「未使用」的能移出）'),
        )
      }
      setSelectedAliasIds([])
      await load()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setPoolChanging(false)
    }
  }

  const exportAliases = (mode: AliasExportMode) => {
    if (targetAliases.length === 0) return
    const exportable = countExportableAliases(targetAliases, mode)
    if (exportable === 0) {
      message.warning('选中的隐私邮箱都还没有邮件 URL，导不出内容')
      return
    }
    downloadTextFile(ALIAS_EXPORT_FILENAME, formatAliasExport(targetAliases, mode))
    const skipped = targetAliases.length - exportable
    if (skipped > 0) {
      message.warning(`已导出 ${exportable} 条，${skipped} 条没有邮件 URL 已跳过`)
      return
    }
    message.success(`已导出 ${exportable} 条`)
  }

  const accountColumns = [
    {
      title: '主号',
      dataIndex: 'email',
      // 实测（390px）：这列原先没有 width，被 antd 等比压缩挤到 45px，
      // 邮箱地址竖排、单元格高 409px。定宽 260 让长地址也保持在两行内。
      width: 260,
      render: (email: string, account: ICloudAccount) => (
        <Space direction="vertical" size={0}>
          <Text strong>{email}</Text>
          {account.display_name && <Text type="secondary">{account.display_name}</Text>}
        </Space>
      ),
    },
    {
      title: '区域',
      dataIndex: 'region',
      width: 180,
      render: (region: string) => getICloudRegionLabel(region),
    },
    {
      title: '会话状态',
      width: 200,
      render: (_: unknown, account: ICloudAccount) => (
        <Space size={4} wrap>
          <Tag color={account.credential_state.has_session_cookies ? 'green' : 'red'}>
            Web Session
          </Tag>
          <Tag color={account.credential_state.has_imap_credentials ? 'green' : 'orange'}>IMAP</Tag>
        </Space>
      ),
    },
    {
      title: '隐私邮箱',
      width: 150,
      render: (_: unknown, account: ICloudAccount) => (
        <Tooltip title={`本小时剩余额度 ${account.quota.remaining}/${account.quota.limit}`}>
          <Badge
            count={account.alias_count}
            showZero
            // 用主题强调色而不是硬编码的蓝：亮色主题下 #5b8db3 太灰，
            // 且它是唯一一处绕过 token 的徽章色。
            //
            // 但 --accent(#2997ff) 配白字只有 3.02:1（AA 需 4.5:1）——
            // 徽章数字正是要看清的内容，所以用同色系更深的一档。
            // #005bb5 实测 6.64:1，且与品牌蓝同色系。
            color="#005bb5"
            style={{ marginRight: 8 }}
          />
          <Text type="secondary">
            {account.quota.remaining}/{account.quota.limit}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '最近同步',
      dataIndex: 'last_sync_at',
      width: 180,
      render: (value: string | null, account: ICloudAccount) =>
        account.sync_error ? (
          <Tooltip title={account.sync_error}>
            <Tag color="red">同步失败</Tag>
          </Tooltip>
        ) : (
          formatDateTime(value)
        ),
    },
    {
      title: '启用',
      width: 90,
      render: (_: unknown, account: ICloudAccount) => (
        <Switch
          checked={account.enabled}
          loading={busyKey === `account:${account.id}`}
          onChange={(enabled) =>
            withBusy(
              `account:${account.id}`,
              () => setICloudAccountEnabled(account.id, enabled),
              enabled ? '已启用' : '已停用',
            )
          }
        />
      ),
    },
    {
      title: '操作',
      width: 180,
      // 固定到右侧：列宽合计超出容器时（实测 1240px vs 984px），操作列
      // 默认落在视口外，用户得横向滚动才能点到「同步 / 删除」——
      // 与账号页（Accounts.tsx）同款处理。
      fixed: 'right' as const,
      render: (_: unknown, account: ICloudAccount) => (
        <Space>
          <Button
            size="small"
            icon={<SyncOutlined />}
            loading={busyKey === `account:${account.id}`}
            onClick={() =>
              withBusy(`account:${account.id}`, () => syncICloudAccount(account.id), '已从 iCloud 同步隐私邮箱')
            }
          >
            同步
          </Button>
          <Popconfirm
            title="删除主号"
            description="将同时移除本地记录的隐私邮箱，不会删除 iCloud 上游地址。"
            onConfirm={() => withBusy(`account:${account.id}`, () => deleteICloudAccount(account.id), '主号已删除')}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const aliasColumns = [
    {
      title: '隐私邮箱',
      dataIndex: 'address',
      width: 260,
      render: (address: string) => <Text copyable>{address}</Text>,
    },
    {
      title: '邮件 URL',
      width: 280,
      render: (_: unknown, alias: ICloudAlias) => {
        const url = aliasMailUrl(alias.share_token)
        if (!url) return <Text type="secondary">-</Text>
        return (
          <Space size={4} style={{ maxWidth: '100%' }}>
            <Tooltip title="免登录打开，只显示最新一封邮件">
              <Typography.Link
                href={url}
                target="_blank"
                rel="noreferrer"
                ellipsis
                style={{ fontSize: 12, maxWidth: 210 }}
              >
                {url}
              </Typography.Link>
            </Tooltip>
            <Text
              type="secondary"
              copyable={{
                text: url,
                icon: [<LinkOutlined key="copy" />, <LinkOutlined key="copied" />],
                tooltips: ['复制链接', '链接已复制'],
              }}
            />
          </Space>
        )
      },
    },
    { title: '标签', dataIndex: 'label', width: 160, render: (value: string) => value || '-' },
    { title: '所属主号', dataIndex: 'account_email', width: 220 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (status: string) => (
        <Tag color={status === 'active' ? 'green' : 'default'}>
          {status === 'active' ? '启用' : '已停用'}
        </Tag>
      ),
    },
    {
      // 号池记账。选了平台筛选时改显示该平台口径（在这个平台注册过没有）。
      title: filterPlatform ? `号池（${getPlatformLabel(filterPlatform)}）` : '号池',
      dataIndex: 'pool_status',
      width: filterPlatform ? 170 : 130,
      render: (value: ICloudAlias['pool_status'], alias: ICloudAlias) => {
        if (filterPlatform) {
          const platformStatus = aliasPlatformStatus(alias, filterPlatform)
          const item = PLATFORM_STATUS_META[platformStatus]
          return <Tag color={item.color}>{item.text}</Tag>
        }
        const item = poolStatusMeta(value)
        return <Tag color={item.color}>{item.text}</Tag>
      },
    },
    {
      // 这个地址注册过哪些平台（记账 ∪ accounts 表证据）。
      title: '已注册平台',
      dataIndex: 'used_platforms',
      width: 200,
      render: (_: unknown, alias: ICloudAlias) => {
        const names = aliasRegisteredPlatforms(alias)
        if (names.length === 0) return <Text type="secondary">-</Text>
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
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: '操作',
      width: 260,
      // 同主号表：列宽合计 1830px 远超容器，不固定操作列就落在视口外。
      fixed: 'right' as const,
      render: (_: unknown, alias: ICloudAlias) => (
        <Space>
          <Button size="small" icon={<InboxOutlined />} onClick={() => setInboxAlias(alias)}>
            收件
          </Button>
          {/* 停用是可逆的（地址仍留在 Apple 那边，只是不再转发），
              删除不可逆 —— 两个动作语义差得远，别混成一个按钮。 */}
          {alias.status === 'active' ? (
            <Popconfirm
              title="停用隐私邮箱"
              description="停用后不再转发邮件，但地址会保留，随时可以重新激活。"
              onConfirm={() =>
                withBusy(`alias:${alias.id}`, () => setICloudAliasActive(alias.id, false), '隐私邮箱已停用')
              }
            >
              <Button size="small" icon={<StopOutlined />} />
            </Popconfirm>
          ) : (
            <Button
              size="small"
              icon={<PlayCircleOutlined />}
              onClick={() =>
                withBusy(`alias:${alias.id}`, () => setICloudAliasActive(alias.id, true), '隐私邮箱已激活')
              }
            />
          )}
          {/* 号池记账是自动的，但生成/同步进来、以及人工导入的地址需要手动
              入池，否则取号逻辑永远跳过它。`unpooled` 是「还没进池」，放回去
              才是它变成可领的唯一途径；`used` 表示这轮跑完了、地址仍可用，
              放回去也能再领到 —— 所以不禁用，但用 tooltip 说清语义差异。 */}
          {alias.pool_status !== 'available' ? (
            <Tooltip
              title={
                alias.pool_status === 'unpooled'
                  ? '导入号池，下次注册可以领到它'
                  : alias.pool_status === 'used'
                    ? '已用过的地址仍可放回池子重复使用'
                    : '放回号池，下次注册可以再领到它'
              }
            >
              <Button
                size="small"
                icon={<ReloadOutlined />}
                aria-label={alias.pool_status === 'unpooled' ? '导入号池' : '放回号池'}
                loading={busyKey === `alias:${alias.id}`}
                onClick={() =>
                  withBusy(
                    `alias:${alias.id}`,
                    () => setICloudAliasPoolStatus(alias.id, 'available'),
                    alias.pool_status === 'unpooled' ? '已导入号池' : '已放回号池',
                  )
                }
              />
            </Tooltip>
          ) : null}
          <Popconfirm
            title="删除隐私邮箱"
            description="会先在 iCloud 停用并删除该地址，删除后无法恢复。"
            onConfirm={() => withBusy(`alias:${alias.id}`, () => deleteICloudAlias(alias.id), '隐私邮箱已删除')}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <PageHeader
        title="iCloud 隐私邮箱"
        subtitle="主号管理与 Hide My Email 别名"
        actions={
          <Space>
            <Button icon={<LoginOutlined />} type="primary" onClick={() => setLoginOpen(true)}>
              应用内登录
            </Button>
            <Button onClick={() => setCookieOpen(true)}>手工导入 Cookie</Button>
            <Button icon={<ReloadOutlined spin={loading} />} onClick={load} aria-label="刷新列表" title="刷新列表" />
          </Space>
        }
      />

      <Tabs
        defaultActiveKey="accounts"
        items={[
          {
            key: 'accounts',
            label: `主号管理 (${accounts.length})`,
            children: (
              <Card>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message={`收件优先 IMAP（未配 App 专用密码回退 Web API）；每主号每小时上限 ${ICLOUD_HOURLY_ALIAS_LIMIT} 个隐私邮箱。`}
                />
                <Table
                  rowKey="id"
                  loading={loading}
                  columns={accountColumns}
                  dataSource={accounts}
                  // 7 列合计 1240px，容器装不下时 antd 会等比压缩所有列 ——
                  // 实测「主号」列被压到 143px（设定 260），邮箱地址换行、行高 78px。
                  // 定住宽度宁可横向滚动，与别名表同款处理。
                  scroll={{ x: 1240 }}
                  pagination={false}
                  locale={{
                    emptyText: (
                      <Empty description="还没有 iCloud 主号，点击右上角完成 Apple ID 登录" />
                    ),
                  }}
                />
              </Card>
            ),
          },
          {
            key: 'aliases',
            label: aliasFilterActive
              ? `隐私邮箱 (${visibleAliases.length}/${aliases.length})`
              : `隐私邮箱 (${aliases.length})`,
            children: (
              <Card>
                {/* 号池概览：注册任务领号先捞「未使用」的，这里能一眼看出还剩多少。
                    「未入池」单独说明 —— 它不是池里的号，勾选导入才会变成可领的。 */}
                <Space style={{ marginBottom: 12 }} wrap>
                  {filterPlatform && platformSummary ? (
                    <>
                      {PLATFORM_SUMMARY_ORDER.map((key) => {
                        const item = PLATFORM_STATUS_META[key]
                        return (
                          <Tag key={key} color={item.color}>
                            {item.text} {platformSummary[key]}
                          </Tag>
                        )
                      })}
                      <Text type="secondary">共 {platformSummary.total} 个</Text>
                      <Tooltip
                        title={`「${getPlatformLabel(filterPlatform)}」口径：在此平台注册过即算已注册。`}
                      >
                        <QuestionCircleOutlined style={{ color: 'var(--text-muted)' }} />
                      </Tooltip>
                    </>
                  ) : (
                    <>
                      {POOL_SUMMARY_ORDER.map((key) => {
                        const item = poolStatusMeta(key)
                        return (
                          <Tag key={key} color={item.color}>
                            {item.text} {pool[key]}
                          </Tag>
                        )
                      })}
                      <Text type="secondary">共 {pool.total} 个</Text>
                      <Tooltip title="「未入池」注册取号会跳过，勾选后点「导入邮箱池」才可领；取号优先复用「未使用」。">
                        <QuestionCircleOutlined style={{ color: 'var(--text-muted)' }} />
                      </Tooltip>
                    </>
                  )}
                </Space>
                <Space style={{ marginBottom: 16 }} wrap>
                  <Select
                    allowClear
                    placeholder="全部主号"
                    style={{ width: 260 }}
                    value={filterAccountId}
                    onChange={setFilterAccountId}
                    options={accountOptions}
                  />
                  {/* 列表内筛选：搜索 + 平台 + 号池状态 + 启用状态。
                      都是**客户端过滤**（见 visibleAliases），改条件不重新请求。 */}
                  <Input
                    allowClear
                    prefix={<SearchOutlined />}
                    placeholder="搜索邮箱 / 标签 / 备注 / 主号"
                    style={{ width: 260 }}
                    value={searchText}
                    onChange={(e) => setSearchText(e.target.value)}
                  />
                  <Select
                    allowClear
                    placeholder="全部平台"
                    style={{ width: 150 }}
                    value={filterPlatform}
                    onChange={(value) => {
                      setFilterPlatform(value)
                      // 两套口径的状态取值不同，切平台时清掉状态筛选，避免筛出空列表。
                      setFilterPoolStatus(undefined)
                    }}
                    options={PLATFORM_FILTER_OPTIONS.filter((item) => item.value)}
                  />
                  <Select
                    allowClear
                    placeholder={filterPlatform ? '全部平台状态' : '全部号池状态'}
                    style={{ width: 170 }}
                    value={filterPoolStatus}
                    onChange={setFilterPoolStatus}
                    options={
                      (filterPlatform
                        ? PLATFORM_SUMMARY_ORDER.map((key) => ({
                            value: key as string,
                            label: PLATFORM_STATUS_META[key].text,
                          }))
                        : POOL_SUMMARY_ORDER.map((key) => ({
                            value: key as string,
                            label: poolStatusMeta(key).text,
                          }))) as { value: string; label: string }[]
                    }
                  />
                  <Select
                    allowClear
                    placeholder="全部状态"
                    style={{ width: 130 }}
                    value={filterAliasStatus}
                    onChange={setFilterAliasStatus}
                    options={[
                      { value: 'active', label: '启用' },
                      { value: 'inactive', label: '已停用' },
                    ]}
                  />
                  {aliasFilterActive && (
                    <Text type="secondary">
                      筛选出 {visibleAliases.length} / {aliases.length} 个
                      <Button type="link" size="small" onClick={clearAliasFilters}>
                        清除
                      </Button>
                    </Text>
                  )}
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    disabled={accounts.length === 0}
                    onClick={() => setGenerateOpen(true)}
                  >
                    生成隐私邮箱
                  </Button>
                  <Dropdown.Button
                    icon={<DownOutlined />}
                    disabled={targetAliases.length === 0}
                    onClick={() => exportAliases('mail_url')}
                    menu={{
                      items: [
                        { key: 'mail_url', label: '隐私邮箱----邮件 URL' },
                        { key: 'account', label: '隐私邮箱----所属主号' },
                      ],
                      onClick: ({ key }) => exportAliases(key as AliasExportMode),
                    }}
                  >
                    <DownloadOutlined /> 导出 ({targetAliases.length})
                  </Dropdown.Button>
                  {selectedAliasIds.length > 0 && (
                    <>
                      {/* 入池 / 出池：生成与同步下来的别名默认「未入池」，注册取号
                          会跳过它们 —— 必须在这里勾选导入才进池。 */}
                      <Button
                        type="primary"
                        icon={<ImportOutlined />}
                        loading={poolChanging}
                        onClick={() => void handlePoolChange('import')}
                      >
                        导入邮箱池 {selectedAliasIds.length} 个
                      </Button>
                      <Button
                        loading={poolChanging}
                        onClick={() => void handlePoolChange('unpool')}
                      >
                        移出邮箱池
                      </Button>
                      <Popconfirm
                        title={`删除选中的 ${selectedAliasIds.length} 个隐私邮箱`}
                        description="会先在 iCloud 停用并删除这些地址，删除后无法恢复。"
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                        onConfirm={handleBatchDelete}
                      >
                        <Button danger icon={<DeleteOutlined />} loading={batchDeleting}>
                          删除 {selectedAliasIds.length} 个
                        </Button>
                      </Popconfirm>
                      <Text type="secondary">已选 {selectedAliasIds.length} 个</Text>
                    </>
                  )}
                </Space>
                <Table
                  rowKey="id"
                  loading={loading}
                  columns={aliasColumns}
                  dataSource={visibleAliases}
                  rowSelection={{
                    selectedRowKeys: selectedAliasIds,
                    onChange: setSelectedAliasIds,
                  }}
                  // 列多了之后窄屏会把邮箱地址挤成三行，宁可横向滚动。
                  // 宽度要盖住所有列（含后加的「号池」「已注册平台」列，
                  // 且平台筛选时「号池」列从 130 变 170 —— 按最大值 1830 算），
                  // 否则最后一列被裁掉 / 被 antd 等比压缩。
                  scroll={{ x: 1830 }}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                  locale={{
                    emptyText: (
                      <Empty
                        description={
                          accounts.length === 0
                            ? '请先添加 iCloud 主号'
                            : aliasFilterActive
                              // 有筛选条件时「还没有隐私邮箱」是误导 —— 邮箱在，
                              // 只是被筛掉了。
                              ? '当前筛选条件下没有匹配的隐私邮箱'
                              : '还没有隐私邮箱，点击“生成隐私邮箱”开始'
                        }
                      />
                    ),
                  }}
                />
              </Card>
            ),
          },
        ]}
      />

      <ICloudLoginModal
        open={loginOpen}
        onClose={() => setLoginOpen(false)}
        onCompleted={() => load()}
      />

      <CookieImportModal
        open={cookieOpen}
        onClose={() => setCookieOpen(false)}
        onImported={() => load()}
      />

      <GenerateAliasModal
        open={generateOpen}
        accounts={accounts}
        defaultAccountId={filterAccountId ?? accounts[0]?.id}
        onClose={() => setGenerateOpen(false)}
        onGenerated={() => load()}
      />

      <AliasInboxDrawer alias={inboxAlias} onClose={() => setInboxAlias(null)} />
    </div>
  )
}

