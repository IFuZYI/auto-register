/**
 * 配置项下拉选项表。
 *
 * 抽到 lib 里是因为「全局配置」的几个页面共用同一批字段，
 * 选项留在页面里会出现两份定义、慢慢分叉。
 */
export const SELECT_FIELDS: Record<string, { label: string; value: string }[]> = {
  mail_provider: [
    { label: 'Outlook（微软号池）', value: 'microsoft' },
    { label: 'iCloud 隐私邮箱（本地主号）', value: 'icloud_local' },
  ],
  // default_executor 已取消：执行器改成**按平台**设置（`<platform>_executor`），
  // 在「注册设置 → 各平台」卡片里渲染。各平台支持的集合不同，一个全局值
  // 必然对某些平台无效（比如给 Grok 设 protocol —— 它只有浏览器路径）。
  // 选项现在从 `/api/platforms` 的 `supported_executors` 生成。
  default_captcha_solver: [
    { label: 'YesCaptcha', value: 'yescaptcha' },
    { label: '本地 Solver (Camoufox)', value: 'local_solver' },
    { label: '手动', value: 'manual' },
  ],
  outlook_backend: [
    { label: 'Graph（默认）', value: 'graph' },
    { label: 'IMAP', value: 'imap' },
  ],
  cpa_cleanup_enabled: [
    { label: '关闭', value: '0' },
    { label: '开启', value: '1' },
  ],
  sms_provider: [
    { label: 'SmsBower', value: 'smsbower' },
    { label: 'HeroSMS', value: 'herosms' },
  ],
  // 平台的执行器与注册方式**不在这里** —— 它们由插件声明
  // （`supported_executors` / `executor_labels` / `registration_modes`），
  // 随 `/api/platforms` 下发，在「注册设置 → 各平台设置」下按平台渲染
  // （见 RegisterSettingsPanel）。在此再写一份会与插件分叉：
  // 界面显示「支持」而运行时静默忽略。
  // 注：注册方式（browser/protocol）已并入执行器 —— 那是「怎么访问目标站」。
}
