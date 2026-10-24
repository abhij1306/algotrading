import { Link } from "react-router";
import { Moon, Sun } from "lucide-react";
import { Button } from "@/components/atoms/button";
import { LoginButton } from "@/components/organisms/login-button";
import { useBrokerStatus } from "@/lib/queries/auth";
import { useTheme } from "@/lib/theme";

export function AppHeader() {
  const { isDark, toggleTheme } = useTheme();
  // Fyers always logs in (market data); Zerodha too while it is the active order broker.
  const zerodha = useBrokerStatus("zerodha").data;
  const zerodhaActive = zerodha !== undefined && zerodha.kind !== "offline" && zerodha.active;

  return (
    <header className="sticky top-0 z-40 bg-background/90 backdrop-blur">
      <div className="mx-auto flex h-16 w-full max-w-[1440px] items-center justify-between gap-4 px-4 md:px-10">
        <Link
          to="/hat"
          className="text-base font-medium tracking-tight text-foreground hover:text-accent"
        >
          HAT
        </Link>
        <div className="flex items-center gap-3 md:gap-5">
          <LoginButton />
          {zerodhaActive ? <LoginButton broker="zerodha" /> : null}
          <Button
            size="icon"
            onClick={toggleTheme}
            aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
            title={isDark ? "Switch to light mode" : "Switch to dark mode"}
          >
            {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
        </div>
      </div>
    </header>
  );
}
