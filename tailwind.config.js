/** @type {import('tailwindcss').Config} */

/*
 * Dynamic color classes used by UI macro components
 * (templates/components/ui/macros.html).
 *
 * Macros accept a `color` parameter and interpolate it into Tailwind classes
 * (e.g. `from-{{ color }}-500`). Tailwind's content scanner can't detect
 * these at build time, so we safelist all combinations here.
 */
const MACRO_COLORS = [
  'amber', 'blue', 'cyan', 'emerald', 'gray', 'green', 'indigo',
  'orange', 'pink', 'purple', 'rose', 'sky', 'slate', 'teal', 'violet',
  'primary', 'accent',
];

module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
  ],
  safelist: MACRO_COLORS.flatMap(c => [
    // Gradient from / to
    `from-${c}-50`, `from-${c}-100`, `from-${c}-400`, `from-${c}-500`,
    `from-${c}-500/10`,
    `to-${c}-100`, `to-${c}-100/50`, `to-${c}-200`, `to-${c}-500`,
    `to-${c}-500/10`, `to-${c}-600`, `to-${c}-800/10`,
    // Shadow
    `shadow-${c}-500/20`, `shadow-${c}-500/25`, `shadow-${c}-500/30`,
    // Text
    `text-${c}-600`, `text-${c}-700`, `text-${c}-900`,
    // Background
    `bg-${c}-100`, `bg-${c}-400`, `bg-${c}-500`, `bg-${c}-500/10`, `bg-${c}-600`,
    // Border & ring
    `border-${c}-200/60`, `border-${c}-300`, `border-${c}-500`, `border-${c}-600`,
    `ring-${c}-500`,
    // Focus
    `focus:border-${c}-500`, `focus:ring-${c}-500`, `focus:ring-${c}-500/20`,
    // Hover
    `hover:bg-${c}-50`, `hover:bg-${c}-100`,
    `hover:text-${c}-600`, `hover:text-${c}-700`,
    `hover:border-${c}-300`, `hover:shadow-${c}-500/30`,
    `hover:from-${c}-50/50`, `hover:to-${c}-50/30`,
    // Dark
    `dark:text-${c}-100`, `dark:text-${c}-300`, `dark:text-${c}-400`,
    `dark:bg-${c}-900/30`,
    `dark:from-${c}-900/20`, `dark:from-${c}-900/40`,
    `dark:to-${c}-900/30`, `dark:to-${c}-900/5`,
    `dark:to-${c}-800/10`, `dark:to-${c}-800/30`,
    `dark:border-${c}-700/40`, `dark:focus:border-${c}-500`,
    // Dark + hover
    `dark:hover:text-${c}-300`, `dark:hover:text-${c}-400`,
    `dark:hover:bg-${c}-900/20`, `dark:hover:bg-${c}-900/30`,
    `dark:hover:border-${c}-500`, `dark:hover:border-${c}-600`,
    `dark:hover:from-${c}-900/10`, `dark:hover:to-${c}-900/5`,
  ]),
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Plus Jakarta Sans', 'system-ui', 'sans-serif'],
        display: ['Outfit', 'system-ui', 'sans-serif'],
      },
      colors: {
        // Distinctive teal-cyan palette with warmth
        primary: {
          50: '#ecfeff',
          100: '#cffafe',
          200: '#a5f3fc',
          300: '#67e8f9',
          400: '#22d3ee',
          500: '#06b6d4',
          600: '#0891b2',
          700: '#0e7490',
          800: '#155e75',
          900: '#164e63',
          950: '#083344',
        },
        // Warm accent for contrast
        accent: {
          50: '#fff7ed',
          100: '#ffedd5',
          200: '#fed7aa',
          300: '#fdba74',
          400: '#fb923c',
          500: '#f97316',
          600: '#ea580c',
          700: '#c2410c',
          800: '#9a3412',
          900: '#7c2d12',
          950: '#431407',
        }
      },
      animation: {
        'fade-in': 'fadeIn 0.2s ease-in-out',
        'stagger-in': 'staggerFadeIn 0.5s ease-out forwards',
        'counter-pop': 'counterPop 0.4s cubic-bezier(0.4, 0, 0.2, 1)',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0', transform: 'translateY(-4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        staggerFadeIn: {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        counterPop: {
          '0%': { transform: 'scale(0.8)', opacity: '0' },
          '50%': { transform: 'scale(1.05)' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
      },
    }
  },
  plugins: [],
}
