from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import zipfile

import cv2
import joblib
import mediapipe as mp
import numpy as np
import pandas as pd
import requests
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
LOG_EVERY_N_FRAMES = 10

# Head nod detection is intentionally conservative. The TensorFlow model can be
# noisy on frontal faces, so cabeceo requires model confidence, pitch movement,
# and time instead of a single-frame prediction.
HEAD_DROWSY_PROB_THRESHOLD = 0.78
HEAD_PITCH_DELTA_THRESHOLD = 18.0
HEAD_DROWSY_SECONDS_THRESHOLD = 1.20
HEAD_NEUTRAL_ALPHA = 0.04

# Eye closure thresholds. The closed-eye alert is fast because in driving a
# closure longer than a normal blink already matters.
EAR_CLOSED_THRESHOLD = 0.22
EAR_PARTIAL_THRESHOLD = 0.28
EYE_CLOSED_SECONDS_THRESHOLD = 0.50
EYE_PARTIAL_SECONDS_THRESHOLD = 1.20
NORMAL_BLINK_MAX_SECONDS = 0.30

# Cambia esta IP por la que imprime el ESP32 en el Monitor Serial.
# Tambien puedes configurarla sin editar el archivo:
#   PowerShell: $env:VISION_ESP32_IP="192.168.2.130"
ESP32_IP = os.getenv("VISION_ESP32_IP", "192.168.2.112")
ESP32_TIMEOUT_SECONDS = 0.8
VISION_SERVER_URL = os.getenv("VISION_SERVER_URL", "http://127.0.0.1:5050").rstrip("/")
VISION_API_KEY = os.getenv("VISION_API_KEY", "vision-internal-key")
VISION_DEVICE_ID = os.getenv("VISION_DEVICE_ID", "ESP32-657593")
DETECTION_REPORT_COOLDOWN_SECONDS = 20.0

BLACKBOX_PRE_EVENT_SECONDS = 10.0
BLACKBOX_CONFIRMATION_SECONDS = 0.75
BLACKBOX_POST_EVENT_SECONDS = 10.0
BLACKBOX_MIN_EVENT_SECONDS = 10.0
BLACKBOX_OUTPUT_DIR = BASE_DIR / "evidencias"
BLACKBOX_QUEUE_MAX_FRAMES = int(os.getenv("VISION_BLACKBOX_QUEUE_MAX_FRAMES", "900"))
DEFAULT_RECORDING_FPS = 20.0

sys.path.insert(0, str(MODEL_DIR))
from model.biometrics import calculate_eye_aspect_ratio, estimate_head_pitch # noqa: E402
from model.data_collection import normalize_landmarks # noqa: E402


def _enviar_peticion_esp32(ruta):
    url = f"http://{ESP32_IP}{ruta}"

    try:
        respuesta = requests.get(url, timeout=ESP32_TIMEOUT_SECONDS)
        respuesta.raise_for_status()
        print(f"[ESP32] OK {ruta}: {respuesta.text}")
        return True
    except requests.exceptions.ConnectionError:
        print(f"[ESP32] No se pudo conectar con {url}")
    except requests.exceptions.Timeout:
        print(f"[ESP32] Timeout al conectar con {url}")
    except requests.exceptions.RequestException as exc:
        print(f"[ESP32] Error HTTP en {url}: {exc}")

    return False


def enviar_alerta_on():
    """Activa motor vibrador y buzzer en el ESP32."""
    return _enviar_peticion_esp32("/alerta_on")


def enviar_alerta_off():
    """Apaga motor vibrador y buzzer en el ESP32."""
    return _enviar_peticion_esp32("/alerta_off")


class Esp32AlarmController:
    """Envia ordenes al ESP32 solo cuando cambia el estado de somnolencia."""

    def __init__(self):
        self.alarma_activada = False
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._executor.submit(enviar_alerta_off)

    def update(self, somnolencia_detectada):
        if somnolencia_detectada == self.alarma_activada:
            return

        self.alarma_activada = somnolencia_detectada
        accion = enviar_alerta_on if somnolencia_detectada else enviar_alerta_off
        self._executor.submit(accion)

    def apagar_y_cerrar(self):
        self._executor.shutdown(wait=True, cancel_futures=False)
        if self.alarma_activada:
            enviar_alerta_off()
            self.alarma_activada = False


def _float_or_none(value):
    if value is None:
        return None
    return float(value)


def tipo_evento_from_decision(decision):
    if decision.eye_drowsy and decision.head_drowsy:
        return "Ambos"
    if decision.eye_drowsy:
        return "Ojos Cerrados"
    if decision.head_drowsy:
        return "Cabeceo"
    return None


@dataclass
class RecordedDrowsinessEvent:
    tipo_evento: str
    video_path: str
    valor_ear: float | None
    valor_pitch: float | None
    duracion_alerta: float
    estado: str
    confianza: float | None
    video_seconds: float


class DetectionReporter:
    """Registra en el backend un evento por episodio de somnolencia."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._active_reported = False
        self._last_report_at = 0.0

    def update(self, somnolencia_detectada, decision, eye_evidence, head_evidence):
        if not somnolencia_detectada:
            self._active_reported = False
            return

        now = time.monotonic()
        if self._active_reported:
            return
        if now - self._last_report_at < DETECTION_REPORT_COOLDOWN_SECONDS:
            return

        tipo_evento = tipo_evento_from_decision(decision)
        if not tipo_evento:
            return

        self._active_reported = True
        self._last_report_at = now
        self._executor.submit(
            self._send,
            self._build_event(
                tipo_evento,
                "sin_video",
                decision,
                eye_evidence,
                head_evidence,
            ),
        )

    def report_event(self, event):
        self._executor.submit(self._send, event)

    def _build_event(self, tipo_evento, video_path, decision, eye_evidence, head_evidence):
        duracion_alerta = max(
            _float_or_none(getattr(eye_evidence, "closed_seconds", None)) or 0.0,
            _float_or_none(getattr(eye_evidence, "partial_seconds", None)) or 0.0,
            _float_or_none(getattr(head_evidence, "drowsy_seconds", None)) or 0.0,
        )
        return RecordedDrowsinessEvent(
            tipo_evento=tipo_evento,
            video_path=video_path,
            valor_ear=_float_or_none(getattr(eye_evidence, "ear", None)),
            valor_pitch=_float_or_none(getattr(head_evidence, "pitch", None)),
            duracion_alerta=duracion_alerta,
            estado=decision.state,
            confianza=_float_or_none(decision.confidence),
            video_seconds=0.0,
        )

    def _send(self, event):
        payload = {
            "api_key": VISION_API_KEY,
            "device_id": VISION_DEVICE_ID,
            "tipo_evento": event.tipo_evento,
            "video_path": event.video_path,
            "valor_ear": event.valor_ear,
            "valor_pitch": event.valor_pitch,
            "duracion_alerta": event.duracion_alerta,
            "estado": event.estado,
            "confianza": event.confianza,
            "duracion_video": event.video_seconds,
        }

        url = f"{VISION_SERVER_URL}/api/deteccion"
        try:
            response = requests.post(url, json=payload, timeout=3.0)
            response.raise_for_status()
            data = response.json()
            print(
                "[API] Deteccion registrada "
                f"id={data.get('deteccion_id')} usuario={data.get('usuario_id')} "
                f"total={data.get('total_eventos')} score={data.get('score_conduccion')}"
            )
        except requests.exceptions.RequestException as exc:
            print(f"[API] No se pudo registrar deteccion en {url}: {exc}")
        except ValueError:
            print(f"[API] Respuesta no JSON al registrar deteccion: {response.text}")

    def cerrar(self):
        self._executor.shutdown(wait=True, cancel_futures=False)


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


@dataclass
class EyeEvidence:
    ear: float | None
    left_ear: float | None
    right_ear: float | None
    state: str
    closed_seconds: float
    partial_seconds: float
    is_drowsy: bool
    confidence: float


@dataclass
class HeadEvidence:
    pitch: float | None
    neutral_pitch: float | None
    pitch_delta: float | None
    drowsy_seconds: float
    is_drowsy: bool
    confidence: float


@dataclass
class DetectionDecision:
    state: str
    confidence: float
    head_drowsy: bool
    eye_drowsy: bool


def _safe_float(value):
    return None if value is None else float(value)


def merge_event_types(current, new):
    if not new:
        return current
    if not current:
        return new
    if current == new:
        return current
    return "Ambos"


def driver_recovered(face_detected, decision, eye_evidence, head_evidence):
    if not face_detected:
        return False
    if decision.head_drowsy or decision.eye_drowsy:
        return False
    if eye_evidence is None or eye_evidence.state != "Ojos abiertos":
        return False
    if head_evidence is None or head_evidence.pitch_delta is None:
        return False
    return abs(head_evidence.pitch_delta) < HEAD_PITCH_DELTA_THRESHOLD


class AsyncVideoWriter:
    """Writes the final MP4 on a worker thread so detection stays responsive."""

    def __init__(self, output_path, fps, frame_size, on_finished):
        self.output_path = Path(output_path)
        self.fps = fps if fps and fps > 1.0 else DEFAULT_RECORDING_FPS
        self.frame_size = frame_size
        self.on_finished = on_finished
        self._queue = queue.Queue(maxsize=BLACKBOX_QUEUE_MAX_FRAMES)
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="BlackBoxVideoWriter",
        )
        self._started = False
        self._closed = False
        self._dropped_frames = 0
        self._last_drop_log_at = 0.0

    def start(self):
        if self._started:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread.start()
        self._started = True

    def write(self, frame):
        if self._closed:
            return
        if not self._started:
            self.start()
        try:
            self._queue.put_nowait(frame.copy())
        except queue.Full:
            self._dropped_frames += 1
            now = time.monotonic()
            if now - self._last_drop_log_at >= 3.0:
                self._last_drop_log_at = now
                print(
                    "[CAJA_NEGRA] Cola de video llena; "
                    f"frames descartados={self._dropped_frames}"
                )

    def write_many(self, frames):
        for frame in frames:
            self.write(frame)

    def close(self, event_data=None):
        if self._closed:
            return
        self._closed = True
        self.event_data = event_data
        if not self._started:
            self.start()
        threading.Thread(
            target=self._queue.put,
            args=(None,),
            daemon=True,
            name="BlackBoxVideoWriterCloser",
        ).start()

    def join(self):
        if self._started:
            self._thread.join()

    def _run(self):
        writer = None
        frame_count = 0
        success = False
        error = None
        try:
            writer = self._open_writer()
            if not writer.isOpened():
                raise RuntimeError(f"No se pudo crear el video: {self.output_path}")

            while True:
                frame = self._queue.get()
                if frame is None:
                    break
                if frame.shape[1] != self.frame_size[0] or frame.shape[0] != self.frame_size[1]:
                    frame = cv2.resize(frame, self.frame_size)
                writer.write(frame)
                frame_count += 1
            success = frame_count > 0
        except Exception as exc:
            error = exc
            print(f"[CAJA_NEGRA] Error escribiendo video: {exc}")
        finally:
            if writer is not None:
                writer.release()
            if self.on_finished:
                self.on_finished(
                    self.output_path,
                    success,
                    frame_count,
                    frame_count / float(self.fps) if self.fps else 0.0,
                    self._dropped_frames,
                    error,
                    getattr(self, "event_data", None),
                )

    def _open_writer(self):
        for codec in ("avc1", "H264", "mp4v"):
            fourcc = cv2.VideoWriter_fourcc(*codec)
            writer = cv2.VideoWriter(
                str(self.output_path),
                fourcc,
                float(self.fps),
                self.frame_size,
            )
            if writer.isOpened():
                print(f"[CAJA_NEGRA] Codec de video activo: {codec}")
                return writer
            writer.release()

        return cv2.VideoWriter()


class BlackBoxRecorder:
    """Keeps a RAM pre-buffer and records only confirmed drowsiness events."""

    def __init__(
        self,
        reporter,
        pre_event_seconds=BLACKBOX_PRE_EVENT_SECONDS,
        confirmation_seconds=BLACKBOX_CONFIRMATION_SECONDS,
        post_event_seconds=BLACKBOX_POST_EVENT_SECONDS,
        min_event_seconds=BLACKBOX_MIN_EVENT_SECONDS,
        output_dir=BLACKBOX_OUTPUT_DIR,
    ):
        self.reporter = reporter
        self.pre_event_seconds = pre_event_seconds
        self.confirmation_seconds = confirmation_seconds
        self.post_event_seconds = post_event_seconds
        self.min_event_seconds = min_event_seconds
        self.output_dir = Path(output_dir)
        self.pre_buffer = deque()
        self.candidate_started_at = None
        self.candidate_pre_frames = []
        self.recording = False
        self.event_started_at = None
        self.last_drowsy_at = None
        self.normal_started_at = None
        self.event_type = None
        self.event_state = None
        self.event_confidence = None
        self.event_ear = None
        self.event_pitch = None
        self.writer = None
        self._writers_to_join = []

    def update(
        self,
        frame,
        now,
        raw_drowsy,
        recovered,
        decision,
        eye_evidence,
        head_evidence,
        fps,
    ):
        self._append_pre_frame(now, frame)

        if self.recording:
            self._record_active_frame(
                frame,
                now,
                raw_drowsy,
                recovered,
                decision,
                eye_evidence,
                head_evidence,
            )
            return raw_drowsy

        if not raw_drowsy:
            self._cancel_candidate()
            return False

        if self.candidate_started_at is None:
            self.candidate_started_at = now
            self.candidate_pre_frames = self._frames_between(
                now - self.pre_event_seconds,
                now,
                include_end=False,
            )
            return False

        if now - self.candidate_started_at < self.confirmation_seconds:
            return False

        self._start_recording(
            frame,
            now,
            decision,
            eye_evidence,
            head_evidence,
            fps,
        )
        return True

    def close(self):
        if self.recording:
            print("[CAJA_NEGRA] Cerrando grabacion activa por salida del programa.")
            self._finish_recording()
        self._cancel_candidate()
        for writer in self._writers_to_join:
            writer.join()
        self._writers_to_join.clear()

    def _append_pre_frame(self, now, frame):
        self.pre_buffer.append((now, frame.copy()))
        min_timestamp = now - self.pre_event_seconds
        while self.pre_buffer and self.pre_buffer[0][0] < min_timestamp:
            self.pre_buffer.popleft()

    def _frames_between(self, start, end, include_end=True):
        frames = []
        for timestamp, frame in self.pre_buffer:
            if timestamp < start:
                continue
            if include_end:
                if timestamp <= end:
                    frames.append(frame)
            elif timestamp < end:
                frames.append(frame)
        return frames

    def _cancel_candidate(self):
        self.candidate_started_at = None
        self.candidate_pre_frames = []

    def _start_recording(self, frame, now, decision, eye_evidence, head_evidence, fps):
        frame_size = (frame.shape[1], frame.shape[0])
        output_path = self._make_output_path()
        self.event_started_at = self.candidate_started_at or now
        self.last_drowsy_at = now
        self.normal_started_at = None
        self.recording = True
        self.event_type = None
        self.event_state = None
        self.event_confidence = None
        self.event_ear = None
        self.event_pitch = None
        self._update_event_metadata(decision, eye_evidence, head_evidence)

        self.writer = AsyncVideoWriter(
            output_path=output_path,
            fps=fps,
            frame_size=frame_size,
            on_finished=self._on_video_finished,
        )
        initial_frames = list(self.candidate_pre_frames)
        initial_frames.extend(self._frames_between(self.event_started_at, now, include_end=True))
        self.writer.write_many(initial_frames)
        self._cancel_candidate()
        print(
            "[CAJA_NEGRA] Evento confirmado; grabando evidencia en "
            f"{output_path}"
        )

    def _record_active_frame(
        self,
        frame,
        now,
        raw_drowsy,
        recovered,
        decision,
        eye_evidence,
        head_evidence,
    ):
        if self.writer:
            self.writer.write(frame)

        if raw_drowsy:
            self.last_drowsy_at = now
            self.normal_started_at = None
            self._update_event_metadata(decision, eye_evidence, head_evidence)
            return

        if recovered:
            if self.normal_started_at is None:
                self.normal_started_at = now
        else:
            self.normal_started_at = None

        enough_total_time = (now - self.event_started_at) >= self.min_event_seconds
        enough_post_time = (
            self.normal_started_at is not None
            and (now - self.normal_started_at) >= self.post_event_seconds
        )
        if enough_total_time and enough_post_time:
            self._finish_recording()

    def _update_event_metadata(self, decision, eye_evidence, head_evidence):
        self.event_type = merge_event_types(self.event_type, tipo_evento_from_decision(decision))
        self.event_state = decision.state
        self.event_confidence = _safe_float(decision.confidence)
        ear = _safe_float(getattr(eye_evidence, "ear", None))
        pitch = _safe_float(getattr(head_evidence, "pitch", None))
        if ear is not None:
            self.event_ear = ear
        if pitch is not None:
            self.event_pitch = pitch

    def _finish_recording(self):
        writer = self.writer
        event_data = self._build_recorded_event(video_path="" if writer is None else str(writer.output_path))
        self.writer = None
        self.recording = False
        self.normal_started_at = None
        self.event_started_at = None
        self.last_drowsy_at = None
        self.event_type = None
        self.event_state = None
        self.event_confidence = None
        self.event_ear = None
        self.event_pitch = None
        if writer:
            writer.close(event_data)
            self._writers_to_join.append(writer)
        print("[CAJA_NEGRA] Grabacion finalizada; cerrando MP4 en segundo plano.")

    def _make_output_path(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        device_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in VISION_DEVICE_ID)
        return self.output_dir / f"somnolencia_{device_id}_{timestamp}.mp4"

    def _build_recorded_event(self, video_path, video_seconds=0.0):
        alert_duration = 0.0
        if self.event_started_at is not None and self.last_drowsy_at is not None:
            alert_duration = max(0.0, self.last_drowsy_at - self.event_started_at)

        return RecordedDrowsinessEvent(
            tipo_evento=self.event_type or "Ojos Cerrados",
            video_path=video_path,
            valor_ear=self.event_ear,
            valor_pitch=self.event_pitch,
            duracion_alerta=alert_duration,
            estado=self.event_state or "Somnolencia",
            confianza=self.event_confidence,
            video_seconds=video_seconds,
        )

    def _on_video_finished(
        self,
        path,
        success,
        frame_count,
        video_seconds,
        dropped_frames,
        error,
        event_data,
    ):
        if not success:
            print(f"[CAJA_NEGRA] No se genero evidencia valida para {path}")
            return

        event = event_data or self._build_recorded_event(str(path.resolve()), video_seconds)
        event.video_path = str(path.resolve())
        event.video_seconds = video_seconds
        print(
            "[CAJA_NEGRA] MP4 listo "
            f"frames={frame_count} duracion={video_seconds:.2f}s "
            f"descartados={dropped_frames}; reportando al servidor."
        )
        self.reporter.report_event(event)


class EyeClosureTracker:
    """Tracks eye closure over time so blinks do not become alerts."""

    def __init__(
        self,
        closed_threshold=EAR_CLOSED_THRESHOLD,
        partial_threshold=EAR_PARTIAL_THRESHOLD,
        closed_seconds_threshold=EYE_CLOSED_SECONDS_THRESHOLD,
        partial_seconds_threshold=EYE_PARTIAL_SECONDS_THRESHOLD,
    ):
        self.closed_threshold = closed_threshold
        self.partial_threshold = partial_threshold
        self.closed_seconds_threshold = closed_seconds_threshold
        self.partial_seconds_threshold = partial_seconds_threshold
        self.closed_started_at = None
        self.partial_started_at = None

    def clear(self):
        self.closed_started_at = None
        self.partial_started_at = None

    def update(self, ear_result, now):
        if ear_result is None:
            self.clear()
            return EyeEvidence(
                ear=None,
                left_ear=None,
                right_ear=None,
                state="Sin EAR",
                closed_seconds=0.0,
                partial_seconds=0.0,
                is_drowsy=False,
                confidence=0.0,
            )

        ear = ear_result.average
        if ear < self.closed_threshold:
            if self.closed_started_at is None:
                self.closed_started_at = now
            if self.partial_started_at is None:
                self.partial_started_at = now
            state = "Parpadeo"
        elif ear < self.partial_threshold:
            self.closed_started_at = None
            if self.partial_started_at is None:
                self.partial_started_at = now
            state = "Ojos parcialmente cerrados"
        else:
            self.clear()
            return EyeEvidence(
                ear=ear,
                left_ear=ear_result.left,
                right_ear=ear_result.right,
                state="Ojos abiertos",
                closed_seconds=0.0,
                partial_seconds=0.0,
                is_drowsy=False,
                confidence=0.0,
            )

        closed_seconds = 0.0 if self.closed_started_at is None else now - self.closed_started_at
        partial_seconds = 0.0 if self.partial_started_at is None else now - self.partial_started_at

        if ear < self.closed_threshold and closed_seconds > NORMAL_BLINK_MAX_SECONDS:
            state = "Ojos cerrados"

        closed_drowsy = ear < self.closed_threshold and closed_seconds >= self.closed_seconds_threshold
        partial_drowsy = ear < self.partial_threshold and partial_seconds >= self.partial_seconds_threshold
        is_drowsy = closed_drowsy or partial_drowsy

        confidence = 0.0
        if ear < self.closed_threshold:
            confidence = min(1.0, closed_seconds / self.closed_seconds_threshold)
        elif ear < self.partial_threshold:
            confidence = min(0.85, partial_seconds / self.partial_seconds_threshold)

        return EyeEvidence(
            ear=ear,
            left_ear=ear_result.left,
            right_ear=ear_result.right,
            state=state,
            closed_seconds=closed_seconds,
            partial_seconds=partial_seconds,
            is_drowsy=is_drowsy,
            confidence=float(confidence),
        )


class HeadNodTracker:
    """Requires sustained model and pitch evidence before reporting cabeceo."""

    def __init__(
        self,
        probability_threshold=HEAD_DROWSY_PROB_THRESHOLD,
        pitch_delta_threshold=HEAD_PITCH_DELTA_THRESHOLD,
        seconds_threshold=HEAD_DROWSY_SECONDS_THRESHOLD,
        neutral_alpha=HEAD_NEUTRAL_ALPHA,
    ):
        self.probability_threshold = probability_threshold
        self.pitch_delta_threshold = pitch_delta_threshold
        self.seconds_threshold = seconds_threshold
        self.neutral_alpha = neutral_alpha
        self.neutral_pitch = None
        self.drowsy_started_at = None

    def clear(self):
        self.drowsy_started_at = None

    def update(self, head_pitch, somnolencia_probability, now):
        if head_pitch is None:
            self.clear()
            return HeadEvidence(None, self.neutral_pitch, None, 0.0, False, 0.0)

        if self.neutral_pitch is None:
            self.neutral_pitch = float(head_pitch)

        pitch_delta = float(head_pitch - self.neutral_pitch)
        strong_model_signal = somnolencia_probability >= self.probability_threshold
        strong_pitch_signal = abs(pitch_delta) >= self.pitch_delta_threshold
        candidate = strong_model_signal and strong_pitch_signal

        if candidate:
            if self.drowsy_started_at is None:
                self.drowsy_started_at = now
        else:
            self.clear()
            self.neutral_pitch = (
                (1.0 - self.neutral_alpha) * self.neutral_pitch
                + self.neutral_alpha * float(head_pitch)
            )

        drowsy_seconds = 0.0 if self.drowsy_started_at is None else now - self.drowsy_started_at
        is_drowsy = candidate and drowsy_seconds >= self.seconds_threshold

        probability_score = min(1.0, somnolencia_probability / self.probability_threshold)
        pitch_score = min(1.0, abs(pitch_delta) / self.pitch_delta_threshold)
        time_score = min(1.0, drowsy_seconds / self.seconds_threshold)
        confidence = probability_score * pitch_score * time_score if candidate else 0.0

        return HeadEvidence(
            pitch=float(head_pitch),
            neutral_pitch=float(self.neutral_pitch),
            pitch_delta=pitch_delta,
            drowsy_seconds=float(drowsy_seconds),
            is_drowsy=is_drowsy,
            confidence=float(confidence),
        )


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


def patch_keras3_model_config(value):
    """Adapts Keras 3 model config keys for TensorFlow/Keras 2.x."""
    patched = False

    if isinstance(value, dict):
        config = value.get("config")
        if isinstance(config, dict) and "quantization_config" in config:
            config.pop("quantization_config")
            patched = True
        if (
            isinstance(config, dict)
            and isinstance(config.get("dtype"), dict)
            and config["dtype"].get("class_name") == "DTypePolicy"
        ):
            config.pop("dtype")
            patched = True

        if value.get("class_name") == "InputLayer":
            config = value.get("config", {})
            if "batch_shape" in config and "batch_input_shape" not in config:
                config["batch_input_shape"] = config.pop("batch_shape")
                patched = True
            if "optional" in config:
                config.pop("optional")
                patched = True

        for child in value.values():
            patched = patch_keras3_model_config(child) or patched

    elif isinstance(value, list):
        for item in value:
            patched = patch_keras3_model_config(item) or patched

    return patched


def make_keras2_compatible_copy(model_path):
    """Creates a temporary .keras copy when the saved config comes from Keras 3."""
    with zipfile.ZipFile(model_path, "r") as source:
        config = json.loads(source.read("config.json"))
        patched = patch_keras3_model_config(config)

        if not patched:
            return None

        temp_file = tempfile.NamedTemporaryFile(suffix=".keras", delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()

        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for item_name in source.namelist():
                if item_name == "config.json":
                    target.writestr(item_name, json.dumps(config))
                else:
                    target.writestr(item_name, source.read(item_name))

    return temp_path


def build_layer_from_config(layer_data):
    class_name = layer_data.get("class_name")
    config = layer_data.get("config", {})
    name = config.get("name")

    if class_name == "Dense":
        return tf.keras.layers.Dense(
            units=config["units"],
            activation=config.get("activation"),
            use_bias=config.get("use_bias", True),
            name=name,
        )

    if class_name == "Dropout":
        return tf.keras.layers.Dropout(
            rate=config["rate"],
            noise_shape=config.get("noise_shape"),
            seed=config.get("seed"),
            name=name,
        )

    raise ValueError(f"Capa no soportada para carga manual: {class_name}")


def load_keras3_weights_manually(model_path):
    """Rebuilds the simple Sequential model and loads Keras 3 .keras weights."""
    import h5py

    weights_path = None
    try:
        with zipfile.ZipFile(model_path, "r") as source:
            config = json.loads(source.read("config.json"))
            layers_config = config["config"]["layers"]
            input_config = layers_config[0]["config"]
            batch_shape = input_config.get("batch_shape") or input_config.get("batch_input_shape")
            if not batch_shape or len(batch_shape) < 2:
                raise ValueError("No se pudo leer el input_shape del modelo.")

            model = tf.keras.Sequential(name=config["config"].get("name", "sequential"))
            model.add(tf.keras.layers.Input(shape=tuple(batch_shape[1:]), name=input_config.get("name")))

            for layer_data in layers_config[1:]:
                model.add(build_layer_from_config(layer_data))

            temp_file = tempfile.NamedTemporaryFile(suffix=".h5", delete=False)
            weights_path = Path(temp_file.name)
            temp_file.write(source.read("model.weights.h5"))
            temp_file.close()

        with h5py.File(weights_path, "r") as weights_file:
            for layer in model.layers:
                layer_weights = weights_file.get(f"layers/{layer.name}/vars")
                if layer_weights is None or len(layer_weights.keys()) == 0:
                    continue

                ordered_keys = sorted(layer_weights.keys(), key=lambda key: int(key))
                layer.set_weights([np.asarray(layer_weights[key]) for key in ordered_keys])

        return model
    finally:
        if weights_path is not None:
            weights_path.unlink(missing_ok=True)


def load_keras_model_compatible(model_path):
    try:
        return tf.keras.models.load_model(model_path, compile=False)
    except Exception as original_exc:
        compatible_path = None
        try:
            compatible_path = make_keras2_compatible_copy(model_path)
            if compatible_path is None:
                raise original_exc

            print("[INIT] Modelo .keras convertido temporalmente para TensorFlow/Keras 2.x.")
            return tf.keras.models.load_model(compatible_path, compile=False)
        except Exception as compatible_exc:
            try:
                print("[INIT] Reconstruyendo modelo .keras manualmente para compatibilidad.")
                return load_keras3_weights_manually(model_path)
            except Exception as manual_exc:
                raise RuntimeError(f"No se pudo cargar el modelo TensorFlow: {model_path}") from manual_exc
        finally:
            if compatible_path is not None:
                compatible_path.unlink(missing_ok=True)


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
    model = load_keras_model_compatible(MODEL_PATH)

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


def combine_signals(smooth_label, smooth_confidence, smooth_probabilities, eye_evidence, head_evidence):
    """Combine the existing TensorFlow signal with direct EAR evidence."""
    head_drowsy = head_evidence.is_drowsy
    eye_drowsy = eye_evidence.is_drowsy

    if head_drowsy and eye_drowsy:
        return DetectionDecision(
            state="Somnolencia crítica",
            confidence=min(1.0, max(head_evidence.confidence, eye_evidence.confidence) + 0.15),
            head_drowsy=True,
            eye_drowsy=True,
        )

    if eye_drowsy:
        return DetectionDecision(
            state="Somnolencia por ojos",
            confidence=eye_evidence.confidence,
            head_drowsy=False,
            eye_drowsy=True,
        )

    if head_drowsy:
        return DetectionDecision(
            state="Somnolencia por cabeceo",
            confidence=head_evidence.confidence,
            head_drowsy=True,
            eye_drowsy=False,
        )

    return DetectionDecision(
        state="Despierto",
        confidence=smooth_probabilities[LABEL_DESPIERTO],
        head_drowsy=False,
        eye_drowsy=False,
    )


def empty_probabilities():
    return {
        LABEL_SOMNOLENCIA: 0.0,
        LABEL_DESPIERTO: 0.0,
    }


def format_metric(value, suffix="", missing="N/A", precision=2):
    if value is None:
        return missing
    return f"{value:.{precision}f}{suffix}"


def log_prediction(
    frame_index,
    input_shape,
    probabilities,
    raw_label,
    raw_confidence,
    smooth_label,
    smooth_confidence,
    eye_evidence,
    head_evidence,
    final_decision,
):
    if frame_index % LOG_EVERY_N_FRAMES != 0:
        return

    print(
        "[PRED] "
        f"input_shape={input_shape} | "
        f"probabilidades={{Somnolencia: {probabilities[LABEL_SOMNOLENCIA]:.4f}, "
        f"Despierto: {probabilities[LABEL_DESPIERTO]:.4f}}} | "
        f"clase_modelo={CLASS_NAMES[raw_label]} | confianza_modelo={raw_confidence:.4f} | "
        f"clase_suavizada={CLASS_NAMES[smooth_label]} | confianza_suavizada={smooth_confidence:.4f} | "
        f"EAR={format_metric(eye_evidence.ear)} | ojos={eye_evidence.state} | "
        f"t_cerrado={eye_evidence.closed_seconds:.2f}s | "
        f"t_parcial={eye_evidence.partial_seconds:.2f}s | "
        f"pitch={format_metric(head_evidence.pitch, suffix=' deg')} | "
        f"delta_pitch={format_metric(head_evidence.pitch_delta, suffix=' deg')} | "
        f"t_cabeceo={head_evidence.drowsy_seconds:.2f}s | "
        f"estado_final={final_decision.state} | confianza_final={final_decision.confidence:.4f}"
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


def render_status(
    frame,
    estado,
    confidence=None,
    probabilities=None,
    eye_evidence=None,
    head_evidence=None,
    model_available=False,
):
    if estado == "No hay rostro":
        color = (0, 165, 255)
    elif estado == "Somnolencia crítica":
        color = (0, 0, 180)
    elif estado in ("Somnolencia por ojos", "Somnolencia por cabeceo"):
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
        probabilities = empty_probabilities()

    model_detail = (
        f"Modelo Somn: {probabilities[LABEL_SOMNOLENCIA]:.2f} | "
        f"Desp: {probabilities[LABEL_DESPIERTO]:.2f}"
    )
    confidence_detail = "Conf final: N/A" if confidence is None else f"Conf final: {confidence:.2f}"
    if not model_available:
        model_detail += " | Modelo: sin rostro/pose"

    if eye_evidence is None:
        ear_detail = "EAR: N/A"
        eye_detail = "Ojos: Sin EAR | Cerrado: 0.00s | Parcial: 0.00s"
    else:
        ear_detail = (
            f"EAR: {format_metric(eye_evidence.ear)} "
            f"(L {format_metric(eye_evidence.left_ear)}, R {format_metric(eye_evidence.right_ear)})"
        )
        eye_detail = (
            f"Ojos: {eye_evidence.state} | Cerrado: {eye_evidence.closed_seconds:.2f}s | "
            f"Parcial: {eye_evidence.partial_seconds:.2f}s"
        )

    if head_evidence is None:
        pitch_detail = "Cabeza: pitch N/A | delta N/A | t 0.00s"
    else:
        pitch_detail = (
            f"Cabeza: pitch {format_metric(head_evidence.pitch, suffix=' deg')} | "
            f"delta {format_metric(head_evidence.pitch_delta, suffix=' deg')} | "
            f"t {head_evidence.drowsy_seconds:.2f}s"
        )

    status_lines = (model_detail, confidence_detail, ear_detail, eye_detail, pitch_detail)
    for line_index, detail in enumerate(status_lines, start=1):
        cv2.putText(
            frame,
            detail,
            (30, 60 + (line_index * 28)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
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
    eye_tracker = EyeClosureTracker()
    head_tracker = HeadNodTracker()
    alarm_controller = Esp32AlarmController()
    detection_reporter = DetectionReporter()
    blackbox_recorder = BlackBoxRecorder(detection_reporter)
    recording_fps = cap.get(cv2.CAP_PROP_FPS) or DEFAULT_RECORDING_FPS
    if recording_fps <= 1.0 or recording_fps > 120.0:
        recording_fps = DEFAULT_RECORDING_FPS

    mp_holistic = mp.solutions.holistic
    mp_drawing = mp.solutions.drawing_utils
    frame_index = 0

    try:
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

                now = time.monotonic()
                face_detected = results.face_landmarks is not None
                ear_result = calculate_eye_aspect_ratio(results.face_landmarks) if face_detected else None
                eye_evidence = eye_tracker.update(ear_result, now) if face_detected else eye_tracker.update(None, now)
                head_pitch = estimate_head_pitch(results.face_landmarks, frame.shape) if face_detected else None
                head_evidence = head_tracker.update(head_pitch, 0.0, now) if face_detected else head_tracker.update(None, 0.0, now)

                estado = "No hay rostro"
                confidence = None
                probabilities = empty_probabilities()
                smooth_probabilities = empty_probabilities()
                smooth_label = LABEL_DESPIERTO
                model_available = False
                decision = DetectionDecision(
                    state=estado,
                    confidence=0.0,
                    head_drowsy=False,
                    eye_drowsy=False,
                )
                values = extract_landmarks(results)

                if not face_detected:
                    # Do not reuse stale predictions when the face is gone.
                    smoother.clear()
                    head_tracker.clear()
                    render_status(
                        frame,
                        estado,
                        confidence,
                        probabilities,
                        eye_evidence,
                        head_evidence,
                        model_available=False,
                    )
                else:
                    if values is None:
                        # EAR can still work with a face only; the model requires pose too.
                        smoother.clear()
                        head_tracker.clear()
                        decision = combine_signals(
                            smooth_label,
                            0.0,
                            smooth_probabilities,
                            eye_evidence,
                            head_evidence,
                        )
                        render_status(
                            frame,
                            decision.state,
                            decision.confidence,
                            smooth_probabilities,
                            eye_evidence,
                            head_evidence,
                            model_available=False,
                        )
                    else:
                        try:
                            model_input = preprocess_landmarks(values, scaler, model, expected_features)
                            raw_label, raw_confidence, probabilities, _ = infer_state(model, model_input, interpreter)
                            smooth_label, smooth_confidence, smooth_probabilities = smoother.update(probabilities)
                            head_evidence = head_tracker.update(
                                head_pitch,
                                smooth_probabilities[LABEL_SOMNOLENCIA],
                                now,
                            )
                            decision = combine_signals(
                                smooth_label,
                                smooth_confidence,
                                smooth_probabilities,
                                eye_evidence,
                                head_evidence,
                            )
                            render_status(
                                frame,
                                decision.state,
                                decision.confidence,
                                smooth_probabilities,
                                eye_evidence,
                                head_evidence,
                                model_available=True,
                            )
                            log_prediction(
                                frame_index,
                                model_input.shape,
                                probabilities,
                                raw_label,
                                raw_confidence,
                                smooth_label,
                                smooth_confidence,
                                eye_evidence,
                                head_evidence,
                                decision,
                            )
                        except Exception as exc:
                            smoother.clear()
                            head_tracker.clear()
                            print(f"[ERROR] Inferencia omitida: {exc}")
                            decision = combine_signals(
                                LABEL_DESPIERTO,
                                0.0,
                                empty_probabilities(),
                                eye_evidence,
                                head_evidence,
                            )
                            render_status(
                                frame,
                                decision.state,
                                decision.confidence,
                                empty_probabilities(),
                                eye_evidence,
                                head_evidence,
                                model_available=False,
                            )

                somnolencia_detectada = decision.head_drowsy or decision.eye_drowsy
                recuperado = driver_recovered(
                    face_detected,
                    decision,
                    eye_evidence,
                    head_evidence,
                )
                alarma_confirmada = blackbox_recorder.update(
                    frame,
                    now,
                    somnolencia_detectada,
                    recuperado,
                    decision,
                    eye_evidence,
                    head_evidence,
                    recording_fps,
                )
                alarm_controller.update(alarma_confirmada)

                cv2.imshow("Deteccion de Somnolencia", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
    finally:
        alarm_controller.apagar_y_cerrar()
        blackbox_recorder.close()
        detection_reporter.cerrar()
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
