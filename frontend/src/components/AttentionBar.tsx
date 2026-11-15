import { Link } from 'react-router-dom'
import { ArrowRightOutlined } from '@ant-design/icons'
import type { AttentionReading } from '@/lib/attention'

/**
 * 待办横条 —— 工作台第一屏只回答一个问题：「现在要紧吗」。
 *
 * 为什么单独做一条而不是再放一张卡片：现有四张卡片等权并排，每张都有
 * 数字、都能点，读者要自己算「607 个已封算不算多」。横条把判断替人做完，
 * 一句话给结论，右侧只留一个「所以去哪」。
 *
 * 视觉上刻意低调：不是告警横幅（没有底色铺满、没有图标轰炸），
 * 它是一行字 + 一条指向。只有 `action` 级别才用左缘竖线标出来 ——
 * 那才是真正需要动手的。
 *
 * 无障碍：`role="status"` + `aria-live="polite"`，数据从「读取中」
 * 变成结论时读屏会播报；结论不只靠颜色（级别同时写在文案里）。
 */
export function AttentionBar({ reading }: { reading: AttentionReading }) {
  const action = reading.href ? (
    <Link className="attention__action" to={reading.href}>
      {reading.action || '去看看'}
      <ArrowRightOutlined />
    </Link>
  ) : null

  return (
    <section
      className={`attention attention--${reading.level}`}
      role="status"
      aria-live="polite"
    >
      <div className="attention__body">
        <p className="attention__headline">{reading.headline}</p>
        {reading.detail ? <p className="attention__detail">{reading.detail}</p> : null}
      </div>
      {action}
    </section>
  )
}