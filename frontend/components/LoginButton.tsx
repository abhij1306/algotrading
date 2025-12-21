import { useState } from 'react'
import { LogIn, Key, CheckCircle, AlertCircle, X } from 'lucide-react'

export default function LoginButton() {
    const [loading, setLoading] = useState(false)
    const [showModal, setShowModal] = useState(false)
    const [authCode, setAuthCode] = useState('')
    const [status, setStatus] = useState<'idle' | 'success' | 'error'>('idle')
    const [message, setMessage] = useState('')

    const handleLoginStart = async () => {
        setLoading(true)
        setStatus('idle')
        setMessage('')

        try {
            const res = await fetch('http://localhost:8000/api/auth/fyers/url')
            const data = await res.json()

            if (res.ok && data.url) {
                // Use Electron's shell.openExternal if available to open in system browser
                const electron = (window as any).electron
                if (electron?.openExternal) {
                    electron.openExternal(data.url)
                } else {
                    window.open(data.url, '_blank', 'width=800,height=600')
                }
                setShowModal(true)
            } else {
                setStatus('error')
                setMessage(data.detail || 'Failed to get Login URL')
            }
        } catch (e) {
            setStatus('error')
            setMessage('Network Error: Backend not reachable')
        } finally {
            setLoading(false)
        }
    }

    const handleSubmit = async () => {
        if (!authCode) return
        setLoading(true)
        setStatus('idle')

        try {
            const res = await fetch('http://localhost:8000/api/auth/fyers/token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ auth_code: authCode })
            })

            const data = await res.json()

            if (res.ok) {
                setStatus('success')
                setMessage('Connected successfully!')
                setTimeout(() => setShowModal(false), 2000)
            } else {
                setStatus('error')
                setMessage(data.detail || 'Authentication failed')
            }
        } catch (e) {
            setStatus('error')
            setMessage('Failed to exchange token')
        } finally {
            setLoading(false)
        }
    }

    return (
        <>
            <button
                onClick={handleLoginStart}
                disabled={loading}
                className="flex items-center gap-2 px-3 py-1.5 bg-[#0A0A0A] border border-white/10 hover:border-cyan-500/50 rounded-lg text-xs font-bold text-gray-300 hover:text-cyan-400 transition-all group"
            >
                <LogIn className="w-3.5 h-3.5 group-hover:scale-110 transition-transform" />
                {loading ? 'Connecting...' : 'Connect Fyers'}
            </button>

            {showModal && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
                    <div className="bg-[#0A0A0A] border border-white/10 rounded-xl p-6 w-96 shadow-2xl relative">
                        <button
                            onClick={() => setShowModal(false)}
                            className="absolute top-3 right-3 text-gray-500 hover:text-white"
                        >
                            <X className="w-4 h-4" />
                        </button>

                        <h3 className="text-lg font-bold text-white mb-2 flex items-center gap-2">
                            <Key className="w-5 h-5 text-cyan-400" /> Authenticate Fyers
                        </h3>

                        <p className="text-xs text-gray-400 mb-4 leading-relaxed">
                            1. A login window has opened.<br />
                            2. Log in to your Fyers account.<br />
                            3. After redirect, copy the <code>auth_code</code> from the URL.<br />
                            4. Paste it below.
                        </p>

                        <div className="space-y-4">
                            <input
                                type="text"
                                value={authCode}
                                onChange={(e) => setAuthCode(e.target.value)}
                                placeholder="Paste auth_code here..."
                                className="w-full bg-black/50 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:border-cyan-500/50 outline-none"
                            />

                            <button
                                onClick={handleSubmit}
                                disabled={loading || !authCode}
                                className="w-full py-2 bg-gradient-to-r from-cyan-600 to-blue-600 rounded-lg text-xs font-bold text-white hover:from-cyan-500 hover:to-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
                            >
                                {loading ? 'Verifying...' : 'Authenticate'}
                            </button>
                        </div>

                        {status === 'success' && (
                            <div className="mt-4 p-2 bg-green-500/10 border border-green-500/20 rounded flex items-center gap-2 text-green-400 text-xs font-bold">
                                <CheckCircle className="w-4 h-4" /> {message}
                            </div>
                        )}

                        {status === 'error' && (
                            <div className="mt-4 p-2 bg-red-500/10 border border-red-500/20 rounded flex items-center gap-2 text-red-400 text-xs font-bold">
                                <AlertCircle className="w-4 h-4" /> {message}
                            </div>
                        )}
                    </div>
                </div>
            )}
        </>
    )
}
