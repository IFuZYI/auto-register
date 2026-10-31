/**
 * 表格 → 窄屏卡片。
 *
 * 宽表（8~10 列）在 390px 上只能横向滚动，而 sticky 的操作列会盖住右侧
 * 内容 —— 实测账号表在 390px 下：容器 358px、表格 1042px，邮箱被压到
 * 「skyway-finale-6c@icloud.co…」，凭据列整个藏在操作列下面。
 *
 * 做法是 dogfood 里那条标准套路：**隐藏表头，每个单元格用 `::before` 渲染
 * 自己的列名**（`content: attr(data-label)`）。这样一行在窄屏上变成一张
 * 小卡片，标签与值成对出现，不需要横向滚动。
 *
 * 这个文件只负责**给单元格挂上 `data-label`** —— 标签从列自己的 `title`
 * 生成，一处定义：标题改了，卡片上的标签跟着改，不会出现「表头叫 AT 到期、
 * 卡片上叫 AT 有效期」这种漂移。
 *
 * 用法：
 *   const columns = withDataLabels([...])
 *   <Table className="table-cards" columns={columns} ... />
 * 样式见 `index.css` 的 `.table-cards` 段（`@media (max-width: 767px)`）。
 */

interface ColumnLike {
  title?: unknown
  onCell?: (...args: unknown[]) => Record<string, unknown>
  [key: string]: unknown
}

/**
 * 给每一列挂 `data-label`。
 *
 * 列标题不是字符串的（自定义节点，比如带 ⓘ 的标题）会被跳过 —— 那时
 * `attr(data-label)` 拿到的是 "[object Object]"，比没有标签更糟。
 * 这类列要自己写 `onCell`，或者干脆在窄屏隐藏（`responsive: ['md']`）。
 */
export function withDataLabels<T extends ColumnLike>(columns: T[]): T[] {
  return columns.map((column) => {
    const title = typeof column.title === 'string' ? column.title.trim() : ''
    if (!title) return column
    const previousOnCell = column.onCell
    return {
      ...column,
      onCell: (...args: unknown[]) => ({
        'data-label': title,
        ...(previousOnCell ? previousOnCell(...args) : {}),
      }),
    } as T
  })
}
