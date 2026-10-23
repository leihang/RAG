/**
 * 管理台登录。
 *
 * 只接受「管理台账号」里新建的登录名和口令。钉钉同事在机器人里提问，不能从这里进入。
 */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Divider, Form, Input, Space, Typography, message } from 'antd'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'
import { useAuthStore } from '@/stores/authStore'

export default function Login() {
  const params = useSearchParams()[0]
  const navigate = useNavigate()
  const { loginWithPassword, isAuthed } = useAuth()
  const [loading, setLoading] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [localError, setLocalError] = useState('')
  const redirect = params.get('redirect') || '/dashboard'

  useEffect(() => {
    if (isAuthed) navigate(redirect, { replace: true })
  }, [isAuthed, navigate, redirect])

  const onLocalLogin = async () => {
    setLoading(true)
    setLocalError('')
    try {
      const result = await loginWithPassword(username, password)
      if (result.mustChange) {
        message.warning('当前为初始口令，登录后请在右上角修改口令')
      }
      message.success(`已登录：${result.nick ?? result.user_id}`)
      navigate(redirect, { replace: true })
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#f5f7fa' }}>
      <Card style={{ width: 460, boxShadow: '0 1px 2px rgba(0,0,0,.06)' }}>
        <Typography.Title level={4} style={{ marginBottom: 4 }}>EKBS 企业知识中枢</Typography.Title>
        <Typography.Text type="secondary">管理台 · 仅限已建登录名的人</Typography.Text>
        <Divider />
        <Space direction="vertical" style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="用登录名和口令进入"
            description="只有在「管理台账号」里新建过的人能进管理台。钉钉同事不能从这里登录，他们继续在钉钉里提问。"
          />
          {localError && (
            <Alert type="error" showIcon message="登录失败" description={localError} />
          )}
          <Form layout="vertical" onFinish={() => void onLocalLogin()}>
            <Form.Item label="登录名" style={{ marginBottom: 12 }}>
              <Input
                value={username}
                autoComplete="username"
                placeholder="管理台账号里新建的登录名"
                onChange={(e) => setUsername(e.target.value)}
              />
            </Form.Item>
            <Form.Item
              label="口令"
              style={{ marginBottom: 12 }}
              extra="口令至少 6 位。还没有账号时，请已有管理员在「管理台账号」点「新建用户」。"
            >
              <Input.Password
                value={password}
                autoComplete="current-password"
                onChange={(e) => setPassword(e.target.value)}
              />
            </Form.Item>
            <Button
              block
              type="primary"
              size="large"
              htmlType="submit"
              loading={loading}
              disabled={!username || !password}
            >
              进入管理台
            </Button>
          </Form>
        </Space>
        <Divider plain>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>退出登录会清空本地会话快照</Typography.Text>
        </Divider>
        <Button
          type="link"
          size="small"
          onClick={() => {
            useAuthStore.getState().clear()
            message.success('已清空本地会话')
          }}
        >
          清空本地会话
        </Button>
      </Card>
    </div>
  )
}
