import { useCallback, useEffect, useMemo, useState } from 'react'
import { App, Button, Form, Input, Select, Switch } from 'antd'
import { ApiOutlined, ReloadOutlined, SaveOutlined, WarningOutlined } from '@ant-design/icons'
import { resolveFeatureEnabledConfig } from '@/components/settings/ConfigPanels'
import { HelpTip, SectionHead, SettingRow } from '@/components/settings/SettingsPrimitives'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { PANEL_ICONS } from '@/lib/panelIcons'
import { apiFetch } from '@/lib/utils'

/**
 * 自动维护 —— 独立的子菜单。
 *
 * 分两节，因为它们是**两件不同的事**：
 *
 * - 本地维护：本应用自己定时跑（检测 / 刷新），不经过任何面板。**按平台分**。
 * - 面板与本地同步：比对本地与某个面板的凭据，本地较新就推过去。**按面板分**。
 *
 * 第一版把「自动刷新 Token」和面板开关挤在同一节里，用户分不清「关掉这个
 * 开关，是本地不刷新了，还是远端不同步了」—— 这次分平台展开，一个平台下
 * 就是它自己的三条规则，作用域不再靠猜。
 *
 * ## 为什么是「分节 + 行」而不是「一平台一张卡」
 *
 * 本地维护不是「一个有身份、有连接状态、能打开的外部服务」，它就是一个定时
 * 任务。给它套面板那种卡壳会让两件事看起来一样。面板同步仍然用卡 —— 那里
 * 卡片确实在表达「这是哪个面板、配没配好」。
 *
 * ## 为什么规则关闭时不显示参数
 *
 * 一条关着的规则只有名字和开关，打开后才露出它要问什么（范围 / 比例 / 时间）。
 * 24 个新键一次性平铺出来是一面墙，而其中大半属于用户根本没启用的规则。
 * 这也让「我到底配了什么」变成可数的：能看到几组参数，就是启用了几条规则。
 */

type MaintenanceMode = 'panel'

interface MaintenanceCard {
  key: string
  label: string
  mode: MaintenanceMode
  /** 开关的配置键 */
  switchKey: string
  /** 卡体那句「怎么维护的」 */
  hint: string
  /** ⓘ 里的完整说明 */
  detail: string
}

/** 面板同步：一个面板一张卡。 */
const PANEL_CARDS: MaintenanceCard[] = [
  {
    key: 'chatgpt2api',
    label: 'ChatGPT2API',
    mode: 'panel',
    switchKey: 'chatgpt2api_auto_sync_enabled',
    hint: '本地凭证较新时，自动推送到面板。',
    detail:
      '定期对比本地与 ChatGPT2API 远端的凭证：本地较新就推送过去，并清理推送后留下的旧记录' +
      '（ChatGPT2API 每次上传新增一条，不清理会积重复）。远端较新时不推 —— 那会用本地旧凭证' +
      '覆盖远端的新凭证。',
  },
  {
    key: 'grok2api',
    label: 'Grok2API',
    mode: 'panel',
    switchKey: 'grok2api_auto_sync_enabled',
    hint: '本地 SSO 较新时，自动推送到面板。',
    detail:
      '定期对比本地与 Grok2API 远端的账号：本地较新就重新导入 SSO。' +
      'Grok2API 的导入按邮箱原地更新，所以不需要清理旧记录。',
  },
  {
    key: 'cpa',
    label: 'CPA 面板',
    mode: 'panel',
    switchKey: 'cpa_auto_sync_enabled',
    hint: '本地 ChatGPT 凭证较新时，自动推送到面板。',
    detail:
      '定期对比本地与 CPA 的 auth-file：本地较新就重新上传（按邮箱原地覆盖）。' +
      'Grok 自 2026-10-27 起不再支持 CPA，所以这里只同步 ChatGPT 账号。',
  },
  {
    key: 'sub2api',
    label: 'Sub2API',
    mode: 'panel',
    switchKey: 'sub2api_auto_sync_enabled',
    hint: '本地凭证较新时，自动推送到面板。',
    detail:
      '定期对比本地与 Sub2API 的账号：本地较新就推送，并清理推送后留下的旧记录' +
      '（Sub2API 每次上传新增一条，不清理会积重复）。',
  },
]

/** 本地维护的平台。顺序 = 界面顺序。 */
const LOCAL_PLATFORMS = [
  { prefix: 'chatgpt', label: 'ChatGPT', subject: '账号' },
  { prefix: 'grok', label: 'Grok', subject: '账号' },
] as const

const PROBE_MODE_OPTIONS = [
  { value: 'sample', label: '抽检' },
  { value: 'all', label: '全部检测' },
]

const PROBE_STRATEGY_OPTIONS = [
  { value: 'oldest', label: '优先最久未检测的' },
  { value: 'random', label: '完全随机' },
]

const REFRESH_SCOPE_OPTIONS = [
  { value: 'failed', label: '只刷失效' },
  { value: 'all', label: '全部' },
  { value: 'random', label: '随机抽一部分' },
]

/**
 * 刷新策略（用户给的三个档）。
 *
 * 措辞按「会不会动远端」写，而不是按内部函数名 —— 用户要判断的是
 * 「这一步会不会碰面板」，不是「调用了哪个分支」。
 */
const SYNC_STRATEGY_OPTIONS = [
  { value: 'local_only', label: '不自动同步远程（只动本地）' },
  { value: 'remote_first', label: '先同步远程 → 本地刷新 → 再同步回远程' },
  { value: 'local_then_push', label: '本地刷新 → 同步到远程' },
]

/**
 * 本页要读写的全部配置键。
 *
 * **显式列出**而不是 `form.getFieldsValue(true)`：后者会把整份 config
 * （101 个键）提交上去，其中未渲染的字段值是 undefined，归一化会把它们
 * 变成「有意义的默认值」，等于保存一次就清空用户配好的东西（踩过）。
 */
const LOCAL_KEYS: string[] = LOCAL_PLATFORMS.flatMap(({ prefix }) => [
  `${prefix}_auto_probe_enabled`,
  `${prefix}_probe_mode`,
  `${prefix}_probe_sample_percent`,
  `${prefix}_probe_strategy`,
  `${prefix}_probe_interval`,
  `${prefix}_auto_refresh_enabled`,
  `${prefix}_refresh_scope`,
  `${prefix}_refresh_random_count`,
  `${prefix}_refresh_interval`,
  `${prefix}_refresh_disable_banned`,
  `${prefix}_sync_strategy`,
])

const LOG_KEYS = ['log_cleanup_enabled', 'log_cleanup_keep_days', 'log_cleanup_interval']
const ALL_KEYS = [...LOCAL_KEYS, ...LOG_KEYS, ...PANEL_CARDS.map((c) => c.switchKey)]

/** 布尔键：提交时要归一成 "0"/"1"，不能把 antd 的 true/false 直接塞进文本列。 */
const BOOLEAN_KEYS = new Set([
  ...LOCAL_PLATFORMS.flatMap(({ prefix }) => [
    `${prefix}_auto_probe_enabled`,
    `${prefix}_auto_refresh_enabled`,
    `${prefix}_refresh_disable_banned`,
  ]),
  'log_cleanup_enabled',
  ...PANEL_CARDS.map((c) => c.switchKey),
])

/**
 * 开关的界面默认值。
 *
 * `*_refresh_disable_banned` 默认**开**：这是加这个开关之前就有的行为
 * （动作链无条件做这件事），默认关等于升级时静默取消一个一直在生效的动作。
 * 其余开关一律默认关 —— 让机器自己动起来必须是显式选择。
 */
const BOOLEAN_DEFAULTS: Record<string, boolean> = Object.fromEntries(
  LOCAL_PLATFORMS.map(({ prefix }) => [`${prefix}_refresh_disable_banned`, true]),
)

/** 字符串键的默认值。`load()` 时把库里是空的键补成默认，界面才不会开出空框。 */
const TEXT_DEFAULTS: Record<string, string> = {
  log_cleanup_keep_days: '7',
  log_cleanup_interval: '24h',
}
for (const { prefix } of LOCAL_PLATFORMS) {
  Object.assign(TEXT_DEFAULTS, {
    [`${prefix}_probe_mode`]: 'sample',
    [`${prefix}_probe_sample_percent`]: '20',
    [`${prefix}_probe_strategy`]: 'oldest',
    [`${prefix}_probe_interval`]: '6h',
    [`${prefix}_refresh_scope`]: 'failed',
    [`${prefix}_refresh_random_count`]: '50',
    [`${prefix}_refresh_interval`]: '6h',
    [`${prefix}_sync_strategy`]: 'local_only',
  })
}

/** 一行开关（规则头）。开关关着时不渲染 `children`。 */
function RuleBlock({
  label,
  hint,
  switchKey,
  switchLabel,
  children,
}: {
  label: string
  hint?: string
  switchKey: string
  /** 读屏用的完整名字：行标签只有「自动检测」，说清是哪个平台的哪件事 */
  switchLabel: string
  children?: React.ReactNode
}) {
  const enabled = Form.useWatch(switchKey)
  return (
    <>
      <SettingRow label={<span className="maint-rule__title">{label}<HelpTip text={hint} /></span>}>
        <Form.Item name={switchKey} valuePropName="checked" noStyle>
          <Switch checkedChildren="开启" unCheckedChildren="关闭" aria-label={switchLabel} />
        </Form.Item>
      </SettingRow>
      {enabled ? <div className="maint-rule__body">{children}</div> : null}
    </>
  )
}

/** 一行文本参数（比例 / 条数）。时间类参数走 IntervalRow —— 见下。 */
function TextRow({
  name,
  label,
  hint,
  suffix,
  width = 120,
  placeholder,
}: {
  name: string
  label: string
  hint?: string
  suffix?: string
  width?: number
  placeholder?: string
}) {
  return (
    <SettingRow label={label} hint={hint}>
      <Form.Item name={name} noStyle>
        <Input style={{ width }} placeholder={placeholder} />
      </Form.Item>
      {suffix ? <span className="maint-rule__suffix">{suffix}</span> : null}
    </SettingRow>
  )
}

/**
 * 一行间隔参数：数字 + 单位。
 *
 * 用户 2026-10-31 明确要的是「执行间隔」而不是「执行时间」——「每天凌晨三点」
 * 只是**间隔 24 小时**的一个特例，而「每 6 小时」「每 30 分钟」这种粒度用固定
 * 时刻根本表达不了。
 *
 * 存的是**一个字符串**（`6h` / `30m` / `1d`）：解析归后端
 * （`services/local_maintenance.parse_interval`），前端只负责拆成两个控件再拼
 * 回去。不在前端算秒数 —— 那样两边的钳制范围会各写一份、迟早不一致。
 */
const INTERVAL_UNITS = [
  { value: 'm', label: '分钟' },
  { value: 'h', label: '小时' },
  { value: 'd', label: '天' },
]

function splitInterval(value: unknown, fallbackUnit: string): { amount: string; unit: string } {
  const text = String(value ?? '').trim().toLowerCase()
  const m = /^(\d+)\s*([mhd])$/.exec(text)
  if (m) return { amount: m[1], unit: m[2] }
  if (/^\d+$/.test(text)) return { amount: text, unit: fallbackUnit }
  return { amount: '', unit: fallbackUnit }
}

function IntervalRow({
  name,
  label,
  hint,
  defaultUnit = 'h',
}: {
  name: string
  label: string
  hint?: string
  defaultUnit?: string
}) {
  const form = Form.useFormInstance()
  const raw = Form.useWatch(name)
  const { amount, unit } = splitInterval(raw, defaultUnit)

  const commit = (nextAmount: string, nextUnit: string) => {
    // 数字位允许清空（用户从「6」删到空再打新数字），但不写回 `h` 这种空壳 ——
    // 后端解析空值 = 任务不跑，会让「正在编辑」变成「悄悄停掉这条规则」。
    if (!nextAmount) return
    form.setFieldValue(name, `${nextAmount}${nextUnit}`)
  }

  return (
    <SettingRow label={label} hint={hint}>
      {/*
        必须先把这个字段**注册**进 Form，`Form.useWatch(name)` 才看得到它。
        下面那两个控件是普通受控组件（值是拼出来的 `6h` 这种字符串，
        没有哪个 antd 控件直接对应），而 `useWatch` 对**未注册**字段一律返回
        undefined —— 实测后果是数字框永远空着，误以为没配过
        （同样的坑 `ConfigPanels.tsx` 里注释过：`_set` 键 watch 不到）。
        `hidden` 只用于注册，不显示。
      */}
      <Form.Item name={name} hidden>
        <Input />
      </Form.Item>
      <span className="maint-interval">
        <Input
          className="maint-interval__amount"
          value={amount}
          inputMode="numeric"
          aria-label={`${label}的数值`}
          onChange={(e) => commit(e.target.value.replace(/[^0-9]/g, ''), unit)}
        />
        <Select
          className="maint-interval__unit"
          value={unit}
          options={INTERVAL_UNITS}
          aria-label={`${label}的单位`}
          onChange={(next) => commit(amount || '1', String(next))}
        />
      </span>
    </SettingRow>
  )
}

/** 一行下拉参数。 */
function SelectRow({
  name,
  label,
  hint,
  options,
  width = 240,
}: {
  name: string
  label: string
  hint?: string
  options: { value: string; label: string }[]
  width?: number
}) {
  return (
    <SettingRow label={label} hint={hint}>
      <Form.Item name={name} noStyle>
        <Select options={options} style={{ width }} />
      </Form.Item>
    </SettingRow>
  )
}

function LocalPlatformSection({
  prefix,
  label,
  subject,
}: {
  prefix: string
  label: string
  subject: string
}) {
  const probeMode = Form.useWatch(`${prefix}_probe_mode`)
  const refreshScope = Form.useWatch(`${prefix}_refresh_scope`)

  return (
    <section className="maint-platform">
      <h3 className="maint-platform__name">{label}</h3>

      <RuleBlock
        label="自动检测"
        hint={
          `定时检查${subject}还能不能用。` +
          `「抽检」按你设的比例挑一批：默认优先挑**最久没检测过的**，` +
          `从没检测过的新号排在最前 —— 抽检的意义是用有限的配额覆盖最可能已经坏掉的那批。` +
          `「完全随机」用在「我怀疑某批号一起坏了、但不知道是哪批」的时候。`
        }
        switchKey={`${prefix}_auto_probe_enabled`}
        switchLabel={`${label} 自动检测${subject}`}
      >
        <SelectRow
          name={`${prefix}_probe_mode`}
          label="检测范围"
          options={PROBE_MODE_OPTIONS}
          hint="「全部检测」会把这批号一个个过一遍，耗时长、出口消耗大。"
        />
        {probeMode === 'sample' ? (
          <>
            <TextRow
              name={`${prefix}_probe_sample_percent`}
              label="抽检比例"
              suffix="%"
              placeholder="20"
              hint="按账号总数算。100 个号、20% = 每次抽 20 个。"
            />
            <SelectRow
              name={`${prefix}_probe_strategy`}
              label="抽检策略"
              options={PROBE_STRATEGY_OPTIONS}
            />
          </>
        ) : null}
        <IntervalRow
          name={`${prefix}_probe_interval`}
          label="执行间隔"
          hint="每隔这么久跑一轮。以「距上次跑完」算，不是对齐整点 —— 所以重启进程不会改变节奏。最短 5 分钟，最长 30 天。"
        />
      </RuleBlock>

      <RuleBlock
        label="自动刷新 Token"
        hint={
          `定时给失效的${subject}换新凭证 —— 和手动点「刷新 Token」走同一条链，` +
          `不是另一套实现。封禁的号不会去刷：那只是白费出口 IP，还会把真正的` +
          `封禁信号淹在噪声里。`
        }
        switchKey={`${prefix}_auto_refresh_enabled`}
        switchLabel={`${label} 自动刷新 Token`}
      >
        <SelectRow
          name={`${prefix}_refresh_scope`}
          label="刷新范围"
          options={REFRESH_SCOPE_OPTIONS}
          hint="正常工作的号默认不刷：刷新对上游就是一次登录动作，对好号做这个只有风险、没有收益。"
        />
        {refreshScope === 'random' ? (
          <TextRow
            name={`${prefix}_refresh_random_count`}
            label="随机条数"
            suffix="个"
            placeholder="50"
            hint="一次刷太多，出口 IP 的表现就像攻击。随机摊开、每天一小批，才不容易触发限流。"
          />
        ) : null}
        <IntervalRow
          name={`${prefix}_refresh_interval`}
          label="执行间隔"
          hint="每隔这么久跑一轮。以「距上次跑完」算，不是对齐整点。最短 5 分钟，最长 30 天。"
        />
        <SettingRow
          label="封禁账号标记为禁用"
          hint="刷新时如果上游回答这个号已被停用，就把它标成「禁用」并停止重试。关掉的话它会一直留在待刷新队列里。"
        >
          <Form.Item name={`${prefix}_refresh_disable_banned`} valuePropName="checked" noStyle>
            <Switch checkedChildren="开启" unCheckedChildren="关闭" aria-label={`${label} 检测到封禁就标记为禁用`} />
          </Form.Item>
        </SettingRow>
        <SelectRow
          name={`${prefix}_sync_strategy`}
          label="刷新策略"
          options={SYNC_STRATEGY_OPTIONS}
          width={340}
          hint={
            '决定这一轮要不要和面板对账。' +
            '「先同步远程」不是可有可无的一步：面板那侧可能已经用别的路径换过凭证，' +
            '本地拿着旧的去刷等于白刷一次，上游还会把旧凭证判失效。'
          }
        />
      </RuleBlock>
    </section>
  )
}

export function MaintenancePanel() {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [configured, setConfigured] = useState<Record<string, boolean>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [config, panelData] = await Promise.all([
        apiFetch('/config') as Promise<Record<string, unknown>>,
        apiFetch('/integrations/panels') as Promise<{
          items?: { key: string; configured: boolean }[]
        }>,
      ])
      const values: Record<string, unknown> = {}
      for (const key of ALL_KEYS) {
        const raw = config[key]
        if (BOOLEAN_KEYS.has(key)) {
          // 库里存 "0"/"1" 字符串，antd Switch 把非空字符串一律当真 ——
          // 不归一的话关着的开关会渲染成打开（实测复现）。
          values[key] = resolveFeatureEnabledConfig(raw, BOOLEAN_DEFAULTS[key] ?? false)
        } else {
          const text = raw === undefined || raw === null ? '' : String(raw)
          values[key] = text.trim() || TEXT_DEFAULTS[key] || ''
        }
      }
      form.setFieldsValue(values)

      const items = Array.isArray(panelData.items) ? panelData.items : []
      const next: Record<string, boolean> = {}
      for (const item of items) next[item.key] = Boolean(item.configured)
      setConfigured(next)
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : '加载自动维护配置失败')
    } finally {
      setLoading(false)
    }
    // `message` / `form` 都是 antd 的稳定实例 —— 放进依赖不会造成循环。
  }, [form, message])

  useEffect(() => {
    void load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {}
      for (const key of ALL_KEYS) {
        const value = form.getFieldValue(key)
        if (value === undefined) continue
        payload[key] = BOOLEAN_KEYS.has(key) ? parseBooleanConfigValue(value) : String(value).trim()
      }
      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: payload }) })
      message.success('自动维护配置已保存')
      await load()
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // 平台节是静态的，用 `useMemo` 只是为了让它的引用稳定（避免每帧重挂载表单行）。
  const platformSections = useMemo(
    () =>
      LOCAL_PLATFORMS.map(({ prefix, label, subject }) => (
        <LocalPlatformSection key={prefix} prefix={prefix} label={label} subject={subject} />
      )),
    [],
  )

  return (
    <Form form={form} layout="vertical">
      {/* 同 PanelConfigPanel：标题由页面顶部统一给，这里只留刷新。 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button
          size="small"
          type="text"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={load}
        >
          刷新
        </Button>
      </div>

      <section className="set-section">
        <SectionHead
          title="本地维护"
          hint="由本应用自己定时执行，不经过任何面板。按平台分开配 —— 一个平台下就是它自己的规则。"
        />
        {platformSections}
      </section>

      <section className="set-section">
        <SectionHead
          title="日志自动清理"
          hint="按保留天数删任务历史。只按天数删，没有「全清」档位 —— 定时执行的动作不该带「抹掉全部」这种选项。"
        />
        <RuleBlock
          label="自动清理"
          hint={
            '定时删掉超过保留天数的任务历史（「任务历史」页那些记录）。' +
            '注册失败的原因、刷新失败的错误码都记在这里，保留期太短会查不到' +
            '「上周那个号为什么掉」。'
          }
          switchKey="log_cleanup_enabled"
          switchLabel="日志自动清理"
        >
          <TextRow
            name="log_cleanup_keep_days"
            label="保留天数"
            suffix="天"
            placeholder="7"
            hint="超过这个天数的记录会被删掉。最少 1 天。"
          />
          <IntervalRow
            name="log_cleanup_interval"
            label="执行间隔"
            hint="每隔这么久清一次。以「距上次跑完」算，不是对齐整点。最短 5 分钟，最长 30 天。"
          />
        </RuleBlock>
      </section>

      <section className="set-section">
        <SectionHead
          title="面板与本地同步"
          hint="比对本地与面板的凭据，本地较新就推过去。面板没配连接参数时开关开着也不会跑。"
        />
        <div className="panel-grid">
          {PANEL_CARDS.map((card) => (
            <section className="panel-card" key={card.key}>
              <header className="panel-card__head">
                <div className={`panel-card__icon${(configured[card.key] ?? false) ? ' is-on' : ''}`}>
                  {PANEL_ICONS[card.key] ?? <ApiOutlined />}
                </div>
                <div className="panel-card__id">
                  <div className="panel-card__name">
                    {card.label}
                    <HelpTip text={card.detail} />
                  </div>
                </div>
              </header>
              <div className="maint-card__body">
                <div className="maint-card__hint">
                  {card.hint}
                  {!(configured[card.key] ?? false) ? (
                    // 必须显眼：开关开着、面板却没配地址 —— 界面上一切正常，
                    // 实际上调度器每轮都因为「地址为空」直接返回 0，什么也不会发生。
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 5,
                        color: 'var(--warning)',
                        marginTop: 6,
                        fontSize: 12,
                      }}
                    >
                      <WarningOutlined />
                      面板还没配置连接参数 —— 开关开着也不会跑。
                    </div>
                  ) : null}
                </div>
                {/* 开关不带文字标签：卡头已经说明是哪个面板，
                    这里只表达「开/关」。标签留给 aria-label，读屏用户照样清楚。 */}
                <Form.Item name={card.switchKey} valuePropName="checked" noStyle>
                  <Switch aria-label={`${card.label} 自动同步`} />
                </Form.Item>
              </div>
            </section>
          ))}
        </div>
      </section>

      <Button
        type="primary"
        icon={<SaveOutlined />}
        onClick={save}
        loading={saving}
        style={{ marginTop: 20 }}
      >
        保存自动维护配置
      </Button>
    </Form>
  )
}
