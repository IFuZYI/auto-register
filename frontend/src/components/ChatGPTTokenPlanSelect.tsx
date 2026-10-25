import { Select, Space, Tag, Tooltip } from 'antd'

import {
  TOKEN_PLANS,
  TOKEN_PLAN_ACCESS_ONLY,
  tokenPlanHint,
  type ChatGPTTokenPlan,
} from '@/lib/chatgptTokenPlan'

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

  // 说明进 tooltip：三个方案的说明长短不一（最长两行），常驻会把下面
  // 「绑定 2FA」的控件顶下去，两列并排时基线也对不齐。
  return (
    <Space align="center" wrap size={8}>
      <Select
        value={plan}
        onChange={onChange}
        style={{ minWidth: 220 }}
        options={TOKEN_PLANS.map((p) => ({ value: p.value, label: p.label }))}
      />
      <Tag color={isAccessOnly ? 'default' : 'success'}>
        {isAccessOnly ? '兼容旧方案' : '默认推荐'}
      </Tag>
      <Tooltip title={tokenPlanHint(plan)}>
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
