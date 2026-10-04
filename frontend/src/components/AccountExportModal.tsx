import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Input, Modal, Radio, Select, Space, Typography, message } from 'antd'
import { CopyOutlined, DownloadOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

export type AccountExportFilters = {
  platform: string
  email?: string
  status?: string
  plus_status?: string
  created_at_start?: string
  created_at_end?: string
}

type ExportFormat = {
  id: string
  label: string
  description: string
  extension: string
  sample: string
}

type ExportPreview = {
  format: string
  total: number
  lines: number
  content: string
  filename: string
}

type AccountExportModalProps = {
  open: boolean
  onClose: () => void
  filters: AccountExportFilters
  selectedIds: number[]
  filteredTotal: number
}

// 预览框里放不下几千行，也没人会去读；超出的部分只报个数字。
const PREVIEW_LINE_LIMIT = 200

function previewText(content: string): string {
  const lines = content.split('\n')
  if (lines.length <= PREVIEW_LINE_LIMIT) return content
  return `${lines.slice(0, PREVIEW_LINE_LIMIT).join('\n')}\n… 其余 ${lines.length - PREVIEW_LINE_LIMIT} 行已省略，复制和下载不受影响`
}

export function AccountExportModal({
  open,
  onClose,
  filters,
  selectedIds,
  filteredTotal,
}: AccountExportModalProps) {
  const [formats, setFormats] = useState<ExportFormat[]>([])
  const [formatId, setFormatId] = useState('')
  const [scopeChoice, setScopeChoice] = useState<'selected' | 'filtered' | null>(null)
  // 连同"这份结果是给哪次请求的"一起存，选项一变旧结果立刻失效，
  // 也就不用再拿一个 loading 状态去追它。
  const [result, setResult] = useState<{ key: string; data: ExportPreview | null } | null>(null)

  // 勾了行就默认导所选，没勾就默认导当前筛选；用户改过之后以用户的选择为准。
  const scope = scopeChoice ?? (selectedIds.length > 0 ? 'selected' : 'filtered')

  // 父组件每次渲染都新建 filters / selectedIds 两个对象，直接进依赖数组会把预览
  // 请求打成死循环。这里把它们压成字符串再还原，effect 只在内容真变了时才重跑。
  const filtersKey = JSON.stringify(filters)
  const selectionKey = selectedIds.join(',')
  const request = useMemo(
    () => ({
      filters: JSON.parse(filtersKey) as AccountExportFilters,
      ids: selectionKey ? selectionKey.split(',').map(Number) : [],
    }),
    [filtersKey, selectionKey],
  )

  useEffect(() => {
    if (!open || formats.length > 0) return
    apiFetch('/accounts/export-formats')
      .then((data) => {
        setFormats(data.formats || [])
        setFormatId((current) => current || data.default || (data.formats?.[0]?.id ?? ''))
      })
      .catch((e) => message.error(`读取导出格式失败: ${e instanceof Error ? e.message : String(e)}`))
  }, [open, formats.length])

  const emptySelection = scope === 'selected' && selectedIds.length === 0
  const requestKey = `${scope}|${formatId}|${filtersKey}|${selectionKey}`

  useEffect(() => {
    if (!open || !formatId || emptySelection) return

    const body: Record<string, unknown> = { format: formatId, platform: request.filters.platform }
    if (scope === 'selected') {
      body.account_ids = request.ids
    } else {
      if (request.filters.email) body.email = request.filters.email
      if (request.filters.status) body.status = request.filters.status
      if (request.filters.plus_status) body.plus_status = request.filters.plus_status
      if (request.filters.created_at_start) body.created_at_start = request.filters.created_at_start
      if (request.filters.created_at_end) body.created_at_end = request.filters.created_at_end
    }

    let cancelled = false
    apiFetch('/accounts/export-text', { method: 'POST', body: JSON.stringify(body) })
      .then((data) => {
        if (!cancelled) setResult({ key: requestKey, data })
      })
      .catch((e) => {
        if (cancelled) return
        setResult({ key: requestKey, data: null })
        message.error(`生成导出内容失败: ${e instanceof Error ? e.message : String(e)}`)
      })

    return () => {
      cancelled = true
    }
  }, [open, formatId, scope, emptySelection, requestKey, request])

  const shown = result?.key === requestKey ? result.data : null
  const loading = !emptySelection && Boolean(formatId) && result?.key !== requestKey

  const close = () => {
    setScopeChoice(null)
    onClose()
  }

  const copyAll = async () => {
    if (!shown?.content) return
    try {
      await navigator.clipboard.writeText(shown.content)
      message.success(`已复制 ${shown.lines} 行`)
    } catch {
      message.error('复制失败，请手动选中预览框内容')
    }
  }

  const download = () => {
    if (!shown?.content) return
    const blob = new Blob([`\uFEFF${shown.content}`], { type: 'text/plain;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = shown.filename
    link.click()
    URL.revokeObjectURL(url)
  }

  const selectedFormat = formats.find((item) => item.id === formatId)
  const empty = !shown?.content

  return (
    <Modal
      title="导出账号"
      open={open}
      onCancel={close}
      width={720}
      maskClosable={false}
      footer={[
        <Button key="close" onClick={close}>
          关闭
        </Button>,
        <Button key="copy" icon={<CopyOutlined />} onClick={copyAll} disabled={empty}>
          复制
        </Button>,
        <Button key="download" type="primary" icon={<DownloadOutlined />} onClick={download} disabled={empty}>
          下载
        </Button>,
      ]}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Radio.Group
          value={scope}
          onChange={(e) => setScopeChoice(e.target.value)}
          optionType="button"
          buttonStyle="solid"
          options={[
            { value: 'selected', label: `所选 ${selectedIds.length} 个`, disabled: selectedIds.length === 0 },
            { value: 'filtered', label: `当前筛选 ${filteredTotal} 个` },
          ]}
        />
        <Select
          style={{ width: '100%' }}
          value={formatId || undefined}
          onChange={setFormatId}
          placeholder="选择导出格式"
          options={formats.map((item) => ({
            value: item.id,
            label: item.label,
            title: item.description,
          }))}
        />
        {selectedFormat ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {selectedFormat.description}
            {selectedFormat.sample ? `：${selectedFormat.sample}` : ''}
          </Text>
        ) : null}
        <Alert
          type="info"
          showIcon
          message={
            shown
              ? `命中 ${shown.total} 个账号，导出 ${shown.lines} 行`
              : '选择范围与格式后生成预览'
          }
          description="空字段照样占位，按 ---- 切列不会错位；单列格式会跳过没有该字段的账号。"
        />
        <Input.TextArea
          value={shown ? previewText(shown.content) : ''}
          readOnly
          rows={12}
          placeholder={loading ? '生成中…' : '没有可导出的内容'}
          style={{
            fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
            fontSize: 12,
          }}
        />
      </Space>
    </Modal>
  )
}
