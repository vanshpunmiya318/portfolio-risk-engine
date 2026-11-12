import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';

type Theme = 'light' | 'dark' | 'system';
const storageKey = 'riskengine-theme';
const ThemeContext = createContext<{ theme: Theme; setTheme: (theme: Theme) => void } | null>(null);

function readTheme(): Theme {
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* Storage may be unavailable in private or restricted browsing. */ }
  return 'system';
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(readTheme);

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => {
      document.documentElement.dataset.theme =
        theme === 'system' ? (media.matches ? 'dark' : 'light') : theme;
    };
    apply();
    media.addEventListener('change', apply);
    try { localStorage.setItem(storageKey, theme); } catch { /* Keep in-memory preference. */ }
    return () => media.removeEventListener('change', apply);
  }, [theme]);

  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>;
}

export function ThemeToggle() {
  const context = useContext(ThemeContext);
  if (!context) throw new Error('ThemeToggle requires ThemeProvider');
  return (
    <fieldset className="theme-toggle">
      <legend className="sr-only">Color theme</legend>
      {(['light', 'dark', 'system'] as const).map((theme) => (
        <label key={theme}>
          <input type="radio" name="theme" value={theme}
            checked={context.theme === theme} onChange={() => context.setTheme(theme)} />
          <span>{theme}</span>
        </label>
      ))}
    </fieldset>
  );
}
