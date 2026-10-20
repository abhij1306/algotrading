import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router/dom";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@/app/globals.css";
import { createQueryClient } from "@/lib/queries/query-client";
import { router } from "@/router";

const queryClient = createQueryClient();

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
