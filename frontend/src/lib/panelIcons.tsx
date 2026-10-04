import type { ReactNode } from 'react'
import {
  CloudUploadOutlined,
  DeploymentUnitOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'

/**
 * 面板 key → 卡片图标。
 *
 * 抽到 lib 里是因为「面板管理」与「全局配置 → 面板配置」两个页面都要画同一批
 * 卡片 —— 各写一份的话加一个面板要改两处，漏改的那页就会掉回通用图标。
 * 缺省图标（`ApiOutlined`）由调用方兜底，这里只列有专属图标的面板。
 */
export const PANEL_ICONS: Record<string, ReactNode> = {
  cpa: <CloudUploadOutlined />,
  sub2api: <DeploymentUnitOutlined />,
  grok2api: <ThunderboltOutlined />,
}
