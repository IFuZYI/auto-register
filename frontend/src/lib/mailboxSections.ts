/**
 * 邮箱服务的配置定义。
 *
 * 从 `pages/Settings.tsx` 抽出来：邮箱服务现在是一级菜单，下面按用途分成几个
 * 二级页面（iCloud 本地 / Outlook 本地），它们读同一份定义。放在 `lib/` 而不是
 * 某个页面里，是为了避免「二级页面 A 从二级页面 B import」这种依赖。
 *
 * 历史：这里曾有十几个一次性临时邮箱渠道（Laoudo / Freemail / MoeMail /
 * SkyMail / CloudMail / MaliAPI / GPTMail / OpenTrashMail / TempMail.lol /
 * Mail.tm / DuckMail / CF Worker）与远程 icloud-hme 的连接参数，以及依赖
 * `cfworker` 的域名规则开关。按用户要求只保留两个「本地号池」渠道后，那些
 * 区块整体删除，本文件因此只剩下 Outlook 一个分组。
 */
import type { SectionConfig } from '@/components/settings/ConfigPanels'

/** 二级页面归属。 */
export type MailboxGroupKey =
  /** Outlook / Hotmail（本地）：微软号池导入 */
  | 'outlook-local'

export interface MailboxSectionConfig extends SectionConfig {
  /** 归到哪个二级页面。 */
  group?: MailboxGroupKey
}

export const MAILBOX_SECTIONS: MailboxSectionConfig[] = [
  {
    title: '邮箱导入（微软 / Outlook / Hotmail）',
    group: 'outlook-local',
    desc: '使用本地导入的微软账号池，运行时支持 Graph / IMAP 轮询（默认 Graph）',
    fields: [
      { key: 'outlook_backend', label: '微软收信方式', type: 'select' },
    ],
  },
]
