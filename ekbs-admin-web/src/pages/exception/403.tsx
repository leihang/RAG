import { Button, Result, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'

export default function Forbidden() {
  const navigate = useNavigate()
  const me = useAuthStore((s) => s.me)
  return (
    <Result
      status="403"
      title="您没有访问该页面的权限"
      subTitle="请联系知识库管理员或超级管理员开通"
      extra={<Button type="primary" onClick={() => navigate('/dashboard')}>回到概览</Button>}
    >
      <Typography.Paragraph type="secondary">
        当前身份：{me?.nick ?? me?.user_id ?? '未登录'}；已授予权限点 {(me?.permissions ?? []).length} 个。
      </Typography.Paragraph>
    </Result>
  )
}
