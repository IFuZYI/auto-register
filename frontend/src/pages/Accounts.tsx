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
  accountStatusMeta,
  authStateMeta,
  codexStateMeta,
  plusTrialMeta,
  planMeta,
  atLifecycleMeta,
  atListSummary,
  atSummaryFromClaims,
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
  Grid,
} from 'antd'
import type { MenuProps } from 'antd'
import { withDataLabels } from '@/lib/tableCards'
import {
  MedicineBoxOutlined,
  ReloadOutlined,
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  DownloadOutlined,
  UploadOutlined,
  CloudUploadOutlined,
  MoreOutlined,
  DeleteOutlined,
  SyncOutlined,
  KeyOutlined,
  FileAddOutlined,
  SearchOutlined,
  ClearOutlined,
} from '@ant-design/icons'
import { AccountExportModal } from '@/components/AccountExportModal'
import { ChatGPTBind2faSwitch } from '@/components/ChatGPTBind2faSwitch'
import { ChatGPTRegisterFlowSelect } from '@/components/ChatGPTRegisterFlowSelect'
import { ChatGPTTokenPlanSelect } from '@/components/ChatGPTTokenPlanSelect'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import type { TaskKind } from '@/components/TaskLogPanel'
import { usePersistentChatGPTBind2fa } from '@/hooks/usePersistentChatGPTBind2fa'
import { usePersistentChatGPTRegisterFlow } from '@/hooks/usePersistentChatGPTRegisterFlow'
import { usePersistentChatGPTTokenPlan } from '@/hooks/usePersistentChatGPTTokenPlan'
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

// 纯前端动作，不发请求，所以不和后端的 action id 抢命名空间
const COPY_TOTP_ACTION_ID = '__copy_totp_secret'

// 这些动作要跑几十秒的协议链（还可能停下来等一封验证码），同步等只能看见一个
// 转圈。改走后台任务，和注册一样用日志弹窗把每一步显示出来。

function ActionMenu({ acc, onRefresh, actions, platform }: { acc: any; onRefresh: () => void; actions: any[]; platform: string }) {
  const [runningActionId, setRunningActionId] = useState<string | null>(null)
  const [taskModalTitle, setTaskModalTitle] = useState('')
  const [taskModalKind, setTaskModalKind] = useState<TaskKind>('backfill_rt')
  const [actionTaskId, setActionTaskId] = useState<string | null>(null)

  /**
   * 跑一个动作 —— **一律作为任务**。
   *
   * 用户要求（2026-10-28）：「所有作业都是任务，包括刷新token，检测状态，
   * 包括单个的也是。都会弹出任务日志窗口，在任务日志中显示。」
   *
   * 所以这里不再有「哪些动作走任务」的白名单：单个账号的动作也走通用任务
   * 端点 `/tasks/batch-action`（account_ids 只有一个），跑完弹出任务日志
   * 窗口。同步端点 `/actions/{platform}/{id}/{actionId}` 只保留给后端内部
   * 与老客户端，前端不再调用。
   *
   * 为什么不用 Toast 报结果：任务可能跑几十秒（刷新要打上游、检测要探测），
   * Toast 一闪而过，用户既看不到过程也没法在任务页里追踪 —— 任务日志窗口
   * 才是正确的呈现。
   */
  const runAsTask = async (actionId: string, actionLabel: string) => {
    setRunningActionId(actionId)
    try {
      const created = await apiFetch('/tasks/batch-action', {
        method: 'POST',
        body: JSON.stringify({
          platform: acc.platform || platform,
          action_id: actionId,
          action_label: actionLabel,
          params: {},
          account_ids: [acc.id],
          concurrency: 1,
          delay_seconds: 0,
        }),
      })
      setTaskModalTitle(`${actionLabel} - ${acc.email}`)
      // 通用动作统一用 batch_action 的日志视图
      setTaskModalKind('batch_action')
      setActionTaskId(created.task_id)
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e)
      // 只有「任务都没建成」才用 Toast —— 这是请求层的失败，没有任务日志可看
      message.error(`${actionLabel}启动失败: ${detail}`)
    } finally {
      setRunningActionId(null)
    }
  }

  const handleAction = async (actionId: string) => {
    if (runningActionId) return
    const actionLabel = actions.find((item) => item.id === actionId)?.label || actionId

    // 「复制 2FA 密钥」是纯前端剪贴板操作，不是作业 —— 保持原样。
    if (actionId === COPY_TOTP_ACTION_ID) {
      const secret = String(acc.totpSecret || '')
      if (!secret) {
        message.warning('该账号没有 2FA 密钥（列表行不含密钥，请打开详情再复制）')
        return
      }
      try {
        await navigator.clipboard.writeText(secret)
        message.success('2FA 密钥已复制')
      } catch {
        message.error('复制失败，请在账号详情里手动复制')
      }
      return
    }

    // 其余全部作为任务跑。
    await runAsTask(actionId, actionLabel)
  }

  const menuItems: MenuProps['items'] = [
    ...(acc.totpSecret
      ? [{ key: COPY_TOTP_ACTION_ID, label: '复制 2FA 密钥' }]
      : []),  // 只有详情行带正文（列表行没有），所以这里仍用正文判断
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
  // 重置筛选用：日期控件是不受控的（antd 要 Dayjs 对象），换 key 重挂载即可清空。
  const [filterEpoch, setFilterEpoch] = useState(0)
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])
  const [recoverLoading, setRecoverLoading] = useState(false)

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  // 「上传到面板」：把筛选/勾选的账号推送到指定面板。
  //
  // 需求变更（2026-10-26）：此前上传按钮被**刻意**移到了「面板管理」页
  // （那里的注释写着「上传就在同一页发」）。用户现在要求回到账号页，因为
  // 他要在**账号的上下文里**按筛选挑号上传。面板页保留对比与双向更新。
  const [panelUploadOpen, setPanelUploadOpen] = useState(false)
  const [panelUploadTarget, setPanelUploadTarget] = useState('chatgpt2api')
  const [panelUploadRunning, setPanelUploadRunning] = useState(false)
  const [exportModalOpen, setExportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [currentAccount, setCurrentAccount] = useState<any>(null)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const { plan: chatgptTokenPlan, setPlan: setChatgptTokenPlan } =
    usePersistentChatGPTTokenPlan()
  const { registerFlow: chatgptRegisterFlow, setRegisterFlow: setChatgptRegisterFlow } =
    usePersistentChatGPTRegisterFlow()
  const { bind2fa: chatgptBind2fa, setBind2fa: setChatgptBind2fa } =
    usePersistentChatGPTBind2fa()
  const [importText, setImportText] = useState('')
  // 导入格式：文本（email 密码）或 JSON（导出格式全字段）
  const [importFormat, setImportFormat] = useState<'text' | 'json'>('text')
  /**
   * 文本导入的**来源格式**（导出格式 id）。
   *
   * `----` 分隔的行靠它决定「第 N 列是哪个字段」。默认 `email_pw` ——
   * 导出侧最常见的那个，让「导出 → 粘贴 → 导入」无需额外选择。
   * 解析器本身拒绝猜（列数歧义会抛错），所以这里必须显式给一个。
   */
  const [importSourceFormat, setImportSourceFormat] = useState('email_pw')
  const [exportFormats, setExportFormats] = useState<{ id: string; label: string }[]>([])
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
  const [refreshTokenModalOpen, setRefreshTokenModalOpen] = useState(false)
  const [refreshTokenLoading, setRefreshTokenLoading] = useState(false)
  const [refreshTokenTaskId, setRefreshTokenTaskId] = useState<string | null>(null)
  const [refreshTokenForm] = Form.useForm()

  useEffect(() => {
    if (platform) setCurrentPlatform(platform)
  }, [platform])

  // 导出格式清单（导入侧的「来源格式」也用它）。
  // 挂一次即可 —— 格式由后端注册表决定，运行期不变。
  useEffect(() => {
    let alive = true
    apiFetch('/accounts/export-formats')
      .then((data) => {
        const list = Array.isArray(data) ? data : (data as { formats?: unknown[] })?.formats
        if (!alive || !Array.isArray(list)) return
        setExportFormats(
          (list as { id?: string; label?: string }[])
            .filter((f) => f?.id)
            .map((f) => ({ id: String(f.id), label: String(f.label || f.id) })),
        )
      })
      .catch(() => {
        // 拿不到就退回内置的那一个默认格式（导入仍可用）
        setExportFormats([{ id: 'email_pw', label: '邮箱----密码' }])
      })
    return () => { alive = false }
  }, [])

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

  /**
   * 列表里是否「有 RT」。
   *
   * 后端列表响应把 `refresh_token` 换成了布尔 `has_refresh_token`（正文占
   * `extra_json` 的大头，列表页只用它判有无 —— 见 `api/accounts.py` 的
   * `_slim_extra_for_list`）。兼容旧值：老响应里可能还是明文。
   */
  //: 列表行不带 token 正文时的占位串。**只用于判有无**，绝不可展示/复制。
  const HAS_TOKEN_PLACEHOLDER = '__has_token__'

  /**
   * 读一行账号的 `extra`（列表行与详情行都能用）。
   *
   * 列表响应把凭证正文换成了 `has_*` 布尔（见 `api/accounts.py` 的
   * `_slim_extra_for_list`），详情端点给全量。所以**判断「有没有」要读布尔，
   * 展示/复制才读正文** —— 混着用就会出现「明明绑了却显示没有」。
   */
  const rowExtra = (record: { extra_json?: string } | null | undefined): Record<string, unknown> => {
    try {
      return JSON.parse(record?.extra_json || '{}') || {}
    } catch {
      return {}
    }
  }

  /** 这一行有没有绑 2FA（列表读布尔，详情读正文）。 */
  const hasTotpSecret = (record: { extra_json?: string } | null | undefined): boolean => {
    const extra = rowExtra(record)
    return Boolean(extra.totp_secret) || extra.has_totp_secret === true
  }

  const getRefreshToken = (record: any): string => {
    try {
      const extra = JSON.parse(record.extra_json || '{}')
      // 正文优先：详情端点给的是全量，能拿到真值就用真值。
      const raw = extra.refresh_token || extra.refreshToken || ''
      if (raw) return String(raw)
      // 列表行没有正文，只有布尔 —— 返回占位串仅用于「有没有」判断，
      // **不可**拿去展示或复制（抽屉里用的是详情数据，不会走到这里）。
      return extra.has_refresh_token === true ? HAS_TOKEN_PLACEHOLDER : ''
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
  const getAccessToken = (record: { token?: string; has_token?: boolean; extra_json?: string } | null | undefined): string => {
    try {
      const extra = JSON.parse(record?.extra_json || '{}')
      // 正文优先（详情端点给全量）；列表行只有布尔，返回占位串仅供判有无。
      const fromExtra = String(extra.access_token || '').trim()
      if (fromExtra) return fromExtra
      if (extra.has_access_token === true) return HAS_TOKEN_PLACEHOLDER
    } catch {
      // ignore，退回列
    }
    if (currentPlatform === 'grok') return ''
    // 列表已不下发 token 正文（只给 has_token），正文在详情端点里
    return record?.has_token ? HAS_TOKEN_PLACEHOLDER : ''
  }

  /**
   * 打开详情抽屉：**先拉全量**再用。
   *
   * 列表响应已把 token 正文裁掉（`session_token`/`access_token` 等换成布尔，
   * 见 `api/accounts.py`）。直接把列表行塞进抽屉，会让 AT 有效期算不出来、
   * RT 显示成占位串、甚至把占位串复制进剪贴板（复审实测）。
   * 详情端点仍返回全量，所以打开时补一次。
   */
  const openDetail = async (record: { id: string; platform?: string }) => {
    setDetailModalOpen(true)
    setCurrentAccount(normalizeAccount(record))  // 先用列表行占位，避免抽屉空白
    try {
      const full = await apiFetch(
        `/accounts/${record.id}?platform=${encodeURIComponent(record.platform || currentPlatform)}`,
      )
      // **必须归一化**：详情弹窗读的是驼峰字段（`totpSecret` /
      // `registerProxy` / `chatgptLocal`），而它们由 `normalizeAccount`
      // 从 `extra_json` 解出来。直接塞原始响应 → 那些字段全是 undefined →
      // 详情里看不到 2FA 密钥、代理、本地状态（实测 2026-10-28）。
      if (full && typeof full === 'object') setCurrentAccount(normalizeAccount(full))
    } catch {
      // 拉不到就继续用列表行：抽屉里其它字段仍可用，只是 token 部分为空
    }
  }

  const handleDelete = async (id: string) => {
    // 带 platform：账号分散在平台库与默认库，带平台才能定位到正确的那个库
    await apiFetch(`/accounts/${id}?platform=${encodeURIComponent(currentPlatform)}`, {
      method: 'DELETE',
    })
    message.success('删除成功')
    load()
  }

  const handleBatchDelete = () => {
    if (selectedRowKeys.length === 0) return
    // 单次最多删 1000 条且**不可恢复**，原来一点即删（全量检测 F-04）。
    Modal.confirm({
      title: `确认删除选中的 ${selectedRowKeys.length} 个账号？`,
      content: '删除后无法恢复（账号凭据、任务历史里的关联记录都会一起消失）。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        await apiFetch('/accounts/batch-delete', {
          method: 'POST',
          body: JSON.stringify({ ids: Array.from(selectedRowKeys), platform: currentPlatform }),
        })
        message.success('批量删除成功')
        setSelectedRowKeys([])
        load()
      },
    })
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
        body: JSON.stringify({
          platform: currentPlatform,
          lines,
          format: importFormat,
          // 文本模式才带（JSON 模式走全字段，不需要列映射）
          source_format: importFormat === 'text' ? importSourceFormat : '',
        }),
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
        icloud_remote_account_id: cfg.icloud_remote_account_id,
        icloud_remote_label: cfg.icloud_remote_label,
        icloud_remote_note: cfg.icloud_remote_note,
      }
      const chatgptRegistrationRequestAdapter =
        buildChatGPTRegistrationRequestAdapter(
          currentPlatform,
          chatgptTokenPlan,
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
        // 账号主键是 UUID 字符串。原来 `Number(uuid)` → NaN → 被下面的
        // `Number.isInteger` 全部过滤掉，于是「同步/补RT/刷新/上传（所选）」
        // 永远提示「请先选择」、导出退化成「全部筛选结果」（全量检测 F-01/F-02）。
        .map((value) => String(value))
        .filter((value) => value.length > 0)

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
      if (createdAtStart) body.created_at_start = createdAtStart
      if (createdAtEnd) body.created_at_end = createdAtEnd
    }

    setStatusSyncLoading(loadingKey)

    // 批量动作跑成**任务**（用户要求「整个项目大部分功能都统一基于任务」）：
    // 每个号都要打一次上游请求，几百个号同步跑会把请求挂住几分钟且界面无
    // 反馈。走通用任务端点后能在「任务运行」页看实时日志、可停止/跳过。
    //
    // 用**通用**端点（`/tasks/batch-action`）而不是逐个动作写专用端点：
    // 新增动作时前端不需要再改。
    {
      try {
        const created = (await apiFetch('/tasks/batch-action', {
          method: 'POST',
          body: JSON.stringify({
            ...body,
            platform: currentPlatform,
            action_id: actionId,
            action_label: `${scopeLabel}${actionLabel}`,
          }),
        })) as { task_id: string; total: number }
        message.success({
          content: `${scopeLabel}${actionLabel}已开始（${created.total} 个），可在「任务运行」查看进度`,
          key: toastKey,
        })
        await load()
        return
      } catch (e: unknown) {
        message.error({
          content: `${actionLabel}失败: ${e instanceof Error ? e.message : e}`,
          key: toastKey,
        })
        return
      } finally {
        setStatusSyncLoading('')
      }
    }

  }

  const missingRtCount = accounts.filter((item) => !getRefreshToken(item)).length

  /** 可选的上传目标面板 → 对应的动作 id（与后端 `_UPLOAD_ACTIONS` 同名）。 */
  const PANEL_UPLOAD_TARGETS = [
    { value: 'chatgpt2api', label: 'chatgpt2api' },
    { value: 'cpa', label: 'CPA 面板' },
    { value: 'sub2api', label: 'Sub2API' },
  ] as const

  const handlePanelUpload = async () => {
    // 与「批量补 RT」同一套 scope 约定：勾了就传勾选的，没勾就传筛选结果。
    const scope = selectedRowKeys.length > 0 ? 'selected' : 'all'
    const actionId = `upload_${panelUploadTarget}`

    const body: Record<string, unknown> = {}
    if (scope === 'selected') {
      body.account_ids = Array.from(selectedRowKeys)
        // 账号主键是 UUID 字符串。原来 `Number(uuid)` → NaN → 被下面的
        // `Number.isInteger` 全部过滤掉，于是「同步/补RT/刷新/上传（所选）」
        // 永远提示「请先选择」、导出退化成「全部筛选结果」（全量检测 F-01/F-02）。
        .map((value) => String(value))
        .filter((value) => value.length > 0)
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
      // 日期筛选**必须转发**：列表上的计数包含它，请求不带的话「处理数会大于
      // 显示数」（本仓既有约定，见 tests/test_batch_scope_consistency.py）。
      if (createdAtStart) body.created_at_start = createdAtStart
      if (createdAtEnd) body.created_at_end = createdAtEnd
    }

    setPanelUploadRunning(true)
    try {
      // 上传也跑成**任务**：一次可能推几百个账号，同步跑会把请求挂住几分钟
      // 且界面无反馈。走通用任务端点后可在「任务运行」看实时日志、可停止。
      const created = (await apiFetch('/tasks/batch-action', {
        method: 'POST',
        body: JSON.stringify({
          ...body,
          platform: currentPlatform,
          action_id: actionId,
          action_label: `上传到 ${panelUploadTarget}`,
        }),
      })) as { task_id: string; total: number }
      message.success(
        `已开始上传 ${created.total} 个账号到 ${panelUploadTarget}，可在「任务运行」查看进度`,
      )
      setPanelUploadOpen(false)
      load()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '上传失败')
    } finally {
      setPanelUploadRunning(false)
    }
  }

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
        // 账号主键是 UUID 字符串。原来 `Number(uuid)` → NaN → 被下面的
        // `Number.isInteger` 全部过滤掉，于是「同步/补RT/刷新/上传（所选）」
        // 永远提示「请先选择」、导出退化成「全部筛选结果」（全量检测 F-01/F-02）。
        .map((value) => String(value))
        .filter((value) => value.length > 0)
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
      if (createdAtStart) body.created_at_start = createdAtStart
      if (createdAtEnd) body.created_at_end = createdAtEnd
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

  const handleRefreshToken = async () => {
    const values = await refreshTokenForm.validateFields()
    const scope = selectedRowKeys.length > 0 ? 'selected' : 'all'

    const body: Record<string, unknown> = {
      concurrency: Number(values.concurrency) || 1,
      delay_seconds: Number(values.delay_seconds) || 0,
    }

    if (scope === 'selected') {
      body.account_ids = Array.from(selectedRowKeys)
        // 账号主键是 UUID 字符串。原来 `Number(uuid)` → NaN → 被下面的
        // `Number.isInteger` 全部过滤掉，于是「同步/补RT/刷新/上传（所选）」
        // 永远提示「请先选择」、导出退化成「全部筛选结果」（全量检测 F-01/F-02）。
        .map((value) => String(value))
        .filter((value) => value.length > 0)
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
      if (createdAtStart) body.created_at_start = createdAtStart
      if (createdAtEnd) body.created_at_end = createdAtEnd
    }

    setRefreshTokenLoading(true)
    try {
      const result = await apiFetch('/tasks/refresh-token', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      setRefreshTokenTaskId(result.task_id)
      message.success(`已开始刷新 ${result.total} 个账号的 Token`)
    } catch (e) {
      message.error(`刷新 Token 启动失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRefreshTokenLoading(false)
    }
  }

  /**
   * 「恢复异常账号」：不用先勾选 —— 按状态自动挑 invalid/expired/limited，
   * 逐个试一次恢复链，救不回来的累计到退避计数，达上限自动落「禁用」。
   * 先拉预览让用户看清会动哪些号，再执行（不可逆的批量动作都要先预览）。
   */
  const startRecover = async () => {
    setRecoverLoading(true)
    try {
      const preview = await apiFetch('/tasks/recover/candidates')
      const recoverable = Number(preview.recoverable || 0)
      if (recoverable === 0) {
        message.info(
          preview.exhausted > 0
            ? `没有可恢复的账号（${preview.exhausted} 个已达重试上限，已标注禁用）`
            : '没有需要恢复的账号',
        )
        return
      }
      const byStatus = preview.by_status || {}
      const detail = Object.entries(byStatus)
        .map(([k, v]) => `${accountStatusMeta(k).label} ${v}`)
        .join(' / ')
      Modal.confirm({
        title: `确认尝试恢复 ${recoverable} 个异常账号？`,
        content: (
          <div>
            <p>{detail}</p>
            <p style={{ marginTop: 8, fontSize: 12, color: '#888' }}>
              逐个走刷新链（session → OAuth → 登录兜底）。失败的会累计重试次数，
              连续 3 次失败后自动标注为「禁用」并移出恢复队列。
              {preview.exhausted > 0 ? `（另有 ${preview.exhausted} 个已达上限，本次跳过）` : ''}
            </p>
          </div>
        ),
        okText: '开始恢复',
        cancelText: '取消',
        onOk: async () => {
          try {
            const result = await apiFetch('/tasks/recover', {
              method: 'POST',
              body: JSON.stringify({}),
            })
            message.success(`已开始恢复 ${result.total} 个账号`)
            load()
          } catch (e: unknown) {
            message.error(`恢复启动失败: ${e instanceof Error ? e.message : String(e)}`)
          }
        },
      })
    } catch (e: unknown) {
      message.error(`读取可恢复账号失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRecoverLoading(false)
    }
  }

  const closeRefreshTokenModal = () => {
    setRefreshTokenModalOpen(false)
    setRefreshTokenTaskId(null)
    refreshTokenForm.resetFields()
  }

  const getStatusSyncScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  // 当前生效的筛选条件数（给「重置 N 项筛选」用）。空串 = 没筛。
  const activeFilterCount =
    (search ? 1 : 0) +
    (filterStatus ? 1 : 0) +
    (filterPlusStatus ? 1 : 0) +
    (createdAtStart ? 1 : 0) +
    (createdAtEnd ? 1 : 0)

  // 窄屏（< md）把表格折叠成卡片：8 列在 390px 上只能横向滚，而 sticky 的
  // 操作列会盖住右侧内容（实测容器 358px、表格 1042px，凭据列整个被盖住）。
  const screens = Grid.useBreakpoint()
  const narrow = screens.md === false

  const isChatgptPlatform = currentPlatform === 'chatgpt'
  const monospaceStyle: React.CSSProperties = {
    fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    fontSize: 12,
  }
  /**
   * 表格密度：一行两行字，不套框。
   *
   * 改造前每个单元格都是「带边框 + 底色的小面板」（`compactPanelStyle`），
   * 一行里叠三个，实测行高 **104~110px** —— 667 个账号的表格一屏只看得到
   * 7 行。用户要的是「扫一遍哪些号有问题」，不是逐张读卡片。
   *
   * 现在：一格最多两行、行距 2px、无边框，行高 ~56px（一屏 ~13 行）。
   * 分组靠**列**（一个列一个语义），不靠框。
   */
  const cellStackStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    minWidth: 0,
  }
  const metaLineStyle: React.CSSProperties = {
    fontSize: 11,
    lineHeight: '16px',
    color: token.colorTextTertiary,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  }
  /**
   * 「有 / 无」型事实：有的亮、无的淡。
   *
   * 不用 Tag —— 一行挂七个彩色块（实测）就是这一页最大的噪音源：
   * 七个颜色互相抢注意力，真正要紧的「账号状态」反而看不出来。
   * 三个词（密码 / RT / 2FA）合成一列，回答的是同一个问题：这个号还能不能救。
   */
  const factStyle = (on: boolean): React.CSSProperties => ({
    fontSize: 11,
    lineHeight: '16px',
    // 有 = 中性次要色，无 = 极淡。**不用绿色** —— 一行里「状态」「AT 到期」
    // 「凭据」三处同时发绿，等于告诉眼睛「三个都一样重要」。
    // 这一列回答的是「有没有」，不是「好不好」。
    color: on ? token.colorTextSecondary : token.colorTextQuaternary,
    // 「没有」再加一道**非颜色**的线索：删除线。只靠明暗差，色弱用户
    // 分不出「密码」和「没有密码」—— 而这列是判断「这号还能不能救」的
    // 依据之一，不能只靠颜色编码。
    textDecoration: on ? 'none' : 'line-through',
    whiteSpace: 'nowrap',
  })

  const columns: any[] = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 250,
      render: (text: string, record: any) => (
        <div style={cellStackStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 2, minWidth: 0 }}>
            <Text
              style={{ ...monospaceStyle, flex: 1, minWidth: 0, whiteSpace: 'nowrap' }}
              ellipsis={{ tooltip: text }}
            >
              {text}
            </Text>
            <Button
              type="text"
              size="small"
              aria-label="复制邮箱"
              style={{ flex: '0 0 auto', width: 20, height: 20, minWidth: 20 }}
              icon={<CopyOutlined />}
              onClick={() => copyText(text)}
            />
          </div>
          <Text style={metaLineStyle} ellipsis={{ tooltip: record.user_id || `账号 #${record.id}` }}>
            {record.user_id ? `UID ${record.user_id}` : `#${record.id}`}
          </Text>
        </div>
      ),
    },
    {
      // 密码 / RT / 2FA 合成一列：三件事回答同一个问题 ——「这个号还能不能救」。
      // 原来占三列（密码、RT、本地状态里的 2FA 徽章），每列一个彩色块。
      // 列表响应本来就不带凭据正文（安全加固），所以这里只表达「有没有」，
      // 正文的复制入口在详情抽屉里（tooltip 里写明）。
      title: '凭据',
      key: 'credentials',
      width: 120,
      // 窄屏折叠成卡片时隐藏：凭据是「这号能不能救」的细节，详情里有；
      // 卡片上优先放「是谁、什么状态、什么时候到期」。
      responsive: ['md'],
      render: (_: any, record: any) => {
        const hasPwd = Boolean(record.has_password)
        const hasRt = Boolean(getRefreshToken(record))
        const hasTotp = hasTotpSecret(record)
        return (
          <Tooltip
            title={
              `密码 ${hasPwd ? '有' : '无'}；RT ${hasRt ? '有' : '无'}；2FA ${hasTotp ? '已绑' : '未绑'}。` +
              '正文在「详情」里复制。'
            }
          >
            <Space
              size={9}
              style={{ whiteSpace: 'nowrap' }}
              // 读屏用户拿不到颜色/删除线 —— 用 aria-label 把三种状态说全。
              aria-label={`密码${hasPwd ? '有' : '无'}，RT${hasRt ? '有' : '无'}，2FA${hasTotp ? '已绑' : '未绑'}`}
            >
              <span style={factStyle(hasPwd)}>密码</span>
              <span style={factStyle(hasRt)}>RT</span>
              <span style={factStyle(hasTotp)}>2FA</span>
            </Space>
          </Tooltip>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 88,
      render: (status: string) => {
        // 显示中文语义标签（正常/过期/失效/禁用）—— 此前直接渲染英文原值。
        // 一行只留这**一个** Tag：它才是「要不要处理这个号」的判据。
        const meta = accountStatusMeta(status)
        return (
          <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
            {meta.label}
          </Tag>
        )
      },
    },
    {
      // AT 有效期：ChatGPT 与 Grok 的 AT 都是 JWT（都带 iat/exp），
      // 两个平台的列表都要显示 —— 详情弹窗本来就是共享的，列表不该分家。
      //
      // 日期只显示 `MM-DD`：完整年份在这张表里没有信息量（都在今年），
      // 而 `到期 2026-11-09` 需要 95px、该列内容宽只有 ~92px，实测被截断成
      // 「到期 2026-11…」。完整时间与剩余时长在 tooltip 里。
      title: 'AT 到期',
      key: 'at_lifecycle',
      width: 132,
      render: (_: unknown, record: { token?: string; has_token?: boolean; extra_json?: string }) => {
        const parsed = (() => {
          try {
            return JSON.parse(record.extra_json || '{}')
          } catch {
            return {}
          }
        })()
        const at = atSummaryFromClaims(parsed) ?? atListSummary(getAccessToken(record))
        const hasDate = Boolean(at.expiresShort || at.remainingText)
        if (at.label === '无 AT' && !hasDate) {
          return <Text style={metaLineStyle}>无 AT</Text>
        }
        const shortDate = at.expiresShort ? String(at.expiresShort).slice(5) : ''
        return (
          <Tooltip
            title={[at.tooltip || at.label, at.expiresShort ? `到期 ${at.expiresShort}` : '', at.remainingText]
              .filter(Boolean)
              .join(' · ')}
          >
            <Space size={6} style={{ whiteSpace: 'nowrap' }}>
              <Tag color={at.color} style={{ marginInlineEnd: 0 }}>
                {at.label}
              </Tag>
              {shortDate ? <span style={metaLineStyle}>{shortDate}</span> : null}
            </Space>
          </Tooltip>
        )
      },
    },
  ]

  if (isChatgptPlatform) {
    columns.push({
      // 账号状态：授权状态 + Codex 可用性 + 套餐 + Plus 试用。
      // 原来是两列（本地状态 260px + Plus 试用 140px）、一行四个 Tag。
      // 现在两行小字：第一行「授权状态 + Codex」，第二行「套餐 + Plus」。
      // 名字不叫「账号状态」—— 旁边那列已经叫「状态」了，两个「状态」并排
      // 谁也说不清哪个是账号本身、哪个是探测结论。这列放的是**本地探测**的
      // 结果（认证 / Codex / 套餐 / Plus 试用）。
      title: '探测结果',
      key: 'chatgpt_local_state',
      width: 176,
      responsive: ['md'],
      render: (_: any, record: any) => {
        const auth = record.chatgptLocal?.auth || {}
        const subscription = record.chatgptLocal?.subscription || {}
        const codex = record.chatgptLocal?.codex || {}
        const authMeta = authStateMeta(auth.state)
        const planTag = planMeta(subscription.plan)
        const codexMeta = codexStateMeta(codex.state)
        const check = record.plusCheck || {}
        const plusMeta = plusTrialMeta(check.status)
        const checkedAt = formatCreatedAt(check.checked_at)

        // 认证正常时**不挂 Tag**：它是预期状态，左边的「AT 到期」已经说了
        // 「有效」。每行都挂一个绿块只会把真正异常的号埋掉 ——
        // 只有不正常时才用颜色说话。
        const authAbnormal = Boolean(auth.state) && auth.state !== 'access_token_valid'
        // Plus 试用只在有结论时显示：「未检测」和「Free」都不带来信息，
        // 而「可领首月免费」「封号」才是要动手的。
        const plusWorthShowing = !['', 'unchecked', 'free'].includes(String(check.status || '').toLowerCase())

        return (
          <div style={cellStackStyle}>
            <Space size={6} style={{ whiteSpace: 'nowrap' }}>
              {authAbnormal ? (
                <Tag color={authMeta.color} style={{ marginInlineEnd: 0 }}>
                  {authMeta.label}
                </Tag>
              ) : null}
              <span style={metaLineStyle}>Codex {codexMeta.label}</span>
            </Space>
            <Tooltip
              title={
                `认证 ${authMeta.label}；套餐 ${planTag.label}；Plus 试用 ${plusMeta.label}` +
                (checkedAt.date !== '-' ? `（检测于 ${checkedAt.date} ${checkedAt.time}）` : '') +
                (check.message ? `：${check.message}` : '')
              }
            >
              <Space size={8} style={{ whiteSpace: 'nowrap' }}>
                {planTag.label && planTag.label !== '未知' ? (
                  <span style={metaLineStyle}>套餐 {planTag.label}</span>
                ) : null}
                {plusWorthShowing ? (
                  <span
                    style={{
                      ...metaLineStyle,
                      color:
                        plusMeta.color === 'success'
                          ? token.colorSuccess
                          : plusMeta.color === 'error' || plusMeta.color === 'warning'
                            ? token.colorWarning
                            : token.colorTextTertiary,
                    }}
                  >
                    {plusMeta.label}
                  </span>
                ) : null}
              </Space>
            </Tooltip>
          </div>
        )
      },
    })
  } else {
    columns.push(
      {
        title: '地区',
        dataIndex: 'region',
        key: 'region',
        width: 90,
        render: (text: string) => <span style={metaLineStyle}>{text || '-'}</span>,
      },
      {
        title: '试用链接',
        dataIndex: 'cashier_url',
        key: 'cashier_url',
        width: 92,
        render: (url: string) =>
          url ? (
            <Space size={0}>
              <Button type="text" size="small" aria-label="复制试用链接" icon={<CopyOutlined />} onClick={() => copyText(url)} />
              <Button type="text" size="small" aria-label="打开试用链接" icon={<LinkOutlined />} onClick={() => window.open(url, '_blank')} />
            </Space>
          ) : (
            <span style={metaLineStyle}>-</span>
          ),
      },
    )
  }

  columns.push(
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 108,
      responsive: ['md'],
      render: (text: string) => {
        // 单行 `MM-DD HH:mm`：两行写法（日期一行、时间一行）与 AT 列的
        // `11-09` 不是同一种格式，同一张表里出现两种日期写法会让人多读一遍。
        const formatted = formatCreatedAt(text)
        const short = String(formatted.date || '').replace(/^(\d{2})\/(\d{2})\/(\d{4})$/, '$1-$2')
        // 时间保留 AM/PM：去掉之后 `03:49` 是凌晨还是下午说不清。
        const stamp = [short, formatted.time].filter(Boolean).join(' ')
        return (
          <Tooltip title={`${formatted.date} ${formatted.time}`.trim()}>
            <span style={{ ...metaLineStyle, fontSize: 12 }}>{stamp || '-'}</span>
          </Tooltip>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 132,
      // 所有平台都固定到右侧：窄屏下不固定的话「操作」列会落在视口外，
      // 用户得横向滚动才能点到按钮。
      //
      // ⚠️ `scroll.x` 必须 ≥ 各列宽度之和。改造前写的是 1430，而实际列宽合计
      // 1458 —— 表格内容比声明的宽，sticky 的「操作」列就**压在「注册时间」
      // 上面**（实测重叠 30px，日期被盖住）。改版后合计 1030，这里取 1040。
      fixed: narrow ? undefined : 'right',
      render: (_: any, record: any) => (
        <Space size={2} style={{ whiteSpace: 'nowrap' }}>
          <Button type="link" size="small" style={{ paddingInline: 4 }} onClick={() => { void openDetail(record) }}>
            详情
          </Button>
          <Popconfirm
            title="确认删除该账号吗？"
            onConfirm={() => handleDelete(record.id)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="link" size="small" danger style={{ paddingInline: 4 }}>
              删除
            </Button>
          </Popconfirm>
          <ActionMenu acc={record} onRefresh={load} actions={platformActions} platform={currentPlatform} />
        </Space>
      ),
    },
  )

  // 所有列定义完之后统一挂 `data-label`（窄屏折叠成卡片时当行标签用）。
  // 只在这里包一次：在创建时包、push 后再包，会把 onCell 套三层。
  const tableColumns = withDataLabels(columns)

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
                  }, {
                    key: 'refresh_token',
                    icon: <SyncOutlined />,
                    label: selectedRowKeys.length > 0 ? `刷新 Token (${selectedRowKeys.length})` : '刷新 Token',
                    disabled: total === 0,
                  }, {
                    // 「恢复异常账号」与「刷新 Token」的区别：前者按状态自动挑
                    // （invalid/expired/limited），失败的累计退避、用尽自动落禁用。
                    key: 'recover',
                    icon: <MedicineBoxOutlined />,
                    label: '恢复异常账号',
                    disabled: recoverLoading,
                  }] : []),
                  { key: 'import', icon: <UploadOutlined />, label: '导入' },
                  ...(currentPlatform === 'chatgpt' ? [{
                    key: 'panel_upload',
                    icon: <CloudUploadOutlined />,
                    label: selectedRowKeys.length > 0
                      ? `上传到面板 (${selectedRowKeys.length})`
                      : '上传到面板',
                    disabled: total === 0,
                  }] : []),
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
                  else if (key === 'refresh_token') setRefreshTokenModalOpen(true)
                  else if (key === 'recover') void startRecover()
                  else if (key === 'import') setImportModalOpen(true)
                  else if (key === 'panel_upload') setPanelUploadOpen(true)
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
          // 受控 + onChange 必须成对：只给 `value` 不给 `onChange`，输入会被
          // 立刻重置回 state 里的空串 —— 打字没反应（实测踩过，还以为是浏览器问题）。
          value={search}
          onChange={(e) => setSearch(e.target.value)}
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
          value={filterStatus || undefined}
          style={{ width: 130 }}
          onChange={(v) => { setPage(1); setFilterStatus(v) }}
          options={[
            { value: 'registered', label: '正常' },
            { value: 'expired', label: '过期' },
            { value: 'invalid', label: '失效' },
            { value: 'limited', label: '限流' },
            { value: 'banned', label: '禁用' },
          ]}
        />
        {currentPlatform === 'chatgpt' && (
          <Select
            placeholder="Plus 试用"
            allowClear
            value={filterPlusStatus || undefined}
            style={{ width: 150 }}
            onChange={(v) => { setPage(1); setFilterPlusStatus(v || '') }}
            options={PLUS_TRIAL_FILTERS}
          />
        )}
        <DatePicker
          showTime
          allowClear
          key={`start-${filterEpoch}`}
          placeholder="开始时间"
          style={{ width: 168 }}
          onChange={(value) => { setPage(1); setCreatedAtStart(value ? value.toISOString() : '') }}
        />
        <DatePicker
          showTime
          allowClear
          key={`end-${filterEpoch}`}
          placeholder="结束时间"
          style={{ width: 168 }}
          onChange={(value) => { setPage(1); setCreatedAtEnd(value ? value.toISOString() : '') }}
        />
        {/* 重置只在**确实有筛选**时出现：平时不占版面，有筛选时给一条出路。
            以前五个控件各带 allowClear，但要一个一个点掉 —— 而用户判断
            「我现在看的是不是全部账号」靠的正是「有没有筛选」。 */}
        {activeFilterCount > 0 ? (
          <Button
            type="text"
            icon={<ClearOutlined />}
            onClick={() => {
              setSearch(''); setFilterStatus(''); setFilterPlusStatus('')
              setCreatedAtStart(''); setCreatedAtEnd(''); setPage(1)
              setFilterEpoch((n) => n + 1)
            }}
          >
            重置 {activeFilterCount} 项筛选
          </Button>
        ) : null}
      </div>

      <Table
        rowKey="id"
        className="table-cards"
        columns={tableColumns}
        dataSource={accounts}
        loading={loading}
        size="middle"
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
        }}
        pagination={{ total, current: page, pageSize, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'], onChange: (p, ps) => { setPage(p); setPageSize(ps) } }}
        // 列宽合计：ChatGPT 1046 / Grok 1052（选择列 40 + 各列）。
        // 取 ≥ 合计的值 —— 写小了内容会被裁、滚到底也够不着；改造前写 1430
        // 而实际 1458，sticky 的操作列就压在「注册时间」上面（实测重叠 30px）。
        // `tests/test_frontend_layout_contract.py` 会从源码算列宽和来钉这一条。
        scroll={narrow ? undefined : { x: isChatgptPlatform ? 1050 : 1060 }}
        onRow={(record) => ({
          onDoubleClick: () => {
            void openDetail(record)
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
                  <ChatGPTTokenPlanSelect
                    plan={chatgptTokenPlan}
                    onChange={setChatgptTokenPlan}
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
                tooltip="关掉会给已有 RT 的账号也重新换一次，没必要时别开"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="allow_login"
                label="会话失效时用邮箱密码重登"
                initialValue={true}
                valuePropName="checked"
                tooltip="关掉则只尝试复用会话，快但成功率低"
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
                tooltip="连续打授权链容易触发风控，建议留几秒"
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
        title="批量刷新 Token"
        open={refreshTokenModalOpen}
        onCancel={closeRefreshTokenModal}
        footer={null}
        width={refreshTokenTaskId ? 720 : 520}
        maskClosable={false}
      >
        {!refreshTokenTaskId ? (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={
                selectedRowKeys.length > 0
                  ? `处理所选 ${selectedRowKeys.length} 个账号`
                  : `处理当前筛选的 ${total} 个账号`
              }
              description="走 session token → OAuth → 登录链，并用 /backend-api/me 校验新 AT；AT 未到期时服务端返回原值，会如实显示「无需换发」。"
            />
            <Form form={refreshTokenForm} layout="vertical" onFinish={handleRefreshToken}>
              <Form.Item name="concurrency" label="并发数" initialValue={1}>
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="delay_seconds"
                label="每个账号间隔(秒)"
                initialValue={5}
                tooltip="连续打会话/授权端点容易触发风控，建议留几秒"
              >
                <InputNumber min={0} precision={1} step={1} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" htmlType="submit" block loading={refreshTokenLoading}>
                  开始刷新
                </Button>
              </Form.Item>
            </Form>
          </>
        ) : (
          <TaskLogPanel taskId={refreshTokenTaskId} kind="refresh_token" onDone={() => { load() }} />
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
          .map((value) => String(value))
          .filter((value) => value.length > 0)}
        filteredTotal={total}
      />

      <Modal
        title="上传到面板"
        open={panelUploadOpen}
        onCancel={() => setPanelUploadOpen(false)}
        onOk={handlePanelUpload}
        okText="开始上传"
        cancelText="取消"
        confirmLoading={panelUploadRunning}
        maskClosable={false}
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div>
            <div style={{ marginBottom: 6, fontSize: 13 }}>目标面板</div>
            <Select
              value={panelUploadTarget}
              onChange={setPanelUploadTarget}
              style={{ width: 220 }}
              options={PANEL_UPLOAD_TARGETS.map((t) => ({ value: t.value, label: t.label }))}
            />
          </div>
          {/* 数量必须写在确认里：筛选条件写错会把几百个号一次推上去。 */}
          <p style={{ margin: 0, fontSize: 12, color: 'var(--text-muted)' }}>
            {selectedRowKeys.length > 0 ? (
              <>将上传<strong>勾选的 {selectedRowKeys.length} 个</strong>账号。</>
            ) : (
              <>未勾选账号 —— 将上传<strong>当前筛选结果的全部 {total} 个</strong>账号。
                想只传一部分就先勾选。</>
            )}
          </p>
          <p style={{ margin: 0, fontSize: 12, color: 'var(--text-muted)' }}>
            上传成功后这些账号会被<strong>登记</strong>到该面板，参与后续的凭证同步。
          </p>
        </Space>
      </Modal>

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
          {importFormat === 'text' ? (
            // 来源格式决定「第 N 列是哪个字段」。`----` 分隔的行靠它解析 ——
            // 导出用 `----`，不选格式的话多列行会解析失败（导出注册表的列
            // 映射是唯一来源）。
            <Select
              value={importSourceFormat}
              onChange={setImportSourceFormat}
              style={{ width: '100%' }}
              options={exportFormats.map((f) => ({ value: f.id, label: f.label }))}
              placeholder="来源格式（决定各列是什么字段）"
            />
          ) : null}
          <p style={{ margin: 0, fontSize: 12, color: 'var(--text-muted)' }}>
            {importFormat === 'json' ? (
              <>
                粘贴<strong>导出的 JSON 数组</strong>（含 AT / RT / 2FA / 状态等全字段）。
                邮箱为唯一键，已存在的账号按字段合并更新 —— 空字段不会覆盖已有值。
              </>
            ) : (
              <>
                两种都收：
                <br />· <strong>导出格式</strong>（按上面选的来源格式解析）：
                <code style={{ background: 'var(--bg-subtle)', padding: '2px 4px', borderRadius: 4 }}>email----密码----…</code>
                <br />· <strong>手输</strong>：
                <code style={{ background: 'var(--bg-subtle)', padding: '2px 4px', borderRadius: 4 }}>email 密码</code>
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
                    { value: 'registered', label: '正常' },
                    { value: 'expired', label: '过期' },
                    { value: 'invalid', label: '失效' },
                    { value: 'limited', label: '限流' },
                    { value: 'banned', label: '禁用' },
                  ]}
                />
              </Form.Item>
              {/* 密码：**只读展示 + 一键复制**。详情端点返回明文（列表响应
                  里已裁成 `has_password` 布尔，安全加固），要看/复制原文只能
                  在这里。用户要求「账号详细信息查看要完整，账号密码2FA等」。 */}
              <Form.Item label="密码">
                {currentAccount.password ? (
                  <Text
                    style={{ fontFamily: 'monospace', fontSize: 12, wordBreak: 'break-all' }}
                    copyable={{ text: String(currentAccount.password), tooltips: ['复制密码', '已复制'] }}
                  >
                    {String(currentAccount.password)}
                  </Text>
                ) : (
                  <Text type="secondary">
                    无密码（手机号注册的号没有邮箱密码，或该号未设过密码）
                  </Text>
                )}
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
