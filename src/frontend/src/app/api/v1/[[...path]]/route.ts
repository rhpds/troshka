import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8200";

async function proxyRequest(request: NextRequest) {
  const url = new URL(request.url);
  const backendUrl = `${BACKEND_URL}${url.pathname}${url.search}`;

  const headers = new Headers();
  for (const [key, value] of request.headers.entries()) {
    if (
      key === "x-forwarded-user" ||
      key === "x-forwarded-email" ||
      key === "content-type" ||
      key === "accept" ||
      key === "authorization"
    ) {
      headers.set(key, value);
    }
  }

  const body =
    request.method !== "GET" && request.method !== "HEAD"
      ? await request.arrayBuffer()
      : undefined;

  try {
    let resp = await fetch(backendUrl, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
      signal: AbortSignal.timeout(600_000),
    });

    if (resp.status === 307 || resp.status === 308) {
      const location = resp.headers.get("location");
      if (location) {
        const redirectUrl = location.startsWith("http")
          ? location
          : `${BACKEND_URL}${location}`;
        resp = await fetch(redirectUrl, {
          method: request.method,
          headers,
          body,
          redirect: "manual",
          signal: AbortSignal.timeout(600_000),
        });
      }
    }

    const responseHeaders = new Headers();
    for (const [key, value] of resp.headers.entries()) {
      if (key !== "transfer-encoding") {
        responseHeaders.set(key, value);
      }
    }

    return new NextResponse(resp.body, {
      status: resp.status,
      headers: responseHeaders,
    });
  } catch {
    return NextResponse.json({ error: "Backend unavailable" }, { status: 502 });
  }
}

export const maxDuration = 600;

export async function GET(request: NextRequest) {
  return proxyRequest(request);
}

export async function POST(request: NextRequest) {
  return proxyRequest(request);
}

export async function PATCH(request: NextRequest) {
  return proxyRequest(request);
}

export async function PUT(request: NextRequest) {
  return proxyRequest(request);
}

export async function DELETE(request: NextRequest) {
  return proxyRequest(request);
}
