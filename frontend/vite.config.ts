import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    // 现代浏览器目标：默认目标会把 backdrop-filter 降级成只剩 -webkit- 前缀，
    // 而现代 Chrome 只认标准属性，毛玻璃会整体失效。
    cssTarget: ['chrome110', 'safari15', 'firefox110', 'edge110'],
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
