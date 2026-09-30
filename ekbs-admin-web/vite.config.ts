import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// 管理台由 ekbs-api 以 /admin 前缀托管（方案 05 §6.9 说由 Caddy 托管，
// 过渡期先由 FastAPI 挂 dist/；两者都是「同源静态资源」，所以 base 固定 /admin/）。
export default defineConfig({
  base: '/admin/',
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    port: 3000, // 方案 §1.1：管理台前端端口 3000
    proxy: {
      // 开发期直接把 /api 代理到本机 API，避免跨域与环境变量两套口径
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/readyz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        // 内容哈希文件名（方案 §6.9：构建产物内容哈希 + index.html 禁缓存）
        entryFileNames: 'assets/[name].[hash].js',
        chunkFileNames: 'assets/[name].[hash].js',
        assetFileNames: 'assets/[name].[hash].[ext]',
      },
    },
  },
})
