import { Button, Result } from 'antd'
import { useNavigate } from 'react-router-dom'

export default function NotFound() {
  const navigate = useNavigate()
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="链接可能已失效，或该功能尚未在首期开放"
      extra={<Button type="primary" onClick={() => navigate('/dashboard')}>回到概览</Button>}
    />
  )
}
