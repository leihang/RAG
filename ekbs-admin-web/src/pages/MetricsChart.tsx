/**
 * 概览看板：`overview.metrics` 柱状图（方案 05 §4.2「图表建议」）。
 *
 * 用 `echarts` + `echarts-for-react`（方案 §1.1 技术栈）。
 * `metrics` 为空时**不画空图**，交给调用方渲染空态 —— 图表组件只在有数据时挂载。
 */
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts-for-react'
import { metricsToBars } from './dashboardData'

export default function MetricsChart({ metrics }: { metrics: Record<string, number> }) {
  const bars = metricsToBars(metrics)
  if (bars.length === 0) return null

  const option: EChartsOption = {
    grid: { top: 24, right: 24, bottom: 64, left: 72 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'category',
      data: bars.map((bar) => bar.name),
      axisLabel: { rotate: bars.length > 6 ? 30 : 0, interval: 0, fontSize: 11 },
      axisTick: { alignWithLabel: true },
    },
    yAxis: { type: 'value', name: '数值', scale: true },
    series: [
      {
        type: 'bar',
        name: '指标值',
        data: bars.map((bar) => bar.value),
        barMaxWidth: 40,
        itemStyle: { color: '#1677ff', borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', fontSize: 11 },
      },
    ],
  }

  return <ReactECharts option={option} style={{ height: 320 }} notMerge lazyUpdate />
}
