"use client";

import { useEffect, useState } from "react";

/** True when PatternFly dark theme class is on <html>. */
export function useIsDarkTheme(): boolean {
  const [isDark, setIsDark] = useState(true);

  useEffect(() => {
    const root = document.documentElement;
    const sync = () => setIsDark(root.classList.contains("pf-v6-theme-dark"));
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  return isDark;
}
