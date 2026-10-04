import { Space, Switch, Tag, Typography } from 'antd'

import {
  CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
  CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'

const { Text } = Typography

type ChatGPTRegistrationModeSwitchProps = {
  mode: ChatGPTRegistrationMode
  onChange: (mode: ChatGPTRegistrationMode) => void
}

export function ChatGPTRegistrationModeSwitch({
  mode,
  onChange,
}: ChatGPTRegistrationModeSwitchProps) {
  const hasRefreshTokenSolution =
    mode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN

  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space align="center" wrap>
        <Switch
          checked={hasRefreshTokenSolution}
          checkedChildren="有 RT"
          unCheckedChildren="无 RT"
          onChange={(checked) =>
            onChange(
              checked
                ? CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
                : CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
            )
          }
        />
        <Tag color={hasRefreshTokenSolution ? 'success' : 'default'}>
          {hasRefreshTokenSolution ? '默认推荐' : '兼容旧方案'}
        </Tag>
      </Space>
      <Text type="secondary">
        {hasRefreshTokenSolution
          ? '产出 Access Token + Refresh Token。'
          : '只产出 Access Token / Session，依赖 RT 的能力可能不可用。'}
      </Text>
    </Space>
  )
}
