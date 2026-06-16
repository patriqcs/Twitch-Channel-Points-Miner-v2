/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: { DEFAULT: "#9147ff", dark: "#772ce8" }, // Twitch purple
      },
    },
  },
  plugins: [],
};
