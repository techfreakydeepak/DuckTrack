import os
import json
import time
from datetime import datetime
from platform import system
from queue import Queue, Empty
from threading import Lock
from pynput import keyboard, mouse
from pynput.keyboard import KeyCode
from PyQt6.QtCore import QThread, pyqtSignal
from .metadata import MetadataManager
from .obs_client import OBSClient
from .util import fix_windows_dpi_scaling, get_recordings_dir
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Recorder(QThread):
    """
    Handles screen recording, mouse and keyboard event tracking,
    and metadata collection using OBS and pynput.
    """

    recording_stopped = pyqtSignal()

    def __init__(self, natural_scrolling: bool, password: str):
        """
        Initializes the Recorder class with necessary settings.
        """
        super().__init__()

        if password is None:
            raise ValueError("Password is required to initialize Recorder.")
        
        self.password = password
        logger.info(f"Recorder initialized with password: {'*' * len(self.password)}")

        # Initialize recording directory and other variables
        self.recording_path = self._initialize_recording_directory()
        self._is_recording = False
        self._is_paused = False
        self._lock = Lock()

        # Event queue and file handling
        self.event_queue = Queue()
        self.events_file = None

        # Metadata and OBS setup
        self.metadata_manager = MetadataManager(
            recording_path=self.recording_path,
            natural_scrolling=natural_scrolling
        )
        self.obs_client = OBSClient(
            password=self.password,  # Pass the password to OBSClient
            recording_path=self.recording_path,
            metadata=self.metadata_manager.metadata
        )

        # Input listeners
        self.mouse_listener = mouse.Listener(
            on_move=self.on_move,
            on_click=self.on_click,
            on_scroll=self.on_scroll
        )
        self.keyboard_listener = keyboard.Listener(
            on_press=self.on_press,
            on_release=self.on_release
        )

        # DPI scaling fix for Windows
        if system() == "Windows":
            fix_windows_dpi_scaling()

    def run(self):
        """
        Starts the recording process and listens for events.
        """
        self._is_recording = True
        try:
            self.metadata_manager.collect()
            self.obs_client.start_recording()
            self._start_listeners()
            self._open_event_file()

            while self._is_recording:
                try:
                    event = self.event_queue.get(timeout=0.1)
                    self._log_event(event)
                except Empty:
                    continue
        except Exception as e:
            logger.error(f"Error during recording: {e}")
        finally:
            self._cleanup_resources()

    def stop_recording(self):
        """
        Stops the recording and finalizes metadata and resources.
        """
        with self._lock:
            if not self._is_recording:
                return
            self._is_recording = False

        try:
            self.obs_client.stop_recording()
            self.metadata_manager.end_collect()
            self.metadata_manager.add_obs_record_state_timings(self.obs_client.record_state_events)
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
        finally:
            self.recording_stopped.emit()

    def pause_recording(self):
        """
        Pauses the recording process.
        """
        with self._lock:
            if not self._is_recording or self._is_paused:
                return
            self._is_paused = True

        try:
            self.obs_client.pause_recording()
            self._add_event_to_queue({"action": "pause"})
        except Exception as e:
            logger.error(f"Error pausing recording: {e}")

    def resume_recording(self):
        """
        Resumes the recording process.
        """
        with self._lock:
            if not self._is_recording or not self._is_paused:
                return
            self._is_paused = False

        try:
            self.obs_client.resume_recording()
            self._add_event_to_queue({"action": "resume"})
        except Exception as e:
            logger.error(f"Error resuming recording: {e}")

    def on_move(self, x, y):
        self._add_event_to_queue({"action": "move", "x": x, "y": y})

    def on_click(self, x, y, button, pressed):
        self._add_event_to_queue({
            "action": "click",
            "x": x,
            "y": y,
            "button": button.name,
            "pressed": pressed
        })

    def on_scroll(self, x, y, dx, dy):
        self._add_event_to_queue({
            "action": "scroll",
            "x": x,
            "y": y,
            "dx": dx,
            "dy": dy
        })

    def on_press(self, key):
        self._add_event_to_queue({
            "action": "press",
            "key": self._get_key_name(key)
        })

    def on_release(self, key):
        self._add_event_to_queue({
            "action": "release",
            "key": self._get_key_name(key)
        })

    def _get_key_name(self, key):
        """
        Extracts a string representation of the key.
        """
        return key.char if isinstance(key, KeyCode) else key.name

    def _add_event_to_queue(self, event_data):
        """
        Adds an event to the queue with a timestamp.
        """
        if not self._is_paused and self._is_recording:
            event_data["time_stamp"] = time.perf_counter()
            self.event_queue.put(event_data, block=False)

    def _log_event(self, event):
        """
        Logs an event to the events file.
        """
        try:
            if self.events_file:
                self.events_file.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.error(f"Error logging event: {e}")

    def _initialize_recording_directory(self) -> str:
        """
        Creates and returns the path for the current recording session.
        """
        recordings_dir = get_recordings_dir()
        os.makedirs(recordings_dir, exist_ok=True)

        current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        recording_path = os.path.join(recordings_dir, f"recording-{current_time}")
        os.mkdir(recording_path)

        return recording_path

    def _start_listeners(self):
        """
        Starts the mouse and keyboard listeners.
        """
        try:
            self.mouse_listener.start()
            self.keyboard_listener.start()
        except Exception as e:
            logger.error(f"Error starting listeners: {e}")

    def _open_event_file(self):
        """
        Opens the events file for logging.
        """
        try:
            self.events_file = open(os.path.join(self.recording_path, "events.jsonl"), "a")
        except Exception as e:
            logger.error(f"Error opening event file: {e}")

    def _cleanup_resources(self):
        """
        Cleans up resources like listeners and file handles.
        """
        try:
            if self.mouse_listener.running:
                self.mouse_listener.stop()
            if self.keyboard_listener.running:
                self.keyboard_listener.stop()
            if self.events_file:
                self.events_file.close()
            self.metadata_manager.save_metadata()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
