import { ConfigProvider, App as AntApp } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import { router } from '@/router'
import '@/styles/global.css'

dayjs.locale('zh-cn')

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 方案原则 P1「配置即生效」：权限类数据不做长期缓存
      staleTime: 0,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          // 方案 05 §5.1 设计令牌
          colorPrimary: '#1677ff',
          colorSuccess: '#52c41a',
          colorWarning: '#faad14',
          colorError: '#ff4d4f',
          colorBgLayout: '#f5f7fa',
          colorBorderSecondary: '#f0f0f0',
          borderRadius: 6,
        },
        components: {
          Layout: { headerHeight: 56 },
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  )
}
