import cv2
import mediapipe as mp
import numpy as np
import tensorflow as tf
import joblib
import sys
import os

# Ajustar paths según desde donde lo corrás
sys.path.insert(0, os.path.dirname(__file__))
from data_collection import normalize_landmarks

model  = tf.keras.models.load_model("drowsy_cnn_model.keras")
scaler = joblib.load("scaler.save")

UMBRAL_FRAMES = 20
contador = 0
prob = 0.5

cap = cv2.VideoCapture(0)
mp_holistic = mp.solutions.holistic

with mp_holistic.Holistic(min_detection_confidence=0.5, min_tracking_confidence=0.5) as holistic:
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        res = holistic.process(rgb)
        rgb.flags.writeable = True

        somnoliento = False

        if res.face_landmarks and res.pose_landmarks:
            try:
                vals = normalize_landmarks(res.face_landmarks, res.pose_landmarks)
                X = np.array(vals).reshape(1, -1).astype("float32")
                X = scaler.transform(X)
                X = X.reshape((1, X.shape[1], 1))
                prob = model.predict(X, verbose=0)[0][0]
                somnoliento = prob < 0.5  # 0 = dormido, 1 = despierto

                if somnoliento:
                    contador += 1
                else:
                    contador = 0
            except Exception as e:
                print("Error:", e)

        # Texto principal
        if not res.face_landmarks:
            estado = "SIN ROSTRO"
            color  = (128, 128, 128)
        elif somnoliento and contador >= UMBRAL_FRAMES:
            estado = "DORMIDO"
            color  = (0, 0, 255)
        else:
            estado = "DESPIERTO"
            color  = (0, 255, 0)

        cv2.putText(frame, estado, (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.8, color, 3)
        cv2.putText(frame, f"prob: {prob:.2f}  frames: {contador}", (30, 110),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        cv2.imshow("Vision — Test", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

cap.release()
cv2.destroyAllWindows()