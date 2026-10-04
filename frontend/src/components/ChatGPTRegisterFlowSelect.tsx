import { Select, Space, Tag, Typography } from 'antd'

import {
  CHATGPT_REGISTER_FLOW_OPTIONS,
  CHATGPT_REGISTER_FLOW_EMAIL,
  normalizeChatGPTRegisterFlow,
  registerFlowUsesPhone,
  type ChatGPTRegisterFlow,
} from '@/lib/chatgptRegisterFlow'

const { Text } = Typography

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

  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space align="center" wrap>
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
      </Space>
      <Text type="secondary">{current.hint}</Text>
      {usesPhone && (
        <Text type="secondary">
          手机号来自「设置 → 接码」，未启用接码时任务会报错。
        </Text>
      )}
    </Space>
  )
}
