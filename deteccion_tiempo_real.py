from collections import Counter, deque
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
from sklearn.utils.class_weight import compute_class_weight


BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "model"
MODEL_PATH = BASE_DIR / "drowsy_cnn_model.keras"
SCALER_PATH = BASE_DIR / "scaler.save"
CSV_CANDIDATES = [BASE_DIR / "landmarks.csv", MODEL_DIR / "landmarks.csv"]

# Labels used while collecting/training:
#   0 = somnolencia
#   1 = despierto
LABEL_SOMNOLENCIA = 0
LABEL_DESPIERTO = 1
CLASS_NAMES = {
    LABEL_SOMNOLENCIA: "Somnolencia",
    LABEL_DESPIERTO: "Despierto",
}

# Keep these constants explicit so the runtime feature contract is visible.
# The order must match model/data_collection.normalize_landmarks:
# face_0 xyz ... face_477 xyz, pose_0 xyzw ... pose_32 xyzw.
FACE_COUNT = 478
POSE_COUNT = 33
EXPECTED_FEATURES = FACE_COUNT * 3 + POSE_COUNT * 4

DESPIERTO_THRESHOLD = 0.50
SMOOTHING_WINDOW = 12
LOG_EVERY_N_FRAMES = 1

sys.path.insert(0, str(MODEL_DIR))
from model.data_collection import normalize_landmarks # noqa: E402


class PredictionSmoother:
    """Smooths predictions by averaging recent class probabilities."""

    def __init__(self, window_size=SMOOTHING_WINDOW):
        self.prob_history = deque(maxlen=window_size)

    def clear(self):
        self.prob_history.clear()

    def update(self, probabilities):
        self.prob_history.append(probabilities)
        avg = {
            LABEL_SOMNOLENCIA: float(np.mean([p[LABEL_SOMNOLENCIA] for p in self.prob_history])),
            LABEL_DESPIERTO: float(np.mean([p[LABEL_DESPIERTO] for p in self.prob_history])),
        }
        predicted_label = (
            LABEL_DESPIERTO
            if avg[LABEL_DESPIERTO] >= DESPIERTO_THRESHOLD
            else LABEL_SOMNOLENCIA
        )
        confidence = avg[predicted_label]
        return predicted_label, confidence, avg


class OutputInterpreter:
    """Converts model outputs into probabilities for labels 0 and 1."""

    def __init__(self, sigmoid_positive_label=LABEL_DESPIERTO, softmax_order=(LABEL_SOMNOLENCIA, LABEL_DESPIERTO)):
        self.sigmoid_positive_label = sigmoid_positive_label
        self.softmax_order = tuple(softmax_order)

    def probabilities_from_output(self, raw_output):
        output = np.asarray(raw_output, dtype=np.float32).reshape(-1)

        if output.size == 1:
            # The single sigmoid output is the probability of the class used as
            # positive during training. Older saved models can be inverted, so
            # this convention is calibrated from landmarks.csv when available.
            positive_prob = float(np.clip(output[0], 0.0, 1.0))
            negative_prob = 1.0 - positive_prob
            if self.sigmoid_positive_label == LABEL_DESPIERTO:
                prob_despierto = positive_prob
                prob_somnolencia = negative_prob
            else:
                prob_somnolencia = positive_prob
                prob_despierto = negative_prob
        elif output.size == 2:
            probs = output.astype(np.float32)
            if not np.isclose(float(np.sum(probs)), 1.0, atol=1e-3):
                exp = np.exp(probs - np.max(probs))
                probs = exp / np.sum(exp)

            prob_somnolencia = 0.0
            prob_despierto = 0.0
            for index, label in enumerate(self.softmax_order):
                if label == LABEL_SOMNOLENCIA:
                    prob_somnolencia = float(probs[index])
                elif label == LABEL_DESPIERTO:
                    prob_despierto = float(probs[index])
        else:
            raise ValueError(f"Salida de modelo no soportada: shape={output.shape}")

        return {
            LABEL_SOMNOLENCIA: prob_somnolencia,
            LABEL_DESPIERTO: prob_despierto,
        }


def find_csv_path():
    for path in CSV_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("No se encontro landmarks.csv.")


def read_landmarks_csv(csv_path):
    """Reads CSVs saved either with or without the header row."""
    df = pd.read_csv(csv_path, low_memory=False)
    if "label" in df.columns:
        return df

    df = pd.read_csv(csv_path, header=None, low_memory=False)
    columns = ["label"] + [f"feature_{i}" for i in range(df.shape[1] - 1)]
    df.columns = columns
    return df


def load_dataset(csv_path):
    df = read_landmarks_csv(csv_path)

    if "label" not in df.columns:
        raise ValueError("El archivo landmarks.csv no contiene la columna 'label'.")

    y = pd.to_numeric(df["label"], errors="coerce")
    x = df.drop(columns=["label"]).apply(pd.to_numeric, errors="coerce")

    valid_mask = y.notna() & (~x.isna().any(axis=1))
    x = x.loc[valid_mask].astype("float32")
    y = y.loc[valid_mask].astype("int32")

    valid_labels = y.isin([LABEL_SOMNOLENCIA, LABEL_DESPIERTO])
    x = x.loc[valid_labels]
    y = y.loc[valid_labels]

    if x.empty:
        raise ValueError("No hay filas validas en landmarks.csv para entrenar.")

    if x.shape[1] != EXPECTED_FEATURES:
        raise ValueError(
            f"landmarks.csv tiene {x.shape[1]} features, pero se esperaban "
            f"{EXPECTED_FEATURES}. Revisa FACE_COUNT/POSE_COUNT y el orden de captura."
        )

    class_counts = y.value_counts().to_dict()
    print(f"[DATASET] balance de clases: {class_counts} (0=Somnolencia, 1=Despierto)")
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


def make_class_weight(y_train):
    classes = np.array([LABEL_SOMNOLENCIA, LABEL_DESPIERTO], dtype=np.int32)
    weights = compute_class_weight(class_weight="balanced", classes=classes, y=y_train.astype(np.int32))
    return {int(label): float(weight) for label, weight in zip(classes, weights)}


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
    class_weight = make_class_weight(y_train)
    print(f"[DATASET] class_weight usado en entrenamiento: {class_weight}")

    model.fit(
        x_train_scaled,
        y_train,
        validation_data=validation_data,
        validation_split=validation_split,
        epochs=40,
        batch_size=32,
        verbose=1,
        callbacks=callbacks,
        class_weight=class_weight,
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
    print("[INIT] Modelo o scaler no encontrados. Entrenando desde landmarks.csv...")
    train_and_save_assets()


def get_model_input_shape(model):
    input_shape = model.input_shape
    if isinstance(input_shape, list):
        input_shape = input_shape[0]
    return tuple(input_shape)


def expected_feature_count_from_assets(model, scaler):
    model_shape = get_model_input_shape(model)
    model_dims = [dim for dim in model_shape[1:] if dim is not None]
    model_features = int(np.prod(model_dims)) if model_dims else None
    scaler_features = getattr(scaler, "n_features_in_", None)

    if scaler_features is not None and scaler_features != EXPECTED_FEATURES:
        raise ValueError(
            f"El scaler espera {scaler_features} features, pero el extractor produce "
            f"{EXPECTED_FEATURES}. Usa el mismo scaler del entrenamiento."
        )

    if model_features is not None and model_features != EXPECTED_FEATURES:
        raise ValueError(
            f"El modelo espera {model_features} features, pero el extractor produce "
            f"{EXPECTED_FEATURES}. Revisa el orden/cantidad de landmarks."
        )

    if scaler_features is not None:
        return int(scaler_features)
    if model_features is not None:
        return int(model_features)
    return EXPECTED_FEATURES


def load_runtime_assets():
    ensure_assets()
    try:
        model = tf.keras.models.load_model(MODEL_PATH)
    except Exception as exc:
        raise RuntimeError(f"No se pudo cargar el modelo TensorFlow: {MODEL_PATH}") from exc

    try:
        scaler = joblib.load(SCALER_PATH)
    except Exception as exc:
        raise RuntimeError(f"No se pudo cargar el scaler: {SCALER_PATH}") from exc

    expected_features = expected_feature_count_from_assets(model, scaler)
    print(f"[INIT] modelo: {MODEL_PATH}")
    print(f"[INIT] scaler: {SCALER_PATH}")
    print(f"[INIT] model.input_shape={get_model_input_shape(model)}")
    print(f"[INIT] scaler.n_features_in_={getattr(scaler, 'n_features_in_', None)}")
    print(f"[INIT] features esperadas={expected_features}")
    print("[INIT] labels: 0=Somnolencia, 1=Despierto")
    interpreter = calibrate_output_interpreter(model, scaler)
    return model, scaler, expected_features, interpreter


def extract_landmarks(results):
    """Returns normalized landmarks in the exact training order, or None."""
    if not results.face_landmarks or not results.pose_landmarks:
        return None

    face_count = len(results.face_landmarks.landmark)
    pose_count = len(results.pose_landmarks.landmark)
    if face_count != FACE_COUNT or pose_count != POSE_COUNT:
        print(
            f"[WARN] landmarks incompletos: face={face_count}/{FACE_COUNT}, "
            f"pose={pose_count}/{POSE_COUNT}"
        )
        return None

    values = normalize_landmarks(results.face_landmarks, results.pose_landmarks)
    if len(values) != EXPECTED_FEATURES:
        print(f"[WARN] extractor devolvio {len(values)} features; se esperaban {EXPECTED_FEATURES}")
        return None

    return values


def reshape_for_model(features, model):
    model_shape = get_model_input_shape(model)
    target_shape = model_shape[1:]

    # Dense models expect (batch, features). Some older CNN variants may expect
    # (batch, features, 1); support both without changing feature order.
    if len(target_shape) == 1:
        return features

    target_dims = []
    unknown_dims = 0
    known_product = 1
    for dim in target_shape:
        if dim is None:
            target_dims.append(None)
            unknown_dims += 1
        else:
            value = int(dim)
            target_dims.append(value)
            known_product *= value

    if unknown_dims > 1:
        raise ValueError(f"Shape del modelo demasiado ambiguo para inferencia: {model_shape}")

    if unknown_dims == 1:
        if features.shape[1] % known_product != 0:
            raise ValueError(
                f"No se puede inferir dimension faltante para input {features.shape} "
                f"y modelo {model_shape}."
            )
        missing_dim = features.shape[1] // known_product
        target_dims = [missing_dim if dim is None else dim for dim in target_dims]

    if int(np.prod(target_dims)) != features.shape[1]:
        raise ValueError(
            f"No se puede adaptar input {features.shape} al shape del modelo {model_shape}."
        )

    return features.reshape((features.shape[0], *target_dims)).astype(np.float32)


def preprocess_landmarks(values, scaler, model, expected_features):
    features = np.asarray(values, dtype=np.float32).reshape(1, -1)
    if features.shape[1] != expected_features:
        raise ValueError(
            f"Shape invalido: {features.shape}. Se esperaban {expected_features} features."
        )

    # Apply exactly the scaler saved during training before calling TensorFlow.
    scaled = scaler.transform(features).astype(np.float32)
    model_input = reshape_for_model(scaled, model)
    return model_input


def format_counts(values):
    return {int(label): int(count) for label, count in Counter(values).items()}


def calibrate_output_interpreter(model, scaler):
    """Detects whether the saved model output is inverted against CSV labels."""
    default_interpreter = OutputInterpreter()

    try:
        csv_path = find_csv_path()
        x, y = load_dataset(csv_path)
        x_scaled = scaler.transform(x).astype(np.float32)
        model_input = reshape_for_model(x_scaled, model)
        raw_outputs = model.predict(model_input, verbose=0)
    except Exception as exc:
        print(f"[WARN] No se pudo calibrar interpretacion de salida: {exc}")
        return default_interpreter

    raw_outputs = np.asarray(raw_outputs)
    y_int = y.astype(np.int32)

    if raw_outputs.ndim == 1 or raw_outputs.shape[-1] == 1:
        raw = raw_outputs.reshape(-1)
        direct_pred = np.where(raw >= DESPIERTO_THRESHOLD, LABEL_DESPIERTO, LABEL_SOMNOLENCIA)
        inverted_pred = np.where(raw >= DESPIERTO_THRESHOLD, LABEL_SOMNOLENCIA, LABEL_DESPIERTO)
        direct_accuracy = float(np.mean(direct_pred == y_int))
        inverted_accuracy = float(np.mean(inverted_pred == y_int))

        print(
            "[CALIBRACION] sigmoid como P(Despierto): "
            f"accuracy={direct_accuracy:.4f}, pred_counts={format_counts(direct_pred)}"
        )
        print(
            "[CALIBRACION] sigmoid como P(Somnolencia): "
            f"accuracy={inverted_accuracy:.4f}, pred_counts={format_counts(inverted_pred)}"
        )

        if inverted_accuracy > direct_accuracy + 0.05:
            print("[CALIBRACION] Se usara sigmoid=P(Somnolencia); el modelo/labels parecen invertidos.")
            return OutputInterpreter(sigmoid_positive_label=LABEL_SOMNOLENCIA)

        print("[CALIBRACION] Se usara sigmoid=P(Despierto).")
        return default_interpreter

    if raw_outputs.shape[-1] == 2:
        direct_pred = np.argmax(raw_outputs, axis=1)
        inverted_pred = np.where(direct_pred == LABEL_DESPIERTO, LABEL_SOMNOLENCIA, LABEL_DESPIERTO)
        direct_accuracy = float(np.mean(direct_pred == y_int))
        inverted_accuracy = float(np.mean(inverted_pred == y_int))

        print(
            "[CALIBRACION] softmax orden [Somnolencia, Despierto]: "
            f"accuracy={direct_accuracy:.4f}, pred_counts={format_counts(direct_pred)}"
        )
        print(
            "[CALIBRACION] softmax orden [Despierto, Somnolencia]: "
            f"accuracy={inverted_accuracy:.4f}, pred_counts={format_counts(inverted_pred)}"
        )

        if inverted_accuracy > direct_accuracy + 0.05:
            print("[CALIBRACION] Se usara softmax orden [Despierto, Somnolencia].")
            return OutputInterpreter(softmax_order=(LABEL_DESPIERTO, LABEL_SOMNOLENCIA))

    return default_interpreter


def infer_state(model, model_input, interpreter):
    raw_output = model.predict(model_input, verbose=0)
    probabilities = interpreter.probabilities_from_output(raw_output)
    predicted_label = (
        LABEL_DESPIERTO
        if probabilities[LABEL_DESPIERTO] >= DESPIERTO_THRESHOLD
        else LABEL_SOMNOLENCIA
    )
    confidence = probabilities[predicted_label]
    return predicted_label, confidence, probabilities, raw_output


def log_prediction(frame_index, input_shape, probabilities, raw_label, raw_confidence, smooth_label, smooth_confidence):
    if frame_index % LOG_EVERY_N_FRAMES != 0:
        return

    print(
        "[PRED] "
        f"input_shape={input_shape} | "
        f"probabilidades={{Somnolencia: {probabilities[LABEL_SOMNOLENCIA]:.4f}, "
        f"Despierto: {probabilities[LABEL_DESPIERTO]:.4f}}} | "
        f"clase_modelo={CLASS_NAMES[raw_label]} | confianza_modelo={raw_confidence:.4f} | "
        f"clase_suavizada={CLASS_NAMES[smooth_label]} | confianza_suavizada={smooth_confidence:.4f}"
    )


def draw_landmarks(frame, results, mp_drawing, mp_holistic):
    if results.face_landmarks:
        mp_drawing.draw_landmarks(
            frame,
            results.face_landmarks,
            mp.solutions.face_mesh.FACEMESH_TESSELATION,
            mp_drawing.DrawingSpec(color=(80, 110, 10), thickness=1, circle_radius=1),
            mp_drawing.DrawingSpec(color=(80, 255, 121), thickness=1),
        )

    if results.pose_landmarks:
        mp_drawing.draw_landmarks(
            frame,
            results.pose_landmarks,
            mp_holistic.POSE_CONNECTIONS,
        )


def render_status(frame, estado, confidence=None, probabilities=None):
    if estado == "No hay landmarks":
        color = (0, 165, 255)
    elif estado == "Somnolencia":
        color = (0, 0, 255)
    else:
        color = (0, 255, 0)

    cv2.putText(
        frame,
        estado,
        (30, 55),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.4,
        color,
        3,
    )

    if probabilities is None:
        detail = "Sin rostro/pose detectados"
    else:
        detail = (
            f"Somn: {probabilities[LABEL_SOMNOLENCIA]:.2f} | "
            f"Desp: {probabilities[LABEL_DESPIERTO]:.2f} | "
            f"Conf: {confidence:.2f}"
        )

    cv2.putText(
        frame,
        detail,
        (30, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
    )


def open_camera(camera_index=0):
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"No se pudo abrir la camara con indice {camera_index}.")
    return cap


def run_realtime_detection():
    model, scaler, expected_features, interpreter = load_runtime_assets()
    cap = open_camera(0)
    smoother = PredictionSmoother()

    mp_holistic = mp.solutions.holistic
    mp_drawing = mp.solutions.drawing_utils
    frame_index = 0

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
            if not ret or frame is None:
                print("[CAMARA] No se pudo leer un frame. Cerrando deteccion.")
                break

            frame_index += 1
            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            results = holistic.process(rgb)
            rgb.flags.writeable = True

            draw_landmarks(frame, results, mp_drawing, mp_holistic)

            estado = "No hay landmarks"
            confidence = None
            probabilities = None
            values = extract_landmarks(results)

            if values is None:
                # Do not reuse stale predictions when the face/pose is gone.
                smoother.clear()
                render_status(frame, estado)
            else:
                try:
                    model_input = preprocess_landmarks(values, scaler, model, expected_features)
                    raw_label, raw_confidence, probabilities, _ = infer_state(model, model_input, interpreter)
                    smooth_label, confidence, smooth_probabilities = smoother.update(probabilities)
                    estado = CLASS_NAMES[smooth_label]
                    render_status(frame, estado, confidence, smooth_probabilities)
                    log_prediction(
                        frame_index,
                        model_input.shape,
                        probabilities,
                        raw_label,
                        raw_confidence,
                        smooth_label,
                        confidence,
                    )
                except Exception as exc:
                    smoother.clear()
                    print(f"[ERROR] Inferencia omitida: {exc}")
                    render_status(frame, "No hay landmarks")

            cv2.imshow("Deteccion de Somnolencia", frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break

    cap.release()
    cv2.destroyAllWindows()


def main():
    try:
        run_realtime_detection()
    except Exception as exc:
        print(f"[FATAL] {exc}")
        raise


if __name__ == "__main__":
    main()
