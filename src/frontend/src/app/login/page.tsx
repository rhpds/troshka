"use client";

import React, { useEffect, useRef } from "react";

export default function LoginPage() {
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    window.location.href = "/projects";
  }, []);

  return null;
}
