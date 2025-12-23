'use client'

export default function ActionCenter() {
    return (
        <div className="p-6 flex flex-col items-center justify-center h-full">
            <div className="text-center space-y-4">
                <div className="w-16 h-16 mx-auto bg-cyan-500/10 rounded-full flex items-center justify-center border border-cyan-500/20">
                    <svg className="w-8 h-8 text-cyan-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />                    </svg>
                </div>
                <div>
                    <h3 className="text-lg font-bold text-white mb-2">Action Center</h3>
                    <p className="text-sm text-gray-400">AI-powered trading actions will appear here</p>
                </div>
            </div>
        </div>
    )
}
