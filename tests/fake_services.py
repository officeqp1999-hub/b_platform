"""Поддельные внешние сервисы для автотестов: 1С (OData), Битрикс24, Google, Telegram, MAX, Claude, WB, Ozon, Яндекс Маркет.

Запуск: python tests/fake_services.py [порт_http] [порт_https]
По умолчанию: http на 9210, https (самоподписанный) на 9211.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import ipaddress
import json
import sys
import threading
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

ONEC_USER = "Админ"
ONEC_PASS = "пароль123"
TG_GOOD = "123456789:AAE_test_token_abcdefghijklmnopqrstuvwxyz"
MAX_GOOD = "max-good-token-1234567890"
CLAUDE_GOOD = "sk-ant-good-key-1234567890"
WB_GOOD = "wb-good-token-abcdefghijklmnop"
OZON_ID, OZON_KEY = "123456", "ozon-good-key-abcdefghijklmnop"
YM_GOOD = "ym-good-key-abcdefghijklmnop"
BITRIX_CODE = "abc123def456"


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def make_jwt(days_left: int) -> str:
    exp = int(time.time() + days_left * 86400)
    return f"{_b64({'alg': 'HS256'})}.{_b64({'exp': exp, 's': 30})}.signaturepartsignaturepart"


WB_EXPIRING = make_jwt(5)
WB_HEALTHY = make_jwt(100)


def _auth_ok(request: Request) -> bool:
    h = request.headers.get("authorization", "")
    if not h.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(h[6:]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return False
    return raw == f"{ONEC_USER}:{ONEC_PASS}"


async def onec(request: Request):
    mode = request.path_params["mode"]
    if mode == "nopublish":
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    if mode == "slow":
        await asyncio.sleep(3)
    if mode == "err500":
        return PlainTextResponse("Ошибка сервера 1С", status_code=500)
    if mode == "gateway":
        return PlainTextResponse("Bad gateway", status_code=502)
    if mode == "html":
        return HTMLResponse("<html><body>Вход в 1С</body></html>")
    if not _auth_ok(request):
        return PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": 'Basic realm="1C"'})
    if mode == "noaccess":
        return PlainTextResponse("Forbidden", status_code=403)
    if mode == "empty":
        return JSONResponse({"odata.metadata": "x", "value": []})
    return JSONResponse({"odata.metadata": "x", "value": [{"name": f"Catalog_{i}", "url": f"Catalog_{i}"} for i in range(37)]})


async def onec_hs(request: Request):
    what = request.path_params["what"]
    if what == "ok":
        return PlainTextResponse("pong")
    return PlainTextResponse("not found", status_code=404)


ONEC_SUMMARY = {"revenue_month": 4520000.5, "cash_total": 812340.0, "stock_value": 9876543.21}
ONEC_SUMMARY_CALLS = {"n": 0}  # только для автотеста кэша: считает реальные обращения к «/summary»


async def onec_hs_summary(request: Request):
    """Эмулирует метод «/summary» будущего расширения 1С (см. ТЗ-1С-HTTP-сервисы.md)."""
    ONEC_SUMMARY_CALLS["n"] += 1
    what = request.path_params["what"]
    if what == "slow":
        await asyncio.sleep(1.5)
        what = "ok"
    if what == "ok":
        return JSONResponse(ONEC_SUMMARY)
    if what == "empty":
        return JSONResponse({"revenue_month": None, "cash_total": None, "stock_value": None})
    if what == "noaccess":
        return PlainTextResponse("Forbidden", status_code=403)
    if what == "err500":
        return PlainTextResponse("Internal error", status_code=500)
    if what == "badjson":
        return PlainTextResponse("{не json", status_code=200, media_type="application/json")
    if what == "notdict":
        return JSONResponse([1, 2, 3])
    return PlainTextResponse("not found", status_code=404)


async def bitrix(request: Request):
    if request.path_params["code"] == BITRIX_CODE:
        return JSONResponse({"result": {"ID": "1", "NAME": "Иван", "LAST_NAME": "Петров", "ADMIN": True}})
    return JSONResponse({"error": "INVALID_CREDENTIALS", "error_description": "Invalid request credentials"}, status_code=401)


# --- Google ---------------------------------------------------------------

REFRESH = {"good-code": "1//refresh-token-first-abcdefghij", "good-code-2": "1//refresh-token-second-klmnopqrs",
           "dup-code": "1//refresh-token-dupdup-abcdefghi"}
EMAIL = {"good-code": "boss@example.com", "good-code-2": "second@example.com", "dup-code": "boss@example.com"}
REFRESH_TO_CODE = {v: k for k, v in REFRESH.items()}


async def g_token(request: Request):
    form = await request.form()
    if form.get("grant_type") == "authorization_code":
        code = str(form.get("code"))
        if code == "nocode-refresh":
            return JSONResponse({"access_token": "at-good-code", "expires_in": 3600, "scope": "email"})
        if code in REFRESH:
            return JSONResponse({"access_token": f"at-{code}", "refresh_token": REFRESH[code], "expires_in": 3600,
                                 "scope": "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/drive "
                                          "https://www.googleapis.com/auth/userinfo.email"})
        return JSONResponse({"error": "invalid_grant", "error_description": "Bad Request"}, status_code=400)
    if form.get("grant_type") == "refresh_token":
        rt = str(form.get("refresh_token"))
        if form.get("client_secret") != "gsecret-abcdef123456":
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if rt in REFRESH_TO_CODE:
            return JSONResponse({"access_token": f"at-{REFRESH_TO_CODE[rt]}", "expires_in": 3600})
        return JSONResponse({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, status_code=400)
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


async def g_userinfo(request: Request):
    tok = request.headers.get("authorization", "").replace("Bearer ", "")
    code = tok.replace("at-", "")
    if code in EMAIL:
        return JSONResponse({"email": EMAIL[code]})
    return JSONResponse({"error": {"code": 401}}, status_code=401)


async def g_gmail(request: Request):
    tok = request.headers.get("authorization", "").replace("Bearer ", "").replace("at-", "")
    if tok not in EMAIL:
        return JSONResponse({"error": {"code": 401, "message": "Invalid Credentials"}}, status_code=401)
    return JSONResponse({"emailAddress": EMAIL[tok], "messagesTotal": 1234})


async def g_drive(request: Request):
    tok = request.headers.get("authorization", "").replace("Bearer ", "").replace("at-", "")
    if tok not in EMAIL:
        return JSONResponse({"error": {"code": 401}}, status_code=401)
    return JSONResponse({"storageQuota": {"usage": str(5 * 1024 ** 3)}})


async def g_auth(request: Request):
    """Поддельная страница входа Google: даёт выбрать, чем закончится вход (для ручного тестирования)."""
    import html as _h
    from urllib.parse import urlencode
    q = request.query_params
    redirect, state = q.get("redirect_uri", ""), q.get("state", "")
    if not redirect:
        return HTMLResponse("Нет redirect_uri", status_code=400)

    def link(label: str, params: dict) -> str:
        sep = "&" if "?" in redirect else "?"
        href = redirect + sep + urlencode({**params, "state": state})
        return f'<p><a href="{_h.escape(href)}">{_h.escape(label)}</a></p>'
    body = ("<html><head><meta charset='utf-8'><title>Вход в Google (эмулятор)</title>"
            "<style>body{font:16px sans-serif;max-width:560px;margin:60px auto;padding:0 16px}a{display:block;padding:12px 16px;"
            "border:1px solid #ccc;border-radius:8px;text-decoration:none;color:#1a56db}</style></head><body>"
            "<h2>Вход в Google — эмулятор</h2><p>Это не настоящий Google. Выберите, как закончится вход:</p>"
            + link("Войти как boss@example.com", {"code": "good-code"})
            + link("Войти как second@example.com", {"code": "good-code-2"})
            + link("Снова boss@example.com (проверка «этот аккаунт уже подключён»)", {"code": "dup-code"})
            + link("Google не выдал постоянный токен доступа", {"code": "nocode-refresh"})
            + link("Нажать «Отмена» на экране Google", {"error": "access_denied"})
            + "</body></html>")
    return HTMLResponse(body)


# --- Боты и Claude ----------------------------------------------------------

async def tg_getme(request: Request):
    if request.path_params["token"] == TG_GOOD:
        return JSONResponse({"ok": True, "result": {"first_name": "Тестовый бот", "username": "test_platforma_bot"}})
    return JSONResponse({"ok": False, "error_code": 401, "description": "Unauthorized"}, status_code=401)


async def max_me(request: Request):
    if request.headers.get("authorization") == MAX_GOOD:
        return JSONResponse({"user_id": 1, "name": "Бот компании", "username": "company_bot", "is_bot": True})
    return JSONResponse({"code": "verify.token", "message": "Invalid access_token"}, status_code=401)


async def claude_models(request: Request):
    if request.headers.get("x-api-key") == CLAUDE_GOOD:
        return JSONResponse({"data": [{"id": "claude-sonnet-5"}, {"id": "claude-haiku-4-5"}]})
    return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, status_code=401)


CLAUDE_MESSAGES_CALLS = {"n": 0}  # только для автотеста лимита расходов: считает реальные обращения к /v1/messages
FINANCE_TOOL_ANSWER = "По данным 1С: выручка 777000, деньги на счетах 55000, остатки на складах 999000."
NO_ACCESS_ANSWER = "Эти данные вам показать не могу — недостаточно прав."


def _claude_message(model: str, content: list, stop_reason: str, stop_details=None) -> dict:
    return {"id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_details": stop_details, "usage": {"input_tokens": 123, "output_tokens": 45}}


async def claude_messages(request: Request):
    """Эмулирует /v1/messages: поведение выбирается по спецсловам в тексте вопроса (как режимы у 1С)."""
    if request.headers.get("x-api-key") != CLAUDE_GOOD:
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, status_code=401)
    CLAUDE_MESSAGES_CALLS["n"] += 1
    body = await request.json()
    model = body.get("model", "")
    messages = body.get("messages", [])
    tools = body.get("tools", [])
    has_finance_tool = any(t.get("name") == "finance_summary" for t in tools)

    last_content = messages[-1].get("content") if messages else None
    if isinstance(last_content, list) and any(b.get("type") == "tool_result" for b in last_content):
        # второй круг: нам вернули результат инструмента — отвечаем текстом
        return JSONResponse(_claude_message(model, [{"type": "text", "text": FINANCE_TOOL_ANSWER}], "end_turn"))

    question = ""
    for m in reversed(messages):
        if isinstance(m.get("content"), str):
            question = m["content"]
            break
    q = question.lower()

    if "фейк:429" in q:
        return JSONResponse({"type": "error", "error": {"type": "rate_limit_error", "message": "rate limited"}}, status_code=429)
    if "фейк:500" in q:
        return JSONResponse({"type": "error", "error": {"type": "api_error", "message": "internal server error"}}, status_code=500)
    if "фейк:refusal" in q:
        return JSONResponse(_claude_message(model, [], "refusal", stop_details={"type": "refusal", "category": "test"}))
    if "фейк:maxtokens" in q:
        return JSONResponse(_claude_message(model, [{"type": "text", "text": "Незаконченный отв"}], "max_tokens"))
    if "фейк:badjson" in q:
        return PlainTextResponse("{не json", status_code=200, media_type="application/json")
    if any(w in q for w in ("выручк", "деньги на счет", "остатк")):
        if has_finance_tool:
            return JSONResponse(_claude_message(
                model, [{"type": "tool_use", "id": "toolu_1", "name": "finance_summary", "input": {}}], "tool_use"))
        return JSONResponse(_claude_message(model, [{"type": "text", "text": NO_ACCESS_ANSWER}], "end_turn"))
    return JSONResponse(_claude_message(model, [{"type": "text", "text": f"Обычный ответ на вопрос: {question}"}], "end_turn"))


# --- Маркетплейсы -----------------------------------------------------------

async def wb_seller(request: Request):
    tok = request.headers.get("authorization", "")
    if tok == WB_GOOD or tok.startswith("eyJ"):
        return JSONResponse({"name": "ИП Иванов", "sid": "abc", "tradeMark": "Ромашка"})
    return JSONResponse({"title": "unauthorized"}, status_code=401)


async def ozon_roles(request: Request):
    if request.headers.get("client-id") == OZON_ID and request.headers.get("api-key") == OZON_KEY:
        return JSONResponse({"roles": [{"name": "Admin read only"}]})
    return JSONResponse({"code": 16, "message": "Api-key is deprecated"}, status_code=401)


async def ym_campaigns(request: Request):
    if request.headers.get("api-key") == YM_GOOD:
        return JSONResponse({"campaigns": [{"id": 111, "domain": "shop.example"}]})
    return JSONResponse({"status": "ERROR", "errors": [{"code": "UNAUTHORIZED"}]}, status_code=401)


async def root(request: Request):
    if request.url.path == "/health":
        return JSONResponse({"fake": True})
    return JSONResponse({"fake": True}, status_code=404)


async def debug_onec_summary_calls(request: Request):
    """Только для автотеста кэша: сколько раз реально дошли до «/summary» (проверка, что кэш не дырявый)."""
    return JSONResponse(dict(ONEC_SUMMARY_CALLS))


async def debug_claude_messages_calls(request: Request):
    """Только для автотеста лимита расходов: сколько раз реально дошли до /v1/messages."""
    return JSONResponse(dict(CLAUDE_MESSAGES_CALLS))


def build_app() -> Starlette:
    return Starlette(routes=[
        Route("/", root),
        Route("/debug/onec-summary-calls", debug_onec_summary_calls),
        Route("/debug/claude-messages-calls", debug_claude_messages_calls),
        Route("/{prefix}/{mode}/odata/standard.odata/", onec),
        Route("/{prefix}/{mode}/odata/standard.odata", onec),
        Route("/{prefix}/{mode}/hs/{what}", onec_hs),
        Route("/{prefix}/{mode}/hs/{what}/summary", onec_hs_summary),
        Route("/rest/1/{code}/profile.json", bitrix),
        Route("/o/oauth2/auth", g_auth),
        Route("/token", g_token, methods=["POST"]),
        Route("/userinfo", g_userinfo),
        Route("/gmail/v1/users/me/profile", g_gmail),
        Route("/drive/v3/about", g_drive),
        Route("/bot{token}/getMe", tg_getme),
        Route("/me", max_me),
        Route("/v1/models", claude_models),
        Route("/v1/messages", claude_messages, methods=["POST"]),
        Route("/api/v1/seller-info", wb_seller),
        Route("/v1/roles", ozon_roles, methods=["POST"]),
        Route("/campaigns", ym_campaigns),
        Route("/ping", root),
        Route("/health", root),
    ])


def make_selfsigned(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    cert_p, key_p = directory / "fake-cert.pem", directory / "fake-key.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-1c")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p


def main() -> None:
    http_port = int(sys.argv[1]) if len(sys.argv) > 1 else 9210
    https_port = int(sys.argv[2]) if len(sys.argv) > 2 else 9211
    tmp = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/fake-certs")
    cert, key = make_selfsigned(tmp)
    app = build_app()
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=http_port, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=https_port, log_level="warning",
                                      ssl_certfile=str(cert), ssl_keyfile=str(key))),
    ]
    for s in servers[1:]:
        threading.Thread(target=s.run, daemon=True).start()
    print(f"fake services: http {http_port}, https {https_port}", flush=True)
    servers[0].run()


if __name__ == "__main__":
    main()
