#!/usr/bin/env python3
"""
NeuroGuard — Diagnóstico de actividades en Firestore
====================================================
Muestra todas las actividades del paciente y verifica si la query
de supresión funciona correctamente.

Uso:
  python scripts/debug_activities.py [patient_id]

Ejemplo:
  python scripts/debug_activities.py paciente_001
"""

import sys
import os
from pathlib import Path
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv(Path(__file__).parent.parent / ".env")

CREDS_PATH = os.getenv(
    "FIREBASE_CREDENTIALS_PATH",
    str(Path(__file__).parent.parent / "firebase-credentials.json"),
)

PATIENT_ID = sys.argv[1] if len(sys.argv) > 1 else "paciente_001"

def main():
    if not firebase_admin._apps:
        cred = credentials.Certificate(CREDS_PATH)
        firebase_admin.initialize_app(cred)

    db = firestore.client()

    print(f"\n{'='*60}")
    print(f"  Diagnóstico de actividades — paciente: {PATIENT_ID}")
    print(f"{'='*60}\n")

    acts_ref = (
        db.collection("patients")
        .document(PATIENT_ID)
        .collection("activities")
    )

    # ── 1. Listar TODAS las actividades ────────────────────────────────
    all_docs = acts_ref.order_by("start_timestamp", direction=firestore.Query.DESCENDING).limit(10).get()
    print(f"[1] Últimas {len(all_docs)} actividades en Firestore:")
    if not all_docs:
        print("    ⚠️  NO HAY ACTIVIDADES. Debes crear una en la app del paciente.")
    for doc in all_docs:
        d = doc.to_dict()
        print(f"\n    ID: {doc.id}")
        for k, v in d.items():
            print(f"      {k}: {repr(v)}")

    # ── 2. Simular la query exacta del backend ─────────────────────────
    print(f"\n[2] Query del backend: .where('end_timestamp', '==', None)")
    null_docs = acts_ref.where("end_timestamp", "==", None).limit(5).get()
    print(f"    Documentos encontrados con end_timestamp == null: {len(null_docs)}")

    if null_docs:
        print("    ✅ La query SÍ encuentra actividades activas:")
        for doc in null_docs:
            d = doc.to_dict()
            print(f"      → ID={doc.id}  type={d.get('type')}  can_suppress={d.get('can_suppress')}")
    else:
        print("    ❌ La query NO encuentra ninguna actividad activa.")
        print("    Posibles causas:")
        print("      a) No hay actividad iniciada en la app")
        print("      b) La actividad fue finalizada (end_timestamp ya no es null)")
        print("      c) El campo se guarda de otra forma (ver listado de arriba)")

    # ── 3. Verificar campo end_timestamp de cada doc ───────────────────
    print(f"\n[3] Detalle del campo end_timestamp por documento:")
    all_docs2 = acts_ref.limit(10).get()
    for doc in all_docs2:
        d = doc.to_dict()
        et = d.get("end_timestamp", "CAMPO_NO_EXISTE")
        is_null = et is None
        field_exists = "end_timestamp" in d
        print(f"    ID={doc.id[:10]}... | end_timestamp={'null' if is_null else repr(et)} | campo_existe={field_exists}")

    print(f"\n{'='*60}\n")

if __name__ == "__main__":
    main()
