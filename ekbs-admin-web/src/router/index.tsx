import { Suspense } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import { Spin } from 'antd'
import BasicLayout from '@/layouts/BasicLayout'
import ErrorBoundary from '@/components/ErrorBoundary'
import { AuthGuard, PermissionGuard } from './guards'
import { ROUTES } from './routes'
import Login from '@/pages/login'
import Forbidden from '@/pages/exception/403'
import NotFound from '@/pages/exception/404'
import ServerError from '@/pages/exception/500'

const Loading = () => (
  <div style={{ padding: 48, textAlign: 'center' }}>
    <Spin tip="加载中…" />
  </div>
)

/** 管理台挂在 /admin 下（FastAPI 托管 dist/），所以整体 basename 固定。 */
export const router = createBrowserRouter(
  [
    { path: '/login', element: <Login /> },
    { path: '/403', element: <Forbidden /> },
    { path: '/404', element: <NotFound /> },
    { path: '/500', element: <ServerError /> },
    {
      path: '/',
      element: (
        <AuthGuard>
          <BasicLayout />
        </AuthGuard>
      ),
      children: [
        { index: true, element: <Navigate to="/dashboard" replace /> },
        { path: 'iam/users', element: <Navigate to="/iam/accounts" replace /> },
        ...ROUTES.map(({ path, Component, perm }) => ({
          path,
          element: (
            <ErrorBoundary>
              <PermissionGuard perm={perm}>
                <Suspense fallback={<Loading />}>
                  <Component />
                </Suspense>
              </PermissionGuard>
            </ErrorBoundary>
          ),
        })),
        { path: '*', element: <NotFound /> },
      ],
    },
  ],
  { basename: '/admin' },
)
