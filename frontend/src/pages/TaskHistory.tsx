import { useCallback, useEffect, useState } from 'react'
import { App, Card, Table, Select, Button, Dropdown, Tag, Popconfirm, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { ReloadOutlined, DeleteOutlined, ClearOutlined, DownOutlined } from '@ant-design/icons'
import {
  PLATFORM_FILTER_OPTIONS,
  getPlatformTagColor,
  getPlatformLabel,
} from '@/lib/platforms'
import { CLEANUP_ENDPOINTS, RETENTION_OPTIONS, retentionLabel } from '@/lib/logCleanup'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'

const { Text } = Typography

interface TaskLogItem {
  id: number
  created_at: string
  platform: string
  email: string
  status: 'success' | 'failed'
  error: string
}

interface TaskLogListResponse {
  total: number
  items: TaskLogItem[]
}

interface TaskLogBatchDeleteResponse {
  deleted: number
  not_found: number[]
  total_requested: number
}

interface LogCleanupResponse {
  deleted: number
  older_than_days: number
  cutoff: string
}

export default function TaskHistory() {
  const { modal, message: msg } = App.useApp()
  const [logs, setLogs] = useState<TaskLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [platform, setPlatform] = useState('')
  const [loading, setLoading] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [cleaning, setCleaning] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: '1', page_size: '50' })
      if (platform) params.set('platform', platform)
      const data = await apiFetch(`/tasks/logs?${params}`) as TaskLogListResponse
      setLogs(data.items || [])
      setTotal(data.total || 0)
      setSelectedRowKeys((prev) => prev.filter((key) => data.items.some((item) => item.id === key)))
    } finally {
      setLoading(false)
    }
  }, [platform])

  useEffect(() => {
    load()
  }, [load])

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return

    const result = await apiFetch('/tasks/logs/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: selectedRowKeys }),
    }) as TaskLogBatchDeleteResponse

    msg.success(`已删除 ${result.deleted} 条任务历史`)
    if (result.not_found.length > 0) {
      msg.warning(`${result.not_found.length} 条记录不存在或已被删除`)
    }
    setSelectedRowKeys([])
    await load()
  }

  const handleCleanup = (days: number) => {
    modal.confirm({
      title: `确认清理 ${retentionLabel(days)}的任务历史？`,
      content: '保留期内的记录不受影响；清理后无法恢复。',
      okText: '清理',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setCleaning(true)
        try {
          const result = (await apiFetch(CLEANUP_ENDPOINTS.logs, {
            method: 'POST',
            body: JSON.stringify({ older_than_days: days }),
          })) as LogCleanupResponse
          msg.success(`已清理 ${result.deleted} 条任务历史（保留 ${retentionLabel(days)}）`)
          setSelectedRowKeys([])
          await load()
        } catch (error) {
          msg.error(error instanceof Error ? error.message : '清理失败')
        } finally {
          setCleaning(false)
        }
      },
    })
  }

  const columns: TableColumnsType<TaskLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (text: string) => (text ? new Date(text).toLocaleString('zh-CN') : '-'),
    },
    {
      title: '平台',
      dataIndex: 'platform',
      key: 'platform',
      width: 140,
      render: (text: string) => <Tag color={getPlatformTagColor(text)}>{getPlatformLabel(text)}</Tag>,
    },
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      render: (text: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{text}</span>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 80,
      render: (status: string) => (
        <Tag color={status === 'success' ? 'success' : 'error'}>
          {status === 'success' ? '成功' : '失败'}
        </Tag>
      ),
    },
    {
      title: '错误信息',
      dataIndex: 'error',
      key: 'error',
      render: (text: string) => text || '-',
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <PageHeader
        title="任务历史"
        subtitle={`注册任务执行记录 · 共 ${total} 条`}
        actions={
          <>
            {selectedRowKeys.length > 0 && (
              <Text type="success" style={{ fontSize: 13 }}>
                已选 {selectedRowKeys.length} 条
              </Text>
            )}
            {selectedRowKeys.length > 0 && (
              <Popconfirm
                title={`确认删除选中的 ${selectedRowKeys.length} 条任务历史？`}
                onConfirm={handleBatchDelete}
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
              >
                <Button danger icon={<DeleteOutlined />}>
                  删除 {selectedRowKeys.length} 条
                </Button>
              </Popconfirm>
            )}
            <Select
              value={platform}
              onChange={(value) => {
                setPlatform(value)
                setSelectedRowKeys([])
              }}
              style={{ width: 160 }}
              options={PLATFORM_FILTER_OPTIONS}
            />
            <Dropdown
              disabled={cleaning}
              menu={{
                items: RETENTION_OPTIONS.map((option) => ({
                  key: String(option.days),
                  label: `清理 ${option.label}`,
                })),
                onClick: ({ key }) => handleCleanup(Number(key)),
              }}
            >
              <Button icon={<ClearOutlined />} loading={cleaning}>
                清理日志 <DownOutlined />
              </Button>
            </Dropdown>
            <Button icon={<ReloadOutlined spin={loading} />} onClick={load} loading={loading} aria-label="刷新记录" title="刷新记录" />
          </>
        }
      />

      <Card className="app-card">
        <Table
          rowKey="id"
          columns={columns}
          dataSource={logs}
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </Card>
    </div>
  )
}
