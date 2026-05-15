"""
Script de investigação — NÃO envia nada, NÃO usa Sults.
Cruza desligados (showInactivates=True) com pendentes de assinatura.
"""
import os
import calendar
from datetime import date, timedelta
from pathlib import Path

import urllib3
import requests
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

AUTH_URL = "https://auth.pipemais.com.br/auth/realms/PipeMais/protocol/openid-connect/token"
API_BASE = "https://api.pipemais.com.br/api"

MESES_ATRAS             = 5
SIGNATURE_PENDING_STATUS = {"OPEN", "USER_PENDING"}

# ── Auth ──────────────────────────────────────────────────────────────────────

print("Autenticando...")
r = requests.post(AUTH_URL, data={
    "client_id":  "pipe-prod",
    "username":   os.getenv("PIPE_USER"),
    "password":   os.getenv("PIPE_PASS"),
    "grant_type": "password",
}, timeout=60, verify=False)
r.raise_for_status()
token = r.json()["access_token"]
HEADERS = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
print("OK\n")

# ── Monta lista de meses ──────────────────────────────────────────────────────

today      = date.today()
first_this = today.replace(day=1)

meses: list[tuple[str, str, str]] = []
cursor = first_this
for _ in range(MESES_ATRAS):
    cursor  = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
    m_start = cursor
    m_end   = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
    meses.append((cursor.strftime("%m/%Y"), m_start.isoformat(), m_end.isoformat()))
meses.reverse()

print(f"Meses consultados: {[m[0] for m in meses]}\n")

# ── Fase 1: coleta desligados mês a mês ───────────────────────────────────────

print("Buscando desligados...")
# {(nome_upper, loja_upper): nome_original}
desligados: dict[tuple[str, str], str] = {}

for label, m_start, m_end in meses:
    payload = {
        "start": m_start, "end": m_end,
        "extensionType": "PDF", "fontSize": "NORMAL",
        "isDraft": False, "queryParam": {}, "reportKind": "WORK_DAYS",
        "sendEmailToUsers": False, "showInactivates": True,
        "showSignFields": False, "splitByMonth": False,
        "userId": "ALL",
    }
    resp = requests.post(
        f"{API_BASE}/reports/generate/mirror/view",
        json=payload, headers=HEADERS, timeout=120, verify=False,
    )
    resp.raise_for_status()
    for rec in resp.json():
        u    = rec.get("user", {})
        nome = (u.get("name") or "").strip()
        loja = (u.get("teamName") or "").strip()
        if nome and loja:
            desligados[(nome.upper(), loja.upper())] = nome

print(f"  {len(desligados)} desligado(s) distintos encontrados\n")

# ── Fase 2: coleta pendentes de assinatura mês a mês ─────────────────────────

print("Buscando pendentes de assinatura...")
# {(nome_upper, loja_upper): [mes_label, ...]}
pendentes: dict[tuple[str, str], list[str]] = {}

for label, m_start, m_end in meses:
    url = (
        f"{API_BASE}/user-closed-periods/open"
        f"?size=40000&page=0"
        f"&sort=summaryTotals.userName,asc"
        f"&startDate={m_start}&endDate={m_end}&userId="
    )
    resp = requests.get(url, headers=HEADERS, timeout=60, verify=False)
    resp.raise_for_status()
    result = resp.json()
    items  = result.get("content") if isinstance(result, dict) else result

    count = 0
    for item in items:
        if item.get("status") not in SIGNATURE_PENDING_STATUS:
            continue
        summary = item.get("summaryTotals") or {}
        nome    = (summary.get("userName") or "").strip()
        loja    = (summary.get("userTeamName") or "").strip()
        if not nome or not loja:
            continue
        key = (nome.upper(), loja.upper())
        if label not in pendentes.get(key, []):
            pendentes.setdefault(key, []).append(label)
        count += 1

    print(f"  {label}: {count} pendente(s)")

print(f"\n  {len(pendentes)} pessoa(s) distintas com assinatura pendente\n")

# ── Fase 3: cruzamento ────────────────────────────────────────────────────────

cruzados = {k: v for k, v in pendentes.items() if k in desligados}

print("=" * 60)
print(f"DESLIGADOS COM ASSINATURA PENDENTE — {len(cruzados)} pessoa(s)")
print("=" * 60)

if cruzados:
    por_loja: dict[str, list] = {}
    for (nome_up, loja_up), ms in sorted(cruzados.items(), key=lambda x: (x[0][1], x[0][0])):
        nome_original = desligados[(nome_up, loja_up)]
        por_loja.setdefault(loja_up, []).append((nome_original, ms))

    for loja in sorted(por_loja):
        print(f"\n[ {loja} ]")
        for nome, ms in por_loja[loja]:
            print(f"  {nome:<50} meses: {', '.join(sorted(ms))}")
else:
    print("  Nenhum desligado com assinatura pendente encontrado.")

print("\nInvestigação concluída.")
