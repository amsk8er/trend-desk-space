import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#faf7e8",
        ink: "#1f2937",
        primary: "#fcd34d",
        ok: "#86efac",
        info: "#93c5fd",
        idle: "#e5e7eb",
        err: "#fca5a5",
        warn: "#fed7aa",
      },
      boxShadow: {
        chunky: "3px 3px 0 #1f2937",
        chunkysm: "2px 2px 0 #1f2937",
      },
    },
  },
  plugins: [],
} satisfies Config;
