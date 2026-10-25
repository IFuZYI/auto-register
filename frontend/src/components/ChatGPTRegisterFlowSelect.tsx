import { Select, Space, Tag, Tooltip } from 'antd'

import {
  CHATGPT_REGISTER_FLOW_OPTIONS,
  CHATGPT_REGISTER_FLOW_EMAIL,
  normalizeChatGPTRegisterFlow,
  registerFlowUsesPhone,
  type ChatGPTRegisterFlow,
} from '@/lib/chatgptRegisterFlow'

type ChatGPTRegisterFlowSelectProps = {
  flow: ChatGPTRegisterFlow
  onChange: (flow: ChatGPTRegisterFlow) => void
}

export function ChatGPTRegisterFlowSelect({
  flow,
  onChange,
}: ChatGPTRegisterFlowSelectProps) {
  const current =
    CHATGPT_REGISTER_FLOW_OPTIONS.find((option) => option.value === flow) ??
    CHATGPT_REGISTER_FLOW_OPTIONS[0]
  const usesPhone = registerFlowUsesPhone(flow)

  // 说明文字进 tooltip：常驻两三行会把相邻表单项的基线拉歪（与全局配置页
  // `RegisterSettingsPanel` 的处理一致）。标签旁的 ⓘ 是唯一入口。
  const hint = usesPhone
    ? `${current.hint} 手机号来自「设置 → 接码」，未启用接码时任务会报错。`
    : current.hint

  return (
    <Space align="center" wrap size={8}>
      <Select
        value={flow}
        style={{ minWidth: 200 }}
        onChange={(value) => onChange(normalizeChatGPTRegisterFlow(value))}
        options={CHATGPT_REGISTER_FLOW_OPTIONS.map(({ value, label }) => ({
          value,
          label,
        }))}
      />
      <Tag color={flow === CHATGPT_REGISTER_FLOW_EMAIL ? 'default' : 'processing'}>
        {flow === CHATGPT_REGISTER_FLOW_EMAIL ? '默认' : '需要接码'}
      </Tag>
      <Tooltip title={hint}>
        <span
          aria-label="说明"
          style={{ color: 'var(--text-muted)', cursor: 'help', fontSize: 13 }}
        >
          ⓘ
        </span>
      </Tooltip>
    </Space>
  )
}
