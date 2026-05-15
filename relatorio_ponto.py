import calendar
import json
import os
import sys
import time
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR   = Path(__file__).parent
LOGS_DIR   = BASE_DIR / "logs"
LOJAS_FILE = BASE_DIR / "lojas.json"
load_dotenv(BASE_DIR / ".env")

AUTH_URL  = "https://auth.pipemais.com.br/auth/realms/PipeMais/protocol/openid-connect/token"
API_BASE  = "https://api.pipemais.com.br/api"
SULTS_API = "https://api.sults.com.br/api/v1"

SULTS_RETRY_ATTEMPTS = 3
SULTS_RETRY_DELAY_S  = 5

SIGNATURE_MONTHS_BACK    = 5
SIGNATURE_PENDING_STATUS = {"OPEN", "USER_PENDING"}

SUNDAY_OFF_STATUSES = {
    "REST_DAY", "DAY_OFF", "DAY_OFF_ALLOWANCE", "VACATION",
    "HOLIDAY", "ABSENCE", "COMPENSATION", "DISABLED",
    "MATERNITY_LEAVE", "PATERNITY_LEAVE", "MARRY_LEAVE", "DEATH_LEAVE",
}

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    cutoff = datetime.now().timestamp() - (90 * 86400)
    for f in LOGS_DIR.glob("ponto_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
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

def first_name(full_name: str) -> str:
    parts = full_name.strip().split()
    return parts[0].capitalize() if parts else full_name.capitalize()

def get_previous_week_range() -> tuple[str, str]:
    today       = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday.isoformat(), last_sunday.isoformat()

def fmt_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m")

def _int_env(name: str, required: bool = True) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise RuntimeError(f"Variável de ambiente obrigatória ausente: {name}")
        return None
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Variável de ambiente {name} deve ser numérica (valor atual: {raw!r})")

def format_balance(total_seconds: int) -> str:
    sign    = "-" if total_seconds < 0 else "+"
    abs_sec = abs(int(total_seconds))
    h, rem  = divmod(abs_sec, 3600)
    m       = rem // 60
    return f"{sign}{h:02d}:{m:02d}h"

def _format_sig_line(fname: str, months: list[str]) -> str:
    if len(months) == 1:
        return f"{fname} - mês {months[0]}"
    elif len(months) == 2:
        return f"{fname} - meses {months[0]} e {months[1]}"
    else:
        return f"{fname} - meses {', '.join(months[:-1])} e {months[-1]}"

# ── Configuração de lojas/gerentes ────────────────────────────────────────────

def load_managers() -> list[dict]:
    log = logging.getLogger(__name__)
    if not LOJAS_FILE.exists():
        raise RuntimeError(f"Arquivo de mapeamento ausente: {LOJAS_FILE}")
    try:
        config = json.loads(LOJAS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lojas.json inválido (JSON malformado): {exc}")
    regions = config.get("regions") or {}
    if not regions:
        raise RuntimeError("lojas.json não contém 'regions' ou está vazio.")
    managers: list[dict] = []
    seen_ids: set[int]   = set()
    for region_name, entries in regions.items():
        if not isinstance(entries, list):
            raise RuntimeError(f"Região '{region_name}' deve ser uma lista.")
        for m in entries:
            name     = (m.get("name") or "").strip()
            sults_id = m.get("sults_id")
            stores   = m.get("stores") or []
            if not name or not isinstance(sults_id, int) or not stores:
                raise RuntimeError(f"Gerente inválido em '{region_name}': {m!r}")
            if sults_id in seen_ids:
                log.warning("sults_id %d aparece mais de uma vez em lojas.json.", sults_id)
            seen_ids.add(sults_id)
            managers.append({
                "name":     name,
                "sults_id": sults_id,
                "stores":   [s.strip() for s in stores],
                "region":   region_name,
            })
    log.info("Carregados %d gerentes de %d regiões.", len(managers), len(regions))
    return managers

# ── PipeMais Auth ─────────────────────────────────────────────────────────────

class PipeAuth:
    SAFETY_MARGIN_SECONDS = 30

    def __init__(self, username: str, password: str) -> None:
        self._username      = username
        self._password      = password
        self._access_token  = ""
        self._refresh_token = ""
        self._expires_at    = 0.0
        self._login()

    def _request(self, data: dict) -> dict:
        r = requests.post(AUTH_URL, data=data, timeout=60, verify=False)
        r.raise_for_status()
        return r.json()

    def _store(self, resp: dict) -> None:
        token = resp.get("access_token")
        if not token:
            raise RuntimeError(f"PipeMais não retornou access_token. Resposta: {str(resp)[:200]}")
        self._access_token  = token
        self._refresh_token = resp.get("refresh_token", "")
        expires_in          = int(resp.get("expires_in", 300))
        self._expires_at    = time.time() + expires_in - self.SAFETY_MARGIN_SECONDS
        logging.getLogger(__name__).info("Token PipeMais armazenado (expira em %ds).", expires_in)

    def _login(self) -> None:
        logging.getLogger(__name__).info("Login completo no PipeMais (usuário/senha).")
        self._store(self._request({
            "client_id": "pipe-prod", "username": self._username,
            "password": self._password, "grant_type": "password",
        }))

    def _refresh(self) -> None:
        log = logging.getLogger(__name__)
        if not self._refresh_token:
            self._login()
            return
        try:
            log.info("Renovando token via refresh_token...")
            self._store(self._request({
                "client_id": "pipe-prod", "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            }))
        except requests.HTTPError as exc:
            log.warning("Refresh falhou (%s) — refazendo login completo.", exc.response.status_code)
            self._login()

    def token(self) -> str:
        if time.time() >= self._expires_at:
            self._refresh()
        return self._access_token

    def force_refresh(self) -> str:
        self._refresh()
        return self._access_token

# ── PipeMais API ──────────────────────────────────────────────────────────────

def fetch_mirror_view(auth: PipeAuth, start: str, end: str) -> list:
    payload = {
        "end": end, "extensionType": "PDF", "fontSize": "NORMAL",
        "isDraft": False, "queryParam": {}, "reportKind": "WORK_DAYS",
        "sendEmailToUsers": False, "showInactivates": False,
        "showSignFields": False, "splitByMonth": False,
        "start": start, "userId": "ALL",
    }
    log = logging.getLogger(__name__)
    for attempt in (1, 2):
        r = requests.post(
            f"{API_BASE}/reports/generate/mirror/view",
            json=payload,
            headers={"Authorization": f"Bearer {auth.token()}", "Content-Type": "application/json"},
            timeout=120, verify=False,
        )
        if r.status_code == 401 and attempt == 1:
            log.warning("Recebido 401 — forçando refresh do token e tentando novamente.")
            auth.force_refresh()
            continue
        r.raise_for_status()
        return r.json()

def fetch_pending_signatures(auth: PipeAuth) -> dict[tuple[str, str], list[str]]:
    """
    Consulta os últimos SIGNATURE_MONTHS_BACK meses individualmente e retorna
    quem tem assinatura pendente, com os meses respectivos.

    Returns: {(nome_completo, store_name): [mes_label, ...]}  ordenado por mês.
    """
    log = logging.getLogger(__name__)
    today      = date.today()
    first_this = today.replace(day=1)

    months: list[tuple[str, str, str]] = []
    cursor = first_this
    for _ in range(SIGNATURE_MONTHS_BACK):
        cursor  = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
        m_start = cursor
        m_end   = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
        months.append((cursor.strftime("%m/%Y"), m_start.isoformat(), m_end.isoformat()))
    months.reverse()

    pending: dict[tuple[str, str], list[str]] = {}

    for label, m_start, m_end in months:
        try:
            url = (
                f"{API_BASE}/user-closed-periods/open"
                f"?size=40000&page=0"
                f"&sort=summaryTotals.userName,asc"
                f"&startDate={m_start}&endDate={m_end}&userId="
            )
            for attempt in (1, 2):
                r = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {auth.token()}"},
                    timeout=60, verify=False,
                )
                if r.status_code == 401 and attempt == 1:
                    auth.force_refresh()
                    continue
                r.raise_for_status()
                break

            result = r.json()
            items  = result.get("content") if isinstance(result, dict) else result
            count  = 0
            for item in items:
                if item.get("status") not in SIGNATURE_PENDING_STATUS:
                    continue
                summary = item.get("summaryTotals") or {}
                nome    = (summary.get("userName") or "").strip()
                loja    = (summary.get("userTeamName") or "").strip()
                if not nome or not loja:
                    continue
                key = (nome, loja)
                if label not in pending.get(key, []):
                    pending.setdefault(key, []).append(label)
                count += 1
            log.info("Assinaturas pendentes %s: %d", label, count)

        except Exception as exc:
            log.warning("Erro ao buscar assinaturas de %s — continuando: %s", label, exc)

    return pending

def fetch_terminated_employees(auth: PipeAuth) -> set[tuple[str, str]]:
    """
    Consulta os últimos SIGNATURE_MONTHS_BACK meses com showInactivates=True
    e retorna um set de (nome.upper(), loja.upper()) de funcionários desligados.
    """
    log = logging.getLogger(__name__)
    today      = date.today()
    first_this = today.replace(day=1)

    months: list[tuple[str, str, str]] = []
    cursor = first_this
    for _ in range(SIGNATURE_MONTHS_BACK):
        cursor  = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
        m_start = cursor
        m_end   = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
        months.append((cursor.strftime("%m/%Y"), m_start.isoformat(), m_end.isoformat()))
    months.reverse()

    terminated: set[tuple[str, str]] = set()

    payload_base = {
        "extensionType": "PDF", "fontSize": "NORMAL",
        "isDraft": False, "queryParam": {}, "reportKind": "WORK_DAYS",
        "sendEmailToUsers": False, "showInactivates": True,
        "showSignFields": False, "splitByMonth": False,
        "userId": "ALL",
    }

    for label, m_start, m_end in months:
        try:
            for attempt in (1, 2):
                r = requests.post(
                    f"{API_BASE}/reports/generate/mirror/view",
                    json={**payload_base, "start": m_start, "end": m_end},
                    headers={"Authorization": f"Bearer {auth.token()}", "Content-Type": "application/json"},
                    timeout=120, verify=False,
                )
                if r.status_code == 401 and attempt == 1:
                    auth.force_refresh()
                    continue
                r.raise_for_status()
                break

            for rec in r.json():
                u    = rec.get("user", {})
                nome = (u.get("name") or "").strip()
                loja = (u.get("teamName") or "").strip()
                if nome and loja:
                    terminated.add((nome.upper(), loja.upper()))

            log.info("Desligados %s: %d acumulado(s).", label, len(terminated))

        except Exception as exc:
            log.warning("Erro ao buscar desligados de %s — continuando: %s", label, exc)

    return terminated

# ── Análise de ponto ──────────────────────────────────────────────────────────

def is_problematic_day(day: dict) -> bool:
    status  = day.get("status", "")
    entries = day.get("timeEntries") or []
    if status == "MISSING_TIME_ENTRIES":
        return True
    if len(entries) > 0 and len(entries) % 2 != 0:
        return True
    return False

def has_duplicate_punches(day: dict) -> bool:
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

def analyse_records(records: list, start: str, end: str) -> list[dict]:
    log = logging.getLogger(__name__)
    out: list[dict] = []
    for rec in records:
        u     = rec.get("user") or {}
        s     = rec.get("summary") or {}
        name  = (u.get("name") or "").strip()
        store = (u.get("teamName") or "").strip()
        if not name:
            log.warning("Registro ignorado: funcionário sem nome (userId=%s).", u.get("id", "?"))
            continue
        if not store:
            log.warning("Funcionário '%s' sem teamName — ignorado.", name)
            continue
        issues: list[tuple[str, int]] = []
        sunday_date: str | None = None
        for day in rec.get("workDays") or []:
            day_date = day.get("date", "")
            if day_date < start or day_date > end:
                continue
            if is_problematic_day(day):
                entries = day.get("timeEntries") or []
                issues.append((day_date, len(entries)))
            elif has_duplicate_punches(day):
                issues.append((day_date, -1))
            if sunday_date is None and worked_on_sunday(day):
                sunday_date = day_date
        out.append({
            "name":        name,
            "store":       store,
            "balance":     int(s.get("balanceAccumulated", 0)),
            "issues":      issues,
            "sunday_date": sunday_date,
        })
    return out

# ── Mensagem ──────────────────────────────────────────────────────────────────

def _format_issue_line(fname: str, issues: list[tuple[str, int]]) -> str:
    groups: dict[int, list[int]] = {}
    for day_date, count in sorted(issues):
        groups.setdefault(count, []).append(datetime.strptime(day_date, "%Y-%m-%d").day)
    parts: list[str] = []
    for count, day_nums in sorted(groups.items(), key=lambda x: x[1][0]):
        day_strs = [f"{d:02d}" for d in day_nums]
        if len(day_strs) == 1:
            prefix = f"dia {day_strs[0]}"
        elif len(day_strs) == 2:
            prefix = f"dias {day_strs[0]} e {day_strs[1]}"
        else:
            prefix = f"dias {', '.join(day_strs[:-1])} e {day_strs[-1]}"
        if count == -1:
            parts.append(f"{prefix} pontos duplicados")
        elif count == 0:
            parts.append(f"{prefix} sem pontos registrados")
        elif count >= 5:
            parts.append(f"{prefix} tem {count} pontos")
        elif count == 1:
            parts.append(f"{prefix} apenas 1 ponto")
        else:
            parts.append(f"{prefix} apenas {count} pontos")
    return f"{fname} {' e '.join(parts)}"

def _build_store_block(store_name: str, employees: list[dict]) -> list[str]:
    issue_lines:     list[str]          = []
    balance_lines:   list[str]          = []
    sundays_by_date: dict[str, list[str]] = {}
    for emp in sorted(employees, key=lambda e: e["name"]):
        fname = first_name(emp["name"])
        if emp["issues"]:
            issue_lines.append(_format_issue_line(fname, emp["issues"]))
        elif emp["balance"] != 0:
            if emp["balance"] < 0:
                balance_lines.append(f"{fname} com um saldo pendente de {format_balance(emp['balance'])}")
            else:
                balance_lines.append(f"{fname} com saldo de {format_balance(emp['balance'])}")
        if emp["sunday_date"]:
            sundays_by_date.setdefault(emp["sunday_date"], []).append(fname)
    if not (issue_lines or balance_lines or sundays_by_date):
        return []
    block: list[str] = [store_name, ""]
    if issue_lines:
        block.extend(issue_lines)
    if balance_lines:
        if issue_lines:
            block.append("")
        block.extend(balance_lines)
    if sundays_by_date:
        if issue_lines or balance_lines:
            block.append("")
        for sd in sorted(sundays_by_date):
            names = sorted(sundays_by_date[sd])
            block.append(f"Trabalharam no domingo {fmt_date(sd)}: {', '.join(names)}")
        block.append("(Verificar folga compensatória conforme escala)")
    return block

def build_message(
    manager: dict,
    employees: list[dict],
    end: str,
    pending_sigs: dict[tuple[str, str], list[str]] | None = None,
) -> str | None:
    end_fmt     = datetime.strptime(end, "%Y-%m-%d").strftime("%d/%m/%Y")
    multi_store = len(manager["stores"]) > 1

    # ── Blocos semanais por loja ──────────────────────────────────────────────
    by_store: dict[str, list[dict]] = {}
    for emp in employees:
        if emp["store"] in manager["stores"]:
            by_store.setdefault(emp["store"], []).append(emp)

    blocks: list[list[str]] = []
    for store_name in manager["stores"]:
        emps = by_store.get(store_name, [])
        if not emps:
            continue
        block = _build_store_block(store_name, emps)
        if block:
            blocks.append(block)

    # ── Seção de assinaturas pendentes ────────────────────────────────────────
    sig_by_store: dict[str, list[tuple[str, list[str]]]] = {}
    if pending_sigs:
        for store in manager["stores"]:
            pessoas = []
            for (nome, store_name), meses in pending_sigs.items():
                if store_name == store:
                    pessoas.append((nome, sorted(meses)))
            if pessoas:
                sig_by_store[store] = sorted(pessoas, key=lambda x: x[0])

    sig_stores = [s for s in manager["stores"] if sig_by_store.get(s)]

    # Nada a enviar
    if not blocks and not sig_stores:
        return None

    # ── Monta mensagem ────────────────────────────────────────────────────────
    lines: list[str] = [
        f"Bom dia {manager['name']}!!",
        "",
        f"Segue prévia de horas até a data de {end_fmt}, "
        "e ocorrências para verificar (caso houver):",
        "",
    ]

    for i, block in enumerate(blocks):
        lines.extend(block)
        if i < len(blocks) - 1:
            lines.append("")

    # Seção de assinaturas
    if sig_stores:
        if blocks:
            lines.append("")
        lines.append("Pendente de assinatura espelho ponto:")
        if multi_store:
            for store in sig_stores:
                pessoas = sig_by_store[store]
                lines.append("")
                lines.append(store)
                lines.append("")
                for nome, meses in pessoas:
                    lines.append(_format_sig_line(first_name(nome), meses))
        else:
            lines.append("")
            for nome, meses in list(sig_by_store.values())[0]:
                lines.append(_format_sig_line(first_name(nome), meses))

    return "\n".join(lines)

# ── Sults ─────────────────────────────────────────────────────────────────────

def _build_sults_payload(titulo: str, html_message: str, prazo: str, responsavel_id: int) -> dict:
    payload = {
        "titulo":         titulo,
        "mensagem":       html_message,
        "tipo":           1,
        "departamentoId": _int_env("SULTS_DEPARTAMENTO_ID"),
        "assuntoId":      _int_env("SULTS_ASSUNTO_ID"),
        "responsavelId":  responsavel_id,
        "dtPrazo":        prazo,
    }
    solic = _int_env("SULTS_SOLICITANTE_ID", required=False)
    if solic:
        payload["solicitanteId"] = solic
    return payload

def _post_sults(payload: dict, token: str) -> dict:
    r = requests.post(
        f"{SULTS_API}/chamado/ticket",
        json=payload,
        headers={"Authorization": token, "Content-Type": "application/json;charset=UTF-8"},
        timeout=30, verify=False,
    )
    r.raise_for_status()
    return r.json()

def send_ticket_with_retry(payload: dict, token: str, label: str) -> int | None:
    log      = logging.getLogger(__name__)
    last_exc = None
    for attempt in range(1, SULTS_RETRY_ATTEMPTS + 1):
        try:
            resp      = _post_sults(payload, token)
            ticket_id = resp.get("id")
            log.info("[%s] Chamado criado — ID: %s (tentativa %d).", label, ticket_id, attempt)
            return ticket_id
        except Exception as exc:
            last_exc = exc
            details  = ""
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                details = f"HTTP {exc.response.status_code} — {exc.response.text[:200]}"
            else:
                details = f"{type(exc).__name__}: {exc}"
            log.warning("[%s] Tentativa %d/%d falhou: %s", label, attempt, SULTS_RETRY_ATTEMPTS, details)
            if attempt < SULTS_RETRY_ATTEMPTS:
                time.sleep(SULTS_RETRY_DELAY_S)
    log.error("[%s] Falhou após %d tentativas. Último erro: %s", label, SULTS_RETRY_ATTEMPTS, last_exc)
    return None

def send_manager_ticket(
    manager: dict,
    message: str,
    test_responsavel_id: int | None = None,
) -> int | None:
    log   = logging.getLogger(__name__)
    token = os.getenv("SULTS_TOKEN")
    if not token:
        log.error("SULTS_TOKEN ausente — não foi possível enviar chamado para %s.", manager["name"])
        return None

    responsavel_id = test_responsavel_id if test_responsavel_id else manager["sults_id"]
    prazo  = (date.today() + timedelta(days=7)).strftime("%Y-%m-%dT12:00:00Z")
    titulo = f"Relatório de ponto - {', '.join(manager['stores'])}"
    payload = _build_sults_payload(
        titulo=titulo,
        html_message=message.replace("\n", "<br>"),
        prazo=prazo,
        responsavel_id=responsavel_id,
    )
    label = f"{manager['name']} #{manager['sults_id']}"
    if test_responsavel_id:
        label += f" [TESTE → #{test_responsavel_id}]"
    return send_ticket_with_retry(payload, token, label)

def send_failure_alert(reason: str, details: str = "") -> None:
    log   = logging.getLogger(__name__)
    token = os.getenv("SULTS_TOKEN")
    if not token:
        log.warning("SULTS_TOKEN ausente — alerta de erro não enviado.")
        return
    solicitante_id = _int_env("SULTS_SOLICITANTE_ID", required=False)
    if not solicitante_id:
        log.warning("SULTS_SOLICITANTE_ID ausente — alerta de erro não enviado.")
        return
    body = (
        f"A execução automática do relatório de ponto falhou.<br><br>"
        f"<b>Motivo:</b> {reason}<br><br>"
    )
    if details:
        safe = details.replace("\n", "<br>")[:1500]
        body += f"<b>Detalhes:</b><br>{safe}<br><br>"
    body += f"<b>Data/hora:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}<br><br>Verifique os logs em <code>logs/</code> no servidor."
    try:
        prazo   = (date.today() + timedelta(days=1)).strftime("%Y-%m-%dT12:00:00Z")
        payload = _build_sults_payload(
            titulo="[ERRO] Automação de Ponto falhou",
            html_message=body,
            prazo=prazo,
            responsavel_id=solicitante_id,
        )
        ticket_id = send_ticket_with_retry(payload, token, "ALERTA-ERRO")
        if ticket_id:
            log.info("Alerta de erro enviado ao Sults — ID: %s", ticket_id)
    except Exception as exc:
        log.error("Falha ao enviar alerta de erro: %s", exc)

def _fail(reason: str, exc: Exception | None = None) -> None:
    log     = logging.getLogger(__name__)
    details = ""
    if exc is not None:
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            details = f"HTTP {exc.response.status_code} — {exc.response.text[:500]}"
        else:
            details = f"{type(exc).__name__}: {exc}"
        log.error("%s — %s", reason, details)
    else:
        log.error(reason)
    send_failure_alert(reason, details)
    sys.exit(1)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    try:
        managers = load_managers()
    except RuntimeError as exc:
        _fail("Falha ao carregar lojas.json", exc)

    username = os.getenv("PIPE_USER")
    password = os.getenv("PIPE_PASS")
    if not username or not password:
        _fail("PIPE_USER e PIPE_PASS não encontrados. Configure o arquivo .env")

    try:
        _int_env("SULTS_DEPARTAMENTO_ID")
        _int_env("SULTS_ASSUNTO_ID")
        _int_env("SULTS_SOLICITANTE_ID", required=False)
    except RuntimeError as exc:
        _fail("Configuração inválida no .env", exc)

    test_responsavel_id = _int_env("SULTS_TEST_RESPONSAVEL_ID", required=False)
    if test_responsavel_id:
        log.info("*** MODO TESTE *** — todos os chamados irão para responsável #%d", test_responsavel_id)

    known_stores: set[str] = set()
    for m in managers:
        known_stores.update(m["stores"])

    start, end = get_previous_week_range()
    log.info("Período semanal: %s a %s", start, end)

    try:
        log.info("Autenticando no PipeMais...")
        auth = PipeAuth(username, password)
        log.info("Autenticação bem-sucedida.")
    except Exception as exc:
        _fail("Falha na autenticação PipeMais", exc)

    try:
        log.info("Buscando dados de ponto...")
        data = fetch_mirror_view(auth, start, end)
        log.info("Dados recebidos: %d funcionários.", len(data))
    except Exception as exc:
        _fail("Erro ao buscar dados do PipeMais", exc)

    if not data:
        _fail("API do PipeMais retornou lista vazia — verifique se há dados no período.")

    employees = analyse_records(data, start, end)
    log.info("Funcionários analisados: %d.", len(employees))

    orphan_stores: dict[str, int] = {}
    for e in employees:
        if e["store"] not in known_stores:
            orphan_stores[e["store"]] = orphan_stores.get(e["store"], 0) + 1
    if orphan_stores:
        log.info("Lojas sem gerente mapeado (ignoradas): %s",
                 ", ".join(f"{s} ({n})" for s, n in sorted(orphan_stores.items())))

    log.info("Buscando desligados (últimos %d meses)...", SIGNATURE_MONTHS_BACK)
    try:
        terminated = fetch_terminated_employees(auth)
        log.info("Desligados encontrados: %d", len(terminated))
    except Exception as exc:
        log.warning("Erro ao buscar desligados — filtro será ignorado: %s", exc)
        terminated = set()

    log.info("Buscando assinaturas pendentes (últimos %d meses)...", SIGNATURE_MONTHS_BACK)
    try:
        pending_sigs_raw = fetch_pending_signatures(auth)
        pending_sigs = {
            (nome, loja): meses
            for (nome, loja), meses in pending_sigs_raw.items()
            if (nome.upper(), loja.upper()) not in terminated
        }
        filtrados = len(pending_sigs_raw) - len(pending_sigs)
        log.info("Funcionários com assinatura pendente: %d (%d desligado(s) filtrado(s))",
                 len(pending_sigs), filtrados)
    except Exception as exc:
        log.warning("Erro ao buscar assinaturas pendentes — seção será omitida: %s", exc)
        pending_sigs = {}

    successes: list[str] = []
    failures:  list[str] = []
    skipped:   list[str] = []

    for manager in managers:
        label   = f"{manager['name']} #{manager['sults_id']}"
        message = build_message(manager, employees, end, pending_sigs)
        if message is None:
            log.info("[%s] Sem ocorrências nem pendências — chamado não enviado.", label)
            skipped.append(label)
            continue
        log.info("[%s] Mensagem gerada:\n%s", label, message)
        log.info("[%s] Enviando chamado...", label)
        ticket_id = send_manager_ticket(manager, message, test_responsavel_id=test_responsavel_id)
        if ticket_id is not None:
            successes.append(f"{label} (ticket {ticket_id})")
        else:
            failures.append(label)

    log.info("=" * 60)
    log.info("RESUMO DA EXECUÇÃO")
    log.info("  Enviados com sucesso: %d", len(successes))
    for s in successes:
        log.info("    OK    %s", s)
    log.info("  Pulados (sem dados): %d", len(skipped))
    for s in skipped:
        log.info("    SKIP  %s", s)
    log.info("  Falharam: %d", len(failures))
    for f in failures:
        log.info("    FAIL  %s", f)
    log.info("=" * 60)

    if failures:
        log.error("Execução finalizada com %d falha(s).", len(failures))
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        logging.getLogger(__name__).exception("Erro não tratado")
        _fail("Erro inesperado não tratado", exc)
