"use client";

import React, { createContext, useContext, useMemo } from "react";

/** Display hints for canvas node trees (project editor vs pattern preview). */
const CanvasDisplayContext = createContext({ isPreview: false });

export function CanvasDisplayProvider({
  isPreview,
  children,
}: Readonly<{
  isPreview?: boolean;
  children: React.ReactNode;
}>) {
  const value = useMemo(() => ({ isPreview: !!isPreview }), [isPreview]);
  return (
    <CanvasDisplayContext.Provider value={value}>
      {children}
    </CanvasDisplayContext.Provider>
  );
}

export function useCanvasDisplay() {
  return useContext(CanvasDisplayContext);
}
