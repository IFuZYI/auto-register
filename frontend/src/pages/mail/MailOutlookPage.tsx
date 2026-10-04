/**
 * 邮箱服务 —— Outlook（本地）。
 *
 * 「本地」= 号池存在本机数据库里（`data/platforms/outlook.db`），与远程的
 * icloud-hme 相对。这里管两件事：收信后端（Graph / IMAP）与号池本身
 * （导入、预览、删除）。
 */
import { MAILBOX_SECTIONS } from '@/lib/mailboxSections'
import { MailServicePage } from '@/components/mail/MailServicePage'
import MailImportPanel from '@/components/settings/MailImportPanel'

export default function MailOutlookPage() {
  const sections = MAILBOX_SECTIONS.filter((s) => s.group === 'outlook-local')

  return (
    <MailServicePage
      title="Outlook（本地）"
      subtitle="微软邮箱号池：导入后注册时按需取号，走 Graph / IMAP 收信"
      sections={sections}
      defaults={{ outlook_backend: 'graph' }}
    >
      {/* 锁定到微软号池：这个页面就是 Outlook 的范围，
          不再给切换入口（小苹果的导入面板在「临时邮箱」页，iCloud 在
          「iCloud 隐私邮箱（本地）」页）。
          这里用函数形式拿到页面共用的 form —— 导入面板与配置区块必须是
          同一个 form 实例，否则「保存配置」拿不到面板里的字段。 */}
      {(form) => <MailImportPanel form={form} lockToProvider="outlook" />}
    </MailServicePage>
  )
}
