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
                "primary": "#6366f1", // New indigo primary
                "background-light": "#f6f6f8",
                "background-dark": "#0f1115", // Deeper black/gray background
                "card-dark": "#181b21", // Slightly lighter card background
                "border-dark": "#2d3139", // New border color
                "text-secondary": "#9ca3af", // Gray-400 for secondary text
                "profit": "#10b981", // Emerald green for profit
                "loss": "#ef4444", // Red for loss
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
