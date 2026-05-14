import os
import sys
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR  = Path(__file__).parent
LOGS_DIR  = BASE_DIR / "logs"

AUTH_URL  = "https://auth.pipemais.com.br/auth/realms/PipeMais/protocol/openid-connect/token"
API_BASE  = "https://api.pipemais.com.br/api"
SULTS_API = "https://api.sults.com.br/api/v1"

STORE_NAME = "BSH"

# Statuses que indicam dia normal de descanso (não contam como "trabalhou no domingo")
SUNDAY_OFF_STATUSES = {
    "REST_DAY", "DAY_OFF", "DAY_OFF_ALLOWANCE", "VACATION",
    "HOLIDAY", "ABSENCE", "COMPENSATION", "DISABLED",
    "MATERNITY_LEAVE", "PATERNITY_LEAVE", "MARRY_LEAVE", "DEATH_LEAVE",
}

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"ponto_{date.today().isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

# ── Helpers ───────────────────────────────────────────────────────────────────

def seconds_to_hhmm(total_seconds: int) -> str:
    sign = "-" if total_seconds < 0 else "+"
    abs_sec = abs(int(total_seconds))
    h, rem = divmod(abs_sec, 3600)
    m = rem // 60
    return f"{sign}{h:02d}:{m:02d}"

def first_name(full_name: str) -> str:
    parts = full_name.strip().split()
    return parts[0].capitalize() if parts else full_name.capitalize()

def get_previous_week_range() -> tuple[str, str]:
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday.isoformat(), last_sunday.isoformat()

def fmt_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m")

# ── PipeMais ──────────────────────────────────────────────────────────────────

def get_pipe_token(username: str, password: str) -> str:
    r = requests.post(
        AUTH_URL,
        data={
            "client_id": "pipe-prod",
            "username": username,
            "password": password,
            "grant_type": "password",
        },
        timeout=60,
        verify=False,
    )
    r.raise_for_status()
    return r.json()["access_token"]

def fetch_mirror_view(token: str, start: str, end: str) -> list:
    r = requests.post(
        f"{API_BASE}/reports/generate/mirror/view",
        json={
            "end": end,
            "extensionType": "PDF",
            "fontSize": "NORMAL",
            "isDraft": False,
            "queryParam": {},
            "reportKind": "WORK_DAYS",
            "sendEmailToUsers": False,
            "showInactivates": False,
            "showSignFields": False,
            "splitByMonth": False,
            "start": start,
            "userId": "ALL",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=120,
        verify=False,
    )
    r.raise_for_status()
    return r.json()

# ── Análise ───────────────────────────────────────────────────────────────────

def filter_store(data: list) -> list:
    return [
        r for r in data
        if STORE_NAME.upper() in (r.get("user") or {}).get("teamName", "").upper()
    ]

def is_problematic_day(day: dict) -> bool:
    status  = day.get("status", "")
    entries = day.get("timeEntries") or []
    if status == "MISSING_TIME_ENTRIES":
        return True
    if len(entries) > 0 and len(entries) % 2 != 0:
        return True
    return False

def has_duplicate_punches(day: dict) -> bool:
    """Returns True if any two consecutive punches are less than 10 minutes apart."""
    entries = day.get("timeEntries") or []
    if len(entries) < 2:
        return False
    times = []
    for e in entries:
        raw = e.get("time") or e.get("dateTime") or e.get("dateTimeFormatted") or ""
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", ""))
            times.append(dt.hour * 3600 + dt.minute * 60 + dt.second)
        except (ValueError, TypeError):
            try:
                parts = str(raw).split(":")
                times.append(int(parts[0]) * 3600 + int(parts[1]) * 60 + (int(parts[2]) if len(parts) > 2 else 0))
            except (ValueError, IndexError):
                continue
    times.sort()
    return any(0 < times[i + 1] - times[i] < 600 for i in range(len(times) - 1))

def worked_on_sunday(day: dict) -> bool:
    return (
        day.get("dayWeek") == 7
        and day.get("status") not in SUNDAY_OFF_STATUSES
        and day.get("status") is not None
    )

def analyse(records: list, start: str, end: str) -> tuple[dict, dict, dict]:
    """
    Returns:
        issues   = {full_name: [(date_str, punch_count), ...]}
        balances = {full_name: balance_seconds}
        sundays  = {full_name: date_str}  — domingo trabalhado sem folga registrada
    """
    issues:   dict = {}
    balances: dict = {}
    sundays:  dict = {}

    for rec in records:
        u    = rec.get("user") or {}
        s    = rec.get("summary") or {}
        name = u.get("name", "")

        balances[name] = s.get("balanceAccumulated", 0)

        for day in rec.get("workDays") or []:
            day_date = day.get("date", "")
            if day_date < start or day_date > end:
                continue

            if is_problematic_day(day):
                entries = day.get("timeEntries") or []
                issues.setdefault(name, []).append((day_date, len(entries)))
            elif has_duplicate_punches(day):
                issues.setdefault(name, []).append((day_date, -1))

            if worked_on_sunday(day) and name not in sundays:
                sundays[name] = day_date

    return issues, balances, sundays

# ── Mensagem ──────────────────────────────────────────────────────────────────

def build_message(records: list, start: str, end: str) -> str:
    issues, balances, sundays = analyse(records, start, end)

    start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%d/%m")
    end_fmt   = datetime.strptime(end,   "%Y-%m-%d").strftime("%d/%m/%Y")

    lines = [
        "Boa tarde Cami!!",
        "",
        f"Segue prévia de horas ate a data de {end_fmt}, "
        "e ocorrências para verificar (caso houver):",
        "",
    ]

    # Seção 1 — funcionários com pontos faltantes
    if issues:
        for full_name in sorted(issues):
            fname = first_name(full_name)
            days  = sorted(issues[full_name])

            # Agrupa dias com mesmo nº de pontos: "dias 05 e 09 apenas 2 pontos"
            groups: dict = {}
            for day_date, count in days:
                groups.setdefault(count, []).append(datetime.strptime(day_date, "%Y-%m-%d").day)

            parts = []
            for count, day_nums in sorted(groups.items(), key=lambda x: x[1][0]):
                day_nums_str = [f"{d:02d}" for d in day_nums]
                if len(day_nums_str) == 1:
                    prefix = f"dia {day_nums_str[0]}"
                else:
                    prefix = f"dias {' e '.join(day_nums_str)}"
                if count == -1:
                    parts.append(f"{prefix} pontos duplicados")
                else:
                    parts.append(f"{prefix} apenas {count} ponto{'s' if count != 1 else ''}")

            suffix = ""
            if full_name in sundays:
                suffix = f" e pendente folga domingo {fmt_date(sundays[full_name])}"

            lines.append(f"{fname} {', '.join(parts)}{suffix}")
        lines.append("")

    # Seção 2 — saldo de funcionários SEM problemas de ponto e com |saldo| >= 50 min
    for full_name in sorted(balances):
        if full_name in issues:
            continue
        bal = balances[full_name]
        fname = first_name(full_name)

        abs_sec = abs(int(bal))
        h, rem  = divmod(abs_sec, 3600)
        m       = rem // 60
        hhmm    = f"{h:02d}:{m:02d}"

        suffix = ""
        if full_name in sundays:
            suffix = f" e pendente folga domingo {fmt_date(sundays[full_name])}"

        if bal < 0:
            lines.append(f"{fname} com saldo negativo de -{hhmm}{suffix}")
        else:
            lines.append(f"{fname} com saldo de {hhmm}{suffix}")

    return "\n".join(lines)

# ── Sults ─────────────────────────────────────────────────────────────────────

def send_sults_ticket(message: str, start: str, end: str) -> None:
    log = logging.getLogger(__name__)

    token      = os.getenv("SULTS_TOKEN")
    dept_id    = os.getenv("SULTS_DEPARTAMENTO_ID")
    assunto_id = os.getenv("SULTS_ASSUNTO_ID")
    resp_id    = os.getenv("SULTS_RESPONSAVEL_ID")
    solic_id   = os.getenv("SULTS_SOLICITANTE_ID")

    if not all([token, dept_id, assunto_id]):
        log.warning("Credenciais Sults incompletas — chamado não enviado.")
        return

    start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%d/%m")
    end_fmt   = datetime.strptime(end,   "%Y-%m-%d").strftime("%d/%m/%Y")
    prazo     = (date.today() + timedelta(days=7)).strftime("%Y-%m-%dT12:00:00Z")

    html_message = message.replace("\n", "<br>")

    payload = {
        "titulo":        f"Ocorrências de Ponto — {STORE_NAME} — {start_fmt} a {end_fmt}",
        "mensagem":      html_message,
        "tipo":          1,
        "departamentoId": int(dept_id),
        "assuntoId":      int(assunto_id),
        "dtPrazo":        prazo,
    }
    if resp_id:
        payload["responsavelId"] = int(resp_id)
    if solic_id:
        payload["solicitanteId"] = int(solic_id)

    try:
        r = requests.post(
            f"{SULTS_API}/chamado/ticket",
            json=payload,
            headers={
                "Authorization": token,
                "Content-Type": "application/json;charset=UTF-8",
            },
            timeout=30,
            verify=False,
        )
        r.raise_for_status()
        ticket_id = r.json().get("id", "?")
        log.info("Chamado Sults criado — ID: %s", ticket_id)
    except requests.HTTPError as exc:
        log.error("Erro ao criar chamado Sults: %s — %s", exc.response.status_code, exc.response.text)
    except Exception as exc:
        log.error("Erro inesperado ao criar chamado Sults: %s", exc)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    setup_logging()
    log = logging.getLogger(__name__)

    username = os.getenv("PIPE_USER")
    password = os.getenv("PIPE_PASS")

    if not username or not password:
        log.error("PIPE_USER e PIPE_PASS não encontrados. Configure o arquivo .env")
        sys.exit(1)

    start, end = get_previous_week_range()
    log.info("Período: %s a %s", start, end)

    try:
        log.info("Autenticando no PipeMais...")
        token = get_pipe_token(username, password)
        log.info("Autenticação bem-sucedida.")
    except requests.HTTPError as exc:
        log.error("Falha na autenticação: %s — %s", exc.response.status_code, exc.response.text)
        sys.exit(1)
    except Exception as exc:
        log.error("Erro inesperado na autenticação: %s", exc)
        sys.exit(1)

    try:
        log.info("Buscando dados de ponto...")
        data = fetch_mirror_view(token, start, end)
        log.info("Dados recebidos: %d funcionários.", len(data))
    except requests.HTTPError as exc:
        log.error("Erro na API: %s — %s", exc.response.status_code, exc.response.text)
        sys.exit(1)
    except Exception as exc:
        log.error("Erro inesperado ao buscar dados: %s", exc)
        sys.exit(1)

    if not data:
        log.warning("API retornou lista vazia.")
        sys.exit(0)

    store_records = filter_store(data)
    log.info("Funcionários do %s: %d", STORE_NAME, len(store_records))

    if not store_records:
        log.warning("Nenhum funcionário encontrado para '%s'.", STORE_NAME)
        sys.exit(0)

    message = build_message(store_records, start, end)
    log.info("Mensagem gerada:\n%s", message)

    log.info("Enviando chamado no Sults...")
    send_sults_ticket(message, start, end)


if __name__ == "__main__":
    main()
