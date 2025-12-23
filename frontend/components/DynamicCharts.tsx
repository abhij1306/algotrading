'use client'

import dynamic from 'next/dynamic'

export const DynamicAreaChart = dynamic(
    () => import('recharts').then((mod) => mod.AreaChart as any),
    { ssr: false }
) as any

export const DynamicArea = dynamic(
    () => import('recharts').then((mod) => mod.Area as any),
    { ssr: false }
) as any

export const DynamicXAxis = dynamic(
    () => import('recharts').then((mod) => mod.XAxis as any),
    { ssr: false }
) as any

export const DynamicYAxis = dynamic(
    () => import('recharts').then((mod) => mod.YAxis as any),
    { ssr: false }
) as any

export const DynamicCartesianGrid = dynamic(
    () => import('recharts').then((mod) => mod.CartesianGrid as any),
    { ssr: false }
) as any

export const DynamicTooltip = dynamic(
    () => import('recharts').then((mod) => mod.Tooltip as any),
    { ssr: false }
) as any

export const DynamicResponsiveContainer = dynamic(
    () => import('recharts').then((mod) => mod.ResponsiveContainer as any),
    { ssr: false }
) as any

export const DynamicScatterChart = dynamic(
    () => import('recharts').then((mod) => mod.ScatterChart as any),
    { ssr: false }
) as any

export const DynamicScatter = dynamic(
    () => import('recharts').then((mod) => mod.Scatter as any),
    { ssr: false }
) as any

export const DynamicPieChart = dynamic(
    () => import('recharts').then((mod) => mod.PieChart as any),
    { ssr: false }
) as any

export const DynamicPie = dynamic(
    () => import('recharts').then((mod) => mod.Pie as any),
    { ssr: false }
) as any

export const DynamicCell = dynamic(
    () => import('recharts').then((mod) => mod.Cell as any),
    { ssr: false }
) as any

export const DynamicLegend = dynamic(
    () => import('recharts').then((mod) => mod.Legend as any),
    { ssr: false }
) as any

export const DynamicBarChart = dynamic(
    () => import('recharts').then((mod) => mod.BarChart as any),
    { ssr: false }
) as any

export const DynamicBar = dynamic(
    () => import('recharts').then((mod) => mod.Bar as any),
    { ssr: false }
) as any

export const DynamicReferenceLine = dynamic(
    () => import('recharts').then((mod) => mod.ReferenceLine as any),
    { ssr: false }
) as any

export const DynamicLineChart = dynamic(
    () => import('recharts').then((mod) => mod.LineChart as any),
    { ssr: false }
) as any

export const DynamicLine = dynamic(
    () => import('recharts').then((mod) => mod.Line as any),
    { ssr: false }
) as any
