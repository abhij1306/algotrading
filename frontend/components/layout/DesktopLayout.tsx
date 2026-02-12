"use client"

import AppLayout from './AppLayout';

interface DesktopLayoutProps {
  children: React.ReactNode;
}

export function DesktopLayout({ children }: DesktopLayoutProps) {
  return <AppLayout>{children}</AppLayout>;
}

export default DesktopLayout;
