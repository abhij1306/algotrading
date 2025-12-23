import { useState, useEffect } from 'react'
import { LogIn, LogOut, Key, CheckCircle, AlertCircle, X, Wifi, WifiOff } from 'lucide-react'

export default function LoginButton({ collapsed = false }: { collapsed?: boolean }) {
    const [connected, setConnected] = useState(false)
    const [userId, setUserId] = useState('')
    const [loading, setLoading] = useState(false)
    const [checking, setChecking] = useState(true)
    const [showModal, setShowModal] = useState(false)
    const [authCode, setAuthCode] = useState('')
    const [status, setStatus] = useState<'idle' | 'success' | 'error'>('idle')
    const [message, setMessage] = useState('')
    const [systemOffline, setSystemOffline] = useState(false)

    // Check connection status on mount
    useEffect(() => {
        checkConnection()
    }, [])

    const checkConnection = async () => {
        setChecking(true)
        setSystemOffline(false)
        const controller = new AbortController()
        const timeoutId = setTimeout(() => controller.abort(), 5000) // Reduced to 5s for faster feedback

        try {
            const res = await fetch('http://localhost:8000/api/auth/fyers/status', {
                signal: controller.signal
            })
            if (!res.ok) throw new Error('Backend error')

            const data = await res.json()

            if (data.connected) {
                setConnected(true)
                setUserId(data.user_id || '')
            } else {
                setConnected(false)
            }
        } catch (error: any) {
            // Silently handle offline/network errors
            setConnected(false)
            setSystemOffline(true)
        } finally {
            clearTimeout(timeoutId)
            setChecking(false)
        }
    }

    const handleDisconnect = async () => {
        if (!confirm('Disconnect from Fyers? You will need to re-authenticate.')) return

        setLoading(true)
        try {
            const res = await fetch('http://localhost:8000/api/auth/fyers/disconnect', { method: 'POST' })
            if (res.ok) {
                setConnected(false)
                setUserId('')
                setMessage('Disconnected successfully')
                setStatus('success')
                setTimeout(() => setStatus('idle'), 2000)
            }
        } catch (error) {
            console.error('[Fyers] Disconnect error:', error)
            setStatus('error')
            setMessage('Failed to disconnect')
        } finally {
            setLoading(false)
        }
    }

    const handleLoginStart = async () => {
        console.log('[Fyers] Button clicked - starting login process...')
        setLoading(true)
        setStatus('idle')
        setMessage('')

        try {
            console.log('[Fyers] Fetching auth URL from backend...')
            const res = await fetch('http://localhost:8000/api/auth/fyers/url')
            const data = await res.json()

            if (res.ok && data.url) {
                console.log('[Fyers] Opening auth URL...')
                // Use Electron's shell.openExternal if available
                const electron = (window as any).electron
                if (electron?.openExternal) {
                    electron.openExternal(data.url)
                } else {
                    window.open(data.url, '_blank', 'width=800,height=600')
                }
                setShowModal(true)
            } else {
                setStatus('error')
                setMessage(data.detail || data.error || 'Failed to get Login URL')
            }
        } catch (e) {
            console.error('[Fyers] Network error:', e)
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
                setConnected(true)
                setTimeout(() => {
                    setShowModal(false)
                    setAuthCode('')
                    checkConnection() // Refresh connection status
                }, 2000)
            } else {
                setStatus('error')
                setMessage(data.detail || 'Authentication failed')
            }
        } catch (e) {
            console.error('[Fyers] Token network error:', e)
            setStatus('error')
            setMessage('Failed to exchange token')
        } finally {
            setLoading(false)
        }
    }

    if (checking) {
        return collapsed ? (
            <div className="flex justify-center w-full py-2">
                <div className="w-4 h-4 border-2 border-cyan-400 border-t-transparent rounded-full animate-spin" />
            </div>
        ) : (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-[#0A0A0A] border border-white/10 rounded-lg">
                <div className="w-3 h-3 border-2 border-cyan-400 border-t-transparent rounded-full animate-spin" />
                <span className="text-xs text-gray-400">Checking...</span>
            </div>
        )
    }

    if (systemOffline) {
        return collapsed ? (
            <div className="flex justify-center w-full py-2" title="System Offline">
                <WifiOff className="w-4 h-4 text-red-500 opacity-50" />
            </div>
        ) : (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-gray-800/50 border border-gray-700/50 rounded-lg cursor-not-allowed opacity-75" title="Backend unreachable">
                <WifiOff className="w-3.5 h-3.5 text-gray-500" />
                <span className="text-xs font-bold text-gray-500">System Offline</span>
                <button onClick={checkConnection} className="ml-1 hover:text-white transition-colors">
                    <div className="w-2 h-2 rounded-full bg-red-500/20 hover:bg-red-500" />
                </button>
            </div>
        )
    }


    if (connected) {
        return collapsed ? (
            <div className="flex flex-col items-center gap-2">
                <div className="flex justify-center w-full py-2" title={`Connected as ${userId}`}>
                    <Wifi className="w-4 h-4 text-green-400" />
                </div>
                <button
                    onClick={handleDisconnect}
                    className="p-1 hover:bg-red-500/10 rounded text-gray-500 hover:text-red-400"
                    title="Disconnect"
                >
                    <LogOut className="w-3 h-3" />
                </button>
            </div>
        ) : (
            <div className="flex items-center justify-between w-full px-3 py-2 bg-[#0A0A0A] border border-green-900/30 rounded-lg group hover:border-green-500/50 transition-colors">
                <div className="flex items-center gap-2.5 overflow-hidden">
                    <div className="relative">
                        <Wifi className="w-4 h-4 text-green-400" />
                        <div className="absolute inset-0 bg-green-400 blur-sm opacity-20" />
                    </div>
                    <div className="flex flex-col truncate">
                        <span className="text-[10px] font-bold text-green-400 uppercase tracking-wider">Connected</span>
                        {userId && <span className="text-[10px] text-gray-500 font-mono truncate">{userId}</span>}
                    </div>
                </div>
                <button
                    onClick={handleDisconnect}
                    disabled={loading}
                    className="p-1.5 rounded-md hover:bg-red-500/10 text-gray-500 hover:text-red-400 transition-colors"
                    title="Disconnect from Fyers"
                >
                    <LogOut className="w-3.5 h-3.5" />
                </button>
            </div>
        )
    }

    return (
        <>
            <div className="relative w-full">
                <button
                    onClick={handleLoginStart}
                    disabled={loading}
                    title={collapsed ? "Connect Fyers" : ""}
                    className={collapsed ?
                        "flex items-center justify-center w-full py-3 text-gray-500 hover:text-cyan-400" :
                        "w-full flex items-center justify-between px-3 py-2 bg-[#0A0A0A] border border-white/10 hover:border-cyan-500/50 rounded-lg text-xs font-bold text-gray-300 hover:text-cyan-400 transition-all group disabled:opacity-50"
                    }
                >
                    <div className="flex items-center gap-2">
                        <WifiOff className="w-4 h-4 group-hover:scale-110 transition-transform" />
                        {!collapsed && <span>Connect Fyers</span>}
                    </div>
                </button>

                {status === 'error' && !showModal && message && (
                    <div className="absolute top-full left-0 mt-2 w-64 p-2 bg-red-500/10 border border-red-500/20 rounded text-xs text-red-400 z-50">
                        <AlertCircle className="w-3 h-3 inline mr-1" />
                        {message}
                    </div>
                )}
            </div>

            {showModal && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
                    <div className="bg-[#0A0A0A] border border-white/10 rounded-xl p-6 w-[500px] shadow-2xl relative">
                        <button
                            onClick={() => setShowModal(false)}
                            className="absolute top-3 right-3 text-gray-500 hover:text-white"
                        >
                            <X className="w-4 h-4" />
                        </button>

                        <h3 className="text-lg font-bold text-white mb-2 flex items-center gap-2">
                            <Key className="w-5 h-5 text-cyan-400" /> Authenticate Fyers
                        </h3>

                        <div className="bg-cyan-500/5 border border-cyan-500/10 rounded-lg p-4 mb-4 text-xs text-cyan-300 leading-relaxed space-y-2">
                            <p><strong>Step 1:</strong> A Fyers login window has opened in your browser</p>
                            <p><strong>Step 2:</strong> Login with your credentials</p>
                            <p className="flex items-start gap-2">
                                <span className="text-cyan-500 font-bold">Step 3:</span>
                                <span>After login, you'll be redirected to a page with URL like:<br />
                                    <code className="text-[10px] bg-black/40 px-2 py-0.5 rounded mt-1 block">
                                        https://example.com/?{'{'}auth_code=<span className="text-yellow-300 font-bold">XXXXXX...</span>{'}'}
                                    </code>
                                </span>
                            </p>
                            <p><strong>Step 4:</strong> Copy only the <code className="bg-yellow-500/10 text-yellow-300 px-1 rounded">auth_code</code> value and paste below</p>
                        </div>

                        <div className="space-y-4">
                            <div>
                                <label className="block text-xs font-bold text-gray-400 mb-2">Authorization Code</label>
                                <input
                                    type="text"
                                    value={authCode}
                                    onChange={(e) => setAuthCode(e.target.value)}
                                    placeholder="Paste auth_code here (without auth_code= prefix)"
                                    className="w-full bg-black/50 border border-white/10 rounded-lg px-4 py-3 text-sm text-white font-mono focus:border-cyan-500/50 outline-none"
                                    autoFocus
                                />
                            </div>

                            <button
                                onClick={handleSubmit}
                                disabled={loading || !authCode}
                                className="w-full py-3 bg-gradient-to-r from-cyan-600 to-blue-600 rounded-lg text-sm font-bold text-white hover:from-cyan-500 hover:to-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
                            >
                                {loading ? 'Verifying...' : 'Authenticate'}
                            </button>
                        </div>

                        {status === 'success' && (
                            <div className="mt-4 p-3 bg-green-500/10 border border-green-500/20 rounded flex items-center gap-2 text-green-400 text-xs font-bold">
                                <CheckCircle className="w-4 h-4" /> {message}
                            </div>
                        )}

                        {status === 'error' && (
                            <div className="mt-4 p-3 bg-red-500/10 border border-red-500/20 rounded flex items-center gap-2 text-red-400 text-xs font-bold">
                                <AlertCircle className="w-4 h-4" /> {message}
                            </div>
                        )}
                    </div>
                </div>
            )}
        </>
    )
}
