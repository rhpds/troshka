"use client";

import React, { createContext, useContext } from "react";

/** Display hints for canvas node trees (project editor vs pattern preview). */
const CanvasDisplayContext = createContext({ isPreview: false });

export function CanvasDisplayProvider({
  isPreview,
  children,
}: {
  isPreview?: boolean;
  children: React.ReactNode;
}) {
  return (
    <CanvasDisplayContext.Provider value={{ isPreview: !!isPreview }}>
      {children}
    </CanvasDisplayContext.Provider>
  );
}

export function useCanvasDisplay() {
  return useContext(CanvasDisplayContext);
}
