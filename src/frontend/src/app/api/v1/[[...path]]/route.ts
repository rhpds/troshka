import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8200";

// Large file uploads (ISO/qcow2 images via the dev MinIO proxy) must be
// streamed straight through to the backend. Buffering a multi-GB body into
// memory first (as the generic path below does, to support redirect retry)
// causes big uploads to stall or fail. This endpoint never redirects, so
// it's safe to stream without a fallback retry.
const STREAMING_PATH = /^\/api\/v1\/library\/[^/]+\/upload-proxy$/;

function buildHeaders(request: NextRequest): Headers {
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
  return headers;
}

// Bound how long we wait on the backend, but also give up immediately if the
// browser disconnects (e.g. the user cancels an upload) instead of leaving
// the backend request running for up to 10 minutes for nothing.
function abortSignal(request: NextRequest): AbortSignal {
  return AbortSignal.any([request.signal, AbortSignal.timeout(600_000)]);
}

function toResponse(resp: Response): NextResponse {
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
}

async function proxyStreamingRequest(
  request: NextRequest,
  backendUrl: string,
  headers: Headers,
) {
  try {
    // `duplex: "half"` is required by undici/Node's fetch when the request
    // body is a ReadableStream, but TypeScript's DOM lib doesn't declare it
    // on RequestInit yet.
    const init: RequestInit & { duplex?: "half" } = {
      method: request.method,
      headers,
      body: request.body,
      duplex: "half",
      redirect: "manual",
      signal: abortSignal(request),
    };
    const resp = await fetch(backendUrl, init);
    return toResponse(resp);
  } catch (err) {
    console.error(`Proxy stream to ${backendUrl} failed:`, err);
    return NextResponse.json({ error: "Backend unavailable" }, { status: 502 });
  }
}

async function proxyRequest(request: NextRequest) {
  const url = new URL(request.url);
  const backendUrl = `${BACKEND_URL}${url.pathname}${url.search}`;
  const headers = buildHeaders(request);
  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  if (hasBody && STREAMING_PATH.test(url.pathname)) {
    return proxyStreamingRequest(request, backendUrl, headers);
  }

  const body = hasBody ? await request.arrayBuffer() : undefined;
  const signal = abortSignal(request);

  try {
    let resp = await fetch(backendUrl, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
      signal,
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
          signal,
        });
      }
    }

    return toResponse(resp);
  } catch (err) {
    console.error(`Proxy request to ${backendUrl} failed:`, err);
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
