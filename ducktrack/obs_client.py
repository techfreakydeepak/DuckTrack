import os
import subprocess
import time
import logging
from platform import system
import obsws_python as obs
import psutil

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def is_obs_running() -> bool:
    """
    Checks if OBS is already running on the system.
    """
    try:
        for process in psutil.process_iter(attrs=["pid", "name"]):
            if "obs" in process.info["name"].lower():
                logger.info("OBS is running.")
                return True
        logger.info("OBS is not running.")
        return False
    except Exception as e:
        logger.error(f"Error checking if OBS is running: {e}")
        raise Exception(f"Error checking if OBS is running: {e}")

def close_obs(obs_process: subprocess.Popen):
    """
    Gracefully terminates the OBS process.
    """
    if obs_process:
        try:
            logger.info("Terminating OBS process...")
            obs_process.terminate()
            obs_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("OBS process did not terminate in time. Forcing kill.")
            obs_process.kill()

def find_obs() -> str:
    """
    Finds the OBS executable path based on the operating system.
    """
    common_paths = {
        "Windows": [
            "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe",
            "C:\\Program Files (x86)\\obs-studio\\bin\\32bit\\obs32.exe"
        ],
        "Darwin": [
            "/Applications/OBS.app/Contents/MacOS/OBS",
            "/opt/homebrew/bin/obs"
        ],
        "Linux": [
            "/usr/bin/obs",
            "/usr/local/bin/obs"
        ]
    }

    for path in common_paths.get(system(), []):
        if os.path.exists(path):
            logger.info(f"Found OBS at {path}.")
            return path

    try:
        if system() == "Windows":
            obs_path = subprocess.check_output("where obs", shell=True).decode().strip()
        else:
            obs_path = subprocess.check_output("which obs", shell=True).decode().strip()

        if os.path.exists(obs_path):
            logger.info(f"Found OBS at {obs_path}.")
            return obs_path
    except subprocess.CalledProcessError:
        logger.error("OBS executable not found.")
        raise FileNotFoundError("OBS executable not found.")

    return "obs"  # Default fallback

def open_obs() -> subprocess.Popen:
    """
    Opens OBS Studio and starts the replay buffer.
    """
    try:
        obs_path = find_obs()
        if system() == "Windows":
            os.chdir(os.path.dirname(obs_path))
            obs_path = os.path.basename(obs_path)
        logger.info("Opening OBS Studio...")
        return subprocess.Popen([obs_path, "--startreplaybuffer", "--minimize-to-tray"])
    except Exception as e:
        logger.error(f"Failed to open OBS: {e}")
        raise Exception(f"Failed to open OBS: {e}")

class OBSClient:
    """
    Controls the OBS client via the OBS WebSocket.
    Configures settings for recording.
    """

    def __init__(
        self,
        recording_path: str,
        metadata: dict,
        password: str,
        fps: int = 30,
        output_width: int = 1280,
        output_height: int = 720,
    ):
        self.metadata = metadata
        self.recording_path = recording_path
        self.password = password

        # Validate that a password is provided if authentication is enabled
        if not self.password:
            raise ValueError("Authentication enabled but no password provided.")

        try:
            # Initialize WebSocket clients with authentication
            self.req_client = obs.ReqClient(password=self.password)
            self.event_client = obs.EventClient(password=self.password)
        except obs.error.OBSSDKError as e:
            logger.error(f"Failed to authenticate with OBS WebSocket: {e}")
            raise Exception(f"Failed to authenticate with OBS WebSocket: {e}")

        self.record_state_events = {}

        self._initialize_callbacks()
        self._configure_obs(metadata, fps, output_width, output_height)

    def _initialize_callbacks(self):
        """
        Registers event callbacks for recording state changes.
        """
        def on_record_state_changed(data):
            output_state = data.output_state
            logger.info(f"Record state changed: {output_state}")
            if output_state not in self.record_state_events:
                self.record_state_events[output_state] = []
            self.record_state_events[output_state].append(time.perf_counter())

        self.event_client.callback.register(on_record_state_changed)

    def _configure_obs(self, metadata, fps, output_width, output_height):
        """
        Configures OBS settings for the desired recording parameters.
        """
        self.old_profile = self.req_client.get_profile_list().current_profile_name

        try:
            # Check if the profile already exists
            profile_name = "computer_tracker"
            profiles = self.req_client.get_profile_list().profiles

            if profile_name not in profiles:
                logger.info(f"Profile '{profile_name}' not found. Creating it.")
                self.req_client.create_profile(profile_name)
            else:
                logger.info(f"Profile '{profile_name}' already exists.")
                self.req_client.set_current_profile(profile_name)

        except obs.error.OBSSDKRequestError as e:
            logger.error(f"Error during profile setup: {e}")
            raise Exception(f"Error during profile setup: {e}")

        base_width = metadata["screen_width"]
        base_height = metadata["screen_height"]

        if metadata["system"] == "Darwin":
            base_width *= 2
            base_height *= 2

        scaled_width, scaled_height = _scale_resolution(base_width, base_height, output_width, output_height)

        self.req_client.set_profile_parameter("Video", "BaseCX", str(base_width))
        self.req_client.set_profile_parameter("Video", "BaseCY", str(base_height))
        self.req_client.set_profile_parameter("Video", "OutputCX", str(scaled_width))
        self.req_client.set_profile_parameter("Video", "OutputCY", str(scaled_height))
        self.req_client.set_profile_parameter("Video", "ScaleType", "lanczos")

        self.req_client.set_profile_parameter("AdvOut", "RescaleRes", f"{base_width}x{base_height}")
        self.req_client.set_profile_parameter("AdvOut", "RecRescaleRes", f"{base_width}x{base_height}")
        self.req_client.set_profile_parameter("AdvOut", "FFRescaleRes", f"{base_width}x{base_height}")

        self.req_client.set_profile_parameter("Video", "FPSCommon", str(fps))
        self.req_client.set_profile_parameter("SimpleOutput", "RecFormat2", "mp4")

        bitrate = int(_get_bitrate_mbps(scaled_width, scaled_height, fps=fps) * 1000 / 50) * 50
        self.req_client.set_profile_parameter("SimpleOutput", "VBitrate", str(bitrate))
        self.req_client.set_profile_parameter("SimpleOutput", "RecQuality", "Small")
        self.req_client.set_profile_parameter("SimpleOutput", "FilePath", self.recording_path)

        try:
            self.req_client.set_input_mute("Mic/Aux", muted=True)
        except obs.error.OBSSDKRequestError:
            logger.warning("No Mic/Aux input found. Skipping muting.")

    def start_recording(self):
        logger.info("Starting recording...")
        try:
            self.req_client.start_record()
        except obs.error.OBSSDKRequestError as e:
            logger.error(f"Error starting recording: {e}")
            raise Exception(f"Error starting recording: {e}")

    def stop_recording(self):
        logger.info("Stopping recording...")
        try:
            self.req_client.stop_record()
            self.req_client.set_current_profile(self.old_profile)  # Restore old profile
        except obs.error.OBSSDKRequestError as e:
            logger.error(f"Error stopping recording: {e}")
            raise Exception(f"Error stopping recording: {e}")

    def pause_recording(self):
        logger.info("Pausing recording...")
        try:
            self.req_client.pause_record()
        except obs.error.OBSSDKRequestError as e:
            logger.error(f"Error pausing recording: {e}")
            raise Exception(f"Error pausing recording: {e}")

    def resume_recording(self):
        logger.info("Resuming recording...")
        try:
            self.req_client.resume_record()
        except obs.error.OBSSDKRequestError as e:
            logger.error(f"Error resuming recording: {e}")
            raise Exception(f"Error resuming recording: {e}")

def _get_bitrate_mbps(width: int, height: int, fps=30) -> float:
    """
    Gets the YouTube recommended bitrate in Mbps for a given resolution and framerate.
    """
    resolutions = {
        (7680, 4320): {30: 120, 60: 180},
        (3840, 2160): {30: 40, 60: 60.5},
        (2160, 1440): {30: 16, 60: 24},
        (1920, 1080): {30: 8, 60: 12},
        (1280, 720): {30: 5, 60: 7.5},
        (640, 480): {30: 2.5, 60: 4},
        (480, 360): {30: 1, 60: 1.5},
    }

    for res, fps_dict in resolutions.items():
        if (width, height) == res and fps in fps_dict:
            return fps_dict[fps]
    return 5  # Default bitrate

def _scale_resolution(base_width, base_height, target_width, target_height):
    """
    Scales the resolution to maintain aspect ratio.
    """
    ratio = min(target_width / base_width, target_height / base_height)
    return int(base_width * ratio), int(base_height * ratio)
