import serial
import cv2
import mediapipe as mp
import numpy as np
import tensorflow as tf
import joblib
from collections import deque

# Cargar modelo y scaler entrenados
model  = tf.keras.models.load_model("drowsy_cnn_model.keras")
scaler = joblib.load("scaler.save")

# Puerto serial al ESP32 (ajustar COM según Windows)
# ser = serial.Serial("COM3", 9600, timeout=1)

# Cuántos frames seguidos somnolientos antes de activar alarma
UMBRAL_FRAMES = 20   # ~1 segundo a 20fps
contador_somnoliento = 0

cap = cv2.VideoCapture(0)
mp_holistic = mp.solutions.holistic

with mp_holistic.Holistic(min_detection_confidence=0.5) as holistic:
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        frame = cv2.flip(frame, 1)
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        res   = holistic.process(rgb)
        
        if res.face_landmarks and res.pose_landmarks:
            # Extraer landmarks igual que en data_collection.py
            from data_collection import normalize_landmarks
            vals = normalize_landmarks(res.face_landmarks, res.pose_landmarks)
            
            X = np.array(vals).reshape(1, -1).astype("float32")
            X = scaler.transform(X)
            X = X.reshape((1, X.shape[1], 1))   # forma que espera Conv1D
            
            prob = model.predict(X, verbose=0)[0][0]
            somnoliento = prob < 0.5  # label 0 = drowsy, label 1 = alert
            
            if somnoliento:
                contador_somnoliento += 1
            else:
                contador_somnoliento = 0
                # ser.write(b"0")   # avisar al ESP32: conductor alerta
            
            if contador_somnoliento >= UMBRAL_FRAMES:
                # ser.write(b"1")   # ALARMA: activar buzzer y vibrador
                cv2.putText(frame, "SOMNOLENCIA DETECTADA", (30, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
        
        cv2.imshow("Vision", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

        estado = "ALERTA" if not somnoliento else "SOMNOLIENTO"

cv2.putText(frame, f"Estado: {estado}", (30, 50),
            cv2.FONT_HERSHEY_SIMPLEX, 1,
            (0,255,0) if not somnoliento else (0,0,255), 2)

cv2.putText(frame, f"Prob: {prob:.2f}", (30, 90),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,255,255), 2)

cv2.putText(frame, f"Frames somnoliento: {contador_somnoliento}", (30, 130),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,255,0), 2)

cap.release()
ser.close()
cv2.destroyAllWindows()

#sisaaaaaaaaaaaaaaaaaaaaa