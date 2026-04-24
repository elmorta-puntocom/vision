#!/usr/bin/env python3
"""
Data collection script for drowsy/not-drowsy landmark logging.
Usage: run this script, it opens your webcam and shows frames.
Press:
  - '0' -> save current landmarks with label 0 (drowsy)
  - '1' -> save current landmarks with label 1 (not drowsy)
  - 'q' or ESC -> quit
Each saved row is appended to a CSV file `landmarks.csv` in the working directory.

We collect both face and pose landmarks if available and flatten them in a fixed order.
If landmarks are missing for a frame, it will not be logged.
"""

import csv
import os

import cv2
import mediapipe as mp
import numpy as np


CSV_FILE = "landmarks.csv"
SHOW_POSE = True
SHOW_FACE = True
FACE_COUNT = 478
POSE_COUNT = 33


def normalize_landmarks(face_landmarks, pose_landmarks):
    """Returns a flat list of normalized face + pose landmarks."""
    if face_landmarks is None or pose_landmarks is None:
        raise ValueError("Missing face or pose landmarks")

    face = np.array([[lm.x, lm.y, lm.z] for lm in face_landmarks.landmark], dtype=np.float32)
    pose = np.array(
        [[lm.x, lm.y, lm.z, getattr(lm, "visibility", 0.0)] for lm in pose_landmarks.landmark],
        dtype=np.float32,
    )

    ref = face[1, :2].copy()

    try:
        left_eye = face[33, :2]
        right_eye = face[263, :2]
        eye_dist = np.linalg.norm(left_eye - right_eye)
        scale = eye_dist if eye_dist >= 1e-6 else 1.0
    except Exception:
        scale = 1.0

    face_xy = (face[:, :2] - ref) / scale
    face_z = face[:, 2:] - np.mean(face[:, 2:])
    face_flat = np.hstack([face_xy, face_z]).flatten().tolist()

    pose_xy = (pose[:, :2] - ref) / scale
    pose_z = pose[:, 2:3] - np.mean(pose[:, 2:3])
    pose_v = pose[:, 3:4]
    pose_flat = np.hstack([pose_xy, pose_z, pose_v]).flatten().tolist()

    return face_flat + pose_flat


def comp_brightness(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return np.mean(gray)


def adjust_brightness(image, target_brightness=130):
    current_brightness = comp_brightness(image)
    ratio = target_brightness / (current_brightness + 1e-5)
    return cv2.convertScaleAbs(image, alpha=ratio, beta=0)


def make_header():
    header = []
    for i in range(FACE_COUNT):
        header += [f"face_{i}_x", f"face_{i}_y", f"face_{i}_z"]
    for i in range(POSE_COUNT):
        header += [f"pose_{i}_x", f"pose_{i}_y", f"pose_{i}_z", f"pose_{i}_v"]
    return ["label"] + header


def ensure_csv_exists(csv_path):
    if os.path.exists(csv_path):
        return
    with open(csv_path, mode="w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(make_header())
    print(f"Created {csv_path} with header")


def main():
    os.makedirs("captures/orig", exist_ok=True)
    os.makedirs("captures/adjusted", exist_ok=True)
    ensure_csv_exists(CSV_FILE)

    mp_drawing = mp.solutions.drawing_utils
    mp_holistic = mp.solutions.holistic

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise RuntimeError("Could not open webcam")

    with mp_holistic.Holistic(
        static_image_mode=False,
        model_complexity=1,
        enable_segmentation=False,
        refine_face_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    ) as holistic:
        print("Started webcam. Press 0 or 1 to log, q to quit.")
        frame_id = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                print("Failed to grab frame")
                break

            frame = cv2.flip(frame, 1)
            image_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image_rgb.flags.writeable = False
            results = holistic.process(image_rgb)
            image_rgb.flags.writeable = True

            adjusted = adjust_brightness(frame, target_brightness=130)
            annotated = adjusted.copy()

            if SHOW_FACE and results.face_landmarks:
                mp_drawing.draw_landmarks(
                    annotated,
                    results.face_landmarks,
                    mp.solutions.face_mesh.FACEMESH_TESSELATION,
                    mp_drawing.DrawingSpec(color=(80, 110, 10), thickness=1, circle_radius=1),
                    mp_drawing.DrawingSpec(color=(80, 256, 121), thickness=1),
                )

            if SHOW_POSE and results.pose_landmarks:
                mp_drawing.draw_landmarks(
                    annotated,
                    results.pose_landmarks,
                    mp_holistic.POSE_CONNECTIONS,
                )

            cv2.putText(
                annotated,
                "Press 0=drowsy, 1=not drowsy, q=quit",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                2,
            )
            cv2.imshow("drowsy-data-collector", annotated)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break

            if key not in (ord("0"), ord("1")):
                continue

            label = 0 if key == ord("0") else 1

            try:
                vals = normalize_landmarks(results.face_landmarks, results.pose_landmarks)
            except Exception as exc:
                print("Skipping save - incomplete landmarks:", exc)
                continue

            row = [label] + vals
            orig_path = f"captures/orig/frame_{frame_id}.jpg"
            adj_path = f"captures/adjusted/frame_{frame_id}.jpg"
            cv2.imwrite(orig_path, frame)
            cv2.imwrite(adj_path, adjusted)

            with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(row)

            frame_id += 1
            print(f"Logged sample label={label}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
