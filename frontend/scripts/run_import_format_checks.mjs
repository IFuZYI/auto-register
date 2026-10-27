// 导入必须带「来源格式」—— 否则 `----` 分隔的多列行解析不了。
import { readFileSync } from 'node:fs'
const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }
const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

ok(/importSourceFormat/.test(page), '导入弹窗缺少来源格式状态')
ok(/source_format/.test(page), '导入请求没带 source_format')
ok(/export-formats/.test(page), '没加载导出格式清单（来源格式选择器会是空的）')
// JSON 模式不该带 source_format（走全字段，不需要列映射）
ok(/importFormat === 'text' \? importSourceFormat : ''/.test(page),
   'source_format 没有按格式区分（JSON 模式不该带）')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
