'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

export default function RootPage() {
  const router = useRouter()

  useEffect(() => {
    router.push('/dashboard')
  }, [router])

  return (
    <div className="h-screen bg-[#050505] flex items-center justify-center">
      <div className="text-cyan-400 font-mono animate-pulse">Redirecting to SmartTrader...</div>
    </div>
  )
}
