import Link from "next/link";
import { Button, PageContainer } from "@/components/ui";

export default function NotFoundPage() {
  return (
    <PageContainer>
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-3 text-center">
        <h1 className="text-xl font-semibold text-foreground">Page Not Found</h1>
        <p className="text-sm text-foreground-secondary">
          The page you requested does not exist.
        </p>
        <Link href="/dashboard">
          <Button size="sm" variant="primary">
            Go to Dashboard
          </Button>
        </Link>
      </div>
    </PageContainer>
  );
}
