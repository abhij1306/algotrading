'use client';

import { AppHeader, MobileNav } from './Header';
import { CommandPalette, useCommandPalette } from './CommandPalette';

interface AppShellProps {
  children: React.ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const { isOpen, open, close } = useCommandPalette();

  return (
    <div className="min-h-screen flex flex-col bg-background">
      {/* Navigation */}
      <AppHeader onCommandPaletteOpen={open} />

      {/* Command Palette */}
      <CommandPalette isOpen={isOpen} onClose={close} />

      {/* Main Content */}
      <main
        className="flex-1 overflow-y-auto overflow-x-hidden"
        style={{ height: 'calc(100vh - 64px)' }}
      >
        {children}
      </main>

      {/* Mobile Navigation */}
      <MobileNav />
    </div>
  );
}

export default AppShell;
