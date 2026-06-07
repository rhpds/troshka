"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function LibraryRedirect() {
  const router = useRouter();
  useEffect(() => { router.replace("/library/images"); }, [router]);
  return null;
}
