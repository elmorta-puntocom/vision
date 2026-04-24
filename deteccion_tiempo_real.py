from pathlib import Path
import sys

import cv2
import joblib
import mediapipe as mp
import numpy as np
import pandas as pd
import tensorflow as tf
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler


BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "model"
MODEL_PATH = BASE_DIR / "drowsy_cnn_model.keras"
SCALER_PATH = BASE_DIR / "scaler.save"
CSV_CANDIDATES = [BASE_DIR / "landmarks.csv", MODEL_DIR / "landmarks.csv"]

sys.path.insert(0, str(MODEL_DIR))
from data_collection import normalize_landmarks  # noqa: E402


def find_csv_path():
    for path in CSV_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("No se encontro landmarks.csv.")


def load_dataset(csv_path):
    df = pd.read_csv(csv_path, low_memory=False)

    if "label" not in df.columns:
        raise ValueError("El archivo landmarks.csv no contiene la columna 'label'.")

    y = pd.to_numeric(df["label"], errors="coerce")
    x = df.drop(columns=["label"]).apply(pd.to_numeric, errors="coerce")

    valid_mask = y.notna() & (~x.isna().any(axis=1))
    x = x.loc[valid_mask].astype("float32")
    y = y.loc[valid_mask].astype("int32")

    valid_labels = y.isin([0, 1])
    x = x.loc[valid_labels]
    y = y.loc[valid_labels]

    if x.empty:
        raise ValueError("No hay filas validas en landmarks.csv para entrenar.")

    class_counts = y.value_counts().to_dict()
    if len(class_counts) < 2:
        raise ValueError("Se necesitan muestras de ambas clases (0 y 1) para entrenar.")

    return x.to_numpy(dtype=np.float32), y.to_numpy(dtype=np.float32)


def build_model(input_dim):
    model = tf.keras.Sequential(
        [
            tf.keras.layers.Input(shape=(input_dim,)),
            tf.keras.layers.Dense(256, activation="relu"),
            tf.keras.layers.Dropout(0.30),
            tf.keras.layers.Dense(128, activation="relu"),
            tf.keras.layers.Dropout(0.20),
            tf.keras.layers.Dense(64, activation="relu"),
            tf.keras.layers.Dense(1, activation="sigmoid"),
        ]
    )
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=1e-3),
        loss="binary_crossentropy",
        metrics=["accuracy"],
    )
    return model


def train_and_save_assets():
    csv_path = find_csv_path()
    x, y = load_dataset(csv_path)

    scaler = StandardScaler()
    class_counts = pd.Series(y).value_counts().to_dict()
    min_class_count = min(class_counts.values())

    if len(x) >= 10 and min_class_count >= 2:
        x_train, x_val, y_train, y_val = train_test_split(
            x,
            y,
            test_size=0.2,
            random_state=42,
            stratify=y,
        )
        x_train_scaled = scaler.fit_transform(x_train).astype(np.float32)
        x_val_scaled = scaler.transform(x_val).astype(np.float32)
        validation_data = (x_val_scaled, y_val)
        validation_split = 0.0
    else:
        x_train_scaled = scaler.fit_transform(x).astype(np.float32)
        y_train = y
        validation_data = None
        validation_split = 0.2 if len(x_train_scaled) >= 10 else 0.0

    joblib.dump(scaler, SCALER_PATH)

    tf.keras.utils.set_random_seed(42)
    model = build_model(x_train_scaled.shape[1])
    callbacks = [
        tf.keras.callbacks.EarlyStopping(
            monitor="val_loss" if validation_data is not None or validation_split > 0 else "loss",
            patience=8,
            restore_best_weights=True,
        )
    ]

    model.fit(
        x_train_scaled,
        y_train,
        validation_data=validation_data,
        validation_split=validation_split,
        epochs=40,
        batch_size=32,
        verbose=1,
        callbacks=callbacks,
    )

    if validation_data is not None:
        loss, accuracy = model.evaluate(validation_data[0], validation_data[1], verbose=0)
        print(f"Modelo entrenado. Val loss: {loss:.4f} | Val accuracy: {accuracy:.4f}")
    else:
        loss, accuracy = model.evaluate(x_train_scaled, y_train, verbose=0)
        print(f"Modelo entrenado. Train loss: {loss:.4f} | Train accuracy: {accuracy:.4f}")
    model.save(MODEL_PATH)


def ensure_assets():
    if MODEL_PATH.exists() and SCALER_PATH.exists():
        return
    print("Modelo o scaler no encontrados. Entrenando desde landmarks.csv...")
    train_and_save_assets()


def draw_status(frame, estado, prob_somnolencia, face_ok, pose_ok):
    if not face_ok or not pose_ok:
        estado = "SIN LANDMARKS"
        color = (0, 165, 255)
    elif estado == "SOMNOLIENTO":
        color = (0, 0, 255)
    else:
        color = (0, 255, 0)

    cv2.putText(
        frame,
        f"Estado: {estado}",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        color,
        2,
    )
    cv2.putText(
        frame,
        f"Probabilidad: {prob_somnolencia:.2f}",
        (20, 80),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
    )


def run_realtime_detection():
    ensure_assets()

    model = tf.keras.models.load_model(MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise RuntimeError("No se pudo abrir la camara.")

    mp_holistic = mp.solutions.holistic
    mp_drawing = mp.solutions.drawing_utils

    with mp_holistic.Holistic(
        static_image_mode=False,
        model_complexity=1,
        enable_segmentation=False,
        refine_face_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    ) as holistic:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            results = holistic.process(rgb)
            rgb.flags.writeable = True

            prob_alerta = 0.5
            prob_somnolencia = 0.5
            estado = "ALERTA"

            if results.face_landmarks:
                mp_drawing.draw_landmarks(
                    frame,
                    results.face_landmarks,
                    mp.solutions.face_mesh.FACEMESH_TESSELATION,
                    mp_drawing.DrawingSpec(color=(80, 110, 10), thickness=1, circle_radius=1),
                    mp_drawing.DrawingSpec(color=(80, 256, 121), thickness=1),
                )

            if results.pose_landmarks:
                mp_drawing.draw_landmarks(
                    frame,
                    results.pose_landmarks,
                    mp_holistic.POSE_CONNECTIONS,
                )

            if results.face_landmarks and results.pose_landmarks:
                try:
                    values = normalize_landmarks(results.face_landmarks, results.pose_landmarks)
                    features = np.asarray(values, dtype=np.float32).reshape(1, -1)
                    features = scaler.transform(features).astype(np.float32)
                    prob_alerta = float(model.predict(features, verbose=0)[0][0])
                    prob_somnolencia = 1.0 - prob_alerta
                    estado = "ALERTA" if prob_alerta >= 0.5 else "SOMNOLIENTO"
                except Exception as exc:
                    cv2.putText(
                        frame,
                        f"Error: {str(exc)[:60]}",
                        (20, 120),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 165, 255),
                        2,
                    )

            draw_status(
                frame,
                estado,
                prob_somnolencia,
                results.face_landmarks is not None,
                results.pose_landmarks is not None,
            )

            cv2.imshow("Deteccion de Somnolencia", frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break

    cap.release()
    cv2.destroyAllWindows()


def main():
    run_realtime_detection()


if __name__ == "__main__":
    main()
