'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function BacktestNewRedirectPage() {
  const router = useRouter();

  useEffect(() => {
    router.replace('/backtest');
  }, [router]);

  return null;
}
