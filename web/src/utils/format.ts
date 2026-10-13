/** 全站共享的字典与格式化工具 */

export const DIFFICULTY_ORDER = ['入门', '普及-', '普及/提高-', '普及+/提高', '提高+/省选'] as const

/** 难度标签配色（Element Plus tag type） */
export function difficultyType(d: string): 'success' | 'info' | 'warning' | 'danger' | '' {
  switch (d) {
    case '入门':
      return 'success'
    case '普及-':
      return 'info'
    case '普及/提高-':
      return 'warning'
    case '普及+/提高':
      return 'warning'
    case '提高+/省选':
      return 'danger'
    default:
      return ''
  }
}

/** 判定结果配色 */
export function verdictType(v: string): 'success' | 'danger' | 'warning' | 'info' {
  switch (v) {
    case 'AC':
      return 'success'
    case 'WA':
      return 'danger'
    case 'TLE':
    case 'MLE':
    case 'OLE':
      return 'warning'
    default:
      return 'info'
  }
}

export function verdictText(v: string): string {
  const map: Record<string, string> = {
    AC: '通过',
    WA: '答案错误',
    TLE: '超时',
    MLE: '超内存',
    RE: '运行错误',
    CE: '编译错误',
    OLE: '输出超限',
    IE: '内部错误',
    PENDING: '等待判题',
  }
  return map[v] || v
}

/** 学习状态五级描述 */
export function levelText(level: string): string {
  const map: Record<string, string> = {
    L1: '起步',
    L2: '入门',
    L3: '熟练',
    L4: '进阶',
    L5: '精通',
  }
  return map[level] || level
}

export function levelColor(level: string): string {
  const map: Record<string, string> = {
    L1: '#909399',
    L2: '#E6A23C',
    L3: '#409EFF',
    L4: '#67C23A',
    L5: '#B882FF',
  }
  return map[level] || '#909399'
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso).getTime()
  if (Number.isNaN(d)) return String(iso)
  const diff = Date.now() - d
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天前`
  return fmtDate(iso)
}

/** 保留率 -> 颜色（越红越该复习） */
export function retentionColor(r: number): string {
  if (r >= 0.9) return '#67C23A'
  if (r >= 0.75) return '#409EFF'
  if (r >= 0.6) return '#E6A23C'
  return '#F56C6C'
}
