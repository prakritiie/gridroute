import type { NextConfig } from "next";

// NEXT_PUBLIC_DEMO=1 builds the static demo (a recorded replay, no backend) into out/ for Vercel.
const nextConfig: NextConfig = process.env.NEXT_PUBLIC_DEMO === "1" ? { output: "export" } : {};

export default nextConfig;
