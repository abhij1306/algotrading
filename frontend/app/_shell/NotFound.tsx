import { Link } from "react-router";
import { PageLayout } from "@/components/templates/page-layout";

export default function NotFoundPage() {
  return (
    <PageLayout>
      <div className="max-w-xl space-y-3">
        <h1 className="text-xl">Page not found</h1>
        <p className="text-sm text-foreground-secondary">The page you asked for does not exist.</p>
        <Link to="/hat" className="text-sm text-accent hover:underline">
          Go to the dashboard
        </Link>
      </div>
    </PageLayout>
  );
}
