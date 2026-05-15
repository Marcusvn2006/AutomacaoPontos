"""
Script de investigação — NÃO envia nada, NÃO usa Sults.
Testa: showInactivates=True e campos de status do user.
"""
import os
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

today      = date.today()
last_monday = today - timedelta(days=today.weekday() + 7)
last_sunday = last_monday + timedelta(days=6)
start = last_monday.isoformat()
end   = last_sunday.isoformat()
print(f"Período: {start} a {end}\n")

PAYLOAD_BASE = {
    "end": end, "extensionType": "PDF", "fontSize": "NORMAL",
    "isDraft": False, "queryParam": {}, "reportKind": "WORK_DAYS",
    "sendEmailToUsers": False, "splitByMonth": False,
    "start": start, "userId": "ALL", "showSignFields": False,
}

# ── Fase 1: comparar ativos vs ativos+inativos ────────────────────────────────

print("=" * 60)
print("FASE 1 — Comparando showInactivates=False vs True")
print("=" * 60)

resp_ativos = requests.post(
    f"{API_BASE}/reports/generate/mirror/view",
    json={**PAYLOAD_BASE, "showInactivates": False},
    headers=HEADERS, timeout=120, verify=False,
)
resp_ativos.raise_for_status()
ativos = resp_ativos.json()

resp_todos = requests.post(
    f"{API_BASE}/reports/generate/mirror/view",
    json={**PAYLOAD_BASE, "showInactivates": True},
    headers=HEADERS, timeout=120, verify=False,
)
resp_todos.raise_for_status()
todos = resp_todos.json()

print(f"  showInactivates=False → {len(ativos)} funcionários")
print(f"  showInactivates=True  → {len(todos)} funcionários")
print(f"  Diferença             → {len(todos) - len(ativos)} desligados\n")

# Nomes que aparecem apenas com showInactivates=True
nomes_ativos = {(r.get("user", {}).get("name", ""), r.get("user", {}).get("teamName", "")) for r in ativos}
desligados = [r for r in todos if (r.get("user", {}).get("name", ""), r.get("user", {}).get("teamName", "")) not in nomes_ativos]

print(f"Funcionários extras (desligados) — {len(desligados)} encontrados:")
for rec in sorted(desligados, key=lambda r: r.get("user", {}).get("teamName", "")):
    u = rec.get("user", {})
    print(f"  {u.get('teamName', '?'):<25} {u.get('name', '?')}")

# ── Fase 2: campos disponíveis no objeto user ─────────────────────────────────

print()
print("=" * 60)
print("FASE 2 — Campos do objeto 'user' (ativo vs desligado)")
print("=" * 60)

if ativos:
    u_ativo = ativos[0].get("user", {})
    print(f"\nCampos em um funcionário ATIVO ({u_ativo.get('name', '?')}):")
    for k, v in u_ativo.items():
        print(f"  {k}: {v!r}")

if desligados:
    u_deslig = desligados[0].get("user", {})
    print(f"\nCampos em um funcionário DESLIGADO ({u_deslig.get('name', '?')}):")
    for k, v in u_deslig.items():
        print(f"  {k}: {v!r}")

    print("\nCampos que DIFEREM entre ativo e desligado:")
    u_a = ativos[0].get("user", {})
    u_d = u_deslig
    todos_campos = set(u_a.keys()) | set(u_d.keys())
    for campo in sorted(todos_campos):
        val_a = u_a.get(campo, "<ausente>")
        val_d = u_d.get(campo, "<ausente>")
        if val_a != val_d:
            print(f"  {campo}:")
            print(f"    ativo    : {val_a!r}")
            print(f"    desligado: {val_d!r}")
else:
    print("\nNenhum desligado encontrado para comparar campos.")

print("\nInvestigação concluída.")
