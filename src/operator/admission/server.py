"""Dedicated HTTPS entrypoint: python3 -m admission.server --help.

No access logger, bearer token, kubeconfig, ApiClient or write endpoint. Each
request is bounded to 1 MiB, 32 levels, 20,000 values, 64 KiB text and signed
64-bit integers. Floats/nonfinite numbers are rejected; resource quantities are
unchanged strings. Parsing/classification has bounded work; body reads time out.
"""

import argparse
import asyncio
import logging
import ssl

from aiohttp import web

from .models import MAX_BODY_BYTES, ValidationError, decode_document, load_enrollments
from .quarantine import review

REQUEST_TIMEOUT_SECONDS = 2
_ENROLLMENTS = web.AppKey("enrollments", tuple)


class _ContentFreeLog(logging.Filter):
    def filter(self, record):
        record.msg = "admission_transport_error"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _transport_logger():
    logger = logging.getLogger("admission.transport")
    if not logger.filters:
        logger.addFilter(_ContentFreeLog())
    return logger


async def _health(_request):
    return web.json_response({"status": "ok"})


async def _review(request):
    if request.headers.get("Content-Encoding", "identity") != "identity":
        return web.json_response({"reason": "unsupported_encoding"}, status=415)
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            raw = await request.read()
            result = review(decode_document(raw), request.app[_ENROLLMENTS])
        return web.json_response(result)
    except TimeoutError:
        return web.json_response({"reason": "request_timeout"}, status=408)
    except web.HTTPRequestEntityTooLarge:
        return web.json_response({"reason": "document_limit"}, status=413)
    except ValidationError:
        return web.json_response({"reason": "invalid_review"}, status=400)
    except Exception:  # noqa: BLE001 - this HTTP boundary must not leak exception payloads.
        # Deliberately omit exception text, request bodies and identities.
        logging.getLogger(__name__).error("admission_internal_error")
        return web.json_response({"reason": "internal_error"}, status=500)


def create_app(enrollments):
    app = web.Application(
        client_max_size=MAX_BODY_BYTES,
        logger=_transport_logger(),
        handler_args={
            "auto_decompress": False,
            "access_log": None,
            "keepalive_timeout": 5,
            "max_line_size": 8190,
            "max_field_size": 8190,
            "max_headers": 64,
        },
    )
    app[_ENROLLMENTS] = tuple(enrollments)
    app.router.add_get("/healthz", _health)
    app.router.add_get("/readyz", _health)
    app.router.add_post("/review", _review)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stateless native quota quarantine HTTPS admission"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args(argv)
    try:
        enrollments = load_enrollments(args.config)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
    except (ValidationError, OSError, ssl.SSLError):
        parser.error("configuration_or_tls_invalid")
    web.run_app(
        create_app(enrollments),
        host=args.host,
        port=args.port,
        ssl_context=context,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
