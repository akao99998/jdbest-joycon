import asyncio
import json
import sys
import time
import threading
import queue
import os
import random
import requests
import websockets
import hid
import binascii
import customtkinter as ctk
from PIL import Image, ImageTk

try:
    from pyjoycon import JoyCon
except ImportError:
    print("[!] Error: 'joycon-python' library not found. Install with: pip install joycon-python")
    sys.exit(1)

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

API_BASE = "https://prod-api.jdbest.online"
WS_URL = "wss://prod-api.jdbest.online/drs/v1/ws"

# ==========================================
# CONTROLLER DRIVERS
# ==========================================

class RumbleJoyCon(JoyCon):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        time.sleep(0.15)  
        self.enable_vibration(True)

    def _write_raw(self, report_id: int, rumble_data: bytes):
        packet = bytes([report_id]) + self._packet_number.to_bytes(1, "little") + rumble_data
        try:
            self._joycon_device.write(packet)
            self._packet_number = (self._packet_number + 1) & 0x0F
        except Exception:
            pass

    def enable_vibration(self, enable: bool):
        argument = b'\x01' if enable else b'\x00'
        try:
            self._write_output_report(b'\x01', b'\x48', argument)
        except Exception:
            pass

    def send_rumble_packet(self, rumble_data: bytes):
        if len(rumble_data) != 8:
            raise ValueError("Rumble data must be exactly 8 bytes")
        self._write_raw(0x10, rumble_data)


class BaseControllerBackend:
    def __init__(self, info, multiplier=1):
        self.info = info
        self.multiplier = multiplier
        self.coach_action = 0  
        self.btn_a_pressed = False

    def connect(self): pass
    def get_motion_data(self): return {"x": 0, "y": 0, "z": 0}
    def set_rumble(self, on: bool, rumble_type: str = "YEAH"): pass
    def disconnect(self): pass

    def get_coach_action(self):
        val = self.coach_action
        self.coach_action = 0
        return val

    def get_btn_a(self):
        val = self.btn_a_pressed
        self.btn_a_pressed = False
        return val


class JoyConBackend(BaseControllerBackend):
    def connect(self):
        serial = self.info.get('serial')
        self.joycon = RumbleJoyCon(self.info['vendor_id'], self.info['product_id'], serial)
        self.stick_locked = False
        self.a_locked = False

    def get_motion_data(self):
        status = self.joycon.get_status()
        accel = status.get('accel', {'x': 0, 'y': 0, 'z': 0})
        scale = (1.0 / 4000.0) * 9.80665 * self.multiplier

        sticks = status.get('analog-sticks', {})
        side = "right" if self.info['side'] == "R" else "left"
        hx = sticks.get(side, {}).get('horizontal', 2048)

        if hx > 3000 and not self.stick_locked:
            self.coach_action = 1
            self.stick_locked = True
        elif hx < 1000 and not self.stick_locked:
            self.coach_action = -1
            self.stick_locked = True
        elif 1000 <= hx <= 3000:
            self.stick_locked = False

        buttons = status.get('buttons', {})
        if self.info['side'] == "R":
            is_a_pressed = buttons.get('right', {}).get('a', False)
        else:
            is_a_pressed = buttons.get('left', {}).get('right', False)

        if is_a_pressed and not self.a_locked:
            self.btn_a_pressed = True
            self.a_locked = True
        elif not is_a_pressed:
            self.a_locked = False

        if self.info['side'] == "R":
            return {"x": accel['x'] * scale, "y": accel['z'] * scale, "z": -accel['y'] * scale}
        else:
            return {"x": -accel['x'] * scale, "y": accel['z'] * scale, "z": accel['y'] * scale}

    def set_rumble(self, on: bool, rumble_type: str = "YEAH"):
        rumble_off = b'\x00\x01\x40\x40\x00\x01\x40\x40'
        if not on:
            self.joycon.send_rumble_packet(rumble_off)
            return

        if rumble_type == "YEAH":
            rumble_on = b'\x00\x7f\x01\x71\x00\x7f\x01\x71'
        else:
            rumble_on = b'\x70\x7f\x70\x71\x70\x7f\x70\x71'
        self.joycon.send_rumble_packet(rumble_on)

    def disconnect(self):
        if hasattr(self, 'joycon'):
            self.set_rumble(False)


class WiimoteBackend(BaseControllerBackend):
    def connect(self):
        self.dev = hid.device()
        self.dev.open(self.info['vendor_id'], self.info['product_id'], self.info.get('serial'))
        self.dev.set_nonblocking(True)
        self.dev.write([0x12, 0x00, 0x31])
        self.set_rumble(False)
        self.stick_locked = False
        self.last_accel = {"x": 0, "y": 0, "z": 0}

    def get_motion_data(self):
        data = None
        while True:
            d = self.dev.read(32)
            if not d: break
            data = d

        if data and len(data) >= 6:
            btn_left = data[1] & 0x01
            btn_right = data[1] & 0x02
            
            if btn_right and not self.stick_locked:
                self.coach_action = 1
                self.stick_locked = True
            elif btn_left and not self.stick_locked:
                self.coach_action = -1
                self.stick_locked = True
            elif not btn_right and not btn_left:
                self.stick_locked = False

            ax = ((data[3] - 128) / 25.0 * 9.80665) * self.multiplier
            ay = ((data[4] - 128) / 25.0 * 9.80665) * self.multiplier
            az = ((data[5] - 128) / 25.0 * 9.80665) * self.multiplier
            self.last_accel = {"x": ax, "y": ay, "z": az}

        return self.last_accel

    def set_rumble(self, on: bool, rumble_type: str = "YEAH"):
        cmd = 0x11 if on else 0x10
        buf = bytearray(22)
        buf[0] = 0x11
        buf[1] = cmd
        try:
            self.dev.write(buf)
        except Exception:
            pass

    def disconnect(self):
        try:
            self.set_rumble(False)
            self.dev.close()
        except Exception:
            pass


class PSMoveBackend(BaseControllerBackend):
    def connect(self):
        self.dev = hid.device()
        self.dev.open(self.info['vendor_id'], self.info['product_id'], self.info.get('serial'))
        self.dev.set_nonblocking(True)
        self.last_accel = {"x": 0, "y": 0, "z": 0}
        self.stick_locked = False

    def get_motion_data(self):
        data = None
        while True:
            d = self.dev.read(64)
            if not d: break
            data = d

        if data and len(data) > 20:
            btn_right = data[2] & 0x20
            btn_left = data[2] & 0x80
            
            if btn_right and not self.stick_locked:
                self.coach_action = 1
                self.stick_locked = True
            elif btn_left and not self.stick_locked:
                self.coach_action = -1
                self.stick_locked = True
            elif not btn_right and not btn_left:
                self.stick_locked = False

            def read_16(idx):
                val = data[idx] | (data[idx+1] << 8)
                return val - 65536 if val > 32767 else val

            ax = ((read_16(13) / 4096.0) * 9.80665) * self.multiplier
            ay = ((read_16(15) / 4096.0) * 9.80665) * self.multiplier
            az = ((read_16(17) / 4096.0) * 9.80665) * self.multiplier
            self.last_accel = {"x": ax, "y": ay, "z": az}

        return self.last_accel

    def set_rumble(self, on: bool, rumble_type: str = "YEAH"):
        r, g, b = (100, 0, 150) if not on else ((255, 200, 0) if rumble_type == "YEAH" else (0, 255, 255))
        rumble_val = 0xFF if on else 0x00

        if self.info['product_id'] in (0x03D5, 0x0CE6):
            try:
                self.dev.write([0x02, 0x00, r, g, b, 0x00, rumble_val])
            except Exception:
                pass
                
        elif self.info['product_id'] == 0x0C5E:
            buf = bytearray(78)
            buf[0] = 0x11
            buf[1] = 0xC0
            buf[2] = 0x04  
            buf[3] = 0x07
            
            buf[4] = rumble_val
            buf[5] = rumble_val
            buf[6] = r
            buf[7] = g
            buf[8] = b
            
            crc = binascii.crc32(b'\xa2' + buf[:74]) & 0xFFFFFFFF
            buf[74] = crc & 0xFF
            buf[75] = (crc >> 8) & 0xFF
            buf[76] = (crc >> 16) & 0xFF
            buf[77] = (crc >> 24) & 0xFF
            
            try:
                self.dev.write(buf)
            except Exception:
                pass

    def disconnect(self):
        try:
            self.set_rumble(False)
            self.dev.close()
        except Exception:
            pass

# ==========================================
# MAIN CONTROLLER BRIDGE
# ==========================================

class DanceSessionController:
    def __init__(self, room_code, user_data, device_info, ui_queue, debug_mode=False, multiplier=1):
        self.room_code = room_code.strip().upper()
        self.user_data = user_data or {}
        self.device_info = device_info
        self.ui_queue = ui_queue
        self.debug_mode = debug_mode
        self.multiplier = multiplier
        
        self.ws = None
        self.loop = None
        self.running = True
        self.socket_id = None
        
        self.coach_index = 0
        self.max_coaches = 1
        
        self.last_score = 0
        self.last_stars = 0
        
        self.start_time = time.time()
        self.current_song_time = 0.0
        self.last_clock_local = 0.0
        self.in_coach_lobby = False
        self.current_map = ""

        if self.device_info['type'] == "JOYCON":
            self.backend = JoyConBackend(self.device_info, multiplier)
        elif self.device_info['type'] == "WIIMOTE":
            self.backend = WiimoteBackend(self.device_info, multiplier)
        elif self.device_info['type'] == "PSMOVE":
            self.backend = PSMoveBackend(self.device_info, multiplier)

    def log_debug(self, msg):
        if self.debug_mode:
            self.ui_queue.put(("DEBUG", f"[{time.strftime('%H:%M:%S')}] {msg}"))

    def connect_device(self):
        try:
            self.ui_queue.put(("LOG", f"Connecting to {self.device_info['label']}..."))
            self.backend.connect()
            self.ui_queue.put(("LOG", "Device connected & ready!"))
            return True
        except Exception as e:
            self.ui_queue.put(("ERROR", f"Device Error: {e}"))
            self.running = False
            return False

    async def play_rumble(self, rumble_type: str):
        duration = 0.70 if rumble_type == "YEAH" else 0.30
        try:
            self.backend.set_rumble(True, rumble_type)
            await asyncio.sleep(duration)
            self.backend.set_rumble(False)
        except Exception:
            pass

    async def ping_loop(self, ws):
        while self.running:
            try:
                await ws.send(json.dumps({"func": "ping"}))
                self.log_debug("Sent ping heartbeat.")
                for _ in range(40):
                    if not self.running:
                        break
                    await asyncio.sleep(0.5)
            except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                break

    async def send_coach_pick(self, ws, coach_index=0):
        self.selected_coach = coach_index
        pick_payload = {
            "func": "action",
            "action": "coach:pick",
            "payload": {
                "coach": coach_index,
                "username": self.user_data.get("displayName") or self.user_data.get("username") or "Dancer",
                "avatarUrl": self.user_data.get("avatarUrl", "https://public-cdn.jdbest.online/avatars/470.png"),
                "skinUrl": self.user_data.get("skinUrl", "https://public-cdn.jdbest.online/skins/0.png"),
                "country": self.user_data.get("country", "US")
            }
        }
        await ws.send(json.dumps(pick_payload))
        self.ui_queue.put(("COACH", {"index": coach_index, "max": self.max_coaches}))
        self.log_debug(f"Picked coach {coach_index + 1}/{self.max_coaches}")

    async def recv_loop(self, ws):
        while self.running:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=0.5)
                data = json.loads(message)
                func = data.get("func")

                if func == "pong":
                    pass
                elif func == "roomJoined":
                    self.socket_id = data.get("youSocketId")
                    self.ui_queue.put(("LOG", f"Joined Room! (Socket: {self.socket_id})"))
                    self.log_debug(f"Room Joined: {self.room_code}")
                elif func == "error":
                    msg = data.get("message", "Unknown error")
                    self.ui_queue.put(("ERROR", f"Server: {msg}"))
                    self.running = False
                    break
                elif func == "action":
                    action = data.get("action")
                    payload = data.get("payload", {})

                    if action == "coach:enter":
                        self.max_coaches = payload.get("count", 1)
                        self.coach_index = 0
                        self.in_coach_lobby = True                      
                        self.current_map = payload.get("mapName", "")   
                        await self.send_coach_pick(ws, 0)
                        self.ui_queue.put(("LOG", "In Coach Lobby - Select coach or press A to start!"))
                        self.log_debug(f"Coach Lobby Opened. Max Coaches: {self.max_coaches}")

                    elif action == "song:start":
                        self.in_coach_lobby = False                     
                        self.last_score = 0
                        self.last_stars = 0
                        title = payload.get("title", "Unknown Title")
                        artist = payload.get("artist", "Unknown Artist")
                        self.ui_queue.put(("SONG_START", f"{title} - {artist}"))
                        self.current_song_time = 0.0
                        self.last_clock_local = time.time()
                        self.log_debug(f"Song Started: {title}")

                    elif action == "hud:clock":
                        if "t" in payload:
                            self.current_song_time = float(payload["t"])
                            self.last_clock_local = time.time()

                    elif action == "score":
                        target_socket = payload.get("socketId")
                        if self.socket_id is None or target_socket == self.socket_id:
                            rating = payload.get("rating", "none").upper()
                            score = payload.get("score", 0)
                            stars = payload.get("stars", 0)
                            
                            self.ui_queue.put(("SCORE", {"rating": rating, "score": score, "stars": stars}))

                            trigger_star = False
                            if stars > self.last_stars:
                                trigger_star = True
                            if score >= 11000 and self.last_score < 11000:
                                trigger_star = True
                            if score >= 12000 and self.last_score < 12000:
                                trigger_star = True
                            if score >= 13000 and self.last_score < 13000:
                                trigger_star = True

                            self.last_score = score
                            self.last_stars = stars

                            if rating == "YEAH":
                                self.log_debug("GOLD MOVE! Triggering violent rumble.")
                                asyncio.create_task(self.play_rumble("YEAH"))
                            elif trigger_star:
                                self.log_debug(f"NEW STAR! Score: {score}")
                                asyncio.create_task(self.play_rumble("STAR"))

                    elif action in ("song:end", "recap:ready"):
                        self.ui_queue.put(("LOG", "Song Finished! Great dancing!"))
                        self.log_debug("Song Ended / Recap Ready.")

            except asyncio.TimeoutError:
                continue
            except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                if self.running:
                    self.ui_queue.put(("LOG", "Disconnected from server."))
                self.running = False
                break
            except Exception as e:
                self.ui_queue.put(("LOG", f"Recv error: {e}"))

    async def motion_loop(self, ws):
        sample_interval = 1.0 / 60.0  
        batch_flush_interval = 0.100  
        
        while self.running:
            samples = []
            flush_start = time.time()
            
            while time.time() - flush_start < batch_flush_interval and self.running:
                loop_start = time.time()
                try:
                    accel = self.backend.get_motion_data()
                    coach_act = self.backend.get_coach_action()
                    btn_a = self.backend.get_btn_a()
                    
                    if coach_act == 1 and self.coach_index < self.max_coaches - 1:
                        self.coach_index += 1
                        asyncio.create_task(self.send_coach_pick(ws, self.coach_index))
                    elif coach_act == -1 and self.coach_index > 0:
                        self.coach_index -= 1
                        asyncio.create_task(self.send_coach_pick(ws, self.coach_index))

                    if btn_a and getattr(self, 'in_coach_lobby', False):
                        self.in_coach_lobby = False
                        start_payload = {
                            "func": "action",
                            "action": "song:start",
                            "payload": {
                                "mapName": getattr(self, 'current_map', "")
                            }
                        }
                        asyncio.create_task(ws.send(json.dumps(start_payload)))
                        self.ui_queue.put(("LOG", "Starting map via Joy-Con!"))

                    if self.last_clock_local > 0:
                        timestamp_ms = self.current_song_time + ((time.time() - self.last_clock_local) * 1000.0)
                    else:
                        timestamp_ms = (time.time() - self.start_time) * 1000.0

                    samples.append({
                        "t": round(timestamp_ms, 2), 
                        "x": round(accel["x"], 3), 
                        "y": round(accel["y"], 3), 
                        "z": round(accel["z"], 3)
                    })
                except Exception:
                    pass
                    
                elapsed = time.time() - loop_start
                await asyncio.sleep(max(0.001, sample_interval - elapsed))

            if samples and self.running:
                payload = {
                    "func": "screenMsg",
                    "payload": {
                        "kind": "accel",
                        "samples": samples
                    }
                }
                try:
                    await ws.send(json.dumps(payload))
                except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                    break

    async def run(self):
        self.loop = asyncio.get_running_loop()
        
        if not self.connect_device():
            return

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Origin": "https://controller.jdbest.online"
        }
        
        token = self.user_data.get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self.ui_queue.put(("LOG", f"Connecting to Room {self.room_code}..."))

        try:
            async with websockets.connect(WS_URL, additional_headers=headers) as ws:
                self.ws = ws
                
                join_payload = {
                    "func": "join",
                    "code": self.room_code,
                    "token": token or None,
                    "guestName": self.user_data.get("displayName") or self.user_data.get("username") or "Dancer",
                    "guestCountry": self.user_data.get("country", "US"),
                    "guestAvatarUrl": self.user_data.get("avatarUrl", "https://public-cdn.jdbest.online/avatars/470.png"),
                    "guestSkinUrl": self.user_data.get("skinUrl", "https://public-cdn.jdbest.online/skins/0.png")
                }
                
                join_payload = {k: v for k, v in join_payload.items() if v is not None}
                
                await ws.send(json.dumps(join_payload))
                self.ui_queue.put(("LOG", "Handshake sent! Waiting for room..."))

                await asyncio.gather(
                    self.ping_loop(ws),
                    self.recv_loop(ws),
                    self.motion_loop(ws)
                )
        except Exception as e:
            self.ui_queue.put(("ERROR", f"WebSocket Error: {e}"))
        finally:
            self.running = False
            self.backend.disconnect()

    def stop(self):
        self.running = False
        if self.ws and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)


# ==========================================
# GUI APPLICATION
# ==========================================

class JDBestApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Just Dance Best - Controller Bridge")
        self.geometry("400x620")
        self.resizable(False, False)
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        
        icon_path = resource_path("icon.png")
        if os.path.exists(icon_path):
            try:
                icon_img = ImageTk.PhotoImage(Image.open(icon_path))
                self.iconphoto(True, icon_img)
            except Exception as e:
                print(f"Icon error: {e}")

        self.user_data = {}
        self.device_list = []
        self.controller = None
        self.ui_queue = queue.Queue()
        self.config_file = "jdb_config.json"
        
        self.debug_mode = ctk.BooleanVar(value=False)
        self.motion_multiplier_var = ctk.DoubleVar(value=1)

        self.container = ctk.CTkFrame(self)
        self.container.pack(fill="both", expand=True, padx=20, pady=(20, 10))
        
        ctk.CTkLabel(self, text="by akao | Extended support for Wiimote & PS Move", text_color="#666666", font=ctk.CTkFont(size=11)).pack(side="bottom", pady=(0, 20))

        if self.load_saved_login():
            self.show_setup_screen()
        else:
            self.show_login_screen()
            
        self.process_queue()

    def load_saved_login(self):
        return False

    def process_queue(self):
        try:
            while True:
                msg_type, msg_data = self.ui_queue.get_nowait()
                
                if msg_type == "LOG":
                    if hasattr(self, 'status_label') and self.status_label.winfo_exists():
                        self.status_label.configure(text=msg_data, text_color="#A0A0A0")
                elif msg_type == "ERROR":
                    if hasattr(self, 'status_label') and self.status_label.winfo_exists():
                        self.status_label.configure(text=msg_data, text_color="#FF4D4D")
                elif msg_type == "SONG_START":
                    if hasattr(self, 'song_label') and self.song_label.winfo_exists():
                        self.song_label.configure(text=msg_data)
                elif msg_type == "COACH":
                    if hasattr(self, 'coach_label') and self.coach_label.winfo_exists():
                        current = msg_data['index'] + 1
                        total = msg_data['max']
                        self.coach_label.configure(text=f"Selected Coach: {current} / {total}")
                elif msg_type == "SCORE":
                    if hasattr(self, 'score_label') and self.score_label.winfo_exists():
                        rating = msg_data.get("rating", "")
                        score = msg_data.get("score", 0)
                        stars = msg_data.get("stars", 0)
                        
                        color_map = {
                            "PERFECT": "#00E676", "SUPER": "#29B6F6", "GOOD": "#AB47BC",
                            "OK": "#FFA726", "YEAH": "#FFD700", "BAD": "#FF5252", "MISS": "#757575"
                        }
                        color = color_map.get(rating, "white")
                        star_icons = "★" * min(5, stars) + "☆" * max(0, 5 - stars)
                        
                        self.score_label.configure(text=f"Move: {rating}", text_color=color)
                        if hasattr(self, 'total_score_label') and self.total_score_label.winfo_exists():
                            self.total_score_label.configure(text=f"Score: {score:,}  |  {star_icons}")
                elif msg_type == "DEBUG":
                    if hasattr(self, 'debug_box') and self.debug_box and self.debug_box.winfo_exists():
                        self.debug_box.insert("end", msg_data + "\n")
                        self.debug_box.see("end")
                        
        except queue.Empty:
            pass
        self.after(80, self.process_queue)

    def clear_container(self):
        for widget in self.container.winfo_children():
            widget.destroy()

    def show_login_screen(self):
        self.clear_container()

        ctk.CTkLabel(self.container, text="Just Dance Best", font=ctk.CTkFont(size=26, weight="bold")).pack(pady=(20, 5))
        ctk.CTkLabel(self.container, text="PC Controller Bridge", font=ctk.CTkFont(size=13), text_color="#A0A0A0").pack(pady=(0, 25))

        self.err_label = ctk.CTkLabel(self.container, text="", text_color="#FF4D4D")
        self.err_label.pack()

        self.entry_user = ctk.CTkEntry(self.container, placeholder_text="Username or Email", width=270, height=38)
        self.entry_user.pack(pady=8)

        self.entry_pass = ctk.CTkEntry(self.container, placeholder_text="Password", show="*", width=270, height=38)
        self.entry_pass.pack(pady=8)

        btn_login = ctk.CTkButton(self.container, text="Log In", width=270, height=40, font=ctk.CTkFont(weight="bold"), command=self.do_login)
        btn_login.pack(pady=(20, 10))

        btn_guest = ctk.CTkButton(self.container, text="Play as Guest", width=270, height=38, fg_color="transparent", border_width=1, command=self.do_guest)
        btn_guest.pack(pady=5)

    def do_login(self):
        username = self.entry_user.get().strip()
        password = self.entry_pass.get().strip()
        if not username or not password:
            self.err_label.configure(text="Enter username and password.")
            return

        self.err_label.configure(text="Authenticating...", text_color="gray")
        self.update()

        try:
            res = requests.post(f"{API_BASE}/auth/v1/login", json={"identifier": username, "password": password}, timeout=6)
            if res.status_code == 200:
                data = res.json()
                self.user_data = data.get("user", {})
                self.user_data["token"] = data.get("token")
                        
                self.show_setup_screen()
            else:
                self.err_label.configure(text="Invalid username or password.", text_color="#FF4D4D")
        except Exception as e:
            self.err_label.configure(text=f"API Connection Error: {e}", text_color="#FF4D4D")

    def do_guest(self):
        dialog = ctk.CTkInputDialog(text="Enter your Guest Name:", title="Guest Login")
        guest_name = dialog.get_input()
        
        if guest_name is None:
            return 
            
        guest_name = guest_name.strip() or "Dancer"
        rand_avatar = random.randint(1, 1870)
        rand_skin = random.randint(1, 170)
        
        self.user_data = {
            "displayName": guest_name,
            "username": guest_name,
            "country": "US",
            "avatarUrl": f"https://public-cdn.jdbest.online/avatars/{rand_avatar}.png",
            "skinUrl": f"https://public-cdn.jdbest.online/skins/{rand_skin}.png"
        }
        self.show_setup_screen()

    def do_logout(self):
        if os.path.exists(self.config_file):
            os.remove(self.config_file)
        self.user_data = {}
        self.show_login_screen()

    def show_setup_screen(self):
        self.clear_container()
        name = self.user_data.get('displayName') or self.user_data.get('username') or 'Guest'
        
        ctk.CTkLabel(self.container, text=f"Welcome, {name}!", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(15, 10))

        self.entry_room = ctk.CTkEntry(self.container, placeholder_text="6-Digit Room Code", width=270, height=45, font=ctk.CTkFont(size=20, weight="bold"), justify="center")
        self.entry_room.pack(pady=10)

        ctk.CTkLabel(self.container, text="Select Connected Device:", font=ctk.CTkFont(size=13)).pack(pady=(10, 2))
        
        self.device_dropdown = ctk.CTkOptionMenu(self.container, values=["Scanning..."], width=270, height=36)
        self.device_dropdown.pack(pady=4)

        btn_refresh = ctk.CTkButton(self.container, text="↻ Rescan Bluetooth Devices", width=270, fg_color="#333333", hover_color="#444444", command=self.scan_controllers)
        btn_refresh.pack(pady=4)

        self.scan_controllers()
        
        # Motion Sensitivity Slider
        self.multiplier_label = ctk.CTkLabel(self.container, text=f"Motion Sensitivity: {self.motion_multiplier_var.get():.2f}x", font=ctk.CTkFont(size=12))
        self.multiplier_label.pack(pady=(5, 0))

        def update_multiplier_label(val):
            self.multiplier_label.configure(text=f"Motion Sensitivity: {float(val):.2f}x")

        self.multiplier_slider = ctk.CTkSlider(self.container, from_=0.5, to=3.0, variable=self.motion_multiplier_var, command=update_multiplier_label, width=270)
        self.multiplier_slider.pack(pady=(0, 10))

        self.chk_debug = ctk.CTkCheckBox(self.container, text="Enable Debug Mode", variable=self.debug_mode)
        self.chk_debug.pack(pady=5)

        btn_connect = ctk.CTkButton(self.container, text="Dance Now!", width=270, height=42, fg_color="#8A2BE2", hover_color="#7B1FA2", font=ctk.CTkFont(size=15, weight="bold"), command=self.start_dancing)
        btn_connect.pack(pady=(15, 10))

        btn_back = ctk.CTkButton(self.container, text="Switch Account / Logout", width=270, fg_color="transparent", command=self.do_logout)
        btn_back.pack(pady=2)

    def scan_controllers(self):
        self.device_list = []
        labels = []
        for device in hid.enumerate():
            vid = device.get('vendor_id')
            pid = device.get('product_id')
            
            if vid == 0x057E and pid in (0x2006, 0x2007):
                side = "Right" if pid == 0x2007 else "Left"
                serial = device.get('serial_number') or f"BT_{side}"
                label = f"Joy-Con ({side}) - {serial[:8]}"
                labels.append(label)
                self.device_list.append({
                    "label": label, "serial": serial, "vendor_id": vid, "product_id": pid, "side": "R" if side == "Right" else "L", "type": "JOYCON"
                })
            elif vid == 0x057E and pid in (0x0306, 0x0330):
                serial = device.get('serial_number') or "Wii"
                label = f"Wiimote - {serial[:8]}"
                labels.append(label)
                self.device_list.append({
                    "label": label, "serial": serial, "vendor_id": vid, "product_id": pid, "type": "WIIMOTE"
                })
            elif vid == 0x054C and pid in (0x03D5, 0x0CE6, 0x0C5E):
                serial = device.get('serial_number') or "PS_Move"
                label = f"PS Move - {serial[:8]}"
                labels.append(label)
                self.device_list.append({
                    "label": label, "serial": serial, "vendor_id": vid, "product_id": pid, "type": "PSMOVE"
                })
        
        unique_labels = []
        self.unique_devices = []
        for d in self.device_list:
            if d['label'] not in unique_labels:
                unique_labels.append(d['label'])
                self.unique_devices.append(d)

        if unique_labels:
            self.device_dropdown.configure(values=unique_labels)
            self.device_dropdown.set(unique_labels[0])
        else:
            self.device_dropdown.configure(values=["No Supported Controllers"])
            self.device_dropdown.set("No Supported Controllers")

    def start_dancing(self):
        room = self.entry_room.get().strip().upper()
        selected_label = self.device_dropdown.get()
        multiplier = self.motion_multiplier_var.get()

        if len(room) != 6:
            return
        if "No Supported Controllers" in selected_label:
            return

        selected_dev = next((d for d in self.unique_devices if d['label'] == selected_label), None)
        if not selected_dev:
            return

        self.clear_container()

        ctk.CTkLabel(self.container, text="Live Session", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(15, 5))
        ctk.CTkLabel(self.container, text=f"Room: {room}", font=ctk.CTkFont(size=16), text_color="#CE93D8").pack(pady=2)
        
        self.song_label = ctk.CTkLabel(self.container, text="Waiting for song...", font=ctk.CTkFont(size=14, weight="bold"))
        self.song_label.pack(pady=(10, 5))

        self.status_label = ctk.CTkLabel(self.container, text="Starting Thread...", text_color="#A0A0A0")
        self.status_label.pack(pady=5)
        
        self.coach_label = ctk.CTkLabel(self.container, text="Selected Coach: 1 / 1", font=ctk.CTkFont(size=14, weight="bold"), text_color="#29B6F6")
        self.coach_label.pack(pady=(5, 5))

        self.score_label = ctk.CTkLabel(self.container, text="Move: Ready", font=ctk.CTkFont(size=24, weight="bold"), text_color="#00E676")
        self.score_label.pack(pady=(15, 5))

        self.total_score_label = ctk.CTkLabel(self.container, text="Score: 0  |  ☆☆☆☆☆", font=ctk.CTkFont(size=16), text_color="#FFD700")
        self.total_score_label.pack(pady=5)

        if self.debug_mode.get():
            self.debug_box = ctk.CTkTextbox(self.container, height=90, font=ctk.CTkFont(size=10))
            self.debug_box.pack(pady=10, fill="x", padx=10)
        else:
            self.debug_box = None

        btn_disconnect = ctk.CTkButton(self.container, text="Exit", width=220, height=40, fg_color="#D32F2F", hover_color="#C62828", command=self.stop_dancing)
        btn_disconnect.pack(pady=(15, 10))

        self.controller = DanceSessionController(room, self.user_data, selected_dev, self.ui_queue, self.debug_mode.get(), multiplier)
        threading.Thread(target=self.run_async_loop, daemon=True).start()

    def run_async_loop(self):
        asyncio.run(self.controller.run())

    def stop_dancing(self):
        if self.controller:
            self.controller.stop()
        self.show_setup_screen()


if __name__ == "__main__":
    app = JDBestApp()
    app.mainloop()