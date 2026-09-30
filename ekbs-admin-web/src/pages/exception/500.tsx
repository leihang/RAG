import { Button, Result } from 'antd'
import { useNavigate } from 'react-router-dom'

export default function ServerError() {
  const navigate = useNavigate()
  return (
    <Result
      status="500"
      title="服务暂时不可用"
      subTitle="请稍后重试或联系运维"
      extra={<Button type="primary" onClick={() => navigate('/dashboard')}>回到概览</Button>}
    />
  )
}
