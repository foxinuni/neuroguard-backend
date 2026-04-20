#!/usr/bin/env python3
"""
NeuroGuard — Setup de autenticación Firebase
=============================================

Crea un usuario en Firebase Authentication y su documento vinculante
en Firestore (colección `users`).

Uso:
  python setup_auth.py

Requisitos:
  - firebase-credentials.json en la misma carpeta
  - pip install firebase-admin python-dotenv
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import auth, credentials, firestore

load_dotenv(Path(__file__).parent / ".env")

FIREBASE_CREDS = os.getenv(
    "FIREBASE_CREDENTIALS_PATH",
    str(Path(__file__).parent / "firebase-credentials.json"),
)

# ── Datos del usuario a crear ─────────────────────────────────────────────────
USER_EMAIL    = "paciente001@neuroguard.com"
USER_PASSWORD = "NeuroGuard2026!"
DISPLAY_NAME  = "Paciente Demo"
PATIENT_ID    = "paciente_001"


def main():
    if not Path(FIREBASE_CREDS).exists():
        print(f"ERROR: No se encontró {FIREBASE_CREDS}")
        sys.exit(1)

    # Inicializar Firebase Admin
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_CREDS)
        firebase_admin.initialize_app(cred)

    db = firestore.client()

    # ── 1. Crear usuario en Firebase Authentication ───────────────────────────
    try:
        user = auth.create_user(
            email=USER_EMAIL,
            password=USER_PASSWORD,
            display_name=DISPLAY_NAME,
        )
        print(f"✓ Usuario creado en Firebase Auth")
        print(f"  UID:   {user.uid}")
        print(f"  Email: {user.email}")
    except auth.EmailAlreadyExistsError:
        user = auth.get_user_by_email(USER_EMAIL)
        print(f"⚠ Usuario ya existía en Firebase Auth")
        print(f"  UID:   {user.uid}")
        print(f"  Email: {user.email}")

    # ── 2. Crear documento en Firestore: users/{uid} ─────────────────────────
    user_doc = {
        "email":        USER_EMAIL,
        "display_name": DISPLAY_NAME,
        "patient_id":   PATIENT_ID,
        "role":         "patient",
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }

    db.collection("users").document(user.uid).set(user_doc, merge=True)
    print(f"✓ Documento users/{user.uid} creado en Firestore")

    # ── 3. Asegurar que el documento del paciente tiene los campos base ──────
    db.collection("patients").document(PATIENT_ID).set(
        {
            "patient_id":    PATIENT_ID,
            "name":          DISPLAY_NAME,
            "epilepsy_type": "Epilepsia generalizada tónico-clónica",
            "basal_hr":      72.0,
        },
        merge=True,
    )
    print(f"✓ Documento patients/{PATIENT_ID} asegurado en Firestore")

    # ── Resumen ───────────────────────────────────────────────────────────────
    print()
    print("═" * 50)
    print("  Credenciales para el Dashboard:")
    print(f"  Email:    {USER_EMAIL}")
    print(f"  Password: {USER_PASSWORD}")
    print("═" * 50)


if __name__ == "__main__":
    main()
