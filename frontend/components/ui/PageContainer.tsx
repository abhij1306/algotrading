import { ReactNode } from "react";

interface PageContainerProps {
    children: ReactNode;
    className?: string;
}

export const PageContainer = ({ children, className = "" }: PageContainerProps) => {
    return (
        <div className={`max-w-7xl mx-auto px-4 py-6 w-full animate-fade-in ${className}`}>
            {children}
        </div>
    );
};
