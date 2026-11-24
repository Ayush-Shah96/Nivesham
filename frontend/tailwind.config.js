/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  safelist: [
    "bg-navy-900", "bg-navy-800", "text-navy-700", "text-navy-800", "text-navy-900",
    "hover:bg-navy-800", "hover:border-navy-700", "focus:border-navy-700",
    "focus:ring-navy-700/20",
  ],
  theme: {
    extend: {
      colors: {
        // Deep navy ink used for primary actions and headings.
        navy: { 50: "#f3f6fa", 700: "#1d3b5e", 800: "#14304f", 900: "#0b1f3a", 950: "#071428" },
        // Warm gold accent used for highlights and eyebrow labels.
        gold: { 400: "#e0b25a", 500: "#d0a04a", 700: "#8a6320" },
      },
      fontFamily: {
        display: ['"Fraunces"', "Georgia", "serif"],
        sans: ['"DM Sans"', "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
    },
  },
  plugins: [],
};
