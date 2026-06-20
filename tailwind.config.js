/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        voiceflow: {
          idle: "#6b7280",
          recording: "#ef4444",
          processing: "#f59e0b",
          error: "#dc2626",
          primary: "#3b82f6",
          bg: "#1e1e2e",
          surface: "#2a2a3e",
          text: "#e2e8f0",
        },
      },
      animation: {
        "pulse-recording": "pulse-recording 1.5s ease-in-out infinite",
      },
      keyframes: {
        "pulse-recording": {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.6", transform: "scale(1.1)" },
        },
      },
    },
  },
  plugins: [],
};
