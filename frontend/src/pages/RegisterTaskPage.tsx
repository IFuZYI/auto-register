import { useEffect, useState } from 'react'
import {
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Button,
  Tag,
  Space,
  Typography,
  Descriptions,
} from 'antd'
import {
  PlayCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons'
import { ChatGPTBind2faSwitch } from '@/components/ChatGPTBind2faSwitch'
import { ChatGPTRegisterFlowSelect } from '@/components/ChatGPTRegisterFlowSelect'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import SmsCountrySelect from '@/components/SmsCountrySelect'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { usePersistentChatGPTBind2fa } from '@/hooks/usePersistentChatGPTBind2fa'
import { usePersistentChatGPTRegisterFlow } from '@/hooks/usePersistentChatGPTRegisterFlow'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { listICloudAccounts } from '@/api/icloud'

/**
 * 邮箱服务下拉项。
 *
 * 这份清单同时是「提示文案」的数据源：注册页要告诉用户「不改就用全局配置里的那个」，
 * 而提示里得写出那个 provider 的中文名，所以 label 必须能按 value 反查。
 * 放在模块级而不是组件里，避免每次渲染重建。
 *
 * 只剩两个本地号池渠道（用户要求：其他邮箱都不要了）。一次性临时邮箱与远程
 * icloud-hme 已整体删除，这里不再列它们。
 */
const MAIL_PROVIDER_OPTIONS = [
  { value: 'microsoft', label: 'Outlook（微软号池）' },
  { value: 'icloud_local', label: 'iCloud 隐私邮箱（本地主号）' },
]

/** value → label，用于把全局默认 provider 显示成中文名 */
const MAIL_PROVIDER_LABELS: Record<string, string> = Object.fromEntries(
  MAIL_PROVIDER_OPTIONS.map((option) => [option.value, option.label]),
)
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import {
  PLATFORM_OPTIONS,
  getExecutorOptions,
  getPlatformMeta,
  loadPlatformCapabilities,
  normalizeExecutorForPlatform,
  rememberPlatformCapabilities,
} from '@/lib/platforms'
import {
  DEFAULT_REGISTER_RETRY_TIMES,
  normalizeRegisterRetryTimes,
} from '@/lib/registerRetry'
import { MAX_REGISTER_CONCURRENCY, MAX_REGISTER_COUNT } from '@/lib/registerLimits'
import {
  MAIL_IMPORT_SOURCE_OPTIONS,
  isMailImportProvider,
  normalizeMailImportSource,
  resolveEffectiveMailProvider,
} from '@/lib/mailImport'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'

const { Text } = Typography

export default function RegisterTaskPage() {
  const [form] = Form.useForm()
  const [task, setTask] = useState<any>(null)
  const [polling, setPolling] = useState(false)
  // iCloud 本地主号列表：选「iCloud 隐私邮箱（本地主号）」时给个真实下拉，
  // 而不是让人手输主号邮箱 —— 输错了要到等码超时才看得出来。
  const [icloudAccounts, setIcloudAccounts] = useState<{ value: string; label: string }[]>([])
  // 全局配置里配的默认邮箱服务 —— 表单初值就来自它（见下面的 /config 加载），
  // 这里只用于给用户一句提示：不改就是用它。
  // 声明必须早于引用它的 useEffect：写在下面会让那个 effect 的依赖数组
  // 捕获到声明前的绑定（eslint react-hooks/immutability 会直接报错）。
  const [globalMailProvider, setGlobalMailProvider] = useState('')
  useEffect(() => {
    let alive = true
    listICloudAccounts()
      .then((items) => {
        if (!alive) return
        setIcloudAccounts(
          (items || [])
            .filter((item) => item.enabled)
            .map((item) => ({
              value: String(item.id),
              label: `${item.email}（${item.alias_count} 个别名）`,
            })),
        )
      })
      .catch(() => {
        // 控制台读不到不影响注册页其余部分，静默降级为「留空自动选」
      })
    return () => {
      alive = false
    }
  }, [])
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()
  const { registerFlow: chatgptRegisterFlow, setRegisterFlow: setChatgptRegisterFlow } =
    usePersistentChatGPTRegisterFlow()
  const { bind2fa: chatgptBind2fa, setBind2fa: setChatgptBind2fa } =
    usePersistentChatGPTBind2fa()
  useEffect(() => {
    // 能力清单（各平台支持的执行器）必须先加载 —— 执行器选项由它决定，
    // 加载前 `getExecutorOptions` 返回空数组（不猜默认值）。
    // 执行器本身的初值由下面的「平台切换」effect 负责（它按平台读配置）。
    loadPlatformCapabilities().then((caps) => {
      rememberPlatformCapabilities(caps)
      const currentPlatform = form.getFieldValue('platform') || 'chatgpt'
      const saved = form.getFieldValue('executor_type')
      const next = normalizeExecutorForPlatform(currentPlatform, saved)
      if (next && next !== saved) {
        form.setFieldValue('executor_type', next)
      }
    })
    apiFetch('/config').then((cfg) => {
      // 全局配置里的邮箱服务可能为空（用户要求默认留空、必须显式选）。
      // 空串时不要兜底成某个渠道 —— 那正是用户要避免的「悄悄用某个号池」。
      // 留着空值，Select 会显示占位文案，提交时 required 规则会拦住。
      const configMailProvider = String(cfg.mail_provider || '').trim()
      const usesMailImport = isMailImportProvider(configMailProvider)
      setGlobalMailProvider(usesMailImport ? 'mail_import' : configMailProvider)
      form.setFieldsValue({
        captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
        register_retry_times: normalizeRegisterRetryTimes(cfg.register_retry_times),
        // 默认注册参数（面板「全局配置 → 注册设置」里配）—— 任务页仍可临时改。
        // 只在配置有值时覆盖，空值保留 initialValues 的兜底，免得把
        // 「没配过」显示成 0。
        ...(Number(cfg.register_count) > 0 ? { count: Number(cfg.register_count) } : {}),
        ...(Number(cfg.register_concurrency) > 0
          ? { concurrency: Number(cfg.register_concurrency) }
          : {}),
        ...(String(cfg.register_delay_seconds ?? '').trim()
          ? { register_delay_seconds: Number(cfg.register_delay_seconds) }
          : {}),
        mail_provider: usesMailImport ? 'mail_import' : configMailProvider,
        mail_import_source: normalizeMailImportSource(cfg.mail_import_source, configMailProvider),
        yescaptcha_key: cfg.yescaptcha_key || '',
        outlook_backend: cfg.outlook_backend || 'graph',
        sms_country: cfg.sms_country || '',
        sms_per_phone_timeout: cfg.sms_per_phone_timeout || '',
        sms_max_phone_attempts: cfg.sms_max_phone_attempts || '',
        // iCloud 隐私邮箱（本地）：主号在本地库，这里只挑用哪个主号
        icloud_local_account_id: cfg.icloud_local_account_id || '',
        icloud_local_label: cfg.icloud_local_label || '',
        icloud_local_note: cfg.icloud_local_note || '',
      })
    })
  }, [form])

  const submit = async () => {
    const values = await form.validateFields()
    const effectiveMailProvider = resolveEffectiveMailProvider(values.mail_provider, values.mail_import_source)
    const registerExtra = {
      mail_provider: effectiveMailProvider,
      // 微软号池里 OAuth 号和 MailAPI URL 号混着放，取号要按这里选的类型筛
      mail_import_source: normalizeMailImportSource(values.mail_import_source, effectiveMailProvider),
      outlook_backend: values.outlook_backend,
      sms_country: values.sms_country,
      sms_per_phone_timeout: values.sms_per_phone_timeout,
      sms_max_phone_attempts: values.sms_max_phone_attempts,
      yescaptcha_key: values.yescaptcha_key,
      solver_url: values.solver_url,
      // iCloud 隐私邮箱（本地）：主号在本地库，这里只挑用哪个主号
      icloud_local_account_id: values.icloud_local_account_id,
      icloud_local_label: values.icloud_local_label,
      icloud_local_note: values.icloud_local_note,
    }
    const chatgptRegistrationRequestAdapter =
      buildChatGPTRegistrationRequestAdapter(
        values.platform,
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
        platform: values.platform,
        email: values.email || null,
        password: values.password || null,
        count: values.count,
        concurrency: values.concurrency,
        register_retry_times: normalizeRegisterRetryTimes(values.register_retry_times),
        register_delay_seconds: values.register_delay_seconds || 0,
        proxy: values.proxy || null,
        executor_type: values.executor_type,
        captcha_solver: values.captcha_solver,
        extra: adaptedRegisterExtra,
      }),
    })
    setTask(res)
    setPolling(true)
    pollTask(res.task_id)
  }

  const pollTask = async (id: string) => {
    const interval = setInterval(async () => {
      const t = await apiFetch(`/tasks/${id}`)
      setTask(t)
      if (t.status === 'done' || t.status === 'failed' || t.status === 'stopped') {
        clearInterval(interval)
        setPolling(false)
        if (t.cashier_urls && t.cashier_urls.length > 0) {
          t.cashier_urls.forEach((url: string) => window.open(url, '_blank'))
        }
      }
    }, 2000)
  }

  const mailProviderRaw = Form.useWatch('mail_provider', form)
  const mailImportSource = Form.useWatch('mail_import_source', form)
  const mailProvider = resolveEffectiveMailProvider(String(mailProviderRaw || ''), String(mailImportSource || 'microsoft'))
  const captchaSolver = Form.useWatch('captcha_solver', form)
  const platform = Form.useWatch('platform', form)
  const executorOptions = getExecutorOptions(platform)
  const platformMeta = getPlatformMeta(platform)
  // 现存两个平台（ChatGPT / Grok）都消耗邮箱池，所以这张卡片恒定显示。
  // iCloud 平台删除前这里是 `platformMeta?.usesMailbox ?? true` 驱动的开关 ——
  // 它当时是唯一的 false。字段留着（PlatformMeta.usesMailbox）是为了以后真出现
  // 自带邮箱来源的平台时还能一键恢复条件渲染。
  const usesMailbox = platformMeta?.usesMailbox ?? true
  const usesCaptcha = platformMeta?.usesCaptcha ?? true

  // 平台切换时**重新加载该平台的执行器** —— 不能只做归一。
  //
  // 踩过：切到 Grok 时沿用 ChatGPT 的值（'protocol'）—— 归一后落到 Grok 的
  // 合法取值，于是「看起来没问题」，但用户给 Grok 保存的 browser 被丢掉了。
  // 每个平台的执行器是独立配置（`<platform>_executor`），切换时要读它。
  useEffect(() => {
    let alive = true
    Promise.all([loadPlatformCapabilities(), apiFetch('/config')])
      .then(([caps, cfg]) => {
        if (!alive) return
        rememberPlatformCapabilities(caps)
        const saved = cfg[`${platform}_executor`]
        const next = normalizeExecutorForPlatform(platform, saved)
        if (next && form.getFieldValue('executor_type') !== next) {
          form.setFieldValue('executor_type', next)
        }
      })
      .catch(() => {
        // 读不到配置时保留当前值，不阻断页面
      })
    return () => {
      alive = false
    }
  }, [form, platform])

  return (
    <div style={{ maxWidth: 'var(--w-page)' }}>
      <PageHeader
        title="注册任务"
        subtitle="创建账号自动注册任务"
      />

      <Form form={form} layout="vertical" onFinish={submit} className="register-form" initialValues={{
        platform: 'chatgpt',
        // 执行器不设初值：它必须由平台的能力清单决定（各平台支持集合不同）。
        // 写死 'protocol' 会替 Grok 预选一个它没有的执行器。
        captcha_solver: 'yescaptcha',
        // 不预设 mail_provider：全局配置里留空就必须显式选，免得任务悄悄
        // 落到某个号池上。留空时下面的 Select 会显示占位文案。
        mail_import_source: 'outlook',
        outlook_backend: 'graph',
        count: 1,
        concurrency: 1,
        register_retry_times: DEFAULT_REGISTER_RETRY_TIMES,
        register_delay_seconds: 0,
        solver_url: 'http://localhost:8889',
      }}>
        <Card title="基本配置" style={{ marginBottom: 16 }}>
          {/* 平台 / 执行器 / 验证码 是同一组「用什么跑」的选择，并排放一行。
              之前各占一整行、每个下拉都拉到 924px，一屏只看得见三个控件。 */}
          <div className="form-grid">
            <Form.Item name="platform" label="平台" rules={[{ required: true }]} className="form-field--md">
              <Select options={PLATFORM_OPTIONS} />
            </Form.Item>
            <Form.Item
              name="executor_type"
              label="执行器"
              rules={[{ required: true }]}
              className="form-field--md"
              // 选项由该平台的能力清单决定（`/api/platforms`）。加载前为空 ——
              // 显示加载态而不是猜一个可能不受支持的值。
              extra={executorOptions.length === 0 ? '读取平台能力中…' : undefined}
            >
              <Select options={executorOptions} loading={executorOptions.length === 0} />
            </Form.Item>
            <Form.Item
              name="captcha_solver"
              label="验证码"
              rules={[{ required: true }]}
              hidden={!usesCaptcha}
              className="form-field--md"
            >
              <Select
                options={[
                  { value: 'yescaptcha', label: 'YesCaptcha' },
                  { value: 'local_solver', label: '本地 Solver (Camoufox)' },
                  { value: 'manual', label: '手动' },
                ]}
              />
            </Form.Item>
          </div>
          {/* 数字类字段按语义收窄（--w-field 200px），不再用 Space + flex:1
              把「批量数量」这种一位数拉到 375px 宽。容器用 flex-wrap，
              窄屏下自动折行而不是挤出横向滚动。 */}
          <div className="form-grid">
            <Form.Item name="count" label="批量数量" className="form-field--sm">
              <Input type="number" min={1} max={MAX_REGISTER_COUNT} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数" className="form-field--sm">
              <Input type="number" min={1} max={MAX_REGISTER_CONCURRENCY} />
            </Form.Item>
            <Form.Item
              name="register_retry_times"
              label="失败重试轮数"
              className="form-field--sm"
              tooltip="整条流程失败后自动重开一轮（换新邮箱 / 号码 / 会话）；0 表示失败即止。与接码的「最多换号」是两回事。"
            >
              <InputNumber min={0} max={10} precision={0} style={{ width: '100%' }} placeholder="1" />
            </Form.Item>
            <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" className="form-field--sm">
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0" />
            </Form.Item>
          </div>
          <Form.Item name="proxy" label="代理 (可选)" className="form-field--lg">
            <Input placeholder="http://user:pass@host:port" />
          </Form.Item>
          {platform === 'chatgpt' && (
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
        </Card>

        {usesMailbox && (
        <Card title="邮箱配置" style={{ marginBottom: 16 }}>
          <Form.Item
            name="mail_provider"
            label="邮箱服务"
            rules={[{ required: true }]}
            extra={MAIL_PROVIDER_LABELS[globalMailProvider]
              ? `默认：${MAIL_PROVIDER_LABELS[globalMailProvider]}（改后只影响本次任务）`
              : undefined}
          >
            <Select options={MAIL_PROVIDER_OPTIONS} placeholder="请选择邮箱服务" />
          </Form.Item>
          {mailProvider === 'microsoft' && (
            <Form.Item
              name="mail_import_source"
              label="导入类型"
              rules={[{ required: true }]}
              extra="Outlook / Hotmail / MailAPI URL 共用同一个微软号池；三类账号互不顶替。"
            >
              <Select options={MAIL_IMPORT_SOURCE_OPTIONS} />
            </Form.Item>
          )}
          {mailProvider === 'microsoft' && (
            <Form.Item
              name="outlook_backend"
              label="微软收信方式"
              extra="默认 Graph；无 OAuth 凭据的账号自动回退 IMAP。"
            >
              <Select
                options={[
                  { value: 'graph', label: 'Graph（默认）' },
                  { value: 'imap', label: 'IMAP' },
                ]}
              />
            </Form.Item>
          )}
          {mailProvider === 'icloud_local' && (
            <>
              <Form.Item
                name="icloud_local_account_id"
                label="主号"
                extra="留空时用第一个启用的主号（主号在「邮箱服务」页登录）。"
              >
                <Select
                  allowClear
                  placeholder="留空自动选第一个可用主号"
                  options={icloudAccounts}
                  notFoundContent="还没有已启用的主号，先到「邮箱服务」页登录"
                />
              </Form.Item>
              <Form.Item name="icloud_local_label" label="别名标签（可选）">
                <Input placeholder="留空则用「隐私邮箱」" />
              </Form.Item>
              <Form.Item name="icloud_local_note" label="备注（可选）">
                <Input placeholder="写进隐私邮箱的备注" />
              </Form.Item>
            </>
          )}
        </Card>
        )}

        {platform === 'chatgpt' && (
          <Card title="ChatGPT 手机接码" style={{ marginBottom: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
              手机注册用它拿号；邮箱注册只在链路进入 add-phone 时用。平台与 API Key
              在「设置 → 手机接码」配置，这里只覆盖本次任务参数。
            </Text>
            <Form.Item name="sms_country" label="国家（可选）">
              <SmsCountrySelect placeholder="留空用设置里的默认国家" />
            </Form.Item>
            <Form.Item name="sms_per_phone_timeout" label="单号等待秒数（可选）">
              <Input placeholder="80" />
            </Form.Item>
            <Form.Item name="sms_max_phone_attempts" label="最多换号次数（可选）">
              <Input placeholder="3" />
            </Form.Item>
          </Card>
        )}

        {usesCaptcha && captchaSolver === 'yescaptcha' && (
          <Card title="验证码配置" style={{ marginBottom: 16 }}>
            <Form.Item name="yescaptcha_key" label="YesCaptcha Key">
              <Input />
            </Form.Item>
          </Card>
        )}

        {captchaSolver === 'local_solver' && (
          <Card title="本地 Solver 配置" style={{ marginBottom: 16 }}>
            <Form.Item name="solver_url" label="Solver URL">
              <Input />
            </Form.Item>
            <Text type="secondary" style={{ fontSize: 12 }}>
              启动命令: python services/turnstile_solver/start.py --browser_type camoufox --port 8889
            </Text>
          </Card>
        )}

        <Button type="primary" htmlType="submit" block disabled={polling} icon={polling ? <LoadingOutlined /> : <PlayCircleOutlined />}>
          {polling ? '注册中...' : '开始注册'}
        </Button>
      </Form>

      {task && (
        <Card title={
          <Space>
            <span>任务状态</span>
            <Tag color={
              task.status === 'done' ? 'success' :
              task.status === 'stopped' ? 'warning' :
              task.status === 'failed' ? 'error' : 'processing'
            }>
              {task.status}
            </Tag>
          </Space>
        } style={{ marginTop: 16 }}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label="任务 ID">
              <Text copyable style={{ fontFamily: 'monospace' }}>{task.id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="进度">{task.progress}</Descriptions.Item>
            <Descriptions.Item label="跳过">{task.skipped ?? 0}</Descriptions.Item>
          </Descriptions>
          {task.success != null && (
            <div style={{ marginTop: 8, color: 'var(--success)' }}>
              <CheckCircleOutlined /> 成功 {task.success} 个
            </div>
          )}
          {task.errors?.length > 0 && (
            <div style={{ marginTop: 8 }}>
              {task.errors.map((e: string, i: number) => (
                <div key={i} style={{ color: 'var(--danger)', marginBottom: 4 }}>
                  <CloseCircleOutlined /> {e}
                </div>
              ))}
            </div>
          )}
          {task.error && (
            <div style={{ marginTop: 8, color: 'var(--danger)' }}>
              <CloseCircleOutlined /> {task.error}
            </div>
          )}
          {task.id ? (
            <div style={{ marginTop: 16 }}>
              <TaskLogPanel taskId={task.id} />
            </div>
          ) : null}
        </Card>
      )}
    </div>
  )
}
