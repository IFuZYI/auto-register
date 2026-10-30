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
    rollupOptions: {
      output: {
        // 默认切分会给 antd 的每个组件单独出一个 chunk（实测 59 个 JS 文件），
        // 一次路由切换要**串行**拉 10~15 个小文件 —— 本机 478ms，走 SSH 隧道
        // 或公网时每个请求都要一次 RTT，界面就是「切个页面加载半天」。
        //
        // 把第三方依赖按体积归成三组，业务代码仍然按路由懒加载：
        // 切页面时只需拉「页面自己那一个 chunk」+ 少量按需图标。
        // `minSize`：小于 40KB 的 chunk 合并进引用它的入口。默认切分会把
        // 十来个共用小模块（logCleanup / panelIcons / SmsCountrySelect …）
        // 各切一个文件，首屏 27 个 JS 请求，走隧道时每个都要一次往返。
        codeSplitting: {
          minSize: 40000,
          groups: [
            {
              name: 'vendor-react',
              test: /node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/,
            },
            {
              name: 'vendor-antd',
              test: /node_modules[\\/](antd|@ant-design|rc-[^\\/]+)[\\/]/,
            },
            // 所有页面合成一个 chunk：首屏只付 entry + vendor + 这一个，
            // 之后每次切页都是 0 请求（配合 App.tsx 的空闲预取）。
            // 不这么归组的话，11 个页面 + 十几个共用小模块 = 27 个文件，
            // 走 SSH 隧道时每个请求都要一次往返。
            { name: 'pages', test: /src[\\/]pages[\\/]/ },
            { name: 'vendor', test: /node_modules[\\/]/ },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
