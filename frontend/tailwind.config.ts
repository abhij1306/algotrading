import type { Config } from 'tailwindcss'

const config: Config = {
    content: [
        './pages/**/*.{js,ts,jsx,tsx,mdx}',
        './components/**/*.{js,ts,jsx,tsx,mdx}',
        './app/**/*.{js,ts,jsx,tsx,mdx}',
    ],
    theme: {
        extend: {
            colors: {
                "primary": "#3b82f6", // Stitch Blue
                "background-light": "#f8fafc",
                "background-dark": "#0f172a", // Slate 900
                "card-dark": "#1e293b", // Slate 800
                "border-dark": "#334155", // Slate 700
                "text-secondary": "#94a3b8", // Slate 400
                "profit": "#10b981",
                "loss": "#ef4444",
                "accent-blue": "#3b82f6",
                "accent-purple": "#8b5cf6",
                "accent-teal": "#14b8a6",
                "accent-orange": "#f97316"
            },
            fontFamily: {
                "display": ["Inter", "sans-serif"],
                "sans": ["Inter", "sans-serif"]
            },
            borderRadius: { "DEFAULT": "0.25rem", "lg": "0.5rem", "xl": "0.75rem", "full": "9999px" },
        },
    },
    plugins: [],
    darkMode: 'class',
}
export default config
