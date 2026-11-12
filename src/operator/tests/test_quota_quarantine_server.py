import asyncio
import gzip
import json
import logging
import ssl
import subprocess

import pytest
from admission import server as admission_server
from admission.models import ValidationError, decode_document, parse_enrollments
from admission.server import create_app, main
from aiohttp.test_utils import TestClient, TestServer

from tests.test_quota_quarantine import bundle, envelope


@pytest.fixture
def tls_context(tmp_path):
    cert, key = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


async def tls_server(tls_context):
    server = TestServer(create_app(parse_enrollments(bundle())))
    await server.start_server(ssl=tls_context, access_log=None)
    assert server.scheme == "https"
    return server


def test_bounded_decoder_rejects_duplicates_depth_and_work():
    for raw in [
        b'{"a":1,"a":2}',
        b"[" * 40 + b"0" + b"]" * 40,
        b'{"x":NaN}',
        b'{"x":1.0}',
        b'"' + b"x" * 65537 + b'"',
        b"[" + b"0," * 20000 + b"0]",
        b"9" * 5000,
    ]:
        with pytest.raises(ValidationError):
            decode_document(raw)


def test_server_review_health_and_safe_errors(caplog, tls_context):
    async def exercise():
        async with TestClient(await tls_server(tls_context)) as client:
            for path in ["/healthz", "/readyz"]:
                response = await client.get(path, ssl=False)
                assert response.status == 200
                assert await response.json() == {"status": "ok"}
            response = await client.post(
                "/review", json=envelope(operation="CREATE"), ssl=False
            )
            assert response.status == 200
            assert not (await response.json())["response"]["allowed"]
            document = envelope()
            document["request"]["object"]["spec"]["replicas"] = 0
            response = await client.post("/review", json=document, ssl=False)
            assert (await response.json())["response"]["allowed"]
            for body, status in [
                (b'{"token":"credential-secret","token":2}', 400),
                (b"x" * (1024 * 1024 + 1), 413),
                (b"{}", 400),
            ]:
                response = await client.post("/review", data=body, ssl=False)
                assert response.status == status
                assert "credential-secret" not in await response.text()
            assert (
                await client.post("/enroll", json=bundle(), ssl=False)
            ).status == 404

    asyncio.run(exercise())
    assert "credential-secret" not in caplog.text


def test_cli_requires_explicit_tls_and_configuration():
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2


def test_http_timeout_and_health_stays_available(monkeypatch, tls_context):
    monkeypatch.setattr(admission_server, "REQUEST_TIMEOUT_SECONDS", 0.05)

    async def exercise():
        async def incomplete_body():
            yield b'{"credential-secret":'
            await asyncio.Event().wait()

        async with TestClient(await tls_server(tls_context)) as client:
            response = await client.post("/review", data=incomplete_body(), ssl=False)
            assert response.status == 408
            assert await response.json() == {"reason": "request_timeout"}
            assert (await client.get("/readyz", ssl=False)).status == 200

    asyncio.run(exercise())


def test_http_compressed_requests_rejected(tls_context):
    async def exercise():
        async with TestClient(await tls_server(tls_context)) as client:
            response = await client.post(
                "/review",
                data=gzip.compress(json.dumps(envelope()).encode()),
                headers={"Content-Encoding": "gzip"},
                ssl=False,
            )
            assert response.status == 415
            assert await response.json() == {"reason": "unsupported_encoding"}

    asyncio.run(exercise())


def test_transport_logger_never_records_exception_or_message_payload(caplog):
    app = create_app(parse_enrollments(bundle()))
    try:
        raise ValueError("credential-secret")
    except ValueError:
        with caplog.at_level(logging.ERROR):
            app.logger.exception("request credential-secret %s", "credential-secret")
    assert "credential-secret" not in caplog.text
    assert "admission_transport_error" in caplog.text


def test_entrypoint_requires_tls_and_disables_access_logs(
    tmp_path, tls_context, monkeypatch
):
    config = tmp_path / "enrollment.json"
    config.write_text(json.dumps(bundle()))
    calls = []
    monkeypatch.setattr(
        admission_server.web,
        "run_app",
        lambda app, **kwargs: calls.append((app, kwargs)),
    )
    main(
        [
            "--config",
            str(config),
            "--tls-cert",
            str(tmp_path / "test-cert.pem"),
            "--tls-key",
            str(tmp_path / "test-key.pem"),
        ]
    )
    _, options = calls[0]
    assert options["port"] == 8443
    assert options["ssl_context"].minimum_version == ssl.TLSVersion.TLSv1_2
    assert options["access_log"] is None
    assert options["print"] is None


def test_http_internal_errors_are_safe(tls_context, caplog, monkeypatch):
    def fail(*_args):
        raise RuntimeError("credential-secret")

    monkeypatch.setattr(admission_server, "review", fail)

    async def exercise():
        async with TestClient(await tls_server(tls_context)) as client:
            response = await client.post("/review", json=envelope(), ssl=False)
            assert response.status == 500
            assert await response.json() == {"reason": "internal_error"}

    asyncio.run(exercise())
    assert "credential-secret" not in caplog.text


@pytest.mark.parametrize(
    "body", [b"[" * 40 + b"0" + b"]" * 40, b'{"x":1,"x":2}', b'{"x":NaN}']
)
def test_http_structural_errors_are_safe(tls_context, body):
    async def exercise():
        async with TestClient(await tls_server(tls_context)) as client:
            response = await client.post("/review", data=body, ssl=False)
            assert response.status == 400
            assert await response.json() == {"reason": "invalid_review"}

    asyncio.run(exercise())
