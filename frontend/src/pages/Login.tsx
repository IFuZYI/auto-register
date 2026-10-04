import { useState } from 'react'
import { App, ConfigProvider, Form, Input, Button, Typography } from 'antd'
import { LockOutlined, SafetyCertificateOutlined, ThunderboltFilled } from '@ant-design/icons'
import { setToken } from '@/lib/utils'
import { darkTheme } from '@/theme'

type Step = 'password' | '2fa'

function LoginContent() {
  const { message } = App.useApp()
  const [step, setStep] = useState<Step>('password')
  const [tempToken, setTempToken] = useState('')
  const [loading, setLoading] = useState(false)

  const handleLogin = async (values: { password: string }) => {
    setLoading(true)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: values.password }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '登录失败')
      if (data.requires_2fa) {
        setTempToken(data.temp_token)
        setStep('2fa')
      } else {
        setToken(data.access_token)
        window.location.href = '/'
      }
    } catch (e: any) {
      message.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleTotp = async (values: { code: string }) => {
    setLoading(true)
    try {
      const res = await fetch('/api/auth/verify-totp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ temp_token: tempToken, code: values.code }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '验证失败')
      setToken(data.access_token)
      window.location.href = '/'
    } catch (e: any) {
      message.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const shellStyle: React.CSSProperties = {
    minHeight: '100vh',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
    // 苹果发布会背景那种大范围柔光渐变
    background: `
      radial-gradient(1200px 600px at 15% -10%, rgba(10,132,255,0.22), transparent 60%),
      radial-gradient(900px 500px at 85% 110%, rgba(94,92,230,0.2), transparent 60%),
      var(--bg-layout)
    `,
  }

  const cardStyle: React.CSSProperties = {
    width: 400,
    maxWidth: '100%',
    padding: '36px 32px 32px',
    borderRadius: 24,
    border: '1px solid var(--glass-border)',
    background: 'var(--glass-bg)',
    backdropFilter: 'saturate(180%) blur(24px)',
    WebkitBackdropFilter: 'saturate(180%) blur(24px)',
    boxShadow: 'var(--shadow-lg)',
    animation: 'slideInUp 0.5s var(--ease-out) both',
  }

  const markStyle: React.CSSProperties = {
    width: 56,
    height: 56,
    margin: '0 auto 18px',
    borderRadius: 17,
    display: 'grid',
    placeItems: 'center',
    fontSize: 26,
    color: '#fff',
    background: 'linear-gradient(135deg, var(--gradient-from), var(--gradient-to))',
    boxShadow: '0 8px 24px var(--accent-ring)',
  }

  const titleStyle: React.CSSProperties = {
    fontSize: 22,
    fontWeight: 700,
    letterSpacing: '-0.03em',
    textAlign: 'center',
    marginBottom: 6,
  }

  const hintStyle: React.CSSProperties = {
    fontSize: 13,
    color: 'var(--text-muted)',
    textAlign: 'center',
    marginBottom: 28,
  }

  if (step === '2fa') {
    return (
      <div style={shellStyle}>
        <div style={cardStyle}>
          <div style={markStyle}>
            <SafetyCertificateOutlined />
          </div>
          <div style={titleStyle}>双因素验证</div>
          <div style={hintStyle}>请输入验证器 App 中的 6 位验证码</div>

          <Form layout="vertical" onFinish={handleTotp} requiredMark={false}>
            <Form.Item
              name="code"
              rules={[
                { required: true, message: '请输入验证码' },
                { len: 6, message: '验证码为 6 位数字' },
              ]}
              style={{ marginBottom: 20 }}
            >
              <Input
                placeholder="000000"
                size="large"
                maxLength={6}
                autoFocus
                style={{
                  letterSpacing: '0.5em',
                  textAlign: 'center',
                  fontSize: 20,
                  fontWeight: 600,
                  fontVariantNumeric: 'tabular-nums',
                }}
              />
            </Form.Item>
            <Form.Item style={{ marginBottom: 0 }}>
              <Button type="primary" htmlType="submit" block size="large" loading={loading}>
                验证并登录
              </Button>
            </Form.Item>
            <div style={{ textAlign: 'center', marginTop: 16 }}>
              <Button type="link" size="small" onClick={() => setStep('password')}>
                返回密码登录
              </Button>
            </div>
          </Form>
        </div>
      </div>
    )
  }

  return (
    <div style={shellStyle}>
      <div style={cardStyle}>
        <div style={markStyle}>
          <ThunderboltFilled />
        </div>
        <div style={titleStyle}>账号管理台</div>
        <div style={hintStyle}>请输入访问密码以继续</div>

        <Form layout="vertical" onFinish={handleLogin} requiredMark={false}>
          <Form.Item
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
            style={{ marginBottom: 20 }}
          >
            <Input.Password
              prefix={<LockOutlined style={{ color: 'var(--text-muted)' }} />}
              placeholder="访问密码"
              size="large"
              autoFocus
            />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" block size="large" loading={loading}>
              登录
            </Button>
          </Form.Item>
        </Form>

        <Typography.Text
          type="secondary"
          style={{
            display: 'block',
            textAlign: 'center',
            fontSize: 12,
            marginTop: 20,
            color: 'var(--text-muted)',
          }}
        >
          密码仅保存在本机服务端
        </Typography.Text>
      </div>
    </div>
  )
}

export default function Login() {
  return (
    <ConfigProvider theme={darkTheme}>
      <App>
        <LoginContent />
      </App>
    </ConfigProvider>
  )
}
