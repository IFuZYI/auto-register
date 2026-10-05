import { useEffect, useState, useCallback } from 'react'
import { useParams } from 'react-router-dom'
import {
  DetailSection,
  LocalProbeSummary,
  TotpSecretAlert,
} from '@/components/account/AccountDetailParts'
import {
  normalizeAccount,
  formatCreatedAt,
  authStateMeta,
  codexStateMeta,
  plusTrialMeta,
  planMeta,
  atLifecycleMeta,
  atListSummary,
} from '@/lib/accountFormat'
import {
  Table,
  Button,
  Input,
  InputNumber,
  Select,
  Tag,
  Space,
  Modal,
  Form,
  message,
  Popconfirm,
  Dropdown,
  Typography,
  Alert,
  DatePicker,
  Segmented,
  Switch,
  Tooltip,
  theme,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  ReloadOutlined,
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  DownloadOutlined,
  UploadOutlined,
  MoreOutlined,
  DeleteOutlined,
  SyncOutlined,
  KeyOutlined,
  FileAddOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { AccountExportModal } from '@/components/AccountExportModal'
import { ChatGPTBind2faSwitch } from '@/components/ChatGPTBind2faSwitch'
import { ChatGPTRegisterFlowSelect } from '@/components/ChatGPTRegisterFlowSelect'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import type { TaskKind } from '@/components/TaskLogPanel'
import { usePersistentChatGPTBind2fa } from '@/hooks/usePersistentChatGPTBind2fa'
import { usePersistentChatGPTRegisterFlow } from '@/hooks/usePersistentChatGPTRegisterFlow'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'
import { getPlatformLabel, loadPlatformCapabilities, normalizeExecutorForPlatform, rememberPlatformCapabilities } from '@/lib/platforms'
import {
  DEFAULT_REGISTER_RETRY_TIMES,
  normalizeRegisterRetryTimes,
} from '@/lib/registerRetry'
import { MAX_REGISTER_CONCURRENCY, MAX_REGISTER_COUNT } from '@/lib/registerLimits'

const { Text } = Typography

const PLUS_TRIAL_FILTERS = [
  { value: 'trial_eligible', label: '可领首月免费' },
  { value: 'plus_active', label: 'Plus 生效中' },
  { value: 'free', label: 'Free' },
  { value: 'banned', label: '封号' },
  { value: 'token_invalid', label: '凭证失效' },
  { value: 'unchecked', label: '未检测' },
]

type PlusCheck = { status?: string; message?: string; checked_at?: string }

const STATUS_COLORS: Record<string, string> = {
  registered: 'default',
  expired: 'warning',
  invalid: 'error',
  banned: 'error',
}

// 纯前端动作，不发请求，所以不和后端的 action id 抢命名空间
const COPY_TOTP_ACTION_ID = '__copy_totp_secret'

// 这些动作要跑几十秒的协议链（还可能停下来等一封验证码），同步等只能看见一个
// 转圈。改走后台任务，和注册一样用日志弹窗把每一步显示出来。
const TASK_BACKED_ACTIONS: Record<
  string,
  { endpoint: string; kind: TaskKind; body: (accountId: number) => Record<string, unknown> }
> = {
  backfill_refresh_token: {
    endpoint: '/tasks/backfill-rt',
    kind: 'backfill_rt',
    body: (accountId) => ({ account_ids: [accountId], only_missing_rt: false, delay_seconds: 0 }),
  },
  bind_2fa: {
    endpoint: '/tasks/bind-2fa',
    kind: 'bind_2fa',
    body: (accountId) => ({ account_ids: [accountId], only_missing_2fa: false, delay_seconds: 0 }),
  },
}



function ActionMenu({ acc, onRefresh, actions }: { acc: any; onRefresh: () => void; actions: any[] }) {
  const [resultOpen, setResultOpen] = useState(false)
  const [resultTitle, setResultTitle] = useState('')
  const [resultStatus, setResultStatus] = useState<'success' | 'error'>('success')
  const [resultText, setResultText] = useState('')
  const [resultUrl, setResultUrl] = useState('')
  const [resultProbe, setResultProbe] = useState<any>(null)
  const [resultSecret, setResultSecret] = useState('')
  const [runningActionId, setRunningActionId] = useState<string | null>(null)
  const [taskModalTitle, setTaskModalTitle] = useState('')
  const [taskModalKind, setTaskModalKind] = useState<TaskKind>('backfill_rt')
  const [actionTaskId, setActionTaskId] = useState<string | null>(null)

  const showResult = (
    title: string,
    status: 'success' | 'error',
    text: string,
    url = '',
    probe: any = null,
    secret = '',
  ) => {
    setResultTitle(title)
    setResultStatus(status)
    setResultText(text)
    setResultUrl(url)
    setResultProbe(probe)
    setResultSecret(secret)
    setResultOpen(true)
  }

  const copyResultUrl = async () => {
    if (!resultUrl) return
    try {
      await navigator.clipboard.writeText(resultUrl)
      message.success('链接已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const runAsTask = async (actionId: string, actionLabel: string) => {
    const task = TASK_BACKED_ACTIONS[actionId]
    setRunningActionId(actionId)
    try {
      const result = await apiFetch(task.endpoint, {
        method: 'POST',
        body: JSON.stringify(task.body(acc.id)),
      })
      setTaskModalTitle(`${actionLabel} - ${acc.email}`)
      setTaskModalKind(task.kind)
      setActionTaskId(result.task_id)
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e)
      message.error(`${actionLabel}失败: ${detail}`)
    } finally {
      setRunningActionId(null)
    }
  }

  const handleAction = async (actionId: string) => {
    if (runningActionId) return
    const actionLabel = actions.find((item) => item.id === actionId)?.label || actionId
    const toastKey = `account-action:${acc?.id}:${actionId}`

    if (actionId === COPY_TOTP_ACTION_ID) {
      try {
        await navigator.clipboard.writeText(acc.totpSecret)
        message.success('2FA 密钥已复制')
      } catch {
        message.error('复制失败，请在账号详情里手动复制')
      }
      return
    }

    if (TASK_BACKED_ACTIONS[actionId]) {
      await runAsTask(actionId, actionLabel)
      return
    }

    setRunningActionId(actionId)
    message.loading({ content: `${actionLabel}运行中...`, key: toastKey, duration: 0 })

    try {
      const r = await apiFetch(`/actions/${acc.platform}/${acc.id}/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({ params: {} }),
      })
      if (!r.ok) {
        const data = r.data || {}
        const probe = typeof data === 'object' && data ? data.probe || null : null
        message.error({ content: `${actionLabel}失败`, key: toastKey })
        showResult(actionLabel, 'error', r.error || data.message || '操作失败', '', probe)
        onRefresh()
        return
      }
      const data = r.data || {}
      // 绑 2FA 只在这一次响应里下发密钥，弹窗里必须能一键复制，关掉就没了
      const secret = typeof data === 'object' && data ? String(data.totp_secret || '') : ''
      if (secret) {
        message.success({ content: data.message || `${actionLabel}完成`, key: toastKey })
        showResult(actionLabel, 'success', String(data.message || '操作成功'), '', null, secret)
        onRefresh()
        return
      }
      if (data.url || data.checkout_url || data.cashier_url) {
        const targetUrl = data.url || data.checkout_url || data.cashier_url
        message.success({ content: `${actionLabel}完成`, key: toastKey })
        showResult(actionLabel, 'success', '操作成功，请在弹窗中打开或复制链接。', targetUrl)
      } else {
        message.success({ content: data.message || `${actionLabel}完成`, key: toastKey })
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const text =
          probe
            ? String(data.message || '操作成功')
            : typeof data === 'string'
            ? data
            : Object.keys(data).length > 0
              ? JSON.stringify(data, null, 2)
              : '操作成功'
        showResult(actionLabel, 'success', text, '', probe)
      }
      onRefresh()
    } catch (e: any) {
      const detail = e?.message ? String(e.message) : '请求失败'
      message.error({ content: detail, key: toastKey })
      showResult(actionLabel, 'error', detail)
    } finally {
      setRunningActionId(null)
    }
  }

  const menuItems: MenuProps['items'] = [
    ...(acc.totpSecret
      ? [{ key: COPY_TOTP_ACTION_ID, label: '复制 2FA 密钥' }]
      : []),
    // 面板动作（`scope: "panel"`，上传 CPA / Sub2API / grok2api、同步远端状态）
    // 不在账号页出现 —— 它们的目标是外部面板，操作面在「面板管理」页。
    // 后端仍声明它们（批量端点与面板注册表按 id 找），只是这里不渲染。
    ...actions
      .filter((a) => a?.scope !== 'panel')
      .map((a) => ({
        key: a.id,
        label: runningActionId === a.id ? `${a.label}（运行中）` : a.label,
        disabled: Boolean(runningActionId),
      })),
  ]

  if (menuItems.length === 0) return null

  return (
    <>
      <Dropdown
        menu={{
          items: menuItems,
          onClick: ({ key }) => handleAction(String(key)),
        }}
      >
        <Button
          type="link"
          size="small"
          icon={<MoreOutlined />}
          loading={Boolean(runningActionId)}
        />
      </Dropdown>
      <Modal
        title={taskModalTitle}
        open={Boolean(actionTaskId)}
        onCancel={() => { setActionTaskId(null); onRefresh() }}
        footer={null}
        width={620}
        maskClosable={false}
      >
        {taskModalKind === 'bind_2fa' && acc.totpSecret ? (
          <div style={{ marginBottom: 12 }}>
            <TotpSecretAlert secret={acc.totpSecret} />
          </div>
        ) : null}
        {actionTaskId ? <TaskLogPanel taskId={actionTaskId} kind={taskModalKind} onDone={onRefresh} /> : null}
      </Modal>
      <Modal
        title={resultTitle}
        open={resultOpen}
        onCancel={() => setResultOpen(false)}
        footer={[
          resultUrl ? (
            <Button key="copy" onClick={copyResultUrl}>
              复制链接
            </Button>
          ) : null,
          resultUrl ? (
            <Button
              key="open"
              type="primary"
              onClick={() => window.open(resultUrl, '_blank', 'noopener,noreferrer')}
            >
              打开链接
            </Button>
          ) : null,
          <Button key="ok" type={resultUrl ? 'default' : 'primary'} onClick={() => setResultOpen(false)}>
            确定
          </Button>,
        ].filter(Boolean)}
        maskClosable={false}
      >
        <Alert
          type={resultStatus}
          showIcon
          message={resultStatus === 'success' ? '操作完成' : '操作失败'}
          style={{ marginBottom: 12 }}
        />
        {resultProbe ? (
          <div style={{ marginBottom: 12 }}>
            <LocalProbeSummary probe={resultProbe} />
          </div>
        ) : null}
        {resultSecret ? (
          <div style={{ marginBottom: 12 }}>
            <TotpSecretAlert secret={resultSecret} />
          </div>
        ) : null}
        {resultUrl ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Text copyable={{ text: resultUrl }} style={{ wordBreak: 'break-all' }}>
              {resultUrl}
            </Text>
          </Space>
        ) : null}
        {resultText ? (
          <pre
            style={{
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {resultText}
          </pre>
        ) : null}
      </Modal>
    </>
  )
}

export default function Accounts() {
  const { platform } = useParams<{ platform: string }>()
  const { token } = theme.useToken()
  const [currentPlatform, setCurrentPlatform] = useState(platform || 'chatgpt')
  const [accounts, setAccounts] = useState<any[]>([])
  const [platformActions, setPlatformActions] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterPlusStatus, setFilterPlusStatus] = useState('')
  const [createdAtStart, setCreatedAtStart] = useState('')
  const [createdAtEnd, setCreatedAtEnd] = useState('')
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [exportModalOpen, setExportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [currentAccount, setCurrentAccount] = useState<any>(null)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()
  const { registerFlow: chatgptRegisterFlow, setRegisterFlow: setChatgptRegisterFlow } =
    usePersistentChatGPTRegisterFlow()
  const { bind2fa: chatgptBind2fa, setBind2fa: setChatgptBind2fa } =
    usePersistentChatGPTBind2fa()
  const [importText, setImportText] = useState('')
  // 导入格式：文本（email 密码）或 JSON（导出格式全字段）
  const [importFormat, setImportFormat] = useState<'text' | 'json'>('text')
  const [importLoading, setImportLoading] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [statusSyncLoading, setStatusSyncLoading] = useState<
    'probe_selected' | 'probe_all' | 'plus_selected' | 'plus_all' | ''
  >('')
  const [backfillRtModalOpen, setBackfillRtModalOpen] = useState(false)
  const [backfillRtLoading, setBackfillRtLoading] = useState(false)
  const [backfillRtTaskId, setBackfillRtTaskId] = useState<string | null>(null)
  const [backfillRtForm] = Form.useForm()

  useEffect(() => {
    if (platform) setCurrentPlatform(platform)
  }, [platform])

  useEffect(() => {
    if (!detailModalOpen || !currentAccount) return
    detailForm.setFieldsValue({
      status: currentAccount.status,
      token: currentAccount.token,
    })
  }, [detailModalOpen, currentAccount, detailForm])

  const load = useCallback(async () => {
    if (createdAtStart && createdAtEnd && new Date(createdAtStart).getTime() > new Date(createdAtEnd).getTime()) {
      message.warning('开始时间不能晚于结束时间')
      setAccounts([])
      setTotal(0)
      return
    }

    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: currentPlatform, page: String(page), page_size: String(pageSize) })
      if (search) params.set('email', search)
      if (filterStatus) params.set('status', filterStatus)
      if (filterPlusStatus) params.set('plus_status', filterPlusStatus)
      if (createdAtStart) params.set('created_at_start', createdAtStart)
      if (createdAtEnd) params.set('created_at_end', createdAtEnd)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts((data.items || []).map(normalizeAccount))
      setTotal(data.total)
    } finally {
      setLoading(false)
    }
  }, [currentPlatform, search, filterStatus, filterPlusStatus, createdAtStart, createdAtEnd, page, pageSize])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    apiFetch(`/actions/${currentPlatform}`)
      .then((data) => setPlatformActions(data.actions || []))
      .catch(() => setPlatformActions([]))
  }, [currentPlatform])

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text)
    message.success('已复制')
  }

  const copySecret = (label: string, text: string) => {
    if (!text) {
      message.warning(`该账号没有${label}`)
      return
    }
    navigator.clipboard.writeText(text)
    message.success(`${label}已复制`)
  }

  const getRefreshToken = (record: any): string => {
    try {
      const extra = JSON.parse(record.extra_json || '{}')
      return extra.refresh_token || extra.refreshToken || ''
    } catch {
      return ''
    }
  }

  /**
   * 取账号的 Access Token —— 按平台主凭证的镜像规则取：
   * - `extra.access_token`：较新的落库路径写这里（实测存在 token 列为空、
   *   extra 有值的账号，只读列会误显示「无 AT」）—— 两平台都优先读它；
   * - `token` 列：chatgpt 的列镜像 AT，可作为兜底；**grok 的列镜像是 SSO**
   *   （不是 AT），不能拿来当 AT —— 否则列表会把 SSO 显示成「无法解析」。
   */
  const getAccessToken = (record: { token?: string; extra_json?: string } | null | undefined): string => {
    try {
      const extra = JSON.parse(record?.extra_json || '{}')
      const fromExtra = String(extra.access_token || '').trim()
      if (fromExtra) return fromExtra
    } catch {
      // ignore，退回列
    }
    if (currentPlatform === 'grok') return ''
    return String(record?.token || '').trim()
  }

  const handleDelete = async (id: number) => {
    // 带 platform：账号 id 是每库自增的，不带平台可能删到别的平台的同 id 账号
    await apiFetch(`/accounts/${id}?platform=${encodeURIComponent(currentPlatform)}`, {
      method: 'DELETE',
    })
    message.success('删除成功')
    load()
  }

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return
    await apiFetch('/accounts/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: Array.from(selectedRowKeys), platform: currentPlatform }),
    })
    message.success('批量删除成功')
    setSelectedRowKeys([])
    load()
  }

  const handleAdd = async () => {
    const values = await addForm.validateFields()
    await apiFetch('/accounts', {
      method: 'POST',
      body: JSON.stringify({ ...values, platform: currentPlatform }),
    })
    message.success('添加成功')
    setAddModalOpen(false)
    addForm.resetFields()
    load()
  }

  const handleImport = async () => {
    if (!importText.trim()) return
    setImportLoading(true)
    try {
      // JSON 模式把整段文本作为一个元素传：后端按 `lines.join("\n")` 还原，
      // 这样多行 JSON 不会被前端的逐行拆分破坏。
      const lines = importFormat === 'json'
        ? [importText.trim()]
        : importText.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', {
        method: 'POST',
        body: JSON.stringify({ platform: currentPlatform, lines, format: importFormat }),
      })
      const skipped = res.skipped ? `，跳过 ${res.skipped} 条` : ''
      message.success(`导入成功：新增 ${res.created}，更新 ${res.updated}${skipped}`)
      setImportModalOpen(false)
      setImportText('')
      load()
    } catch (e: any) {
      message.error(`导入失败: ${e.message}`)
    } finally {
      setImportLoading(false)
    }
  }

  const handleRegister = async () => {
    const values = await registerForm.validateFields()
    setRegisterLoading(true)
    try {
      const cfg = await apiFetch('/config')
      // 能力清单要先到位，否则 normalizeExecutorForPlatform 无法判断支持集合
      // （未加载时它原样返回，会把不受支持的值发下去）。后端还会再归一一次。
      rememberPlatformCapabilities(await loadPlatformCapabilities())
      // 执行器按平台取。不回落已取消的全局 `default_executor` —— 它的
      // 'protocol' 会替所有平台预选（Grok 并没有协议路径）。
      // 老数据的 `grok_register_mode` 已在 config_store 里迁移过来。
      const executorType = normalizeExecutorForPlatform(
        currentPlatform,
        cfg[`${currentPlatform}_executor`],
      )
      const registerExtra = {
        mail_provider: cfg.mail_provider || '',
        mail_import_source: cfg.mail_import_source,
        yescaptcha_key: cfg.yescaptcha_key,
        outlook_backend: cfg.outlook_backend,
        icloud_local_account_id: cfg.icloud_local_account_id,
        icloud_local_label: cfg.icloud_local_label,
        icloud_local_note: cfg.icloud_local_note,
      }
      const chatgptRegistrationRequestAdapter =
        buildChatGPTRegistrationRequestAdapter(
          currentPlatform,
          chatgptRegistrationMode,
          chatgptRegisterFlow,
          chatgptBind2fa,
        )
      const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
        ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
        : registerExtra

      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: currentPlatform,
          count: values.count,
          concurrency: values.concurrency,
          register_retry_times: normalizeRegisterRetryTimes(values.register_retry_times),
          register_delay_seconds: values.register_delay_seconds || 0,
          executor_type: executorType,
          captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
          proxy: null,
          extra: adaptedRegisterExtra,
        }),
      })
      setTaskId(res.task_id)
    } finally {
      setRegisterLoading(false)
    }
  }

  const handleDetailSave = async () => {
    const values = await detailForm.validateFields()
    await apiFetch(`/accounts/${currentAccount.id}?platform=${encodeURIComponent(currentPlatform)}`, {
      method: 'PATCH',
      body: JSON.stringify(values),
    })
    message.success('保存成功')
    setDetailModalOpen(false)
    load()
  }

  const showBatchActionResult = (title: string, result: any) => {
    const lines = (result.items || [])
      .filter((item: any) => !item.ok)
      .map((item: any) => `[${item.id || '-'}] ${item.email || '-'}: ${item.message || '失败'}`)

    if (lines.length === 0) return

    Modal.info({
      title,
      width: 760,
      content: (
        <pre
          style={{
            margin: 0,
            maxHeight: 360,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'var(--bg-subtle)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </pre>
      ),
    })
  }

  const handleBatchStatusSync = async (kind: 'probe' | 'plus', scope: 'selected' | 'all') => {
    if (currentPlatform !== 'chatgpt') return

    const loadingKey = `${kind}_${scope}` as typeof statusSyncLoading
    const actionId = kind === 'probe' ? 'probe_local_status' : 'check_plus_trial'
    const actionLabel = kind === 'probe' ? '本地状态同步' : 'Plus 试用检测'
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'
    const toastKey = `status-sync:${loadingKey}`

    const body: Record<string, unknown> = {
      params: {},
    }

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要同步的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
    }

    setStatusSyncLoading(loadingKey)
    message.loading({ content: `${scopeLabel}${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch(`/actions/${currentPlatform}/${actionId}/batch`, {
        method: 'POST',
        body: JSON.stringify(body),
      })

      if (!result.total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (!result.failed) {
        message.success({ content: `${scopeLabel}${actionLabel}完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else if (!result.success) {
        message.error({ content: `${scopeLabel}${actionLabel}失败：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else {
        message.warning({ content: `${scopeLabel}${actionLabel}部分完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      }

      showBatchActionResult(`${scopeLabel}${actionLabel}结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `${actionLabel}失败: ${e.message}`, key: toastKey })
    } finally {
      setStatusSyncLoading('')
    }
  }

  const missingRtCount = accounts.filter((item) => !getRefreshToken(item)).length

  const handleBackfillRt = async () => {
    const values = await backfillRtForm.validateFields()
    const scope = selectedRowKeys.length > 0 ? 'selected' : 'all'

    const body: Record<string, unknown> = {
      only_missing_rt: values.only_missing_rt !== false,
      allow_login: values.allow_login !== false,
      concurrency: Number(values.concurrency) || 1,
      delay_seconds: Number(values.delay_seconds) || 0,
    }

    if (scope === 'selected') {
      body.account_ids = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
    }

    setBackfillRtLoading(true)
    try {
      const result = await apiFetch('/tasks/backfill-rt', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      setBackfillRtTaskId(result.task_id)
      message.success(`已开始给 ${result.total} 个账号补 RT`)
    } catch (e) {
      message.error(`补 RT 启动失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBackfillRtLoading(false)
    }
  }

  const closeBackfillRtModal = () => {
    setBackfillRtModalOpen(false)
    setBackfillRtTaskId(null)
    backfillRtForm.resetFields()
  }

  const getStatusSyncScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  const isChatgptPlatform = currentPlatform === 'chatgpt'
  const monospaceStyle: React.CSSProperties = {
    fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    fontSize: 12,
  }
  const secondaryTextStyle: React.CSSProperties = {
    fontSize: 12,
    color: token.colorTextSecondary,
  }
  const cellStackStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    minWidth: 0,
  }
  // 复制按钮紧跟在截断后的密文右边，别被超长 token 顶到列的最右侧。
  const secretCellStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    minWidth: 0,
  }
  const secretPreviewStyle: React.CSSProperties = {
    ...monospaceStyle,
    flex: 1,
    minWidth: 0,
    filter: 'blur(4px)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    opacity: 0.9,
  }
  const compactPanelStyle: React.CSSProperties = {
    padding: '8px 10px',
    borderRadius: token.borderRadiusLG,
    border: `1px solid ${token.colorBorder}`,
    background: token.colorFillAlter,
  }

  const columns: any[] = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 260,
      render: (text: string, record: any) => (
        <div style={cellStackStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            <Text
              style={{ ...monospaceStyle, flex: 1, minWidth: 0, whiteSpace: 'nowrap' }}
              ellipsis={{ tooltip: text }}
            >
              {text}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
          </div>
          <Text type="secondary" style={secondaryTextStyle} ellipsis={{ tooltip: record.user_id || `账号 #${record.id}` }}>
            {record.user_id ? `UID: ${record.user_id}` : `账号 #${record.id}`}
          </Text>
        </div>
      ),
    },
    {
      title: '密码',
      dataIndex: 'password',
      key: 'password',
      width: 120,
      render: (text: string) => (
        <div style={secretCellStyle}>
          <Text style={secretPreviewStyle} title={text}>
            {text}
          </Text>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
        </div>
      ),
    },
    {
      title: 'RT',
      key: 'refresh_token',
      width: 120,
      render: (_: any, record: any) => {
        const rt = getRefreshToken(record)
        if (!rt) return <span style={{ color: 'var(--text-muted)' }}>-</span>
        return (
          <div style={secretCellStyle}>
            <Text style={{ ...secretPreviewStyle, fontSize: 11 }} title={rt}>
              {rt}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(rt)} />
          </div>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string) => <Tag color={STATUS_COLORS[status] || 'default'}>{status}</Tag>,
    },
    {
      // AT 有效期：ChatGPT 与 Grok 的 AT 都是 JWT（都带 iat/exp），
      // 两个平台的列表都要显示 —— 详情弹窗本来就是共享的，列表不该分家。
      title: 'AT 有效期',
      key: 'at_lifecycle',
      width: 130,
      render: (_: unknown, record: { token?: string; extra_json?: string }) => {
        // 与详情弹窗同口径（atListSummary 内部复用 atLifecycleMeta）。
        // 参考实现（chatgpt2api）在列表行内直接显示凭据状态，用户反馈
        // 「GPT 界面没见到 AT 日期」——此前只在详情弹窗里。
        const at = atListSummary(getAccessToken(record))
        const hasDate = Boolean(at.expiresShort || at.remainingText)
        if (at.label === '无 AT' && !hasDate) {
          return (
            <Text type="secondary" style={secondaryTextStyle}>
              无 AT
            </Text>
          )
        }
        return (
          <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
            <Tooltip title={at.tooltip || at.label}>
              <Tag color={at.color} style={{ marginInlineEnd: 0 }}>
                {at.label}
              </Tag>
            </Tooltip>
            {hasDate ? (
              // 日期与剩余时间**分行**：两者拼一行需要 146px，而该列可用
              // 内容宽只有 ~92px（130px 列宽 - 两重 padding）—— 实测日期
              // 被截断成「到期 2026-10-…」（dogfood 2026-10-05，缩放前后
              // 均复现）。拆行后「到期 2026-10-12」78px、「6 天 20 小时」
              // 59px，都放得下。
              <Text type="secondary" style={secondaryTextStyle} ellipsis={{ tooltip: at.tooltip }}>
                {at.expiresShort ? `到期 ${at.expiresShort}` : ''}
              </Text>
            ) : null}
            {at.remainingText ? (
              <Text type="secondary" style={secondaryTextStyle} ellipsis={{ tooltip: at.tooltip }}>
                {at.remainingText}
              </Text>
            ) : null}
          </div>
        )
      },
    },
  ]

  if (isChatgptPlatform) {
    columns.push(
      {
        title: '本地状态',
        key: 'chatgpt_local_state',
        width: 260,
        render: (_: any, record: any) => {
          const auth = record.chatgptLocal?.auth || {}
          const subscription = record.chatgptLocal?.subscription || {}
          const codex = record.chatgptLocal?.codex || {}
          const authMeta = authStateMeta(auth.state)
          const planTag = planMeta(subscription.plan)
          const codexMeta = codexStateMeta(codex.state)

          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                <Tag color={authMeta.color}>{authMeta.label}</Tag>
                <Tag color={planTag.color}>{planTag.label}</Tag>
                <Tag color={codexMeta.color}>Codex {codexMeta.label}</Tag>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {record.totpSecret ? (
                  <Tag
                    color="purple"
                    title="点击复制 TOTP 密钥，可直接导入验证器"
                    style={{ cursor: 'pointer', marginInlineEnd: 0 }}
                    onClick={() => copySecret('2FA 密钥', record.totpSecret)}
                  >
                    <Space size={4}>
                      2FA 已绑
                      <CopyOutlined />
                    </Space>
                  </Tag>
                ) : null}
              </div>
            </div>
          )
        },
      },
      {
        title: 'Plus 试用',
        key: 'plus_check',
        width: 140,
        render: (_: unknown, record: { plusCheck?: PlusCheck }) => {
          const check = record.plusCheck || {}
          const meta = plusTrialMeta(check.status)
          // 两行日期格式（与「注册时间」列同款）：单行 toLocaleString
          // （`10/1/2026, 6:43:48 PM`）实测需要 118px，该列可用内容宽只有
          // ~102px，时间戳会被截断成「10/1/2026, 6:43…」（dogfood 2026-10-05）。
          const checkedAt = formatCreatedAt(check.checked_at)
          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <Tag color={meta.color} title={check.message || ''}>
                {meta.label}
              </Tag>
              {checkedAt.date !== '-' && (
                <Text type="secondary" style={secondaryTextStyle} ellipsis={{ tooltip: `${checkedAt.date} ${checkedAt.time}`.trim() }}>
                  {checkedAt.date}
                </Text>
              )}
              {checkedAt.time ? (
                <Text type="secondary" style={secondaryTextStyle}>
                  {checkedAt.time}
                </Text>
              ) : null}
            </div>
          )
        },
      },
    )
  } else {
    columns.push(
      {
        title: '地区',
        dataIndex: 'region',
        key: 'region',
        width: 100,
        render: (text: string) => text || '-',
      },
      {
        title: '试用链接',
        dataIndex: 'cashier_url',
        key: 'cashier_url',
        width: 120,
        render: (url: string) =>
          url ? (
            <Space size={0}>
              <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(url)} />
              <Button type="text" size="small" icon={<LinkOutlined />} onClick={() => window.open(url, '_blank')} />
            </Space>
          ) : (
            '-'
          ),
      },
    )
  }

  columns.push(
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 132,
      render: (text: string) => {
        const formatted = formatCreatedAt(text)
        return (
          <div style={cellStackStyle}>
            <Text style={{ fontSize: 13 }}>{formatted.date}</Text>
            {formatted.time ? <Text type="secondary" style={secondaryTextStyle}>{formatted.time}</Text> : null}
          </div>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      // 所有平台都固定到右侧：列宽合计远大于容器（实测 1448px vs 984px），
      // 不固定的话「操作」列默认落在视口外，用户得横向滚动才能点到按钮。
      fixed: 'right',
      render: (_: any, record: any) => (
        <Space size={4} wrap>
          <Button type="link" size="small" onClick={() => { setCurrentAccount(record); setDetailModalOpen(true); }}>
            详情
          </Button>
          <Popconfirm
            title="确认删除该账号吗？"
            onConfirm={() => handleDelete(record.id)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
          <ActionMenu acc={record} onRefresh={load} actions={platformActions} />
        </Space>
      ),
    },
  )

  // 只留账号自身的动作：本地状态探测、Plus 试用检测。
  // 「同步 CLIProxyAPI 状态」已移到「面板管理」页（用户要求平台管理只管账号）——
  // 那个动作的目标是外部面板，和账号本身的状态不是一回事。
  const statusSyncMenuItems: MenuProps['items'] = [
    {
      key: `probe:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选本地状态 (${selectedRowKeys.length})`
          : `同步当前筛选本地状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
    {
      key: `plus:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `检测所选 Plus 试用资格 (${selectedRowKeys.length})`
          : `检测当前筛选 Plus 试用资格 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
  ]

  return (
    <div>
      <PageHeader
        title={getPlatformLabel(currentPlatform)}
        subtitle={`${total} 个账号${selectedRowKeys.length > 0 ? ` · 已选 ${selectedRowKeys.length} 个` : ''}`}
        actions={
          <Space wrap>
            {/* 主操作单独突出，其余低频动作收进「更多」。
                之前 8 个按钮平铺、除「注册」外全是同权重描边按钮，没有视觉层级；
                且「新增」与「注册」共用同一个 + 图标。 */}
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setRegisterModalOpen(true)}>注册</Button>
            {currentPlatform === 'chatgpt' && (
              <Dropdown
                trigger={['click']}
                menu={{
                  items: statusSyncMenuItems,
                  onClick: ({ key }) => {
                    const [kind, scope] = String(key).split(':') as [
                      'probe' | 'plus',
                      'selected' | 'all',
                    ]
                    handleBatchStatusSync(kind, scope)
                  },
                }}
              >
                <Button icon={<SyncOutlined />} loading={statusSyncLoading !== ''} disabled={total === 0}>
                  状态同步
                </Button>
              </Dropdown>
            )}
            {/* CPA / grok2api 上传按钮已移到「面板管理」页：那里能看到
                「谁没上传」的对比结果，上传就在同一页发（用户要求平台管理
                只管账号）。 */}
            <Button icon={<ReloadOutlined spin={loading} />} onClick={load} aria-label="刷新列表" title="刷新列表" />
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  ...(currentPlatform === 'chatgpt' ? [{
                    key: 'rt',
                    icon: <KeyOutlined />,
                    label: selectedRowKeys.length > 0 ? `补 RT (${selectedRowKeys.length})` : '补 RT',
                    disabled: total === 0,
                  }] : []),
                  { key: 'import', icon: <UploadOutlined />, label: '导入' },
                  { key: 'export', icon: <DownloadOutlined />, label: selectedRowKeys.length > 0 ? `导出 (${selectedRowKeys.length})` : '导出', disabled: total === 0 },
                  { type: 'divider' as const },
                  // 「手工新增」= 手动录一条，与「注册」（自动跑流程）不是一回事，
                  // 换个图标避免和「注册」的 + 混淆。
                  { key: 'add', icon: <FileAddOutlined />, label: '手工新增' },
                  ...(selectedRowKeys.length > 0 ? [{
                    key: 'delete',
                    icon: <DeleteOutlined />,
                    label: `删除选中 ${selectedRowKeys.length} 个`,
                    danger: true,
                  }] : []),
                ],
                onClick: ({ key }) => {
                  if (key === 'rt') setBackfillRtModalOpen(true)
                  else if (key === 'import') setImportModalOpen(true)
                  else if (key === 'export') setExportModalOpen(true)
                  else if (key === 'add') setAddModalOpen(true)
                  else if (key === 'delete') { void handleBatchDelete() }
                },
              }}
            >
              <Button icon={<MoreOutlined />}>更多</Button>
            </Dropdown>
          </Space>
        }
      />

      {/* 筛选条：单独一行，避免和操作按钮挤在一起 */}
      <div
        style={{
          display: 'flex',
          gap: 10,
          flexWrap: 'wrap',
          alignItems: 'center',
          marginBottom: 16,
        }}
      >
        <Input.Search
          placeholder="搜索邮箱..."
          allowClear
          aria-label="搜索邮箱"
          // antd 的 Input.Search 会渲染一个纯图标搜索按钮，没有可访问名时
          // 读屏只能念 "button"。给图标一个 aria-label，它就成为了按钮的名字。
          enterButton={<SearchOutlined aria-label="搜索" />}
          onSearch={(v) => { setPage(1); setSearch(v) }}
          style={{ width: 'var(--w-field-md)' }}
        />
        <Select
          placeholder="状态筛选"
          allowClear
          style={{ width: 130 }}
          onChange={(v) => { setPage(1); setFilterStatus(v) }}
          options={[
            { value: 'registered', label: '已注册' },
            { value: 'expired', label: '已过期' },
            { value: 'invalid', label: '已失效' },
            { value: 'banned', label: '已封禁' },
          ]}
        />
        {currentPlatform === 'chatgpt' && (
          <Select
            placeholder="Plus 试用"
            allowClear
            style={{ width: 150 }}
            onChange={(v) => { setPage(1); setFilterPlusStatus(v || '') }}
            options={PLUS_TRIAL_FILTERS}
          />
        )}
        <DatePicker
          showTime
          allowClear
          placeholder="开始时间"
          style={{ width: 168 }}
          onChange={(value) => { setPage(1); setCreatedAtStart(value ? value.toISOString() : '') }}
        />
        <DatePicker
          showTime
          allowClear
          placeholder="结束时间"
          style={{ width: 168 }}
          onChange={(value) => { setPage(1); setCreatedAtEnd(value ? value.toISOString() : '') }}
        />
      </div>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        loading={loading}
        size="middle"
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
        }}
        pagination={{ total, current: page, pageSize, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'], onChange: (p, ps) => { setPage(p); setPageSize(ps) } }}
        scroll={{ x: isChatgptPlatform ? 1430 : 1250 }}
        onRow={(record) => ({
          onDoubleClick: () => {
            setCurrentAccount(record)
            setDetailModalOpen(true)
          },
        })}
      />

      <Modal
        title={`注册 ${currentPlatform}`}
        open={registerModalOpen}
        onCancel={() => { setRegisterModalOpen(false); setTaskId(null); registerForm.resetFields(); }}
        footer={null}
        width={500}
        maskClosable={false}
      >
        {!taskId ? (
          <Form form={registerForm} layout="vertical" onFinish={handleRegister}>
            <Form.Item name="count" label="注册数量" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} max={MAX_REGISTER_COUNT} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} max={MAX_REGISTER_CONCURRENCY} />
            </Form.Item>
            <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" initialValue={0}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
            </Form.Item>
            <Form.Item
              name="register_retry_times"
              label="失败重试轮数"
              initialValue={DEFAULT_REGISTER_RETRY_TIMES}
              tooltip="整条流程失败后自动重开一轮（换新邮箱 / 号码 / 会话）；0 表示失败即止。与接码的「最多换号」是两回事。"
            >
              <InputNumber min={0} max={10} precision={0} style={{ width: '100%' }} placeholder="1" />
            </Form.Item>
            {currentPlatform === 'chatgpt' && (
              <>
                <Form.Item label="注册方式">
                  <ChatGPTRegisterFlowSelect
                    flow={chatgptRegisterFlow}
                    onChange={setChatgptRegisterFlow}
                  />
                </Form.Item>
                <Form.Item label="ChatGPT Token 方案">
                  <ChatGPTRegistrationModeSwitch
                    mode={chatgptRegistrationMode}
                    onChange={setChatgptRegistrationMode}
                  />
                </Form.Item>
                <Form.Item label="绑定 2FA">
                  <ChatGPTBind2faSwitch
                    enabled={chatgptBind2fa}
                    onChange={setChatgptBind2fa}
                  />
                </Form.Item>
              </>
            )}
            <Form.Item>
              <Button type="primary" htmlType="submit" block loading={registerLoading}>
                开始注册
              </Button>
            </Form.Item>
          </Form>
        ) : (
          <TaskLogPanel taskId={taskId} onDone={() => { load(); }} />
        )}
      </Modal>

      <Modal
        title="批量补 RT"
        open={backfillRtModalOpen}
        onCancel={closeBackfillRtModal}
        footer={null}
        width={backfillRtTaskId ? 720 : 520}
        maskClosable={false}
      >
        {!backfillRtTaskId ? (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={
                selectedRowKeys.length > 0
                  ? `处理所选 ${selectedRowKeys.length} 个账号`
                  : `处理当前筛选的 ${total} 个账号（本页缺 RT ${missingRtCount} 个）`
              }
              description="先复用库里的会话换 refresh_token；会话失效则重新登录（可能要收验证码）。"
            />
            <Form form={backfillRtForm} layout="vertical" onFinish={handleBackfillRt}>
              <Form.Item
                name="only_missing_rt"
                label="只补缺 RT 的账号"
                initialValue={true}
                valuePropName="checked"
                extra="关掉会给已有 RT 的账号也重新换一次，没必要时别开"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="allow_login"
                label="会话失效时用邮箱密码重登"
                initialValue={true}
                valuePropName="checked"
                extra="关掉则只尝试复用会话，快但成功率低"
              >
                <Switch />
              </Form.Item>
              <Form.Item name="concurrency" label="并发数" initialValue={1}>
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="delay_seconds"
                label="每个账号间隔(秒)"
                initialValue={5}
                extra="连续打授权链容易触发风控，建议留几秒"
              >
                <InputNumber min={0} precision={1} step={1} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" htmlType="submit" block loading={backfillRtLoading}>
                  开始补 RT
                </Button>
              </Form.Item>
            </Form>
          </>
        ) : (
          <TaskLogPanel taskId={backfillRtTaskId} kind="backfill_rt" onDone={() => { load() }} />
        )}
      </Modal>

      <Modal
        title="手动新增账号"
        open={addModalOpen}
        onCancel={() => { setAddModalOpen(false); addForm.resetFields(); }}
        onOk={handleAdd}
        okText="确定"
        cancelText="取消"
        maskClosable={false}
      >
        <Form form={addForm} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="token" label={currentPlatform === 'grok' ? 'SSO' : 'Token'}>
            <Input />
          </Form.Item>
          <Form.Item name="cashier_url" label="试用链接">
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      <AccountExportModal
        open={exportModalOpen}
        onClose={() => setExportModalOpen(false)}
        filters={{
          platform: currentPlatform,
          email: search,
          status: filterStatus,
          plus_status: filterPlusStatus,
          created_at_start: createdAtStart,
          created_at_end: createdAtEnd,
        }}
        selectedIds={Array.from(selectedRowKeys)
          .map((value) => Number(value))
          .filter((value) => Number.isInteger(value) && value > 0)}
        filteredTotal={total}
      />

      <Modal
        title="批量导入"
        open={importModalOpen}
        onCancel={() => { setImportModalOpen(false); setImportText(''); }}
        onOk={handleImport}
        okText="确定"
        cancelText="取消"
        confirmLoading={importLoading}
        maskClosable={false}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Segmented
            value={importFormat}
            onChange={(value) => setImportFormat(value as 'text' | 'json')}
            options={[
              { value: 'text', label: '文本（email 密码）' },
              { value: 'json', label: 'JSON（全字段）' },
            ]}
          />
          <p style={{ margin: 0, fontSize: 12, color: 'var(--text-muted)' }}>
            {importFormat === 'json' ? (
              <>
                粘贴<strong>导出的 JSON 数组</strong>（含 AT / RT / 2FA / 状态等全字段）。
                邮箱为唯一键，已存在的账号按字段合并更新 —— 空字段不会覆盖已有值。
              </>
            ) : (
              <>
                每行格式: <code style={{ background: 'var(--bg-subtle)', padding: '2px 4px', borderRadius: 4 }}>email password [cashier_url]</code>
              </>
            )}
          </p>
          <Input.TextArea
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            rows={8}
            style={{ fontFamily: 'monospace' }}
          />
        </Space>
      </Modal>

      <Modal
        title="账号详情"
        open={detailModalOpen}
        onCancel={() => setDetailModalOpen(false)}
        onOk={handleDetailSave}
        okText="保存"
        cancelText="取消"
        maskClosable={false}
        width={760}
        styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
      >
        {currentAccount && (
          <>
            <Form form={detailForm} layout="vertical" initialValues={currentAccount}>
              <Form.Item name="status" label="状态">
                <Select
                  options={[
                    { value: 'registered', label: '已注册' },
                    { value: 'expired', label: '已过期' },
                    { value: 'invalid', label: '已失效' },
                    { value: 'banned', label: '已封禁' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="token" label={currentPlatform === 'grok' ? 'SSO（token 列）' : 'Access Token'}>
                <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
              </Form.Item>
            </Form>
            {(() => {
              // AT 生成时间 / 到期时间（从 JWT 的 iat / exp 解出，纯前端计算）。
              // 取 AT 走 getAccessToken：token 列与 extra.access_token 都看。
              const at = atLifecycleMeta(getAccessToken(currentAccount))
              if (at.status === 'invalid' && !at.issuedText && !at.expiresText) return null
              return (
                <div style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>
                    AT 有效期 <Tag color={at.color} style={{ marginInlineStart: 4 }}>{at.label}</Tag>
                  </div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {at.issuedText ? `生成于 ${at.issuedText}` : '生成时间未知'}
                    {at.expiresText ? ` · 到期 ${at.expiresText}` : ''}
                    {at.remainingText ? ` · ${at.remainingText}` : ''}
                  </Text>
                </div>
              )
            })()}
            {(() => {
              const rt = getRefreshToken(currentAccount)
              if (!rt) return null
              return (
                <div style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>Refresh Token</div>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'flex-start',
                      gap: 8,
                      background: token.colorFillAlter,
                      border: `1px solid ${token.colorBorder}`,
                      borderRadius: token.borderRadius,
                      padding: '8px 10px',
                    }}
                  >
                    <Text
                      style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                      copyable={{ text: rt, tooltips: ['复制 RT', '已复制'] }}
                    >
                      {rt}
                    </Text>
                  </div>
                </div>
              )
            })()}
            {currentAccount.totpSecret ? (
              <div style={{ marginTop: 8 }}>
                <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>TOTP 2FA 密钥</div>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 8,
                    background: token.colorFillAlter,
                    border: `1px solid ${token.colorBorder}`,
                    borderRadius: token.borderRadius,
                    padding: '8px 10px',
                  }}
                >
                  <Text
                    style={{ fontFamily: 'monospace', fontSize: 12, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                    copyable={{ text: currentAccount.totpSecret, tooltips: ['复制密钥', '已复制'] }}
                  >
                    {currentAccount.totpSecret}
                  </Text>
                </div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  在验证器 App 里选「手动输入密钥」，账户名填 {currentAccount.email}。
                </Text>
              </div>
            ) : null}
            {currentAccount.registerProxy ? (
              <DetailSection title="注册代理">
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 8,
                    background: token.colorFillAlter,
                    border: `1px solid ${token.colorBorder}`,
                    borderRadius: token.borderRadius,
                    padding: '8px 10px',
                  }}
                >
                  <Text
                    style={{ fontFamily: 'monospace', fontSize: 12, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                    copyable={{ text: currentAccount.registerProxy, tooltips: ['复制代理', '已复制'] }}
                  >
                    {currentAccount.registerProxy}
                  </Text>
                </div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  注册这个账号时用的出口。复用时（测活、补 RT、绑 2FA）优先回到它 ——
                  同一账号反复换 IP 容易被上游当成异常登录。原代理不可用时会自动换成
                  新的并更新这里。
                </Text>
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="本地真实状态">
                {currentAccount.chatgptLocal && Object.keys(currentAccount.chatgptLocal).length > 0 ? (
                  <LocalProbeSummary probe={currentAccount.chatgptLocal} />
                ) : (
                  <Text type="secondary">尚未探测。可在操作菜单中点击“探测本地状态”。</Text>
                )}
              </DetailSection>
            ) : null}
          </>
        )}
      </Modal>
    </div>
  )
}
