"""Веб-панель: маршруты Starlette. Каждый маршрут проверяет права на сервере (guarded)."""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import secrets
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import auth, backup, config, connectors, crypto, diagnostics, forms, settings_schema, store
from .connectors import google as google_conn
from .connectors.base import Ctx as CheckCtx
from .connectors.base import explain_exception, http

TEMPLATES_DIR = config.BASE_DIR / "app" / "templates"
STATIC_DIR = config.BASE_DIR / "app" / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

STATUS_RU = {"ok": "Работает", "warn": "Внимание", "error": "Ошибка", "off": "Отключено", "": "Не проверялось"}
STATUS_ICON = {"ok": "●", "warn": "▲", "error": "✕", "off": "○", "": "○"}

NAV = [
    ("overview", "Обзор", "/", "overview.view"),
    ("connections", "Подключения", "/connections", "connections.view"),
    ("users", "Сотрудники", "/users", "users.view"),
    ("testing", "Тестирование", "/testing", "testing.run"),
    ("logs", "Журнал", "/logs", "logs.view"),
    ("settings", "Настройки", "/settings", "settings.view"),
]


def _dt(value: str, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Время из базы (UTC) → местное время сервера."""
    if not value:
        return "—"
    try:
        d = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
        return d.strftime(fmt)
    except ValueError:
        return value


templates.env.filters["dt"] = _dt
templates.env.filters["status_ru"] = lambda s: STATUS_RU.get(s or "", s)
templates.env.filters["status_icon"] = lambda s: STATUS_ICON.get(s or "", "○")
templates.env.filters["role_ru"] = lambda r: store.ROLES.get(r, r)
templates.env.filters["level_ru"] = lambda r: store.LEVELS.get(r, r)
templates.env.filters["section_ru"] = lambda r: store.SECTIONS.get(r, r)
templates.env.globals.update(config=config, ROLES=store.ROLES, LEVELS=store.LEVELS, SECTIONS=store.SECTIONS)


# ---------------------------------------------------------------------------
# Контекст запроса и защита маршрутов
# ---------------------------------------------------------------------------

@dataclass
class Req:
    request: Request
    user: dict
    session: dict
    form: Any = None

    @property
    def ip(self) -> str:
        return self.request.client.host if self.request.client else "?"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def render(r: Req | None, template: str, request: Request | None = None, status: int = 200, **data) -> HTMLResponse:
    request = request or (r.request if r else None)
    assert request is not None
    ctx: dict[str, Any] = {"status_code": status}
    if r is not None:
        user = r.user
        ctx.update(
            user=user,
            csrf=r.session["csrf"],
            flashes=auth.pop_flash(r.session["token_hash"]),
            nav=[(k, t, h) for k, t, h, p in NAV if auth.can(user, p)],
            can=lambda perm, u=user: auth.can(u, perm),
            default_admin_pending=auth.default_admin_pending() if auth.can(user, "settings.view") else False,
        )
    else:
        ctx.update(user=None, csrf="", flashes=[], nav=[], can=lambda perm: False, default_admin_pending=False)
    try:
        ctx["company_name"] = store.get_settings().get("company_name") or "Моя компания"
    except Exception:  # noqa: BLE001
        ctx["company_name"] = "Моя компания"
    ctx.update(data)
    return templates.TemplateResponse(request, template, ctx, status_code=status)


def flash(r: Req, kind: str, text: str) -> None:
    auth.add_flash(r.session["token_hash"], kind, text)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def guarded(perm: str | None = None, *, pw_ok: bool = False, nav: str = ""):
    """Обёртка маршрута: вход, обязательная смена пароля, право доступа, CSRF для POST."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(request: Request):
            hours = store.setting_int("session_hours", 12)
            token = request.cookies.get(auth.COOKIE_NAME)
            sess = auth.get_session(token, hours)
            is_api = request.url.path.startswith("/api/")
            if not sess:
                if is_api:
                    return JSONResponse({"error": "Нужно войти в панель."}, status_code=401)
                nxt = request.url.path + (("?" + request.url.query) if request.url.query and request.method == "GET" else "")
                target = "/login" + (("?next=" + quote(nxt)) if request.method == "GET" and nxt != "/" else "")
                return redirect(target)
            user = sess["user"]
            if user["must_change_password"] and not pw_ok:
                if is_api:
                    return JSONResponse({"error": "Сначала смените пароль."}, status_code=403)
                return redirect("/password")
            if perm and not auth.can(user, perm):
                return deny(request, user, perm, sess)
            form = None
            if request.method == "POST":
                form = await request.form()
                sent = str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
                if not sent or not hmac.compare_digest(sent, sess["csrf"]):
                    store.log_event("warning", "auth", f"Отклонён запрос без действующего токена защиты ({request.url.path}).", user=user["login"])
                    r = Req(request, user, sess, form)
                    return render(r, "error.html", status=400, title="Страница устарела",
                                  message="Форма была открыта слишком давно или в другом окне.",
                                  action="Вернитесь назад, обновите страницу (F5) и повторите действие.")
            r = Req(request, user, sess, form)
            return await fn(r)

        return wrapper

    return deco


def deny(request: Request, user: dict, perm: str, sess: dict) -> Response:
    store.log_event("warning", "auth",
                    f"Отказано в доступе: {user['login']} ({store.ROLES[user['role']]}) открыл {request.method} {request.url.path} "
                    f"— нужно право «{auth.PERMISSION_TITLES.get(perm, perm)}».", user=user["login"])
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Недостаточно прав для этого действия."}, status_code=403)
    r = Req(request, user, sess)
    return render(r, "error.html", status=403, title="Недостаточно прав",
                  message=f"Ваша роль — «{store.ROLES[user['role']]}». Этот раздел вам не доступен.",
                  action="Доступ есть у ролей: " + ", ".join(auth.roles_allowed(perm)) + ". Если он вам нужен, попросите администратора изменить вашу роль.")


# ---------------------------------------------------------------------------
# Служебные маршруты
# ---------------------------------------------------------------------------

async def health(request: Request) -> Response:
    return JSONResponse({"app": config.APP_ID, "status": "ok", "version": config.VERSION})


async def login(request: Request) -> Response:
    hours = store.setting_int("session_hours", 12)
    existing = auth.get_session(request.cookies.get(auth.COOKIE_NAME), hours)
    nxt = _safe_next(request.query_params.get("next"))
    if request.method == "GET":
        if existing:
            return redirect("/")
        return render(None, "login.html", request, next=nxt, error="", login_value="", first_run=auth.default_admin_pending())
    form = await request.form()
    login_value = str(form.get("login") or "")
    nxt = _safe_next(str(form.get("next") or ""))
    user, error = auth.authenticate(login_value, str(form.get("password") or ""), client_ip(request))
    if not user:
        await asyncio.sleep(0.4)
        return render(None, "login.html", request, status=401, next=nxt, error=error, login_value=login_value,
                      first_run=auth.default_admin_pending())
    token, _ = auth.create_session(user["id"], client_ip(request), hours)
    resp = redirect("/password" if user["must_change_password"] else nxt)
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=int(hours * 3600), httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")
    return resp


@guarded(None, pw_ok=True)
async def logout(r: Req) -> Response:
    auth.destroy_session(r.request.cookies.get(auth.COOKIE_NAME))
    store.log_event("info", "auth", f"Выход: {r.user['login']}.", user=r.user["login"])
    resp = redirect("/login")
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@guarded(None, pw_ok=True)
async def password_view(r: Req) -> Response:
    forced = r.user["must_change_password"]
    if r.request.method == "GET":
        return render(r, "password.html", forced=forced, error="")
    old = str(r.form.get("old") or "")
    new = str(r.form.get("new") or "")
    confirm = str(r.form.get("confirm") or "")
    if not auth.verify_password(old, r.user["password_hash"]):
        return render(r, "password.html", status=400, forced=forced, error="Текущий пароль введён неверно.")
    problem = auth.validate_new_password(new, confirm, login=r.user["login"], old_hash=r.user["password_hash"])
    if problem:
        return render(r, "password.html", status=400, forced=forced, error=problem)
    store.set_password(r.user["id"], auth.hash_password(new), must_change=False)
    auth.destroy_other_sessions(r.user["id"], r.session["token_hash"])
    store.log_event("info", "auth", f"Пользователь {r.user['login']} сменил пароль.", user=r.user["login"])
    flash(r, "ok", "Пароль изменён.")
    return redirect("/")


# ---------------------------------------------------------------------------
# Обзор
# ---------------------------------------------------------------------------

def _checklist() -> list[dict]:
    conns = store.list_connections()
    by_type = {c["type"]: c for c in conns}
    path, _, _ = backup.last_backup()
    items = [
        ("Сменить пароль администратора", "/password", not auth.default_admin_pending()),
        ("Подключить ядро Claude", "/connections/new/claude", "claude" in by_type),
        ("Подключить 1С", "/connections/new/onec", "onec" in by_type),
        ("Подключить бота MAX (или запасной Telegram)", "/connections/new/max", "max" in by_type or "telegram" in by_type),
        ("Добавить сотрудников, которые будут пользоваться ботом", "/users/new", len(store.list_users()) > 1),
        ("Сделать резервную копию", "/settings", path is not None),
        ("Запустить проверку «Тестирование»", "/testing", store.last_test_run() is not None),
    ]
    return [{"title": t, "href": h, "done": bool(d)} for t, h, d in items]


@guarded("overview.view")
async def overview(r: Req) -> Response:
    data: dict[str, Any] = {}
    if auth.can(r.user, "connections.view"):
        conns = store.list_connections()
        counts = {"ok": 0, "warn": 0, "error": 0, "off": 0, "": 0}
        for c in conns:
            counts["off" if not c["enabled"] else (c["last_status"] or "")] += 1
        data["conn_counts"] = counts
        data["conn_total"] = len(conns)
    if auth.can(r.user, "users.view"):
        users = store.list_users()
        data["users_total"] = len(users)
        data["users_bot"] = sum(1 for u in users if u["max_user_id"] or u["telegram_id"])
    if auth.can(r.user, "testing.run"):
        data["last_test"] = store.last_test_run()
    if auth.can(r.user, "logs.view"):
        data["events"] = store.recent_events(7)
        data["event_stats"] = store.event_stats()
    if auth.can(r.user, "settings.view"):
        data["checklist"] = _checklist()
    return render(r, "overview.html", nav_active="overview", **data)


@guarded("finance.view")
async def api_finance(r: Req) -> Response:
    # Заглушка для второго этапа: реальные цифры появятся после выгрузки из 1С.
    return JSONResponse({"status": "not_connected",
                         "message": "Финансовые показатели появятся после подключения 1С (этап 2).",
                         "revenue": None, "cash": None})


# ---------------------------------------------------------------------------
# Подключения
# ---------------------------------------------------------------------------

def _form_view(ctype: connectors.ConnectorType, existing_plain: dict, submitted: dict | None = None) -> dict:
    """Значения для отрисовки формы: секреты — только маска; введённые несекретные значения сохраняются при ошибке."""
    view = store.masked_view(ctype, existing_plain)
    if submitted:
        for f in ctype.fields:
            if not f.secret and f.name in submitted:
                view[f.name]["value"] = submitted[f.name]
    return view


def _defaults_for(ctype: connectors.ConnectorType) -> dict:
    d: dict[str, Any] = {}
    for f in ctype.fields:
        d[f.name] = (f.default not in ("", "0")) if f.type == "bool" else f.default
    return d


def redirect_uri_for(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return base + "/oauth/google/callback"


def _conn_page(r: Req, ctype, conn: Optional[dict], view: dict, *, name: str, errors: dict | None = None,
               form_error: str = "", status: int = 200) -> Response:
    readonly = not auth.can(r.user, "connections.manage")
    return render(r, "connection_form.html", nav_active="connections", ctype=ctype, conn=conn, view=view, name=name,
                  errors=errors or {}, form_error=form_error, readonly=readonly, status=status,
                  redirect_uri=redirect_uri_for(r.request), settings=store.get_settings())


@guarded("connections.view")
async def connections_list(r: Req) -> Response:
    conns = store.list_connections()
    groups = []
    for gname, types in connectors.grouped():
        keys = {t.key for t in types}
        items = []
        for c in conns:
            if c["type"] in keys:
                items.append({"conn": c, "ctype": connectors.get_type(c["type"])})
        groups.append({"name": gname, "items": items})
    return render(r, "connections.html", nav_active="connections", groups=groups, total=len(conns))


@guarded("connections.manage")
async def connections_new(r: Req) -> Response:
    existing = {c["type"] for c in store.list_connections()}
    return render(r, "connection_new.html", nav_active="connections", groups=connectors.grouped(), existing=existing)


@guarded("connections.manage")
async def connection_create(r: Req) -> Response:
    type_key = r.request.path_params["type"]
    ctype = connectors.get_type(type_key)
    if ctype is None:
        return render(r, "error.html", status=404, title="Такого типа нет", message="Тип подключения не найден.",
                      action="Вернитесь к списку и выберите тип из предложенных.")
    if r.request.method == "GET":
        if ctype.single:
            dup = [c for c in store.list_connections() if c["type"] == type_key]
            if dup:
                flash(r, "error", f"Подключение «{ctype.title}» уже есть — такое можно создать только одно. Открыто существующее.")
                return redirect(f"/connections/{dup[0]['id']}")
        return _conn_page(r, ctype, None, _form_view(ctype, _defaults_for(ctype)), name="")

    name = str(r.form.get("name") or "").strip()
    res = forms.process_form(ctype.fields, r.form, _defaults_for(ctype), validate=ctype.validate, normalize=ctype.normalize)
    errors = dict(res.errors)
    if not name:
        errors["name"] = "Впишите название подключения — по нему вы будете отличать его от других."
    if errors:
        return _conn_page(r, ctype, None, _form_view(ctype, {}, res.values), name=name, errors=errors,
                          form_error="Подключение не сохранено — исправьте отмеченные поля.", status=400)
    try:
        new_id = store.create_connection(type_key, name, res.values)
    except store.StoreError as exc:
        store.log_event("warning", "connections", f"Отклонена попытка создать подключение «{ctype.title}»: {exc}", user=r.user["login"])
        return _conn_page(r, ctype, None, _form_view(ctype, {}, res.values), name=name, form_error=str(exc), status=409)
    store.log_event("info", "connections", f"Создано подключение «{name}» ({ctype.title}).", user=r.user["login"])
    for n in res.notes:
        flash(r, "info", n)
    flash(r, "ok", f"Подключение «{name}» сохранено.")
    if ctype.oauth == "google":
        return redirect(f"/connections/{new_id}")
    if r.form.get("action") == "save_check":
        await _check_and_flash(r, new_id)
    return redirect(f"/connections/{new_id}")


async def _check_and_flash(r: Req, conn_id: int) -> None:
    conn = store.get_connection(conn_id)
    if not conn:
        return
    res = await diagnostics.run_connection_check(conn, diagnostics.make_ctx())
    kind = {"ok": "ok", "warn": "warn", "error": "error"}[res["status"]]
    text = f"«{conn['name']}»: {res['message']}"
    if res["status"] != "ok" and res["action"]:
        text += f" Что делать: {res['action']}"
    flash(r, kind, text)
    lvl = {"ok": "info", "warn": "warning", "error": "error"}[res["status"]]
    store.log_event(lvl, "connections", f"Проверка связи «{conn['name']}»: {res['message']}", user=r.user["login"])


@guarded("connections.view")
async def connection_detail(r: Req) -> Response:
    conn_id = r.request.path_params["id"]
    conn = store.get_connection(conn_id)
    if not conn:
        return render(r, "error.html", status=404, title="Подключение не найдено", message="Возможно, его уже удалили.",
                      action="Вернитесь к списку подключений.")
    ctype = connectors.get_type(conn["type"])
    plain, failed = store.plain_config(conn)
    if r.request.method == "GET":
        return _conn_page(r, ctype, conn, _form_view(ctype, plain), name=conn["name"],
                          form_error=("Часть секретов не удалось расшифровать — ключ шифрования не подходит. Введите их заново."
                                      if failed else ""))
    if not auth.can(r.user, "connections.manage"):
        return deny(r.request, r.user, "connections.manage", r.session)
    name = str(r.form.get("name") or "").strip()
    res = forms.process_form(ctype.fields, r.form, plain, validate=ctype.validate, normalize=ctype.normalize)
    errors = dict(res.errors)
    if not name:
        errors["name"] = "Впишите название подключения."
    if errors:
        return _conn_page(r, ctype, conn, _form_view(ctype, plain, res.values), name=name, errors=errors,
                          form_error="Изменения не сохранены — исправьте отмеченные поля. Ранее сохранённые секреты остались без изменений.",
                          status=400)
    try:
        store.update_connection(conn_id, name, res.values)
    except store.StoreError as exc:
        return _conn_page(r, ctype, conn, _form_view(ctype, plain, res.values), name=name, form_error=str(exc), status=409)
    store.log_event("info", "connections", f"Изменено подключение «{name}» ({ctype.title}).", user=r.user["login"])
    for n in res.notes:
        flash(r, "info", n)
    flash(r, "ok", "Сохранено.")
    if r.form.get("action") == "save_check":
        await _check_and_flash(r, conn_id)
    return redirect(f"/connections/{conn_id}")


@guarded("testing.run")
async def connection_check(r: Req) -> Response:
    conn_id = r.request.path_params["id"]
    if not store.get_connection(conn_id):
        return redirect("/connections")
    await _check_and_flash(r, conn_id)
    return redirect(f"/connections/{conn_id}")


@guarded("connections.manage")
async def connection_toggle(r: Req) -> Response:
    conn_id = r.request.path_params["id"]
    conn = store.get_connection(conn_id)
    if conn:
        store.set_connection_enabled(conn_id, not conn["enabled"])
        store.log_event("info", "connections", f"Подключение «{conn['name']}» {'отключено' if conn['enabled'] else 'включено'}.", user=r.user["login"])
        flash(r, "ok", "Подключение " + ("отключено." if conn["enabled"] else "включено."))
    return redirect(f"/connections/{conn_id}")


@guarded("connections.manage")
async def connection_delete(r: Req) -> Response:
    conn_id = r.request.path_params["id"]
    conn = store.get_connection(conn_id)
    if conn:
        store.delete_connection(conn_id)
        store.log_event("warning", "connections", f"Удалено подключение «{conn['name']}».", user=r.user["login"])
        flash(r, "ok", f"Подключение «{conn['name']}» удалено.")
    return redirect("/connections")


# --- Google OAuth ----------------------------------------------------------

@guarded("connections.manage")
async def google_start(r: Req) -> Response:
    conn_id = r.request.path_params["id"]
    conn = store.get_connection(conn_id)
    if not conn or conn["type"] != "google":
        return redirect("/connections")
    s = store.get_settings()
    if not s.get("google_client_id") or not s.get("google_client_secret"):
        flash(r, "error", "Сначала впишите Client ID и Client Secret Google в разделе «Настройки» — без них Google не пустит. "
                          "Где их взять — написано там же, под полями.")
        return redirect("/settings#google")
    state = secrets.token_urlsafe(24)
    redirect_uri = redirect_uri_for(r.request)
    store.save_oauth_state(state, conn_id, r.user["id"], redirect_uri)
    store.log_event("info", "connections", f"Начат вход через Google для «{conn['name']}».", user=r.user["login"])
    return redirect(google_conn.build_auth_url(s["google_client_id"], redirect_uri, state))


@guarded("connections.manage")
async def google_callback(r: Req) -> Response:
    params = r.request.query_params
    if params.get("error"):
        code = params.get("error")
        text = ("Вход через Google отменён: доступ не был разрешён." if code == "access_denied"
                else "Google вернул ошибку при входе.")
        store.log_event("warning", "connections", f"Вход через Google не завершён: {code}", user=r.user["login"])
        flash(r, "error", text + " Что делать: откройте подключение и нажмите «Подключить аккаунт Google» ещё раз, "
                                 "разрешите все запрошенные доступы. Если аккаунта нет в «Тестовых пользователях» в Google Cloud — добавьте его.")
        return redirect("/connections")
    state = params.get("state", "")
    st = store.pop_oauth_state(state) if state else None
    if not st or st["user_id"] != r.user["id"]:
        flash(r, "error", "Вход через Google не удалось завершить: страница устарела или открыта не из панели. "
                          "Что делать: откройте подключение и нажмите «Подключить аккаунт Google» заново.")
        return redirect("/connections")
    conn_id = st["connection_id"]
    conn = store.get_connection(conn_id)
    if not conn:
        flash(r, "error", "Подключение, для которого шёл вход, уже удалено.")
        return redirect("/connections")
    code = params.get("code", "")
    s = store.get_settings()
    try:
        resp = await http("POST", config.GOOGLE_TOKEN_URL, timeout=20, data={
            "code": code, "client_id": s.get("google_client_id", ""), "client_secret": s.get("google_client_secret", ""),
            "redirect_uri": st["redirect_uri"], "grant_type": "authorization_code"})
    except Exception as exc:  # noqa: BLE001
        msg, action = explain_exception(exc, "Google")
        store.log_event("error", "connections", f"Обмен кода Google: {msg}", user=r.user["login"])
        flash(r, "error", f"{msg} Что делать: {action}")
        return redirect(f"/connections/{conn_id}")
    if resp.status_code != 200:
        msg, action = google_conn.explain_token_error(resp)
        store.log_event("error", "connections", f"Обмен кода Google: {msg}", user=r.user["login"])
        flash(r, "error", f"{msg} Что делать: {action}")
        return redirect(f"/connections/{conn_id}")
    tok = resp.json()
    refresh = tok.get("refresh_token", "")
    access = tok.get("access_token", "")
    if not refresh:
        flash(r, "error", "Google не выдал постоянный доступ (refresh-токен). Что делать: зайдите на myaccount.google.com/permissions, "
                          "удалите доступ этого приложения, затем нажмите «Подключить аккаунт Google» ещё раз.")
        return redirect(f"/connections/{conn_id}")
    email = ""
    try:
        ui = await http("GET", config.GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access}"}, timeout=20)
        if ui.status_code == 200:
            email = ui.json().get("email", "")
    except Exception:  # noqa: BLE001
        pass
    if not email:
        flash(r, "error", "Доступ получен, но Google не сообщил адрес почты аккаунта. Что делать: повторите вход и разрешите пункт «Просмотр адреса электронной почты».")
        return redirect(f"/connections/{conn_id}")
    for other in store.list_connections():
        if other["type"] == "google" and other["id"] != conn_id:
            oplain, _ = store.plain_config(other)
            if (oplain.get("email") or "").lower() == email.lower():
                # один аккаунт — одно подключение; пустое, только что созданное подключение убираем
                cur_plain, _ = store.plain_config(conn)
                if not cur_plain.get("refresh_token"):
                    store.delete_connection(conn_id)
                store.log_event("warning", "connections", f"Аккаунт {email} уже подключён как «{other['name']}».", user=r.user["login"])
                flash(r, "error", f"Аккаунт {email} уже подключён — это подключение «{other['name']}». Один аккаунт можно подключить только один раз. "
                                  f"Что делать: откройте «{other['name']}» и при необходимости нажмите «Подключить заново».")
                return redirect(f"/connections/{other['id']}")
    plain, _ = store.plain_config(conn)
    plain["refresh_token"] = refresh
    plain["email"] = email
    store.update_connection(conn_id, conn["name"], plain)
    granted = tok.get("scope", "")
    missing = [n for n, sc in (("Gmail", "gmail.modify"), ("Google Диск", "auth/drive")) if sc not in granted]
    store.log_event("info", "connections", f"Аккаунт Google {email} подключён к «{conn['name']}».", user=r.user["login"])
    flash(r, "ok", f"Аккаунт {email} подключён.")
    if missing:
        flash(r, "warn", "Вы разрешили не все доступы (не хватает: " + ", ".join(missing) + "). Что делать: нажмите «Подключить заново» и отметьте все галочки на экране Google.")
    await _check_and_flash(r, conn_id)
    return redirect(f"/connections/{conn_id}")


# ---------------------------------------------------------------------------
# Сотрудники
# ---------------------------------------------------------------------------

@guarded("users.view")
async def users_list(r: Req) -> Response:
    return render(r, "users.html", nav_active="users", users=store.list_users(), role_desc=auth.ROLE_DESCRIPTIONS)


def _user_form_values(form) -> dict:
    return {
        "login": str(form.get("login") or "").strip(),
        "full_name": str(form.get("full_name") or "").strip(),
        "role": str(form.get("role") or "employee"),
        "is_active": bool(form.get("is_active")),
        "max_user_id": str(form.get("max_user_id") or "").strip(),
        "telegram_id": str(form.get("telegram_id") or "").strip(),
        "note": str(form.get("note") or "").strip(),
        "must_change": bool(form.get("must_change")),
    }


@guarded("users.manage")
async def user_create(r: Req) -> Response:
    blank = {"login": "", "full_name": "", "role": "employee", "is_active": True, "max_user_id": "", "telegram_id": "",
             "note": "", "must_change": True}
    if r.request.method == "GET":
        return render(r, "user_form.html", nav_active="users", target=None, values=blank, errors={}, role_desc=auth.ROLE_DESCRIPTIONS)
    v = _user_form_values(r.form)
    errors: dict[str, str] = {}
    if not v["login"]:
        errors["login"] = "Впишите логин: латинскими буквами и цифрами, без пробелов."
    elif " " in v["login"]:
        errors["login"] = "В логине не должно быть пробелов."
    if not v["full_name"]:
        errors["full_name"] = "Впишите имя и фамилию сотрудника."
    if v["role"] not in store.ROLES:
        errors["role"] = "Выберите роль из списка."
    pw = str(r.form.get("password") or "")
    problem = auth.validate_new_password(pw, pw, login=v["login"])
    if problem:
        errors["password"] = problem
    if not errors:
        try:
            uid = store.create_user(v["login"], v["full_name"], v["role"], auth.hash_password(pw), must_change=v["must_change"],
                                    max_user_id=v["max_user_id"], telegram_id=v["telegram_id"], note=v["note"], is_active=v["is_active"])
        except store.StoreError as exc:
            errors["login"] = str(exc)
    if errors:
        return render(r, "user_form.html", status=400, nav_active="users", target=None, values=v, errors=errors, role_desc=auth.ROLE_DESCRIPTIONS)
    store.log_event("info", "users", f"Добавлен сотрудник {v['login']} ({store.ROLES[v['role']]}).", user=r.user["login"])
    flash(r, "ok", f"Сотрудник «{v['full_name']}» добавлен.")
    return redirect("/users")


@guarded("users.view")
async def user_edit(r: Req) -> Response:
    uid = r.request.path_params["id"]
    target = store.get_user(uid)
    if not target:
        return render(r, "error.html", status=404, title="Сотрудник не найден", message="Возможно, его уже удалили.", action="Вернитесь к списку.")
    readonly = not auth.can(r.user, "users.manage")
    if r.request.method == "GET":
        values = {k: target[k] for k in ("login", "full_name", "role", "is_active", "max_user_id", "telegram_id", "note")}
        values["must_change"] = target["must_change_password"]
        return render(r, "user_form.html", nav_active="users", target=target, values=values, errors={}, readonly=readonly, role_desc=auth.ROLE_DESCRIPTIONS)
    if readonly:
        return deny(r.request, r.user, "users.manage", r.session)
    v = _user_form_values(r.form)
    errors: dict[str, str] = {}
    if not v["login"] or " " in v["login"]:
        errors["login"] = "Логин обязателен и не должен содержать пробелов."
    if not v["full_name"]:
        errors["full_name"] = "Впишите имя и фамилию сотрудника."
    pw = str(r.form.get("password") or "")
    new_hash = None
    if pw:
        problem = auth.validate_new_password(pw, pw, login=v["login"])
        if problem:
            errors["password"] = problem
        else:
            new_hash = auth.hash_password(pw)
    if target["id"] == r.user["id"] and (not v["is_active"] or v["role"] != target["role"]):
        errors["role"] = "Нельзя менять собственную роль или отключать самого себя. Попросите другого администратора."
    if not errors:
        try:
            store.update_user(uid, login=v["login"], full_name=v["full_name"], role=v["role"], is_active=v["is_active"],
                              max_user_id=v["max_user_id"], telegram_id=v["telegram_id"], note=v["note"],
                              password_hash=new_hash, must_change=(v["must_change"] if new_hash else None))
        except store.StoreError as exc:
            errors["role"] = str(exc)
    if errors:
        return render(r, "user_form.html", status=400, nav_active="users", target=target, values=v, errors=errors, readonly=False, role_desc=auth.ROLE_DESCRIPTIONS)
    store.log_event("info", "users", f"Изменён сотрудник {v['login']} ({store.ROLES[v['role']]})" + (", пароль сброшен" if new_hash else "") + ".",
                    user=r.user["login"])
    flash(r, "ok", "Сохранено.")
    return redirect("/users")


@guarded("users.manage")
async def user_delete(r: Req) -> Response:
    uid = r.request.path_params["id"]
    target = store.get_user(uid)
    if not target:
        return redirect("/users")
    if target["id"] == r.user["id"]:
        flash(r, "error", "Нельзя удалить самого себя. Попросите другого администратора.")
        return redirect("/users")
    try:
        store.delete_user(uid)
    except store.StoreError as exc:
        flash(r, "error", str(exc))
        return redirect("/users")
    store.log_event("warning", "users", f"Удалён сотрудник {target['login']}.", user=r.user["login"])
    flash(r, "ok", f"Сотрудник «{target['full_name'] or target['login']}» удалён.")
    return redirect("/users")


# ---------------------------------------------------------------------------
# Тестирование
# ---------------------------------------------------------------------------

@guarded("testing.run")
async def testing_page(r: Req) -> Response:
    return render(r, "testing.html", nav_active="testing", run=store.last_test_run(), STATUS_RU=STATUS_RU)


@guarded("testing.run")
async def testing_run(r: Req) -> Response:
    result = await diagnostics.run_all(own_https=r.request.url.scheme == "https")
    store.save_test_run(result, r.user["login"])
    sm = result["summary"]
    level = "error" if sm["error"] else ("warning" if sm["warn"] else "info")
    store.log_event(level, "testing", f"Тестирование: работает {sm['ok']}, внимание {sm['warn']}, ошибок {sm['error']} "
                                      f"({result['duration_ms'] / 1000:.1f} с).", user=r.user["login"])
    return redirect("/testing")


@guarded("report.download")
async def testing_report(r: Req) -> Response:
    result = store.last_test_run()
    text = diagnostics.build_report(result)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    ascii_name = f"otchet-platforma-{stamp}.txt"
    utf_name = quote(f"отчёт-платформа-{stamp}.txt")
    store.log_event("info", "testing", "Скачан отчёт для разработчика.", user=r.user["login"])
    body = ("﻿" + text).encode("utf-8")
    return Response(body, media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf_name}",
        "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Журнал
# ---------------------------------------------------------------------------

@guarded("logs.view")
async def logs_page(r: Req) -> Response:
    q = r.request.query_params
    level, section, text = q.get("level", ""), q.get("section", ""), (q.get("q", "") or "").strip()
    try:
        page = max(1, int(q.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 50
    events, total = store.query_events(level=level, section=section, q=text, page=page, per_page=per_page)
    pages = max(1, (total + per_page - 1) // per_page)
    base_q = urlencode({k: v for k, v in (("level", level), ("section", section), ("q", text)) if v})
    return render(r, "logs.html", nav_active="logs", events=events, total=total, page=page, pages=pages,
                  level=level, section=section, q=text, base_q=base_q, retention=store.setting_int("log_retention_days", 90))


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def _settings_page(r: Req, existing: dict, submitted: dict | None = None, errors: dict | None = None, status: int = 200,
                   form_error: str = "") -> Response:
    sections = []
    for title, desc, fields in settings_schema.SECTIONS:
        view: dict[str, dict] = {}
        for f in fields:
            v = existing.get(f.name, "")
            if f.secret:
                view[f.name] = {"value": "", "mask": crypto.mask(str(v)) if v else "", "has": bool(v)}
            else:
                shown = (submitted or {}).get(f.name, v)
                view[f.name] = {"value": shown, "mask": "", "has": bool(v)}
        sections.append({"title": title, "desc": desc, "fields": fields, "view": view})
    path, mtime, count = backup.last_backup()
    https_files = config.SSL_CERT.exists() and config.SSL_KEY.exists()
    return render(r, "settings.html", nav_active="settings", sections=sections, errors=errors or {}, status=status,
                  form_error=form_error, redirect_uri=redirect_uri_for(r.request), backup_dir=str(backup.backup_dir()),
                  backup_count=count, backup_last=datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M") if path else "",
                  https_files=https_files, scheme=r.request.url.scheme)


@guarded("settings.view")
async def settings_page(r: Req) -> Response:
    existing = store.get_settings()
    if r.request.method == "GET":
        return _settings_page(r, existing)
    if not auth.can(r.user, "settings.manage"):
        return deny(r.request, r.user, "settings.manage", r.session)
    res = forms.process_form(settings_schema.ALL_FIELDS, r.form, existing, validate=settings_schema.validate)
    if res.errors:
        return _settings_page(r, existing, res.values, res.errors, 400, "Настройки не сохранены — исправьте отмеченные поля.")
    values = dict(res.values)
    if values.get("public_url"):
        values["public_url"] = values["public_url"].rstrip("/")
    store.save_settings(values)
    store.log_event("info", "settings", "Изменены настройки панели.", user=r.user["login"])
    for n in res.notes:
        flash(r, "info", n)
    flash(r, "ok", "Настройки сохранены.")
    return redirect("/settings")


@guarded("settings.manage")
async def settings_backup(r: Req) -> Response:
    try:
        path = await asyncio.get_running_loop().run_in_executor(None, backup.make_backup)
    except Exception as exc:  # noqa: BLE001
        store.log_event("error", "backup", f"Не удалось сделать резервную копию: {exc}", user=r.user["login"])
        flash(r, "error", f"Резервная копия не сделана: {exc}")
        return redirect("/settings")
    store.log_event("info", "backup", f"Создана резервная копия {path.name}.", user=r.user["login"])
    flash(r, "ok", f"Резервная копия создана: {path}. Скопируйте её ещё и на другой диск или компьютер.")
    return redirect("/settings")


@guarded("settings.manage")
async def settings_cleanup(r: Req) -> Response:
    removed = store.cleanup_events()
    store.log_event("info", "settings", f"Очистка журнала: удалено записей — {removed}.", user=r.user["login"])
    flash(r, "ok", f"Старые записи журнала удалены: {removed}.")
    return redirect("/settings")


# ---------------------------------------------------------------------------
# Ошибки, промежуточный слой, запуск
# ---------------------------------------------------------------------------

async def not_found(request: Request, exc) -> Response:
    hours = store.setting_int("session_hours", 12)
    sess = auth.get_session(request.cookies.get(auth.COOKIE_NAME), hours)
    r = Req(request, sess["user"], sess) if sess else None
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Не найдено."}, status_code=404)
    return render(r, "error.html", request, status=404, title="Страница не найдена",
                  message="Такой страницы нет.", action="Вернитесь на главную и выберите раздел в меню слева.")


async def server_error(request: Request, exc: Exception) -> Response:
    store.log_event("error", "system", f"Внутренняя ошибка панели при обращении к {request.url.path}: {type(exc).__name__}: {exc}",
                    details=traceback.format_exc())
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Внутренняя ошибка панели."}, status_code=500)
    try:
        hours = store.setting_int("session_hours", 12)
        sess = auth.get_session(request.cookies.get(auth.COOKIE_NAME), hours)
        r = Req(request, sess["user"], sess) if sess else None
        return render(r, "error.html", request, status=500, title="Внутренняя ошибка панели",
                      message="Что-то пошло не так внутри самой панели. Ваши данные не пострадали.",
                      action="Повторите действие. Если ошибка остаётся — откройте «Тестирование», скачайте отчёт и передайте его разработчику.")
    except Exception:  # noqa: BLE001
        return PlainTextResponse("Внутренняя ошибка панели. Перезапустите панель (остановить.bat, затем запустить.bat) "
                                 "и передайте разработчику файл logs\\server.log.", status_code=500)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response


def _maintenance_once(state: dict) -> None:
    """Автоочистка журнала и автоматическая резервная копия (каждые ~сутки)."""
    now = time.time()
    if now - state.get("cleanup", 0) > 20 * 3600:
        removed = store.cleanup_events()
        state["cleanup"] = now
        if removed:
            store.log_event("info", "system", f"Автоочистка журнала: удалено записей — {removed}.")
    if store.get_settings().get("auto_backup"):
        _, mtime, _ = backup.last_backup()
        if now - mtime > 23 * 3600:
            try:
                path = backup.make_backup()
                store.log_event("info", "backup", f"Автоматическая резервная копия: {path.name}.")
            except Exception as exc:  # noqa: BLE001
                store.log_event("error", "backup", f"Автоматическая резервная копия не удалась: {exc}")


async def _background_maintenance() -> None:
    state: dict = {"cleanup": time.time()}
    await asyncio.sleep(90)   # даём панели спокойно запуститься
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, _maintenance_once, state)
            await asyncio.sleep(3 * 3600)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            store.log_event("error", "system", f"Плановое обслуживание не удалось: {exc}")
            await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: Starlette):
    config.ensure_dirs()
    created = crypto.ensure_key()
    store.init_store()
    if auth.ensure_default_admin():
        pass
    if created:
        store.log_event("info", "system", "Создан ключ шифрования data\\secret.key. Сделайте резервную копию — без него токены не прочитать.")
    removed = store.cleanup_events()
    store.log_event("info", "system", f"Панель запущена (версия {config.VERSION})." + (f" Автоочистка журнала: удалено {removed}." if removed else ""))
    task = asyncio.create_task(_background_maintenance())
    try:
        yield
    finally:
        task.cancel()
        store.log_event("info", "system", "Панель остановлена.")


routes = [
    Route("/health", health),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/password", password_view, methods=["GET", "POST"]),
    Route("/", overview),
    Route("/api/finance/summary", api_finance),
    Route("/connections", connections_list),
    Route("/connections/new", connections_new),
    Route("/connections/new/{type}", connection_create, methods=["GET", "POST"]),
    Route("/connections/{id:int}", connection_detail, methods=["GET", "POST"]),
    Route("/connections/{id:int}/check", connection_check, methods=["POST"]),
    Route("/connections/{id:int}/toggle", connection_toggle, methods=["POST"]),
    Route("/connections/{id:int}/delete", connection_delete, methods=["POST"]),
    Route("/connections/{id:int}/google/start", google_start, methods=["POST"]),
    Route("/oauth/google/callback", google_callback),
    Route("/users", users_list),
    Route("/users/new", user_create, methods=["GET", "POST"]),
    Route("/users/{id:int}", user_edit, methods=["GET", "POST"]),
    Route("/users/{id:int}/delete", user_delete, methods=["POST"]),
    Route("/testing", testing_page),
    Route("/testing/run", testing_run, methods=["POST"]),
    Route("/testing/report.txt", testing_report),
    Route("/logs", logs_page),
    Route("/settings", settings_page, methods=["GET", "POST"]),
    Route("/settings/backup", settings_backup, methods=["POST"]),
    Route("/settings/cleanup", settings_cleanup, methods=["POST"]),
    Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(SecurityHeaders)],
    exception_handlers={404: not_found, 500: server_error},
    lifespan=lifespan,
)
