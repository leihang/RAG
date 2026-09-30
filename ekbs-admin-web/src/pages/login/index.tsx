/**
 * 登录页（方案 05 §4.1）。
 *
 * 首期实现的是**开发身份切换**：`POST /auth/dev-token`（生产环境该端点 404，方案 §4.1）。
 * 演示身份从 `GET /auth/dev-users` **动态取** —— 不要硬编码用户 ID：
 * 用户 ID 是自增的，补一次种子就会变（实测 Dave/Root 从 4/5 变成 7/8，
 * 硬编码导致 401，而这种故障只有真点一下才看得到）。
 *
 * 钉钉免登（`authCode` → `POST /auth/dingtalk-login`）在后端还不接受真实 authCode，
 * 所以这里只把入口标出来、不假装能用。
 */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Divider, Form, Select, Space, Spin, Typography, message } from 'antd'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { http } from '@/api/client'
import { useAuth } from '@/hooks/useAuth'
import { useAuthStore } from '@/stores/authStore'

interface DevUser {
  user_id: number
  nick?: string
  dingtalk_userid: string
  clearance: number
  roles: string[]
}

export default function Login() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const { loginAsDev, isAuthed } = useAuth()
  const [loading, setLoading] = useState(false)
  const [users, setUsers] = useState<DevUser[] | null>(null)
  const [picked, setPicked] = useState<number | undefined>()
  const redirect = params.get('redirect') || '/dashboard'

  useEffect(() => {
    if (isAuthed) navigate(redirect, { replace: true })
  }, [isAuthed, navigate, redirect])

  useEffect(() => {
    // 免登录拿演示身份；生产环境该端点 404 → users 保持为空并给出提示
    http
      .get('/auth/dev-users')
      .then((res) => {
        const items = (res as { items?: DevUser[] }).items ?? []
        setUsers(items)
        setPicked(items[0]?.user_id)
      })
      .catch(() => setUsers([]))
  }, [])

  const onDevLogin = async (userId: number) => {
    setLoading(true)
    try {
      const me = await loginAsDev(userId)
      message.success(`已登录：${me.nick ?? me.user_id}`)
      navigate(redirect, { replace: true })
    } catch (err) {
      message.error(`登录失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setLoading(false)
    }
  }

  const onDingtalk = () => {
    message.info('钉钉免登需要企业应用凭据（~/.ekbs/secrets.env），当前未配置，先用下方演示身份。')
  }

  const options = (users ?? []).map((u) => ({
    value: u.user_id,
    // 人名 + 角色 + 密级分开展示，不再依赖 nick 里塞「（总部/L1）」
    label: `${u.nick ?? u.dingtalk_userid} · ${(u.roles ?? []).join('/') || '无角色'} · L${u.clearance}`,
  }))

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#f5f7fa' }}>
      <Card style={{ width: 460, boxShadow: '0 1px 2px rgba(0,0,0,.06)' }}>
        <Typography.Title level={4} style={{ marginBottom: 4 }}>EKBS 企业知识中枢</Typography.Title>
        <Typography.Text type="secondary">管理台 · 仅限授权管理员</Typography.Text>
        <Divider />
        {users === null ? (
          <div style={{ textAlign: 'center', padding: 24 }}><Spin tip="正在读取演示身份…" /></div>
        ) : users.length === 0 ? (
          <Alert
            type="warning"
            showIcon
            message="没有可用的身份切换入口"
            description="当前环境未开放 /auth/dev-users（生产环境按设计返回 404）。请通过钉钉免登进入。"
          />
        ) : (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="开发环境身份切换"
              description="生产环境请由钉钉免登进入；此处仅为联调与验收提供快捷入口。"
            />
            <Form layout="vertical">
              <Form.Item label="演示身份">
                <Select
                  value={picked}
                  options={options}
                  loading={loading}
                  onChange={(v) => {
                    setPicked(Number(v))
                    void onDevLogin(Number(v))
                  }}
                />
              </Form.Item>
              <Space direction="vertical" style={{ width: '100%' }}>
                <Button block type="primary" loading={loading} disabled={picked == null} onClick={() => picked != null && void onDevLogin(picked)}>
                  进入管理台
                </Button>
                <Button block type="dashed" onClick={onDingtalk}>
                  钉钉免登（未配置凭据）
                </Button>
              </Space>
            </Form>
          </>
        )}
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
