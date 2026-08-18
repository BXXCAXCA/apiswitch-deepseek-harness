import { describe, expect, it } from 'vitest'
import { router } from '../src/router'
import { productNavigation } from '../src/navigation'

describe('router', () => {
  it('contains every product navigation entry in order, including system settings', () => {
    expect(productNavigation.map((item) => item.label)).toEqual([
      '仪表盘', '供应商', '上游模型', '统一模型', '辅助模型', '预算控制',
      '调用日志', '价格与用量', '系统设置'
    ])
    const paths = new Set(router.getRoutes().map((route) => route.path))
    for (const path of productNavigation.map((item) => item.path)) {
      expect(paths.has(path)).toBe(true)
    }
    expect(paths.has('/provider-connections')).toBe(false)
    expect(paths.has('/router-status')).toBe(false)
  })
})
