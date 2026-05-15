"""
Script de investigação — descobre qual assuntoId o #485 tem permissão para receber,
iterando os assuntos dentro dos departamentos 35 (Canal VD) e 36 (Canal Loja).
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

SULTS_API        = "https://api.sults.com.br/api/v1"
TOKEN            = os.getenv("SULTS_TOKEN")
HEADERS          = {"Authorization": TOKEN, "Content-Type": "application/json;charset=UTF-8"}
RESPONSAVEL_TEST = 274
SOLICITANTE_ID   = int(os.getenv("SULTS_SOLICITANTE_ID", "479"))
PRAZO            = (date.today() + timedelta(days=1)).strftime("%Y-%m-%dT12:00:00Z")

# Dept 36 = Canal Loja, Dept 35 = Canal VD
DEPARTAMENTOS = [
    (36, "Canal Loja"),
    (35, "Canal VD"),
]

print("Buscando assuntoId correto para cada departamento...\n")

for dept_id, dept_nome in DEPARTAMENTOS:
    print(f"{'='*50}")
    print(f"departamentoId = {dept_id} ({dept_nome})")
    print(f"{'='*50}")
    encontrado = False
    for assunto_id in range(1, 500):
        payload = {
            "titulo":         f"[TESTE] dept={dept_id} assunto={assunto_id}",
            "mensagem":       "Teste automático. Pode ignorar.",
            "tipo":           1,
            "departamentoId": dept_id,
            "assuntoId":      assunto_id,
            "responsavelId":  RESPONSAVEL_TEST,
            "solicitanteId":  SOLICITANTE_ID,
            "dtPrazo":        PRAZO,
        }
        try:
            r = requests.post(
                f"{SULTS_API}/chamado/ticket",
                json=payload,
                headers=HEADERS,
                timeout=10,
                verify=False,
            )
            if r.status_code in (200, 201):
                ticket_id = r.json().get("id", "?")
                print(f"  assuntoId={assunto_id} → SUCESSO! Chamado criado: #{ticket_id}")
                encontrado = True
                # Não para — queremos ver todos os assuntos válidos
            else:
                try:
                    err = r.json().get("error", r.text[:80])
                except Exception:
                    err = r.text[:80]
                # Só mostra erros que não sejam os esperados
                ignorar = [
                    "não pertence ao departamento",
                    "assuntoId",
                    "inválido ou esta inativo",
                    "não é do mesmo tipo",
                    "não tem permissão",
                ]
                if not any(s in err for s in ignorar):
                    print(f"  assuntoId={assunto_id} → Erro inesperado: {err}")
        except Exception as exc:
            print(f"  assuntoId={assunto_id} → Exception: {exc}")

    if not encontrado:
        print(f"  Nenhum assunto encontrado em 1-499 para dept={dept_id}.")
    print()

print("Investigação concluída.")
