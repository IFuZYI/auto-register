import { useEffect, useState } from 'react'
import { Card, Col, Collapse, Form, InputNumber, Row, Select, Space, Tag, Tooltip, Typography } from 'antd'
import { QuestionCircleOutlined } from '@ant-design/icons'
import { EXECUTOR_LABELS, type ExecutorType } from '@/lib/platforms'
import { apiFetch } from '@/lib/utils'

/**
 * 「注册设置」的渐进式面板。
 *
 * 设计取舍（原先是 3 张平铺的卡片，所有字段一眼全露）：
 *
 * 1. **默认参数与平台方式分开**。前者是每次开任务都会用到的数字
 *    （数量/并发/延迟/重试），后者是「偶尔调一次就不动」的注册方式。
 *    混在一起时，用户为了改并发数要在一屏平台下拉里找。
 * 2. **平台方式收进折叠面板**，折叠时只显示当前选择的摘要
 *    （「浏览器 · 真实页面」）—— 不展开也能确认配置，展开才编辑。
 * 3. **每个平台一张卡**，不再把所有平台的方式堆在一个列表里。
 *
 * 数据来源：`GET /api/platforms`（各插件的 `registration_modes` 声明）。
 * 界面不硬编码任何注册方式取值 —— 那是插件内部实现的细节。
 */

interface RegistrationModeSpec {
  key: string
  label: string
  desc?: string
  default?: string | number
  /** 不填 = 下拉；`number` = 数值输入框 */
  type?: 'number'
  min?: number
  max?: number
  options?: { value: string; label: string }[]
}

interface PlatformInfo {
  name: string
  display_name: string
  supported_executors?: string[]
  /** 执行器显示名（平台专属执行器靠它，如 Grok 的 `browser`） */
  executor_labels?: Record<string, string>
  registration_modes?: RegistrationModeSpec[]
}

/** 默认注册参数：注册任务打开时预填的值。 */
const DEFAULT_PARAM_FIELDS: {
  key: string
  label: string
  hint: string
  min: number
  max?: number
  step?: number
  precision?: number
  placeholder: string
}[] = [
  {
    key: 'register_count',
    label: '注册数量',
    hint: '一次任务注册几个账号',
    min: 1,
    placeholder: '1',
  },
  {
    key: 'register_concurrency',
    label: '并发数',
    hint: '同时跑几个。并发越高越容易被目标站限流，建议不超过 3',
    min: 1,
    max: 20,
    placeholder: '1',
  },
  {
    key: 'register_delay_seconds',
    label: '每个注册延迟（秒）',
    hint: '每个账号启动前的等待，用于错开请求',
    min: 0,
    step: 0.5,
    precision: 1,
    placeholder: '0',
  },
  {
    key: 'register_retry_times',
    label: '失败重试轮数',
    hint: '整条流程失败后自动重开一轮（换新邮箱 / 号码 / 会话）；0 表示失败即止',
    min: 0,
    max: 10,
    precision: 0,
    placeholder: '1',
  },
]

function DefaultParamsCard() {
  return (
    <Card
      title="默认注册参数"
      style={{ marginBottom: 'var(--section-gap)' }}
      styles={{ body: { paddingBottom: 8 } }}
    >
      <Typography.Paragraph type="secondary" style={{ marginTop: 0, fontSize: 13, maxWidth: 'var(--w-prose)' }}>
        新建注册任务时预填这些值，任务页里仍可临时修改。
      </Typography.Paragraph>
      <Row gutter={[16, 8]}>
        {DEFAULT_PARAM_FIELDS.map((field) => (
          <Col key={field.key} xs={24} sm={12} lg={6}>
            <Form.Item
              label={
                <Space size={4}>
                  {field.label}
                  <Tooltip title={field.hint}>
                    <QuestionCircleOutlined style={{ color: 'var(--text-muted)', fontSize: 12 }} />
                  </Tooltip>
                </Space>
              }
              name={field.key}
              style={{ marginBottom: 12 }}
            >
              <InputNumber
                min={field.min}
                max={field.max}
                step={field.step}
                precision={field.precision}
                placeholder={field.placeholder}
                style={{ width: '100%' }}
              />
            </Form.Item>
          </Col>
        ))}
      </Row>
    </Card>
  )
}

/** 折叠时显示的摘要：把已选值拼成一行，不展开也能确认配置。 */
function modeSummary(
  options: { value: string; label: string }[] | undefined,
  value: unknown,
  defaultValue?: string | number,
): string {
  // 未设置（空串 / null）时回落到插件声明的默认值 —— 运行时对「没配」
  // 用的就是它，摘要要显示**实际生效**的值。此前空串直接显示「未设置」，
  // 与运行时的真实行为不一致（例如发码方式空着时实际走协议）。
  const raw = value === undefined || value === null || value === '' ? defaultValue : value
  if (raw === undefined || raw === null || raw === '') return '未设置'
  // 数值项（type: 'number'）：没有选项表可查，直接显示数字
  if (!options || options.length === 0) return String(raw)
  const current = String(raw)
  return options.find((o) => o.value === current)?.label ?? current
}

function PlatformModeCard({ platform }: { platform: PlatformInfo }) {
  const modes = platform.registration_modes || []
  const executors = (platform.supported_executors || []) as ExecutorType[]
  // 标签优先用平台声明的（`executor_labels`），缺失时回落通用表 ——
  // 平台专属执行器（Grok 的 `browser`）不在通用表里，回落会显示原始值。
  const labels = platform.executor_labels || {}
  const executorLabelOf = (value: string) => labels[value] || EXECUTOR_LABELS[value] || value
  // 用 Form.useWatch 读当前值做摘要：Collapse 收起时 Form.Item 不渲染，
  // 但 form store 里仍有值，所以摘要始终准确。
  const form = Form.useFormInstance()
  // 执行器按平台存：每个平台一个配置键（`<platform>_executor`）。
  // 不做全局默认 —— 各平台支持的集合不同，一个全局值必然对某些平台无效。
  const executorKey = `${platform.name}_executor`

  return (
    <Card
      key={platform.name}
      title={
        <Space size={8}>
          <span>{platform.display_name || platform.name}</span>
          {executors.length ? (
            <Space size={4} wrap>
              {executors.map((e) => (
                <Tag key={e} style={{ marginInlineEnd: 0, fontWeight: 400 }}>
                  {executorLabelOf(e)}
                </Tag>
              ))}
            </Space>
          ) : null}
        </Space>
      }
      style={{ marginBottom: 12 }}
      styles={{ body: { paddingTop: 8, paddingBottom: 8 } }}
    >
      <Collapse
        ghost
        items={[
          {
            key: platform.name,
            label: (
              // 折叠标题就是摘要：不展开也能看到当前选了什么
              <PlatformModeSummary
                executorKey={executorKey}
                executors={executors}
                executorLabels={labels}
                modes={modes}
                form={form}
              />
            ),
            children: (
              <Row gutter={[24, 8]}>
                {/* 执行器按平台单独设置 —— 各平台支持的集合不同（由插件的
                    `supported_executors` 声明），全局默认没有意义：
                    给 Grok 设 protocol 会被归一掉（它的协议路径已删除）。
                    没有可选方式的平台也要能设执行器，所以这一项独立于 modes。 */}
                {executors.length > 0 ? (
                  <Col xs={24} lg={12}>
                    <Form.Item
                      label="执行器"
                      name={executorKey}
                      tooltip="该平台用什么方式访问目标站。不支持的会自动降级为纯协议"
                      style={{ marginBottom: 12 }}
                    >
                      <Select
                        options={executors.map((e) => ({
                          value: e,
                          label: executorLabelOf(e),
                        }))}
                        style={{ maxWidth: 'var(--w-field-md)' }}
                      />
                    </Form.Item>
                  </Col>
                ) : null}
                {modes.map((mode) => (
                  <Col key={mode.key} xs={24} lg={12}>
                    <Form.Item
                      label={mode.label}
                      name={mode.key}
                      // 说明文字放 tooltip 而不是 extra：两个字段并排时，
                      // 长段 extra 会把两列的基线拉得不一样高（实测一列
                      // 说明三行、另一列一行，下拉框因此错位）。
                      tooltip={mode.desc}
                      style={{ marginBottom: 12 }}
                    >
                      {mode.type === 'number' ? (
                        <InputNumber
                          min={mode.min}
                          max={mode.max}
                          placeholder={
                            mode.default === undefined ? undefined : String(mode.default)
                          }
                          style={{ maxWidth: 'var(--w-field-md)' }}
                        />
                      ) : (
                        <Select
                          options={mode.options}
                          // 不跟随容器拉满：两列并排时全宽会让下拉横跨半个屏幕，
                          // 标签与控件之间空一大片（--w-field-md = 360px 够放
                          // 最长的选项文案）。
                          style={{ maxWidth: 'var(--w-field-md)' }}
                        />
                      )}
                    </Form.Item>
                  </Col>
                ))}
              </Row>
            ),
          },
        ]}
      />
    </Card>
  )
}

/** 折叠标题里的摘要行（订阅 form 值，实时更新）。 */
function PlatformModeSummary({
  executorKey,
  executors,
  executorLabels,
  modes,
  form,
}: {
  executorKey: string
  executors: ExecutorType[]
  /** 执行器显示名（平台声明，见 `PlatformInfo.executor_labels`） */
  executorLabels: Record<string, string>
  modes: RegistrationModeSpec[]
  form: ReturnType<typeof Form.useFormInstance>
}) {
  const labelOf = (value: string) => executorLabels[value] || EXECUTOR_LABELS[value] || value
  return (
    <Space size={12} wrap>
      {executors.length > 0 ? (
        <SummaryItem
          label="执行器"
          options={executors.map((e) => ({ value: e, label: labelOf(e) }))}
          fieldKey={executorKey}
          // 未设置时实际生效的是第一个受支持的执行器（与
          // `normalizeExecutorForPlatform` 的兜底一致）
          default={executors[0]}
          form={form}
        />
      ) : null}
      {modes.map((mode) => (
        <SummaryItem
          key={mode.key}
          label={mode.label}
          options={mode.options}
          fieldKey={mode.key}
          default={mode.default}
          form={form}
        />
      ))}
    </Space>
  )
}

function SummaryItem({
  label,
  options,
  fieldKey,
  default: defaultValue,
  form,
}: {
  label: string
  options?: { value: string; label: string }[]
  fieldKey: string
  default?: string | number
  form: ReturnType<typeof Form.useFormInstance>
}) {
  // 订阅该字段：值变化时摘要跟着更新（收起状态下也要准）。
  //
  // `preserve: true` 是关键：Collapse 收起时 Form.Item 不渲染、字段未注册，
  // 默认的 useWatch 只读**已注册字段**（`values`），读不到就回落到声明默认值 ——
  // 实测摘要会显示 90 而实际值是 120，展开一次才变对。`preserve` 让它读
  // 整个 store（`allValues`），收起状态下也是实际值。
  const value = Form.useWatch(fieldKey, { form, preserve: true })
  return (
    <Space size={6}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Typography.Text>
      <Typography.Text strong style={{ fontSize: 13 }}>
        {modeSummary(options, value, defaultValue)}
      </Typography.Text>
    </Space>
  )
}

/**
 * 注册设置的完整面板：默认参数 → 各平台设置。
 *
 * 顺序按「改动频率」排：参数每次开任务都可能动，平台方式配一次就不管了。
 */
export function RegisterSettingsPanel() {
  const [platforms, setPlatforms] = useState<PlatformInfo[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    apiFetch('/platforms')
      .then((data) => {
        if (alive) setPlatforms(Array.isArray(data) ? data : [])
      })
      .catch(() => {
        if (alive) setFailed(true)
      })
    return () => {
      alive = false
    }
  }, [])

  return (
    <div>
      <DefaultParamsCard />

      <div style={{ marginBottom: 8, fontSize: 13, color: 'var(--text-muted)' }}>
        各平台设置 —— 执行器与注册方式由各平台插件声明，展开可改
      </div>
      {failed ? (
        <Card>
          <Typography.Text type="secondary">
            读取平台清单失败，刷新页面重试。
          </Typography.Text>
        </Card>
      ) : platforms === null ? (
        <Card>
          <Typography.Text type="secondary">读取中…</Typography.Text>
        </Card>
      ) : platforms.length === 0 ? (
        <Card>
          <Typography.Text type="secondary">没有已加载的平台。</Typography.Text>
        </Card>
      ) : (
        platforms.map((p) => <PlatformModeCard key={p.name} platform={p} />)
      )}
    </div>
  )
}
