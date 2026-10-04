/**
 * 邮箱服务 —— iCloud 隐私邮箱（本地）。
 *
 * 这里放的是**真正的 iCloud 隐私邮箱控制台**：导入 iCloud 主号 Cookie、生成/停用
 * Hide My Email 别名（也就是 iCloud 这边的号池）、查看每个别名的收件箱。主号与
 * 别名存在本机 `data/platforms/icloud.db`。
 *
 * 与 Outlook（本地）页的分工：那边管「微软邮箱号池」的导入与收信后端，这边管
 * 「iCloud 主号 + Hide My Email 别名池」的生成与收信。两边都是本地号池，
 * 注册时按 provider 取号。
 *
 * 「本地」是与「远程 icloud-hme」相对：这条链路里凭据、别名、收信都在本机自己
 * 维护。（远程那条链路已按用户要求整体删除 —— 独立的 icloud-hme 服务、它的
 * 客户端与配置项都不在了。保留这句对照是为了让老文档里的说法有个落点。）
 *
 * 注意别把「小苹果 / AppleMail（appleemail.top）」混进来 —— 那是个第三方临时
 * 邮箱服务，只是名字带 Apple，与 iCloud 无关，且已弃用删除。
 */
import ICloudPage from '@/pages/ICloud'

export default function MailICloudLocalPage() {
  return <ICloudPage />
}
