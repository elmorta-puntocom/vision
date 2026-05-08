"""Biometric measurements derived directly from MediaPipe landmarks."""

from dataclasses import dataclass

import cv2
import numpy as np


# MediaPipe Face Mesh eye contour points used by the standard EAR formula.
# The order is: horizontal outer/inner corners, then two upper/lower pairs.
LEFT_EYE_EAR = (33, 133, 160, 144, 158, 153)
RIGHT_EYE_EAR = (362, 263, 385, 380, 387, 373)

# Stable facial points commonly used for a coarse solvePnP head-pose estimate.
HEAD_POSE_POINTS = (1, 152, 33, 263, 61, 291)

# Approximate 3D face model in millimeters. It is only used to display a
# pitch-like angle and to help debug cabeceo; the TensorFlow signal remains the
# main existing posture/cabeceo detector.
HEAD_POSE_MODEL_POINTS = np.array(
    [
        (0.0, 0.0, 0.0),          # Nose tip
        (0.0, -63.6, -12.5),      # Chin
        (-43.3, 32.7, -26.0),     # Left eye outer corner
        (43.3, 32.7, -26.0),      # Right eye outer corner
        (-28.9, -28.9, -24.1),    # Left mouth corner
        (28.9, -28.9, -24.1),     # Right mouth corner
    ],
    dtype=np.float64,
)


@dataclass(frozen=True)
class EyeAspectRatio:
    left: float
    right: float
    average: float


def _landmark_xy(face_landmarks, index):
    landmark = face_landmarks.landmark[index]
    return np.array([landmark.x, landmark.y], dtype=np.float32)


def _distance(point_a, point_b):
    return float(np.linalg.norm(point_a - point_b))


def _single_eye_ear(face_landmarks, indices):
    outer, inner, upper_1, lower_1, upper_2, lower_2 = indices
    horizontal = _distance(_landmark_xy(face_landmarks, outer), _landmark_xy(face_landmarks, inner))
    if horizontal <= 1e-6:
        return None

    vertical_1 = _distance(_landmark_xy(face_landmarks, upper_1), _landmark_xy(face_landmarks, lower_1))
    vertical_2 = _distance(_landmark_xy(face_landmarks, upper_2), _landmark_xy(face_landmarks, lower_2))
    return (vertical_1 + vertical_2) / (2.0 * horizontal)


def calculate_eye_aspect_ratio(face_landmarks):
    """Return EAR for both eyes and their average, or None if landmarks are invalid."""
    if face_landmarks is None:
        return None

    landmark_count = len(face_landmarks.landmark)
    required_index = max(max(LEFT_EYE_EAR), max(RIGHT_EYE_EAR))
    if landmark_count <= required_index:
        return None

    left = _single_eye_ear(face_landmarks, LEFT_EYE_EAR)
    right = _single_eye_ear(face_landmarks, RIGHT_EYE_EAR)
    if left is None or right is None:
        return None

    return EyeAspectRatio(left=float(left), right=float(right), average=float((left + right) / 2.0))


def _image_point(face_landmarks, index, width, height):
    landmark = face_landmarks.landmark[index]
    return (float(landmark.x * width), float(landmark.y * height))


def estimate_head_pitch(face_landmarks, frame_shape):
    """Estimate head pitch in degrees from face landmarks.

    This value is intended for debugging/visualization and should not replace
    the already trained posture/cabeceo model signal.
    """
    if face_landmarks is None or frame_shape is None:
        return None

    landmark_count = len(face_landmarks.landmark)
    if landmark_count <= max(HEAD_POSE_POINTS):
        return None

    height, width = frame_shape[:2]
    if width <= 0 or height <= 0:
        return None

    image_points = np.array(
        [_image_point(face_landmarks, index, width, height) for index in HEAD_POSE_POINTS],
        dtype=np.float64,
    )

    focal_length = float(width)
    camera_matrix = np.array(
        [
            [focal_length, 0.0, width / 2.0],
            [0.0, focal_length, height / 2.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)

    success, rotation_vector, _ = cv2.solvePnP(
        HEAD_POSE_MODEL_POINTS,
        image_points,
        camera_matrix,
        dist_coeffs,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not success:
        return None

    rotation_matrix, _ = cv2.Rodrigues(rotation_vector)
    euler_angles = cv2.RQDecomp3x3(rotation_matrix)[0]
    pitch = float(euler_angles[0])

    # Some OpenCV builds expose the value normalized; keep displayed units sane.
    if abs(pitch) <= 1.0:
        pitch *= 360.0

    return float(np.clip(pitch, -90.0, 90.0))
