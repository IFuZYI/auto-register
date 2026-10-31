import { useCallback, useEffect, useState } from 'react'
import { App, Button, Form, Switch } from 'antd'
import { ApiOutlined, ReloadOutlined, SaveOutlined, WarningOutlined } from '@ant-design/icons'
import { resolveFeatureEnabledConfig } from '@/components/settings/ConfigPanels'
import { HelpTip, SectionHead, SettingRow } from '@/components/settings/SettingsPrimitives'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { PANEL_ICONS } from '@/lib/panelIcons'
import { apiFetch } from '@/lib/utils'

/**
 * 自动维护 —— 独立的子菜单。
 *
 * 原来这两个开关塞在「面板配置」里某张卡的最下面，和它们的实际作用域对不上：
 * 「自动刷新 Token」维护的是**本地账号库**（根本不经过任何面板），
 * 「凭证自动同步」维护的才是面板。放在同一个节里，用户没法判断
 * 「关掉这个开关，是本地不刷新了，还是远端不同步了」。
 *
 * 现在按**维护方式**分卡：
 *
 * - 本地维护：本应用自己定时刷新 ChatGPT 的 AT/RT。
 * - 面板同步：比对本地与某个面板的凭据，本地较新就推过去。
 *
 * 一个平台一张卡，卡上写明它是哪一种 —— 这正是最需要一眼看清的信息。
 */

type MaintenanceMode = 'local' | 'panel'

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

const CARDS: MaintenanceCard[] = [
  {
    key: 'chatgpt',
    label: 'ChatGPT',
    mode: 'local',
    switchKey: 'chatgpt_auto_refresh_enabled',
    hint: 'AT 临期、已过期或已失效时，自动刷新 Token。',
    detail:
      '定时扫描 ChatGPT 账号：AT 临期（剩余 ≤ 24h）、已过期或已失效时自动刷新 Token。' +
      '临期账号在到期前**随机**一个时刻刷新（至少提前 1 小时，不设固定更新时刻），' +
      '失效/过期账号尽快尝试；失败自动退避重试，连续失败 3 次或已封禁的账号不再自动尝试。' +
      '检测出封禁的账号会标注为「禁用」，且不参与凭据同步与对比。' +
      '手动刷新 Token 与注册任务不受影响。',
  },
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

/** 本地维护（不经面板）。目前只有 ChatGPT 一条 —— 它有自己的 AT/RT 生命周期。 */
const LOCAL_CARD = CARDS.find((card) => card.mode === 'local') as MaintenanceCard
/** 面板同步：一个面板一张卡。 */
const PANEL_CARDS = CARDS.filter((card) => card.mode === 'panel')

function MaintenanceCardView({
  card,
  configured,
}: {
  card: MaintenanceCard
  /** 对应面板是否已配置连接参数；本地维护永远为真 */
  configured: boolean
}) {
  return (
    <section className="panel-card">
      <header className="panel-card__head">
        <div className={`panel-card__icon${configured ? ' is-on' : ''}`}>
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
          {!configured ? (
            // 必须显眼：开关开着、面板却没配地址 —— 界面上一切正常，
            // 实际上调度器每轮都因为「地址为空」直接返回 0，什么也不会发生。
            // 用 warning 色而不是灰色小字，就是为了不让这种「静默不工作」过关。
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
        {/* 开关不带文字标签：卡头已经说明是哪个平台、哪种方式，
            这里只表达「开/关」。标签留给 aria-label，读屏用户照样清楚。 */}
        <Form.Item
          name={card.switchKey}
          valuePropName="checked"
          noStyle
        >
          <Switch aria-label={`${card.label} 自动同步`} />
        </Form.Item>
      </div>
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
      for (const card of CARDS) {
        // 库里存 "0"/"1" 字符串，antd Switch 把非空字符串一律当真 ——
        // 不归一的话关着的开关会渲染成打开（实测复现）。
        values[card.switchKey] = resolveFeatureEnabledConfig(config[card.switchKey], false)
      }
      form.setFieldsValue(values)

      const items = Array.isArray(panelData.items) ? panelData.items : []
      const next: Record<string, boolean> = { chatgpt: true }
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
      for (const card of CARDS) {
        const value = form.getFieldValue(card.switchKey)
        // 只写本页确实读到的键 —— 用 `getFieldsValue(true)` 会把整份 config
        // （110+ 键）提交上去，其中未渲染的字段值是 undefined，归一化会把它们
        // 变成有意义的默认值，等于保存一次就清空用户配好的东西（踩过）。
        if (value !== undefined) payload[card.switchKey] = parseBooleanConfigValue(value)
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

      {/* 本地维护只有一项、而且不是「一个面板」—— 用普通配置行，不用卡片。
          卡片是给「有身份、有连接状态、能打开」的外部服务用的；给一条本地
          定时任务套同样的卡壳，等于把它说成是另一个面板。
          改造前它和面板同步开关挤在同一节里，用户分不清「关掉的是哪一个」，
          根子就在这里。 */}
      <section className="set-section">
        <SectionHead title="本地维护" hint="由本应用自己定时执行，不经过任何面板。" />
        <SettingRow label="自动刷新 Token" hint={LOCAL_CARD.detail}>
          <Form.Item name={LOCAL_CARD.switchKey} valuePropName="checked" noStyle>
            <Switch
              checkedChildren="开启"
              unCheckedChildren="关闭"
              aria-label="ChatGPT 自动刷新 Token"
            />
          </Form.Item>
        </SettingRow>
      </section>

      <section className="set-section">
        <SectionHead
          title="面板与本地同步"
          hint="比对本地与面板的凭据，本地较新就推过去。面板没配连接参数时开关开着也不会跑。"
        />
        <div className="panel-grid">
          {PANEL_CARDS.map((card) => (
            <MaintenanceCardView
              key={card.key}
              card={card}
              configured={configured[card.key] ?? false}
            />
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
