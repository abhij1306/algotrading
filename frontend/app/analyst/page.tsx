import AnalystClient from './AnalystClient'
import { ToastProvider } from '@/components/Toast'

export const metadata = {
    title: 'Analyst | SmartTrader',
    description: 'Portfolio Risk & Performance Analytics',
}

export default function AnalystPage() {
    return (
        <ToastProvider>
            <AnalystClient />
        </ToastProvider>
    )
}
