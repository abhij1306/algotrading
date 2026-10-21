import { useNavigate } from "react-router";
import { Moon, Sun, Command } from "lucide-react";
import LoginButton from "@/components/LoginButton";
import { useTheme } from "@/lib/theme";

export function AppHeader() {
  const navigate = useNavigate();
  const { isDark, toggleTheme } = useTheme();

  return (
    <header className="h-14 flex items-center justify-between px-4 sticky top-0 z-50 bg-background border-b border-border-subtle">
      <button
        onClick={() => void navigate("/hat")}
        className="flex items-center gap-2 hover:opacity-80 transition-opacity"
      >
        <div className="w-8 h-8 flex items-center justify-center rounded-md bg-gradient-to-br from-primary to-primary-hover shadow-sm">
          <Command className="w-5 h-5 text-primary-fg" />
        </div>
        <span className="font-semibold text-lg hidden sm:block text-foreground">Hire a Trader</span>
      </button>

      <div className="flex items-center gap-1">
        <button
          onClick={toggleTheme}
          className="p-2 rounded-lg transition-all text-foreground-secondary hover:bg-background-tertiary"
          title={isDark ? "Switch to light mode" : "Switch to dark mode"}
        >
          {isDark ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
        </button>
        <div className="ml-2 pl-2 border-l border-border-subtle">
          <LoginButton />
        </div>
      </div>
    </header>
  );
}
