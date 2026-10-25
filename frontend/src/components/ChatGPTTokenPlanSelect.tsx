import { Select, Space, Tag, Typography } from 'antd'

import {
  TOKEN_PLANS,
  TOKEN_PLAN_ACCESS_ONLY,
  tokenPlanHint,
  type ChatGPTTokenPlan,
} from '@/lib/chatgptTokenPlan'

const { Text } = Typography

type ChatGPTTokenPlanSelectProps = {
  plan: ChatGPTTokenPlan
  onChange: (plan: ChatGPTTokenPlan) => void
}

/**
 * 「Token 方案」三选一。
 *
 * 早先这里是「有 RT / 无 RT」的两态开关；加了第二条换 RT 的路线之后，
 * 用户要在一处看清三种选择，所以合并成一个下拉。后端仍是两个键
 * （见 `lib/chatgptTokenPlan.ts` 的映射）。
 */
export function ChatGPTTokenPlanSelect({
  plan,
  onChange,
}: ChatGPTTokenPlanSelectProps) {
  const isAccessOnly = plan === TOKEN_PLAN_ACCESS_ONLY

  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space align="center" wrap>
        <Select
          value={plan}
          onChange={onChange}
          style={{ minWidth: 220 }}
          options={TOKEN_PLANS.map((p) => ({ value: p.value, label: p.label }))}
        />
        <Tag color={isAccessOnly ? 'default' : 'success'}>
          {isAccessOnly ? '兼容旧方案' : '默认推荐'}
        </Tag>
      </Space>
      <Text type="secondary">{tokenPlanHint(plan)}</Text>
    </Space>
  )
}
