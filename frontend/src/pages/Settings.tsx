import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { App, Button, Form, Input, message, Tabs, Space, Tag, Typography, QRCode } from 'antd'
import {
  SaveOutlined,
  SafetyOutlined,
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  SyncOutlined,
  LockOutlined,
  MobileOutlined,
  MailOutlined,
  ToolOutlined,
} from '@ant-design/icons'
import { setToken } from '@/lib/utils'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { parseCountryIdList } from '@/lib/smsCountries'
import {
  dropEmptySecrets,
  secretSetKeysFromConfig,
  stripSecretSetFlags,
} from '@/lib/secretConfig'
import {
  ConfigSection,
  type SectionConfig,
} from '@/components/settings/ConfigPanels'
import { PanelConfigPanel } from '@/components/settings/PanelConfigPanel'
import { MaintenancePanel } from '@/components/settings/MaintenancePanel'
import { RegisterSettingsPanel } from '@/components/settings/RegisterSettingsPanel'
import { DataBackupPanel } from '@/components/settings/DataBackupPanel'
import { HelpTip, SectionHead, SettingRow } from '@/components/settings/SettingsPrimitives'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'

/**
 * 全局配置。
 *
 * 组织原则是**一个子菜单说一件事**，而且每件事的作用域要能从菜单名上看出来：
 *
 *   注册设置   一次注册任务怎么跑
 *   验证码     人机验证怎么解
 *   手机接码   进 add-phone 时怎么租号收码
 *   邮箱       验证码邮件从哪来、等多久
 *   面板配置   四个外部面板连在哪、推什么过去
 *   自动维护   后台定时跑什么（本地刷新 AT/RT / 与面板同步）
 *   安全       面板自己的访问口令与 2FA
 *   数据迁移   备份与恢复
 *
 * 「自动维护」是独立子菜单而不是面板配置里的一节：它维护的对象**不止面板**
 * —— 「自动刷新 Token」动的是本地账号库，根本不经过任何面板。放在一起
 * 会让人分不清「关掉它，是本地不刷新了，还是远端不同步了」。
 *
 * 排版：一行一个配置项，标签在左、控件在右，说明文字全部进 ⓘ 气泡。
 * 改造前每节是一张 Card + 一段两三行的常驻说明，100 多个配置项铺开时
 * 解释文字占的版面比控件还多。
 */

const SMS_BOOLEAN_KEYS = ['sms_enabled', 'sms_auto_country', 'sms_reuse_phone'] as const

/**
 * 本页出现过的口令字段（与服务端 `api/config.py` 的 `SECRET_CONFIG_KEYS` 对应）。
 *
 * 服务端已按这份清单在读取时打码、写入时拦空串，这里再声明一次是为了在
 * **提交前**就把空值摘掉 —— 少一次「靠服务端兜底」的往返，也让 payload
 * 在开发者工具里干净。新增口令字段时两边都要补。
 */
const SECRET_FIELD_KEYS = ['yescaptcha_key', 'twocaptcha_key', 'sms_api_key'] as const

const TAB_ITEMS = [
  {
    key: 'register',
    label: '注册设置',
    summary: '默认参数、执行器与各平台的注册方式',
    icon: <ApiOutlined />,
    hint:
      '一次注册任务的默认参数：执行器、数量、并发、重试、间隔，以及各平台的注册方式。' +
      '任务页可以逐个覆盖 —— 这里的值只是「不指定时用什么」。',
    // 注册设置整块交给 RegisterSettingsPanel（渐进式：默认参数 → 执行器 →
    // 各平台方式折叠）。原来的 sections 平铺三张卡，改个并发数要在一屏
    // 平台下拉里找。发码节流也并入该面板。
    custom: 'register' as const,
    sections: [],
  },
  {
    key: 'captcha',
    label: '验证码',
    summary: '人机验证的服务与密钥',
    icon: <SafetyOutlined />,
    hint: '注册时绕过人机验证的服务。YesCaptcha 与 2Captcha 二选一即可，留空的那个不会发起请求。',
    sections: [
      {
        title: '验证码服务',
        desc: 'Key 在服务端加密存储，接口只回「是否已配置」不回明文 —— 输入框留空表示不修改。',
        fields: [
          {
            key: 'default_captcha_solver',
            label: '默认服务',
            hint: '新任务默认用哪一个。任务页可以逐个改。',
          },
          {
            key: 'yescaptcha_key',
            label: 'YesCaptcha Key',
            secret: true,
            hint: '在 YesCaptcha 后台的「API 密钥」页复制。',
          },
        ],
      },
    ],
  },
  {
    key: 'sms',
    label: '手机接码',
    summary: 'add-phone 时自动租号收码',
    icon: <MobileOutlined />,
    hint: 'ChatGPT 链路进入 add-phone 时自动租号、收码，全程无人值守。',
    sections: [
      {
        title: '接码平台',
        desc: '遇到 add-phone 页面时，自动向接码平台租一个号收验证码。',
        fields: [
          {
            key: 'sms_enabled',
            label: '启用接码',
            type: 'boolean',
            hint: '关掉后遇到 add-phone 直接判定失败、不会尝试租号 —— 想「跳过这类账号」时可以关它。',
          },
          // 标签不叫「接码平台」—— 那与它所在的节同名，页面上会出现两行
          // 一模一样的「接码平台」，其中一行还是节标题（没有控件），
          // 看起来像渲染坏了（截图实测被读成「孤立的空行」）。
          { key: 'sms_provider', label: '服务商', type: 'select' },
          { key: 'sms_api_key', label: 'API Key', secret: true, placeholder: '在接码平台后台复制' },
          { key: 'sms_service', label: '服务代码', placeholder: 'dr（OpenAI / ChatGPT）' },
        ],
      },
      {
        title: '国家选择',
        desc:
          '多数国家已改用 WhatsApp 验证，实测只有泰国（52）走纯短信稳定可用 —— ' +
          '所以默认给的是泰国。「自动选国家」会在允许的国家里按库存与价格挑一个。',
        fields: [
          { key: 'sms_country', label: '默认国家', type: 'country', placeholder: '默认泰国 (52)' },
          {
            key: 'sms_auto_country',
            label: '自动选国家',
            type: 'boolean',
            hint: '在「允许的国家」里按库存与价格挑一个，而不是固定用默认国家。',
          },
          {
            key: 'sms_allowed_countries',
            label: '允许的国家',
            type: 'country-multi',
            placeholder: '留空则不限',
            hint: '自动选国家时只在这些国家里挑。留空表示不限。',
          },
          {
            key: 'sms_auto_min_stock',
            label: '最低库存',
            placeholder: '20',
            hint: '库存低于这个数的国家不会被自动选中。',
          },
          {
            key: 'sms_auto_max_price',
            label: '价格上限',
            placeholder: '0 表示不限',
            hint: '自动选号时的单价上限，0 表示不限。',
          },
        ],
      },
      {
        title: '租号与重试',
        desc: '单号窗口内只轮询接码平台收码，窗口用尽则换号。',
        fields: [
          { key: 'sms_max_price', label: '单号价格上限', placeholder: '留空或 0 表示不限' },
          {
            key: 'sms_fixed_price',
            label: '固定价格',
            placeholder: '留空表示不锁价',
            hint: '只租这个价格的号；留空表示不锁价。',
          },
          {
            key: 'sms_reuse_phone',
            label: '复用号码',
            type: 'boolean',
            hint: '同一个号码成功后继续用于下一个注册，直到达到复用上限。',
          },
          { key: 'sms_phone_success_max', label: '复用上限', placeholder: '3', hint: '同一个号码最多成功注册几次。' },
          { key: 'sms_per_phone_timeout', label: '单号等待', placeholder: '80', hint: '一个号等码多少秒，超时就换号。' },
          { key: 'sms_max_phone_attempts', label: '最多换号', placeholder: '3' },
          {
            key: 'sms_code_retries_per_phone',
            label: '单号重试',
            placeholder: '2',
            hint: '同一个号上验证码输错后重试几次。',
          },
        ],
      },
    ],
  },
  {
    key: 'mail',
    label: '邮箱',
    summary: '默认邮箱服务与验证码等待时长',
    icon: <MailOutlined />,
    hint:
      '「默认邮箱服务」是全局默认值：注册任务不指定邮箱时用哪个 provider、等验证码等多久。' +
      '各 provider 的连接参数在「邮箱服务」一级菜单各自的页面里，这里不重复。',
    sections: [
      {
        // 默认邮箱服务放回全局配置：它是「全局默认值」—— 注册任务不指定邮箱时
        // 用哪个 provider、等验证码等多久。各 provider 的连接参数仍在
        // 「邮箱服务」一级菜单各自的页面里，这里不重复。
        title: '默认邮箱服务',
        desc: '注册任务不单独指定邮箱时用这一项；下面的等待时长对所有邮箱渠道生效。',
        fields: [
          { key: 'mail_provider', label: '邮箱服务', type: 'select' },
          {
            key: 'mailbox_otp_timeout_seconds',
            label: '等待验证码',
            placeholder: '例如 60 / 90 / 120',
            hint: '超过这个时间还没收到验证码就判定本次失败。信箱慢（iCloud 转发）时可以调大。',
          },
        ],
      },
    ],
  },
  {
    key: 'panel',
    label: '面板配置',
    summary: '四个外部面板连在哪、推什么过去',
    icon: <CloudUploadOutlined />,
    hint: '四个外部面板连在哪、推什么过去。一个平台一张卡片，卡片里就是它的全部配置。',
    // 面板配置整块交给 PanelConfigPanel（一平台一卡片：卡头给身份与状态，
    // 卡体给旋钮）。曾经是「上面卡片 + 下面连接配置表单」，同一个平台的
    // 参数写在两个位置，要滚来滚去才对得上。
    custom: 'panel' as const,
    sections: [],
  },
  {
    key: 'maintenance',
    label: '自动维护',
    summary: '后台定时刷新 AT/RT 或与面板同步',
    icon: <ToolOutlined />,
    hint: '后台定时跑什么：本地刷新 AT/RT，或与面板同步凭据。一个平台一张卡片，卡上写明是哪种维护。',
    custom: 'maintenance' as const,
    sections: [],
  },
  {
    key: 'security',
    label: '安全',
    summary: '面板自己的访问口令与 2FA',
    icon: <LockOutlined />,
    hint: '面板自己的访问口令与双因素认证 —— 与注册账号的凭据无关。',
    sections: [],
  },
  {
    key: 'backup',
    label: '数据迁移',
    summary: '整库导出与导入',
    icon: <DatabaseOutlined />,
    hint: '整库导出与导入。导出文件包含全部明文凭据（含密码与 RT），等同密码本身。',
    // 数据备份/迁移整块交给 DataBackupPanel（导出下载 + 导入上传 + 概况）。
    // 它不是配置项表单（导出是二进制下载、导入是文件上传），
    // 塞进通用 ConfigSection 渲染器表达不了。
    custom: 'backup' as const,
    sections: [],
  },
]

interface TabConfig {
  key: string
  label: string
  icon: React.ReactNode
  /** 常驻一行，一句话说这页管什么（不重复左侧导航上的标签）。 */
  summary?: string
  /** 更长的背景，只进 ⓘ。 */
  hint?: string
  sections: SectionConfig[]
  /**
   * 用自定义组件替代 sections 渲染（`sections` 留空）。
   *
   * 这四个子菜单需要的不是「一组字段」：注册设置是渐进式折叠面板，面板配置
   * 与自动维护是一平台一卡，数据迁移是下载/上传 —— 通用的 `ConfigSection`
   * 渲染器表达不了。与其把它们的配置项塞进 SectionConfig 再打补丁，
   * 不如整块交给专用组件。
   */
  custom?: 'register' | 'panel' | 'maintenance' | 'backup'
}

interface SmsCountryRow {
  country: string
  name: string
  price: number | null
  count: number | null
  openai_sms_whitelisted: boolean
}

function SmsProbePanel({ form }: { form: any }) {
  const [balance, setBalance] = useState<number | null>(null)
  const [countries, setCountries] = useState<SmsCountryRow[]>([])
  const [loading, setLoading] = useState<'' | 'balance' | 'countries'>('')

  // 探针用表单里的现值而不是已保存的配置：用户往往是刚粘上一把新 key 就想试试。
  const probeBody = () => ({
    provider: String(form.getFieldValue('sms_provider') || ''),
    api_key: String(form.getFieldValue('sms_api_key') || ''),
    service: String(form.getFieldValue('sms_service') || ''),
    limit: 20,
  })

  const probe = async (kind: 'balance' | 'countries') => {
    setLoading(kind)
    try {
      const data = await apiFetch(`/sms/${kind}`, {
        method: 'POST',
        body: JSON.stringify(probeBody()),
      })
      if (kind === 'balance') {
        setBalance(Number(data.balance))
        message.success(`余额 ${data.balance}`)
      } else {
        setCountries(data.items || [])
        message.success(`已获取 ${(data.items || []).length} 个国家`)
      }
    } catch (err: any) {
      message.error(err?.message || '接码平台探测失败')
    } finally {
      setLoading('')
    }
  }

  return (
    <section className="set-section">
      <SectionHead
        title="平台自检"
        hint={
          '用表单里**当前**的值去问接码平台，不必先保存 —— 刚粘上一把新 Key 时最常用。' +
          '查询结果里绿色是 OpenAI 走纯短信的国家，其余可能被要求用 WhatsApp 验证从而收不到码。'
        }
      />
      <SettingRow label="自检">
        <Space wrap>
          <Button size="small" onClick={() => probe('balance')} loading={loading === 'balance'}>
            测试余额
          </Button>
          <Button size="small" onClick={() => probe('countries')} loading={loading === 'countries'}>
            查询国家排名
          </Button>
          {balance !== null ? <Tag color="green">余额 {balance}</Tag> : null}
        </Space>
      </SettingRow>
      {countries.length > 0 ? (
        <SettingRow label="国家">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {countries.map((row) => (
              <Tag key={row.country} color={row.openai_sms_whitelisted ? 'green' : 'default'}>
                {row.country} {row.name} · {row.price} · 库存 {row.count}
              </Tag>
            ))}
          </div>
        </SettingRow>
      ) : null}
    </section>
  )
}

function SolverStatus() {
  const [running, setRunning] = useState<boolean | null>(null)

  const checkSolver = async () => {
    try {
      const d = await apiFetch('/solver/status')
      setRunning(d.running)
    } catch {
      setRunning(false)
    }
  }

  const restartSolver = async () => {
    await apiFetch('/solver/restart', { method: 'POST' })
    setRunning(null)
    setTimeout(checkSolver, 2000)
  }

  useEffect(() => {
    checkSolver()
    const timer = window.setInterval(checkSolver, 5000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <section className="set-section">
      <SectionHead
        title="Turnstile Solver"
        hint="本机跑的无头浏览器服务，负责解 Cloudflare Turnstile。注册前它必须处于运行状态。"
      />
      <SettingRow label="状态">
        <Space size={12} wrap>
          <Space size={8}>
            {running === null ? (
              <SyncOutlined spin style={{ color: 'var(--text-muted)' }} />
            ) : running ? (
              <CheckCircleOutlined style={{ color: 'var(--success)' }} />
            ) : (
              <CloseCircleOutlined style={{ color: 'var(--danger)' }} />
            )}
            <span style={{ color: running ? 'var(--success)' : 'var(--text-muted)', fontWeight: 500 }}>
              {running === null ? '检测中' : running ? '运行中' : '未运行'}
            </span>
          </Space>
          <Button size="small" onClick={restartSolver}>
            重启 Solver
          </Button>
        </Space>
      </SettingRow>
    </section>
  )
}

type TotpSetupState = 'idle' | 'setup'

function SecurityPanel() {
  const { message: msg, modal } = App.useApp()
  const [status, setStatus] = useState<{ has_password: boolean; has_totp: boolean } | null>(null)
  const [loading, setLoading] = useState(false)

  const [enableForm] = Form.useForm()
  const [pwForm] = Form.useForm()
  const [codeForm] = Form.useForm()

  const [totpSetupState, setTotpSetupState] = useState<TotpSetupState>('idle')
  const [totpSecret, setTotpSecret] = useState('')
  const [totpUri, setTotpUri] = useState('')

  const loadStatus = async () => {
    try {
      const s = await apiFetch('/auth/status')
      setStatus(s)
    } catch {}
  }

  useEffect(() => { loadStatus() }, [])

  const handleEnable = async (values: { password: string; confirm: string }) => {
    if (values.password !== values.confirm) {
      msg.error('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      const d = await apiFetch('/auth/setup', {
        method: 'POST',
        body: JSON.stringify({ password: values.password }),
      })
      localStorage.setItem('auth_token', d.access_token)
      msg.success('密码保护已启用')
      enableForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableAuth = () => {
    // 一键关闭面板密码保护 = 把全量凭据库敞开（非本机来源在未设密码时被
    // 中间件挡成 503，但本机/隧道里就是全开）。必须二次确认（全量检测 F-05）。
    modal.confirm({
      title: '确认关闭访问密码保护？',
      content: '关闭后，任何能访问面板端口的本机/隧道请求都能直接读取全部账号与凭据。',
      okText: '关闭保护',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setLoading(true)
        try {
          await apiFetch('/auth/disable', { method: 'POST' })
          localStorage.removeItem('auth_token')
          msg.success('密码保护已关闭')
          await loadStatus()
        } catch (e: any) {
          msg.error(e.message)
        } finally {
          setLoading(false)
        }
      },
    })
  }

  const handleChangePassword = async (values: { current_password: string; new_password: string; confirm: string }) => {
    if (values.new_password !== values.confirm) {
      msg.error('两次输入的新密码不一致')
      return
    }
    setLoading(true)
    try {
      const changed = await apiFetch('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password: values.current_password, new_password: values.new_password }),
      })
      // 改密后服务端会轮换 JWT 密钥（吊销所有旧令牌），旧 token 立刻失效。
      // 必须把响应里的新令牌存回去，否则用户改完密码下一次请求就被踢回登录页。
      if (changed?.access_token) setToken(changed.access_token)
      msg.success('密码已更新')
      pwForm.resetFields()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleSetupTotp = async () => {
    setLoading(true)
    try {
      const d = await apiFetch('/auth/2fa/setup')
      setTotpSecret(d.secret)
      setTotpUri(d.uri)
      setTotpSetupState('setup')
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleEnableTotp = async (values: { code: string }) => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/enable', {
        method: 'POST',
        body: JSON.stringify({ secret: totpSecret, code: values.code }),
      })
      msg.success('双因素认证已启用')
      setTotpSetupState('idle')
      codeForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableTotp = () => {
    modal.confirm({
      title: '确认关闭双因素认证？',
      content: '关闭后仅凭密码即可登录面板。',
      okText: '关闭 2FA',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setLoading(true)
        try {
          await apiFetch('/auth/2fa/disable', { method: 'POST' })
          msg.success('双因素认证已关闭')
          await loadStatus()
        } catch (e: any) {
          msg.error(e.message)
        } finally {
          setLoading(false)
        }
      },
    })
  }

  return (
    <>
      <section className="set-section">
        <SectionHead
          title="访问密码"
          hint="面板本身的门。默认不开启 —— 任何能访问这个地址的人都能读写全部账号与凭据。只在本机/内网暴露时也应至少设一个密码。"
        />
        <SettingRow label="状态">
          <Space size={12} wrap>
            {status?.has_password ? (
              <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
            ) : (
              <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
            )}
            {status?.has_password ? (
              <Button size="small" danger loading={loading} onClick={handleDisableAuth}>
                关闭密码保护
              </Button>
            ) : null}
          </Space>
        </SettingRow>
        {!status?.has_password ? (
          <SettingRow label="设置密码">
            <Form
              form={enableForm}
              layout="inline"
              onFinish={handleEnable}
              requiredMark={false}
              style={{ rowGap: 8, flexWrap: 'wrap' }}
            >
              <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }, { min: 6, message: '至少 6 位' }]}>
                <Input.Password placeholder="至少 6 位" style={{ width: 190 }} />
              </Form.Item>
              <Form.Item name="confirm" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入密码" style={{ width: 190 }} />
              </Form.Item>
              <Form.Item style={{ margin: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<LockOutlined />}>
                  启用密码保护
                </Button>
              </Form.Item>
            </Form>
          </SettingRow>
        ) : null}
      </section>

      {status?.has_password && (
        <>
          <section className="set-section">
            <SectionHead
              title="修改密码"
              hint="改密会轮换服务端 JWT 密钥 —— 所有已登录的会话（含其它设备）立刻失效，只剩当前这一个。"
            />
            <SettingRow label="新密码">
              <Form form={pwForm} layout="inline" onFinish={handleChangePassword} requiredMark={false} style={{ rowGap: 8, flexWrap: 'wrap' }}>
                <Form.Item name="current_password" rules={[{ required: true, message: '请输入当前密码' }]}>
                  <Input.Password placeholder="当前密码" style={{ width: 180 }} />
                </Form.Item>
                <Form.Item name="new_password" rules={[{ required: true, message: '请输入新密码' }, { min: 6, message: '至少 6 位' }]}>
                  <Input.Password placeholder="新密码（至少 6 位）" style={{ width: 180 }} />
                </Form.Item>
                <Form.Item name="confirm" rules={[{ required: true, message: '请再次输入' }]}>
                  <Input.Password placeholder="再次输入" style={{ width: 160 }} />
                </Form.Item>
                <Form.Item style={{ margin: 0 }}>
                  <Button type="primary" htmlType="submit" loading={loading} icon={<SaveOutlined />}>
                    更新密码
                  </Button>
                </Form.Item>
              </Form>
            </SettingRow>
          </section>

          <section className="set-section">
            <SectionHead
              title="双因素认证"
              hint="登录时除密码外还要输入验证器 App 里的 6 位验证码。开启后即使密码泄露，别人也进不来。"
            />
            <SettingRow label="状态">
              <Space size={12} wrap>
                {status?.has_totp ? (
                  <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
                ) : (
                  <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
                )}
                {status?.has_totp ? (
                  <Button size="small" danger loading={loading} onClick={handleDisableTotp}>
                    关闭双因素认证
                  </Button>
                ) : totpSetupState === 'idle' ? (
                  <Button size="small" type="primary" loading={loading} onClick={handleSetupTotp} icon={<SafetyOutlined />}>
                    开启双因素认证
                  </Button>
                ) : null}
              </Space>
            </SettingRow>
            {!status?.has_totp && totpSetupState === 'setup' ? (
              <SettingRow label="绑定验证器">
                <Space direction="vertical" style={{ width: '100%' }} size={12}>
                  <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                    用验证器 App 扫描二维码，再输入 App 中显示的 6 位验证码确认绑定。
                  </Typography.Text>
                  <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                    <QRCode value={totpUri} size={180} />
                    <div style={{ flex: 1, minWidth: 200 }}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        无法扫码？手动输入密钥：
                      </Typography.Text>
                      <Typography.Paragraph copyable style={{ fontFamily: 'monospace', fontSize: 13, marginTop: 4 }}>
                        {totpSecret}
                      </Typography.Paragraph>
                      <Space wrap>
                        <Form form={codeForm} layout="inline" onFinish={handleEnableTotp}>
                          <Form.Item name="code" rules={[{ required: true, message: '请输入验证码' }, { len: 6, message: '6 位数字' }]}>
                            <Input placeholder="000000" maxLength={6} style={{ width: 130, letterSpacing: 4, textAlign: 'center' }} />
                          </Form.Item>
                          <Form.Item style={{ margin: 0 }}>
                            <Button type="primary" htmlType="submit" loading={loading}>确认启用</Button>
                          </Form.Item>
                        </Form>
                        <Button onClick={() => setTotpSetupState('idle')}>取消</Button>
                      </Space>
                    </div>
                  </div>
                </Space>
              </SettingRow>
            ) : null}
          </section>
        </>
      )}
    </>
  )
}

export default function Settings() {
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  // 已设置口令的键（服务端 `<key>_set`）：给输入框显示「已配置」提示用。
  const [secretSetKeys, setSecretSetKeys] = useState<Set<string>>(new Set())
  // 当前标签页**由 URL 推导**（`?tab=panel`），不另存一份 state：
  // 面板管理的「去配置」、旧 `/platform-config` 书签、以及侧栏点「全局配置」
  // 都只改 URL —— 单一真相源，不用写「URL 与 state 谁该覆盖谁」的同步逻辑。
  // 没有 `?tab` 时回到默认的「注册设置」。
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTab = searchParams.get('tab') || ''
  const activeTab = TAB_ITEMS.some((t) => t.key === requestedTab) ? requestedTab : 'register'

  const switchTab = (key: string) => {
    // 写进 URL：刷新后停在原处，也方便把某一栏直接发给别人。
    setSearchParams(key === 'register' ? {} : { tab: key }, { replace: true })
  }

  useEffect(() => {
    apiFetch('/config').then((data) => {
      // 邮箱相关字段（mail_provider / outlook_backend / icloud_local_*）不在这里
      // 处理 —— 它们已随「邮箱服务」一级菜单搬到各自页面，由 `MailServicePage`
      // 的 `defaults` 提供缺省值。在这里 setFieldsValue 它们没有意义（本页表单
      // 没有这些字段），反而会让后来的人以为「本页负责这些配置」。
      for (const key of SMS_BOOLEAN_KEYS) {
        data[key] = parseBooleanConfigValue(data[key])
      }
      // 库里存的是 "52,4,10"，下拉要的是数组
      data.sms_allowed_countries = parseCountryIdList(data.sms_allowed_countries)
      setSecretSetKeys(secretSetKeysFromConfig(data))
      form.setFieldsValue(data)
    })
  }, [form])

  const save = async () => {
    setSaving(true)
    try {
      // 只归一化「本次确实提交的字段」—— 不能凭空创造键。
      //
      // 踩过两次坑，都是「提交了本页不该管的字段」：
      //   1) 用 getFieldsValue(true) 会带上「已注册但当前页没有的字段」，它们的值
      //      是 undefined，归一化会把它变成有意义的默认值（空数组 / false / "2"）
      //      —— 保存一次就把用户配好的东西清空。
      //   2) 换成 getFieldsValue() 后仍有残留：挂载时的 setFieldsValue(data) 把
      //      **整份 config（110+ 键）**灌进了 form store，字段卸载不会把它们移出
      //      store。于是站在「邮箱」tab 点保存，SMS 那几个键（当前没渲染）照样被
      //      读出来。
      //
      // 关键不在「读到什么」，而在**归一化时不要补键**：原先是
      // `values[key] = parseBooleanConfigValue(values[key])` —— 键不存在时
      // 这句会把它创造出来（undefined → false / ""），再提交上去就把库里的值
      // 覆盖了。实测把 sms_allowed_countries 的 "52,4,10" 清成了 ""。
      // 加上 `in` 守卫后，没读到的键就不会被写回，两种 tab 都安全。
      const payload = stripSecretSetFlags(form.getFieldsValue() as Record<string, unknown>)

      for (const key of SMS_BOOLEAN_KEYS) {
        if (key in payload) payload[key] = parseBooleanConfigValue(payload[key])
      }
      if ('sms_allowed_countries' in payload) {
        payload.sms_allowed_countries = parseCountryIdList(payload.sms_allowed_countries).join(',')
      }
      // 口令留空 = 不修改（`sms_api_key` 等由服务端定为口令键）。
      dropEmptySecrets(payload, SECRET_FIELD_KEYS)

      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: payload }) })
      // 回写也要带守卫：`sms_allowed_countries` 不在 payload 里时（站在别的
      // tab 保存），`parseCountryIdList(undefined)` 返回 []，写回 store 会把
      // 用户已选的标签清空；再保存一次就把它写成了空串 —— 实测从「注册设置」
      // 保存后回「手机接码」，标签从「泰国(52) 菲律宾(4) 越南(10)」变成空。
      form.setFieldsValue({
        ...('sms_allowed_countries' in payload
          ? { sms_allowed_countries: parseCountryIdList(payload.sms_allowed_countries) }
          : {}),
        ...Object.fromEntries(
          SMS_BOOLEAN_KEYS.filter((key) => key in payload).map((key) => [key, payload[key]]),
        ),
      })
      message.success('保存成功')
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const currentTab = TAB_ITEMS.find((t) => t.key === activeTab) as TabConfig

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--section-gap)' }}>
      <PageHeader
        title="全局配置"
        subtitle="配置将持久化保存，注册任务自动使用"
      />

      <div className="settings-layout" style={{ display: 'flex', gap: 28, alignItems: 'flex-start' }}>
        {/* 这个 Tabs 只当左侧导航用 —— 真正的内容在右边那个 div 里
            （安全 tab 与其余 tab 的内容结构差异太大，套进 Tabs 的 pane 反而更乱）。
            antd 在 tabPosition="left" 时会把 nav 与 pane 并排放在 Tabs 根内，
            所以外层宽度必须是「nav 宽度 + pane 宽度」；pane 是空的，
            用 CSS 把它压掉，200px 就全部留给 nav 了。 */}
        <div className="settings-tab-rail">
          <Tabs
            tabPosition="left"
            activeKey={activeTab}
            onChange={switchTab}
            items={TAB_ITEMS.map((t) => ({
              key: t.key,
              label: (
                <span>
                  {t.icon}
                  <span style={{ marginLeft: 8 }}>{t.label}</span>
                </span>
              ),
            }))}
          />
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          {/* 每个子菜单顶部一行「这一页管什么」+ ⓘ 展开更长背景。
              正文里不再铺解释文字 —— 那是这一页以前最大的噪音来源。 */}
          {currentTab.summary ? (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                marginBottom: 18,
                fontSize: 13,
                color: 'var(--text-muted)',
              }}
            >
              <span>{currentTab.summary}</span>
              <HelpTip text={currentTab.hint} />
            </div>
          ) : null}

          {activeTab === 'security' ? (
            <SecurityPanel />
          ) : activeTab === 'panel' ? (
            // 面板配置自带 Form 与保存按钮（它的字段来自面板注册表，
            // 与上面那份全局表单不是一批键）。套进外层 Form 会出现两层
            // Form 实例，保存时互相看不到对方的字段。
            <PanelConfigPanel />
          ) : activeTab === 'maintenance' ? (
            <MaintenancePanel />
          ) : activeTab === 'backup' ? (
            // 数据迁移：导出是二进制下载、导入是文件上传，与配置表单无关。
            <DataBackupPanel />
          ) : (
            <Form form={form} layout="vertical">
              {activeTab === 'captcha' ? <SolverStatus /> : null}
              {activeTab === 'sms' ? <SmsProbePanel form={form} /> : null}
              {currentTab.custom === 'register' ? (
                <RegisterSettingsPanel />
              ) : (
                currentTab.sections.map((section) => (
                  <ConfigSection
                    key={section.title}
                    section={section}
                    secretSetKeys={secretSetKeys}
                  />
                ))
              )}
              <Button
                type="primary"
                icon={<SaveOutlined />}
                onClick={save}
                loading={saving}
                style={{ marginTop: 24 }}
              >
                {saved ? '已保存 ✓' : '保存配置'}
              </Button>
            </Form>
          )}
        </div>
      </div>
    </div>
  )
}
