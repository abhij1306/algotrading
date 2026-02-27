import type { NextConfig } from "next";

const devHosts = ["127.0.0.1", "localhost", "*.localhost"];
const appDevOrigins = Array.from({ length: 51 }, (_, index) => 3000 + index).flatMap((port) => [
  `http://127.0.0.1:${port}`,
  `http://localhost:${port}`,
]);
const localBridgeOrigins = [
  "http://127.0.0.1",
  "http://localhost",
  "http://127.0.0.1:8000",
  "http://localhost:8000",
];
const allowedDevOrigins = Array.from(new Set([...devHosts, ...appDevOrigins, ...localBridgeOrigins]));

const nextConfig: NextConfig = {
  allowedDevOrigins,
  images: {
    formats: ['image/avif', 'image/webp'],
  },
  typescript: {
    ignoreBuildErrors: true,
  },

  // Compiler options
  compiler: {
    removeConsole: process.env.NODE_ENV === 'production' ? {
      exclude: ['error', 'warn'],
    } : false,
  },

  // Experimental features
  experimental: {
    optimizePackageImports: [
      'lucide-react',
      'recharts',
      '@radix-ui/react-tabs',
    ],
  },

  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
