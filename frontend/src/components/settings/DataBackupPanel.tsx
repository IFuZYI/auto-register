import { useCallback, useEffect, useRef, useState } from 'react'
import { App, Alert, Button, Card, Descriptions, Space, Table, Tag, Typography } from 'antd'
import {
  CloudDownloadOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { apiFetch, getToken } from '@/lib/utils'

/**
 * 数据与配置的一键导出 / 导入（服务迁移用）。
 *
 * 导出：`GET /api/backup/export` 返回一个 ZIP（内含各库的 SQLite 一致性
 * 快照 + 凭据加密密钥 + manifest）。导入：把 ZIP 当**原始字节** POST 到
 * `/api/backup/import` —— 不是 multipart，后端刻意不引 python-multipart。
 *
 * 导入是破坏性操作（替换整个数据目录），所以：
 *   1. 必须二次确认，确认文案里写明「会先自动备份」；
 *   2. 有运行中任务时后端会 409 拒绝，界面把原因原样显示；
 *   3. 结果里回报备份目录路径 —— 导入错了用户知道去哪捞回来。
 */

interface BackupDbInfo {
  name: string
  source?: string
  path: string
  size: number
  tables: Record<string, number>
}

interface BackupInfo {
  data_dir: string
  databases: BackupDbInfo[]
  total_size: number
  has_credential_key: boolean
  has_active_tasks: boolean
}

interface ImportResult {
  ok: boolean
  imported: string[]
  key_restored: boolean
  backup: string
  row_counts: Record<string, Record<string, number>>
  manifest: { format_version?: number; exported_at?: string }
}

function formatBytes(size: number): string {
  const value = Number(size) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export function DataBackupPanel() {
  const { message } = App.useApp()
  const [info, setInfo] = useState<BackupInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [importing, setImporting] = useState(false)
  const [lastImport, setLastImport] = useState<ImportResult | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = (await apiFetch('/backup/info')) as BackupInfo
      setInfo(data)
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : '读取数据概况失败')
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const handleExport = async () => {
    setExporting(true)
    try {
      // 用原生 fetch 而不是 apiFetch：响应是二进制 ZIP，不是 JSON
      const token = getToken()
      const res = await fetch('/api/backup/export', {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!res.ok) {
        const text = await res.text()
        let detail = text
        try {
          detail = JSON.parse(text).detail || text
        } catch {
          /* 非 JSON 错误体，原样显示 */
        }
        throw new Error(detail)
      }
      const blob = await res.blob()
      // 文件名从 Content-Disposition 里取（服务端已按 RFC 5987 编码）
      const disposition = res.headers.get('Content-Disposition') || ''
      const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
      const filename = match ? decodeURIComponent(match[1]) : 'register-backup.zip'

      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      document.body.removeChild(anchor)
      // revoke 延后一拍：Firefox/Safari 的下载管理器可能在 click() 返回后
      // 才真正去读 blob，同步 revoke 会让下载**静默失败**（无任何报错）。
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      message.success(`已导出 ${filename}（${formatBytes(blob.size)}）`)
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : '导出失败')
    } finally {
      setExporting(false)
    }
  }

  const pickFile = () => fileRef.current?.click()

  const handleFile = async (file: File) => {
    const confirmed = window.confirm(
      `导入将【替换当前全部数据】（账号、任务、代理、配置），覆盖后无法通过界面撤销。\n\n` +
        `导入前会自动备份到 data/import_backups/ 下，可用它回退。\n\n` +
        `确认导入 ${file.name}（${formatBytes(file.size)}）？`,
    )
    if (!confirmed) return

    setImporting(true)
    setLastImport(null)
    try {
      const token = getToken()
      // 原始字节体（非 multipart）：后端刻意不引 python-multipart。
      // body 直接给 File（它本身就是 Blob）：fetch 会流式读，不必先
      // arrayBuffer() 把整个文件拷进内存再发（大包时白占一份内存）。
      const res = await fetch('/api/backup/import', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/zip',
          'X-Bundle-Filename': encodeURIComponent(file.name).slice(0, 200),
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: file,
      })
      const text = await res.text()
      let payload: unknown = null
      try {
        payload = JSON.parse(text)
      } catch {
        /* 非 JSON 响应 */
      }
      if (!res.ok) {
        const detail =
          (payload && typeof payload === 'object' && 'detail' in payload
            ? String((payload as { detail?: unknown }).detail || '')
            : '') || text
        throw new Error(detail || `HTTP ${res.status}`)
      }
      const result = payload as ImportResult
      setLastImport(result)
      message.success(
        `导入完成：${result.imported.length} 个数据库已恢复` +
          (result.key_restored ? '（含凭据密钥）' : ''),
      )
      await load()
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : '导入失败')
    } finally {
      setImporting(false)
      // 允许重复选同一个文件
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const activeTasks = Boolean(info?.has_active_tasks)

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        size="small"
        title={
          <Space>
            <DatabaseOutlined />
            <span>数据备份与迁移</span>
          </Space>
        }
        extra={
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
          把全部数据库（账号、任务、代理、配置）与凭据密钥打成 ZIP；
          在另一台机器导入即可完成迁移（日志不进包）。
        </Typography.Paragraph>

        {info ? (
          <Descriptions size="small" column={1} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="数据目录">
              <Typography.Text code>{info.data_dir}</Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="凭据密钥">
              {info.has_credential_key ? (
                <Tag color="success">在包内（导入端可直接解密凭据）</Tag>
              ) : (
                <Tag color="warning">不存在 —— 导入端需自备同款密钥</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="运行中任务">
              {activeTasks ? <Tag color="error">有 —— 导入会被拒绝</Tag> : <Tag>无</Tag>}
            </Descriptions.Item>
          </Descriptions>
        ) : null}

        {info?.databases?.length ? (
          <Table<BackupDbInfo>
            size="small"
            rowKey="name"
            pagination={false}
            dataSource={info.databases}
            columns={[
              { title: '数据库', dataIndex: 'name' },
              { title: '内容', dataIndex: 'path' },
              {
                title: '大小',
                dataIndex: 'size',
                width: 100,
                render: (value: number) => formatBytes(value),
              },
              {
                title: '行数',
                dataIndex: 'tables',
                render: (tables: Record<string, number>) =>
                  Object.entries(tables || {})
                    .map(([table, count]) => `${table} ${count}`)
                    .join(' · ') || '—',
              },
            ]}
          />
        ) : null}

        <Space style={{ marginTop: 16 }} wrap>
          <Button
            type="primary"
            icon={<CloudDownloadOutlined />}
            loading={exporting}
            onClick={() => void handleExport()}
            data-hermes-action="backup-export"
          >
            导出全部数据
          </Button>
          <Button
            danger
            icon={<CloudUploadOutlined />}
            loading={importing}
            onClick={pickFile}
            disabled={activeTasks}
            data-hermes-action="backup-import"
          >
            导入备份包
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip"
            style={{ display: 'none' }}
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void handleFile(file)
            }}
          />
        </Space>

        {activeTasks ? (
          <Alert
            style={{ marginTop: 12 }}
            type="warning"
            showIcon
            message="有运行中的注册任务"
            description="导入会替换任务脚下的数据库，请先在「任务运行」页停止全部任务再导入。"
          />
        ) : null}
      </Card>

      {lastImport ? (
        <Card size="small" title="上次导入结果">
          <Descriptions size="small" column={1}>
            <Descriptions.Item label="恢复的数据库">
              {lastImport.imported.join('、') || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="凭据密钥">
              {lastImport.key_restored ? '已恢复' : '包内没有（未改动现有密钥）'}
            </Descriptions.Item>
            <Descriptions.Item label="自动备份">
              <Typography.Text code>{lastImport.backup || '（无）'}</Typography.Text>
            </Descriptions.Item>
            {lastImport.manifest?.exported_at ? (
              <Descriptions.Item label="备份包导出时间">
                {lastImport.manifest.exported_at}
              </Descriptions.Item>
            ) : null}
          </Descriptions>
          <Alert
            style={{ marginTop: 8 }}
            type="info"
            showIcon
            message="导入后可刷新页面查看新数据；如需回退，用上面路径里的备份文件再导入一次。"
          />
        </Card>
      ) : null}
    </Space>
  )
}

export default DataBackupPanel
