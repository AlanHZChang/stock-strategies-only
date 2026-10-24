/** @type {import('next').NextConfig} */
const nextConfig = {
  // AI 生成為單次請求；需長於後端 90 秒期限，避免預設 30 秒先切斷。
  experimental: {
    proxyTimeout: 120_000,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000"}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
