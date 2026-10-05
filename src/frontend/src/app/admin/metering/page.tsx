"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/** Rates moved to Settings (admin section). */
export default function AdminMeteringRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings#metering-rates");
  }, [router]);
  return null;
}
