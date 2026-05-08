"""Manual webcam test that reuses the production realtime detector logic."""

from pathlib import Path
import sys

import cv2
import mediapipe as mp
import time


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from deteccion_tiempo_real import (  # noqa: E402
    LABEL_DESPIERTO,
    PredictionSmoother,
    EyeClosureTracker,
    calculate_eye_aspect_ratio,
    combine_signals,
    empty_probabilities,
    estimate_head_pitch,
    extract_landmarks,
    infer_state,
    load_runtime_assets,
    preprocess_landmarks,
    render_status,
)


def main():
    model, scaler, expected_features, interpreter = load_runtime_assets()
    smoother = PredictionSmoother()
    eye_tracker = EyeClosureTracker()

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise RuntimeError("No se pudo abrir la camara.")

    mp_holistic = mp.solutions.holistic
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
                break

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            results = holistic.process(rgb)
            rgb.flags.writeable = True

            now = time.monotonic()
            face_detected = results.face_landmarks is not None
            ear_result = calculate_eye_aspect_ratio(results.face_landmarks) if face_detected else None
            eye_evidence = eye_tracker.update(ear_result, now) if face_detected else eye_tracker.update(None, now)
            head_pitch = estimate_head_pitch(results.face_landmarks, frame.shape) if face_detected else None

            if not face_detected:
                smoother.clear()
                render_status(
                    frame,
                    "No hay rostro",
                    probabilities=empty_probabilities(),
                    eye_evidence=eye_evidence,
                    head_pitch=head_pitch,
                    model_available=False,
                )
            else:
                values = extract_landmarks(results)
                if values is None:
                    smoother.clear()
                    decision = combine_signals(
                        LABEL_DESPIERTO,
                        0.0,
                        empty_probabilities(),
                        eye_evidence,
                    )
                    render_status(
                        frame,
                        decision.state,
                        decision.confidence,
                        empty_probabilities(),
                        eye_evidence,
                        head_pitch,
                        model_available=False,
                    )
                else:
                    model_input = preprocess_landmarks(values, scaler, model, expected_features)
                    _, _, probabilities, _ = infer_state(model, model_input, interpreter)
                    smooth_label, smooth_confidence, smooth_probabilities = smoother.update(probabilities)
                    decision = combine_signals(
                        smooth_label,
                        smooth_confidence,
                        smooth_probabilities,
                        eye_evidence,
                    )
                    render_status(
                        frame,
                        decision.state,
                        decision.confidence,
                        smooth_probabilities,
                        eye_evidence,
                        head_pitch,
                        model_available=True,
                    )

            cv2.imshow("Vision - Test hibrido", frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
