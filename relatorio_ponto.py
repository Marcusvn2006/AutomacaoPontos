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

# Statuses que indicam dia normal de descanso (não contam como "trabalhou no domingo")
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
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday.isoformat(), last_sunday.isoformat()

def fmt_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m")

def _int_env(name: str, required: bool = True) -> int | None:
    """Lê variável de ambiente como int. Falha clara se inválida."""
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
    """Formata saldo como +HH:MMh ou -HH:MMh."""
    sign = "-" if total_seconds < 0 else "+"
    abs_sec = abs(int(total_seconds))
    h, rem = divmod(abs_sec, 3600)
    m = rem // 60
    return f"{sign}{h:02d}:{m:02d}h"

# ── Configuração de lojas/gerentes ────────────────────────────────────────────

def load_managers() -> list[dict]:
    """Lê lojas.json e retorna lista achatada de gerentes.

    Cada item: {"name": str, "sults_id": int, "stores": list[str], "region": str}
    Valida estrutura básica e detecta inconsistências (sults_id duplicado, etc.).
    """
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
    seen_ids: set[int] = set()

    for region_name, entries in regions.items():
        if not isinstance(entries, list):
            raise RuntimeError(f"Região '{region_name}' deve ser uma lista.")
        for m in entries:
            name     = (m.get("name") or "").strip()
            sults_id = m.get("sults_id")
            stores   = m.get("stores") or []
            if not name or not isinstance(sults_id, int) or not stores:
                raise RuntimeError(
                    f"Gerente inválido em '{region_name}': {m!r}"
                )
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

# ── PipeMais ──────────────────────────────────────────────────────────────────

class PipeAuth:
    """Gerencia o token do PipeMais com refresh automático."""

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
            raise RuntimeError(
                f"PipeMais não retornou access_token. Resposta: {str(resp)[:200]}"
            )
        self._access_token  = token
        self._refresh_token = resp.get("refresh_token", "")
        expires_in          = int(resp.get("expires_in", 300))
        self._expires_at    = time.time() + expires_in - self.SAFETY_MARGIN_SECONDS
        logging.getLogger(__name__).info(
            "Token PipeMais armazenado (expira em %ds).", expires_in
        )

    def _login(self) -> None:
        logging.getLogger(__name__).info("Login completo no PipeMais (usuário/senha).")
        self._store(self._request({
            "client_id": "pipe-prod",
            "username":  self._username,
            "password":  self._password,
            "grant_type": "password",
        }))

    def _refresh(self) -> None:
        log = logging.getLogger(__name__)
        if not self._refresh_token:
            self._login()
            return
        try:
            log.info("Renovando token via refresh_token...")
            self._store(self._request({
                "client_id":     "pipe-prod",
                "refresh_token": self._refresh_token,
                "grant_type":    "refresh_token",
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

def fetch_mirror_view(auth: PipeAuth, start: str, end: str) -> list:
    payload = {
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
    }
    log = logging.getLogger(__name__)

    for attempt in (1, 2):
        r = requests.post(
            f"{API_BASE}/reports/generate/mirror/view",
            json=payload,
            headers={
                "Authorization": f"Bearer {auth.token()}",
                "Content-Type":  "application/json",
            },
            timeout=120,
            verify=False,
        )
        if r.status_code == 401 and attempt == 1:
            log.warning("Recebido 401 — forçando refresh do token e tentando novamente.")
            auth.force_refresh()
            continue
        r.raise_for_status()
        return r.json()

# ── Análise ───────────────────────────────────────────────────────────────────

def is_problematic_day(day: dict) -> bool:
    status  = day.get("status", "")
    entries = day.get("timeEntries") or []
    if status == "MISSING_TIME_ENTRIES":
        return True
    if len(entries) > 0 and len(entries) % 2 != 0:
        return True
    return False

def has_duplicate_punches(day: dict) -> bool:
    """Retorna True se dois pontos consecutivos estão a menos de 10 min."""
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
    """Analisa todos os funcionários e devolve uma lista achatada.

    Cada item:
      {
        "name":         str,
        "store":        str,
        "balance":      int (segundos, ± ),
        "issues":       [(date_str, count), ...],   # count == -1 para duplicados
        "sunday_date":  str | None,
      }
    """
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
    """Monta uma única linha de ocorrências para um funcionário."""
    # Agrupa por contagem de pontos: {count: [day_num, ...]}
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
        elif count >= 5:
            parts.append(f"{prefix} tem {count} pontos")
        elif count == 1:
            parts.append(f"{prefix} apenas 1 ponto")
        else:
            parts.append(f"{prefix} apenas {count} pontos")

    return f"{fname} {' e '.join(parts)}"

def _build_store_block(store_name: str, employees: list[dict]) -> list[str]:
    """Monta o bloco de uma loja. Retorna [] se a loja não tem nada a reportar."""
    issue_lines:   list[str] = []
    balance_lines: list[str] = []
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

def build_message(manager: dict, employees: list[dict], end: str) -> str | None:
    """Monta a mensagem do chamado para um gerente.

    Retorna None se nenhum funcionário das lojas do gerente tem algo a reportar.
    """
    end_fmt = datetime.strptime(end, "%Y-%m-%d").strftime("%d/%m/%Y")

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

    if not blocks:
        return None

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
    """POST cru ao endpoint de chamado (sem retry, sem tratamento)."""
    r = requests.post(
        f"{SULTS_API}/chamado/ticket",
        json=payload,
        headers={
            "Authorization": token,
            "Content-Type":  "application/json;charset=UTF-8",
        },
        timeout=30,
        verify=False,
    )
    r.raise_for_status()
    return r.json()

def send_ticket_with_retry(payload: dict, token: str, label: str) -> int | None:
    """Envia chamado com até 3 tentativas. Retorna ID se sucesso, None se falhou."""
    log = logging.getLogger(__name__)
    last_exc: Exception | None = None

    for attempt in range(1, SULTS_RETRY_ATTEMPTS + 1):
        try:
            resp = _post_sults(payload, token)
            ticket_id = resp.get("id")
            log.info("[%s] Chamado criado — ID: %s (tentativa %d).", label, ticket_id, attempt)
            return ticket_id
        except Exception as exc:
            last_exc = exc
            details = ""
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                details = f"HTTP {exc.response.status_code} — {exc.response.text[:200]}"
            else:
                details = f"{type(exc).__name__}: {exc}"
            log.warning("[%s] Tentativa %d/%d falhou: %s",
                        label, attempt, SULTS_RETRY_ATTEMPTS, details)
            if attempt < SULTS_RETRY_ATTEMPTS:
                time.sleep(SULTS_RETRY_DELAY_S)

    log.error("[%s] Falhou após %d tentativas. Último erro: %s",
              label, SULTS_RETRY_ATTEMPTS, last_exc)
    return None

def send_manager_ticket(manager: dict, message: str) -> int | None:
    """Envia o chamado de um gerente. Retorna ticket_id ou None se falhou."""
    log   = logging.getLogger(__name__)
    token = os.getenv("SULTS_TOKEN")

    if not token:
        log.error("SULTS_TOKEN ausente — não foi possível enviar chamado para %s.", manager["name"])
        return None

    prazo  = (date.today() + timedelta(days=7)).strftime("%Y-%m-%dT12:00:00Z")
    titulo = f"Relatório de ponto - {', '.join(manager['stores'])}"

    payload = _build_sults_payload(
        titulo=titulo,
        html_message=message.replace("\n", "<br>"),
        prazo=prazo,
        responsavel_id=manager["sults_id"],
    )
    label = f"{manager['name']} #{manager['sults_id']}"
    return send_ticket_with_retry(payload, token, label)

def send_failure_alert(reason: str, details: str = "") -> None:
    """Envia chamado no Sults avisando que a automação falhou globalmente."""
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
    body += (
        f"<b>Data/hora:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}<br><br>"
        f"Verifique os logs em <code>logs/</code> no servidor."
    )

    try:
        prazo = (date.today() + timedelta(days=1)).strftime("%Y-%m-%dT12:00:00Z")
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
    """Loga, envia alerta para o Sults e encerra o script com código 1."""
    log = logging.getLogger(__name__)
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

    # Carrega gerentes
    try:
        managers = load_managers()
    except RuntimeError as exc:
        _fail("Falha ao carregar lojas.json", exc)

    # Valida env vars
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

    # Lojas conhecidas (para detectar funcionários "órfãos" só pra debug)
    known_stores: set[str] = set()
    for m in managers:
        known_stores.update(m["stores"])

    # Período
    start, end = get_previous_week_range()
    log.info("Período: %s a %s", start, end)

    # Autenticação
    try:
        log.info("Autenticando no PipeMais...")
        auth = PipeAuth(username, password)
        log.info("Autenticação bem-sucedida.")
    except Exception as exc:
        _fail("Falha na autenticação PipeMais", exc)

    # Busca dados
    try:
        log.info("Buscando dados de ponto...")
        data = fetch_mirror_view(auth, start, end)
        log.info("Dados recebidos: %d funcionários.", len(data))
    except Exception as exc:
        _fail("Erro ao buscar dados do PipeMais", exc)

    if not data:
        _fail("API do PipeMais retornou lista vazia — verifique se há dados no período.")

    # Analisa
    employees = analyse_records(data, start, end)
    log.info("Funcionários analisados: %d.", len(employees))

    # Log de funcionários em lojas não mapeadas (apenas info)
    orphan_stores: dict[str, int] = {}
    for e in employees:
        if e["store"] not in known_stores:
            orphan_stores[e["store"]] = orphan_stores.get(e["store"], 0) + 1
    if orphan_stores:
        log.info("Lojas sem gerente mapeado (ignoradas): %s",
                 ", ".join(f"{s} ({n})" for s, n in sorted(orphan_stores.items())))

    # Envia chamado por gerente
    successes: list[str] = []
    failures:  list[str] = []
    skipped:   list[str] = []

    for manager in managers:
        label = f"{manager['name']} #{manager['sults_id']}"
        message = build_message(manager, employees, end)
        if message is None:
            log.info("[%s] Sem ocorrências — chamado não enviado.", label)
            skipped.append(label)
            continue

        log.info("[%s] Enviando chamado...", label)
        ticket_id = send_manager_ticket(manager, message)
        if ticket_id is not None:
            successes.append(f"{label} (ticket {ticket_id})")
        else:
            failures.append(label)

    # Resumo final
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
