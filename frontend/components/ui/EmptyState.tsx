"use client";

import { cn } from "@/lib/utils";
import { LucideIcon } from "lucide-react";

interface EmptyStateProps {
  icon?: LucideIcon | React.ComponentType<React.SVGProps<SVGSVGElement>>;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export default function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: Readonly<EmptyStateProps>) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center p-8", className)}>
      {Icon && (
        <div className="mb-4 text-foreground-muted">
          <Icon className="w-10 h-10" />
        </div>
      )}
      <h3 className="text-lg font-semibold text-foreground mb-2">{title}</h3>
      {description && (
        <p className="text-sm text-foreground-secondary mb-4 max-w-sm">{description}</p>
      )}
      {action}
    </div>
  );
}
