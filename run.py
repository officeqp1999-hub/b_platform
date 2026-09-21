"""Запуск и служебные команды платформы. Обычно вызывается из .bat-файлов, руками запускать не нужно.

  run.py serve      — запустить панель в этом окне (по умолчанию)
  run.py start      — запустить панель в фоне
  run.py stop       — остановить панель
  run.py status     — работает ли панель
  run.py init       — первая подготовка: .env, папки, ключ шифрования, база
  run.py check      — все ли нужные библиотеки установлены
  run.py backup     — резервная копия
  run.py info       — адреса, по которым открывается панель
  run.py port       — только номер порта
  run.py selfsigned — самоподписанный сертификат для https
  run.py resetpw    — сбросить пароль администратора (на «admin», с обязательной сменой при входе)
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Вывод всегда в UTF-8, даже когда его перенаправили в файл
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def say(text: str = "") -> None:
    print(text, flush=True)


def _require_python() -> None:
    if sys.version_info < (3, 11):
        say(f"ОШИБКА: нужен Python 3.11 или новее, а запущен {sys.version_info.major}.{sys.version_info.minor}.")
        say("ЧТО ДЕЛАТЬ: установите Python 3.11 с сайта python.org (поставьте галочку «Add python.exe to PATH»),")
        say("удалите папку venv и запустите установить.bat заново.")
        sys.exit(2)


def _cfg():
    from app import config
    return config


def local_addresses() -> list[str]:
    found: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found


def port_is_free(port: int, host: str = "0.0.0.0") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def scheme() -> str:
    c = _cfg()
    return "https" if c.SSL_CERT.exists() and c.SSL_KEY.exists() else "http"


def health_ok(timeout: float = 2.5) -> bool:
    """Отвечает ли наша панель. Пробуем и https, и http: сертификат могли добавить, пока панель ещё работает по-старому."""
    import httpx
    c = _cfg()
    first = scheme()
    for sch in (first, "http" if first == "https" else "https"):
        try:
            r = httpx.get(f"{sch}://127.0.0.1:{c.PORT}/health", timeout=timeout, verify=False, trust_env=False)
            if r.status_code == 200 and r.json().get("app") == c.APP_ID:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def read_pid() -> int | None:
    c = _cfg()
    try:
        return int(c.PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, timeout=20)
            text = out.stdout.decode("cp866", errors="ignore").lower()
            return f'"{pid}"' in text and "python" in text
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # зомби (уже завершившийся дочерний процесс) считаем неживым
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            if fh.read().split(")")[-1].split()[0] == "Z":
                return False
    except OSError:
        pass
    return True


def tail_log(n: int = 15) -> str:
    c = _cfg()
    lines: list[str] = []
    for name in ("server-console.log", "server.log"):
        p = c.LOG_DIR / name
        if p.exists():
            try:
                lines += p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
            except OSError:
                pass
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------

def cmd_check() -> int:
    missing = []
    for mods, pip_name in ((("starlette",), "starlette"), (("jinja2",), "jinja2"), (("uvicorn",), "uvicorn"),
                           (("httpx",), "httpx"), (("cryptography",), "cryptography"),
                           (("python_multipart", "multipart"), "python-multipart")):
        for mod in mods:
            try:
                __import__(mod)
                break
            except ImportError:
                continue
        else:
            missing.append(pip_name)
    if missing:
        say("ОШИБКА: не установлены нужные библиотеки: " + ", ".join(missing))
        say("ЧТО ДЕЛАТЬ: запустите установить.bat ещё раз. Если ошибка повторится — проверьте, что сервер выходит в интернет (pypi.org).")
        return 1
    say("Все нужные библиотеки на месте.")
    return 0


def cmd_init() -> int:
    c = _cfg()
    env = ROOT / ".env"
    if not env.exists():
        port = 8080
        while port < 8100 and not port_is_free(port):
            port += 1
        env.write_text(
            "# Файл создан автоматически программой установить.bat.\r\n"
            "# Править его не нужно: все настройки делаются в браузере, в разделе «Настройки».\r\n"
            "PANEL_HOST=0.0.0.0\r\n"
            f"PANEL_PORT={port}\r\n", encoding="utf-8")
        say(f"Создан файл .env, порт панели: {port}.")
        os.environ["PANEL_PORT"] = str(port)
        c.PORT = port
    else:
        say(f"Файл .env уже есть, порт панели: {c.PORT}.")
    c.ensure_dirs()
    from app import auth, crypto, store
    try:
        created = crypto.ensure_key()
    except crypto.KeyProblem as exc:
        say("ОШИБКА: " + str(exc))
        return 3
    store.init_store()
    auth.ensure_default_admin()
    say("Ключ шифрования создан: data\\secret.key." if created else "Ключ шифрования на месте.")
    say("База данных готова.")
    return 0


def cmd_port() -> int:
    say(str(_cfg().PORT))
    return 0


def cmd_info() -> int:
    c = _cfg()
    sch = scheme()
    say(f"Откройте панель в браузере: {sch}://localhost:{c.PORT}")
    for ip in local_addresses():
        say(f"С других компьютеров сети:  {sch}://{ip}:{c.PORT}")
    return 0


def cmd_status() -> int:
    c = _cfg()
    if health_ok():
        say(f"Панель работает: {scheme()}://localhost:{c.PORT}")
        return 0
    pid = read_pid()
    if pid and pid_alive(pid):
        say("Процесс панели запущен, но она пока не отвечает (возможно, ещё запускается). Подождите минуту и повторите.")
        return 1
    say("Панель не запущена. Запустите запустить.bat.")
    return 1


def cmd_serve() -> int:
    _require_python()
    c = _cfg()
    c.ensure_dirs()
    from app import crypto
    try:
        crypto.ensure_key()
    except crypto.KeyProblem as exc:
        say("ОШИБКА: " + str(exc))
        return 3
    try:
        import uvicorn
    except ImportError:
        say("ОШИБКА: не установлены нужные библиотеки. ЧТО ДЕЛАТЬ: запустите установить.bat.")
        return 1

    pid = read_pid()
    if pid and pid != os.getpid() and pid_alive(pid) and health_ok():
        say("Панель уже запущена. Ничего делать не нужно: " + f"{scheme()}://localhost:{c.PORT}")
        return 0
    if not port_is_free(c.PORT):
        say(f"ОШИБКА: порт {c.PORT} уже занят другой программой.")
        say("ЧТО ДЕЛАТЬ: если это старая копия панели — запустите остановить.bat и повторите. "
            "Иначе откройте файл .env в блокноте и замените число после PANEL_PORT= на другое (например 8090).")
        return 4

    import logging
    from logging.handlers import RotatingFileHandler
    from app import crypto as _crypto

    class RedactFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                record.msg = _crypto.redact(record.getMessage())
                record.args = ()
            except Exception:  # noqa: BLE001
                pass
            return True

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [RotatingFileHandler(c.LOG_DIR / "server.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")]
    if sys.stdout is not None and sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # в адресах запросов бывают токены

    c.PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    kwargs: dict = {}
    if c.SSL_CERT.exists() and c.SSL_KEY.exists():
        kwargs.update(ssl_certfile=str(c.SSL_CERT), ssl_keyfile=str(c.SSL_KEY))
    if c.BEHIND_PROXY:
        kwargs.update(proxy_headers=True, forwarded_allow_ips="*")
    say(f"Панель запускается: {scheme()}://localhost:{c.PORT}  (остановить — Ctrl+C или остановить.bat)")
    try:
        uvicorn.run("app.main:app", host=c.HOST, port=c.PORT, log_config=None, access_log=False,
                    timeout_graceful_shutdown=5, **kwargs)
    finally:
        try:
            if read_pid() == os.getpid():
                c.PID_PATH.unlink()
        except OSError:
            pass
    return 0


def cmd_start() -> int:
    _require_python()
    c = _cfg()
    c.ensure_dirs()
    if cmd_check() != 0:
        return 1
    if health_ok():
        say("Панель уже работает. " + f"{scheme()}://localhost:{c.PORT}")
        return 0
    from app import crypto
    try:
        crypto.ensure_key()
    except crypto.KeyProblem as exc:
        say("ОШИБКА: " + str(exc))
        return 3
    if not port_is_free(c.PORT):
        say(f"ОШИБКА: порт {c.PORT} занят другой программой. ЧТО ДЕЛАТЬ: запустите остановить.bat; если не помогло — "
            "в файле .env замените число после PANEL_PORT= на другое (например 8090).")
        return 4
    exe = sys.executable
    creation = 0
    if os.name == "nt":
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.exists():
            exe = str(pyw)
        creation = 0x08000000 | 0x00000008 | 0x00000200  # без окна, отдельный процесс
    console_log = open(c.LOG_DIR / "server-console.log", "w", encoding="utf-8")
    proc = subprocess.Popen([exe, str(ROOT / "run.py"), "serve"], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=console_log, stderr=console_log, creationflags=creation,
                            start_new_session=(os.name != "nt"), close_fds=True)
    say("Запускаю панель…")
    for _ in range(40):
        time.sleep(0.5)
        if health_ok(1.5):
            say("Панель запущена.")
            cmd_info()
            return 0
        if proc.poll() is not None:
            break
    say("ОШИБКА: панель не запустилась.")
    text = tail_log()
    if text:
        say("Последние строки журнала запуска:")
        say(text)
    say("ЧТО ДЕЛАТЬ: прочитайте сообщение выше. Если непонятно — отправьте файл logs\\server.log разработчику.")
    return 1


def cmd_stop() -> int:
    c = _cfg()
    pid = read_pid()
    if not pid or not pid_alive(pid):
        if health_ok():
            say("Панель отвечает, но её процесс не найден по записи в data\\server.pid. "
                "ЧТО ДЕЛАТЬ: перезагрузите сервер или завершите процесс python.exe в диспетчере задач.")
            return 1
        say("Панель и так не запущена.")
        try:
            c.PID_PATH.unlink()
        except OSError:
            pass
        return 0
    say("Останавливаю панель…")
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(30):
        time.sleep(0.4)
        if not pid_alive(pid):
            break
    else:
        if os.name != "nt":
            import signal
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.5)
    if pid_alive(pid) or health_ok(1.0):
        say("ОШИБКА: панель не удалось остановить. ЧТО ДЕЛАТЬ: завершите процесс python.exe в диспетчере задач или перезагрузите сервер.")
        return 1
    try:
        c.PID_PATH.unlink()
    except OSError:
        pass
    say("Панель остановлена.")
    return 0


def cmd_backup() -> int:
    c = _cfg()
    c.ensure_dirs()
    from app import backup, db, store
    if not c.DB_PATH.exists():
        say("ОШИБКА: базы данных ещё нет — копировать нечего. ЧТО ДЕЛАТЬ: сначала запустите установить.bat и запустить.bat.")
        return 1
    db.init_db()
    try:
        path = backup.make_backup()
    except Exception as exc:  # noqa: BLE001
        store.log_event("error", "backup", f"Резервная копия не создана: {exc}")
        say("ОШИБКА: " + str(exc))
        return 1
    store.log_event("info", "backup", f"Создана резервная копия {path.name} (из резервная-копия.bat).")
    size_kb = path.stat().st_size / 1024
    say(f"Готово. Резервная копия: {path}  ({size_kb:.0f} КБ)")
    say(f"Всего копий в папке: {len(backup.list_backups(path.parent))} (старые удаляются автоматически).")
    say("ВАЖНО: в копии лежит ключ шифрования и токены. Скопируйте её на другой диск или компьютер и не передавайте посторонним.")
    return 0


def cmd_selfsigned() -> int:
    c = _cfg()
    c.SSL_DIR.mkdir(parents=True, exist_ok=True)
    if c.SSL_CERT.exists() or c.SSL_KEY.exists():
        say("Файлы сертификата уже есть в папке data\\ssl. Если хотите создать заново — удалите cert.pem и key.pem и повторите.")
        return 1
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.DNSName(host)]
    for ip in ["127.0.0.1"] + local_addresses():
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    c.SSL_KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                            serialization.NoEncryption()))
    c.SSL_CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    say("Готово: создан самоподписанный сертификат на 825 дней (data\\ssl\\cert.pem и key.pem).")
    say("Перезапустите панель (остановить.bat → запустить.bat). Браузер при первом входе покажет предупреждение о сертификате —")
    say("это нормально для самоподписанного сертификата: нажмите «Дополнительно» → «Всё равно перейти».")
    say("Для входа через Google и доступа из интернета нужен настоящий сертификат на домен — см. ИНСТРУКЦИЮ.")
    return 0

def cmd_resetpw() -> int:
    """Аварийный сброс пароля. Работает только на самом сервере (нужен доступ к папке программы)."""
    c = _cfg()
    if not c.DB_PATH.exists():
        say("ОШИБКА: базы данных ещё нет. ЧТО ДЕЛАТЬ: сначала запустите запустить.bat — база создастся сама.")
        return 1
    from app import auth, db, store
    db.init_db()
    login = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    users = store.list_users()
    if login:
        target = store.get_user_by_login(login)
    else:
        admins = [u for u in users if u["role"] == "admin" and u["is_active"]]
        target = admins[0] if len(admins) == 1 else None
        if target is None:
            say("Администраторов несколько — укажите логин: run.py resetpw ЛОГИН")
            say("Логины: " + ", ".join(u["login"] for u in users))
            return 1
    if not target:
        say(f"ОШИБКА: пользователя «{login}» нет. Логины: " + ", ".join(u["login"] for u in users))
        return 1
    store.set_password(target["id"], auth.hash_password("admin"), must_change=True)
    with db.session() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
    store.log_event("warn", "users", f"Пароль пользователя «{target['login']}» сброшен с сервера (сброс-пароля.bat).")
    say(f"Готово. Пользователь «{target['login']}»: пароль сброшен на  admin")
    say("При входе панель сразу попросит придумать новый пароль. Подключения и настройки не затронуты.")
    return 0


COMMANDS = {
    "serve": cmd_serve, "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "init": cmd_init,
    "check": cmd_check, "backup": cmd_backup, "info": cmd_info, "port": cmd_port, "selfsigned": cmd_selfsigned, "resetpw": cmd_resetpw,
}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    func = COMMANDS.get(cmd)
    if func is None:
        say(f"Неизвестная команда «{cmd}». Доступно: " + ", ".join(COMMANDS))
        return 2
    try:
        return func()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        say(f"ОШИБКА: неожиданный сбой ({type(exc).__name__}: {exc}).")
        say("ЧТО ДЕЛАТЬ: отправьте файл logs\\server.log и этот текст разработчику.")
        try:
            import traceback
            c = _cfg()
            c.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(c.LOG_DIR / "server.log", "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
