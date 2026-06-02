import pickle

from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix
)

# =====================================================
# CARGAR DATASET
# =====================================================

with open("X.pkl", "rb") as f:
    X = pickle.load(f)

with open("y.pkl", "rb") as f:
    y = pickle.load(f)

print(f"Muestras: {len(X)}")
print(f"Etiquetas: {len(y)}")

# =====================================================
# DIVIDIR DATOS
# =====================================================

X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.2,
    random_state=42,
    stratify=y
)

# =====================================================
# ENTRENAR
# =====================================================

model = RandomForestClassifier(
    n_estimators=100,
    max_depth=10,
    random_state=42,
    n_jobs=-1
)

model.fit(X_train, y_train)

# =====================================================
# EVALUAR
# =====================================================

predictions = model.predict(X_test)

print("\nAccuracy:")
print(accuracy_score(y_test, predictions))

print("\nMatriz de confusión:")
print(confusion_matrix(y_test, predictions))

print("\nReporte:")
print(classification_report(y_test, predictions))

# =====================================================
# GUARDAR MODELO
# =====================================================

with open("model.pkl", "wb") as f:
    pickle.dump(model, f)

print("\nModelo guardado en model.pkl")