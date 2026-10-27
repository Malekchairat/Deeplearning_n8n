import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        paper: "#F6F8FB",
        ink: "#172033",
        stone: "#667085",
        line: "#DCE3EC",
        clay: "#2563EB",
        sage: "#2563EB",
        ochre: "#475569",
      },
      fontFamily: {
        sans: ["var(--font-manrope)", "sans-serif"],
        editorial: ["var(--font-manrope)", "sans-serif"],
        mono: ["var(--font-mono)", "monospace"],
      },
      boxShadow: {
        tactile: "0 8px 24px rgba(67, 55, 42, 0.06)",
      },
    },
  },
  plugins: [],
};

export default config;
