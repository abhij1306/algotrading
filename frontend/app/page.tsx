"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function RootPage() {
  const router = useRouter();

  useEffect(() => {
    router.push("/dashboard");
  }, [router]);

  return (
    <div className="h-screen bg-void flex items-center justify-center">
      <div className="text-primary font-mono animate-pulse">Redirecting to SmartTrader...</div>
    </div>
  );
}
