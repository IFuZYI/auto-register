import { useState } from 'react'
import { App, ConfigProvider, Form, Input, Button } from 'antd'
import {
  AppstoreOutlined,
  LockOutlined,
  MailOutlined,
  SafetyCertificateOutlined,
  UserAddOutlined,
} from '@ant-design/icons'
import { setToken } from '@/lib/utils'
import { ConsoleBrand } from '@/components/ConsoleBrand'
import { darkTheme, lightTheme } from '@/theme'
import zhCN from 'antd/es/locale/zh_CN'

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
      if (!res.ok) throw new Error(data.detail || '无法登录，请检查密码后重试')
      if (data.requires_2fa) {
        setTempToken(data.temp_token)
        setStep('2fa')
      } else {
        setToken(data.access_token)
        window.location.href = '/'
      }
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : '无法登录，请检查密码后重试')
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
      if (!res.ok) throw new Error(data.detail || '验证码不正确或已过期，请重新输入')
      setToken(data.access_token)
      window.location.href = '/'
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : '无法验证，请重新输入验证码')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="login-shell">
      <div className="login-frame">
        <section className="login-intro" aria-label="关于账号管理台">
          <div className="login-brand"><ConsoleBrand /></div>
          <div>
            <h1>从邮箱到面板，<br />清楚每一步。</h1>
            <p>注册与维护多平台账号，核对凭据后再交付到远端面板。</p>
          </div>
          <ol className="login-flow">
            <li><MailOutlined /><span>准备邮箱<small>导入邮箱号池或创建隐私邮箱</small></span></li>
            <li><UserAddOutlined /><span>注册与维护<small>跟进任务，处理过期和失效账号</small></span></li>
            <li><AppstoreOutlined /><span>交付到面板<small>核对本地与远端，再选择推送或拉回</small></span></li>
          </ol>
        </section>

        <section className="login-panel" aria-labelledby="login-title">
          <div className="login-mobile-brand"><ConsoleBrand /></div>
          <div className="login-panel__icon">
            {step === '2fa' ? <SafetyCertificateOutlined /> : <LockOutlined />}
          </div>
          <h2 id="login-title">{step === '2fa' ? '完成安全验证' : '登录管理台'}</h2>
          <p className="login-panel__hint">
            {step === '2fa' ? '输入验证器 App 中当前的 6 位验证码。' : '输入管理台的访问密码以继续。'}
          </p>

          {step === '2fa' ? (
            <Form layout="vertical" onFinish={handleTotp} requiredMark={false}>
              <Form.Item
                label="6 位验证码"
                name="code"
                rules={[
                  { required: true, message: '请输入验证码' },
                  { len: 6, message: '验证码为 6 位数字' },
                ]}
              >
                <Input
                  placeholder="000000"
                  size="large"
                  maxLength={6}
                  autoFocus
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  aria-label="6 位验证码"
                  className="login-code-input"
                />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" block size="large" loading={loading}>
                  验证并登录
                </Button>
              </Form.Item>
              <Button type="link" size="small" className="login-back" onClick={() => setStep('password')}>
                返回密码登录
              </Button>
            </Form>
          ) : (
            <Form layout="vertical" onFinish={handleLogin} requiredMark={false}>
              <Form.Item
                label="访问密码"
                name="password"
                rules={[{ required: true, message: '请输入密码' }]}
              >
                <Input.Password
                  prefix={<LockOutlined style={{ color: 'var(--text-muted)' }} />}
                  placeholder="输入访问密码"
                  size="large"
                  autoFocus
                  autoComplete="current-password"
                />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" block size="large" loading={loading}>
                  登录管理台
                </Button>
              </Form.Item>
            </Form>
          )}
          <p className="login-panel__note">密码仅在服务端验证。请通过可信连接访问管理台。</p>
        </section>
      </div>
    </main>
  )
}

export default function Login() {
  const isLight = (localStorage.getItem('theme') || 'light') === 'light'
  return (
    <ConfigProvider theme={isLight ? lightTheme : darkTheme} locale={zhCN}>
      <App>
        <LoginContent />
      </App>
    </ConfigProvider>
  )
}
