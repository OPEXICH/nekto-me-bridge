import sys
import os
import json
import time
import threading
import subprocess
import urllib.request
import urllib.parse
import asyncio
import websockets
import customtkinter as ctk
import tkinter as tk

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# Store Chrome profiles in user AppData so recompiling exe NEVER wipes cookies/captchas!
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NektoBridgeData")
PROFILE_A = os.path.join(DATA_DIR, "chrome_profile_a")
PROFILE_B = os.path.join(DATA_DIR, "chrome_profile_b")
PORT_A = 9222
PORT_B = 9223
AUDIO_RELAY_PORT = 9224

# Ensure permanent profile dirs exist
os.makedirs(PROFILE_A, exist_ok=True)
os.makedirs(PROFILE_B, exist_ok=True)


def setup_chrome_permissions(profile_dir):
    try:
        default_dir = os.path.join(profile_dir, "Default")
        os.makedirs(default_dir, exist_ok=True)
        pref_file = os.path.join(default_dir, "Preferences")

        prefs = {}
        if os.path.exists(pref_file):
            try:
                with open(pref_file, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
            except Exception:
                prefs = {}

        profile_pref = prefs.setdefault("profile", {})
        content_settings = profile_pref.setdefault("content_settings", {})
        exceptions = content_settings.setdefault("exceptions", {})

        media_mic = exceptions.setdefault("media_stream_mic", {})
        media_mic["https://nekto.me:443,*"] = {
            "last_modified": "13370000000000000",
            "setting": 1
        }

        sound = exceptions.setdefault("sound", {})
        sound["https://nekto.me:443,*"] = {
            "last_modified": "13370000000000000",
            "setting": 1
        }

        with open(pref_file, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


class AudioRelayServer:
    """WebRTC signaling relay between Chrome instances on 127.0.0.1:9224"""
    def __init__(self, port=AUDIO_RELAY_PORT):
        self.port = port
        self.connections = {}
        self.loop = None
        self.thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def handler(ws):
            role = None
            try:
                init_msg = await ws.recv()
                data = json.loads(init_msg)
                role = data.get("role")
                self.connections[role] = ws

                target = "b" if role == "a" else "a"
                dest = self.connections.get(target)
                if dest:
                    try:
                        await dest.send(json.dumps({"type": "peer_connected", "peer": role}))
                        await ws.send(json.dumps({"type": "peer_connected", "peer": target}))
                    except Exception:
                        pass

                async for message in ws:
                    target = "b" if role == "a" else "a"
                    dest = self.connections.get(target)
                    if dest:
                        try:
                            await dest.send(message)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                if role in self.connections:
                    del self.connections[role]
                target = "b" if role == "a" else "a"
                dest = self.connections.get(target)
                if dest:
                    try:
                        await dest.send(json.dumps({"type": "peer_disconnected", "peer": role}))
                    except Exception:
                        pass

        async def main():
            server = await websockets.serve(handler, "127.0.0.1", self.port)
            await server.wait_closed()

        try:
            self.loop.run_until_complete(main())
        except Exception:
            pass


def get_audio_bridge_js(role, port=AUDIO_RELAY_PORT):
    return f"""
    (() => {{
        if (window.top !== window.self) return 'in_iframe';

        // 1. Storage & State Sanitization
        try {{
            localStorage.removeItem('ban_bad_ice');
            localStorage.removeItem('count_bad_ice');
        }} catch(e) {{}}

        // 2. Webpack Module Hook - Neutralizes anti-cheat monitor and sets up audio relay
        let realWebpackJsonp = window['webpackJsonp'] || [];

        function hookChunk(chunk) {{
            if (!chunk || !chunk[1]) return;
            const modules = chunk[1];

            // Module bfa5: WebRTC Controller 'R'
            if (modules['bfa5']) {{
                const origBfa5 = modules['bfa5'];
                modules['bfa5'] = function(m, exports, req) {{
                    origBfa5.apply(this, arguments);
                    if (exports && exports.a) {{
                        const R = exports.a;
                        window.__nektoR = R;

                        // Neutralize track integrity monitor completely so replaceTrack is never flagged
                        R.startTrackIntegrityMonitor = function() {{
                            console.log('[ANTI-BAN] startTrackIntegrityMonitor blocked.');
                        }};

                        // Guarantee ICE candidate status so hadLocalIce is ALWAYS true
                        const origStartPC = R.startPeerConnection;
                        if (origStartPC) {{
                            R.startPeerConnection = function(...args) {{
                                const ret = origStartPC.apply(this, args);
                                if (this.peer) {{
                                    this.peer.hadLocalRelayCandidate = true;
                                    this.peer.hadLocalStunCandidate = true;
                                    this.peer.hadRemoteRelayCandidate = true;
                                    this.peer.hadRemoteStunCandidate = true;
                                }}
                                return ret;
                            }};
                        }}
                    }}
                }};
            }}
        }}

        const origPush = realWebpackJsonp.push.bind(realWebpackJsonp);
        realWebpackJsonp.push = function(...chunks) {{
            for (const ch of chunks) hookChunk(ch);
            return origPush.apply(this, chunks);
        }};

        Object.defineProperty(window, 'webpackJsonp', {{
            configurable: true,
            enumerable: true,
            get() {{ return realWebpackJsonp; }},
            set(v) {{
                realWebpackJsonp = v;
                const p = v.push.bind(v);
                v.push = function(...chunks) {{
                    for (const ch of chunks) hookChunk(ch);
                    return p.apply(this, chunks);
                }};
            }}
        }});

        // 3. Vue / Vuex Anti-Ban hooks
        const hookVueTree = () => {{
            const el = document.querySelector('.wraps, [class*="chat"], #app') || Array.from(document.querySelectorAll('*')).find(n => n.__vue__);
            if (el && el.__vue__) {{
                const vm = el.__vue__;

                // Neutralize rtc.track_integrity on Vue prototype
                let proto = Object.getPrototypeOf(vm);
                while (proto && !proto.$emit) proto = Object.getPrototypeOf(proto);
                if (proto && !proto.__antiBanEmitHooked) {{
                    proto.__antiBanEmitHooked = true;
                    const origEmit = proto.$emit;
                    proto.$emit = function(ev, ...args) {{
                        if (ev === 'rtc.track_integrity') return this;
                        return origEmit.apply(this, [ev, ...args]);
                    }};
                }}

                // Neutralize reportRtcTrackReplacement & banBadIce in Vuex Store
                if (vm.$store && !vm.$store.__antiBanStoreHooked) {{
                    vm.$store.__antiBanStoreHooked = true;
                    const sp = Object.getPrototypeOf(vm.$store);
                    const origDispatch = sp.dispatch;
                    sp.dispatch = function(act, ...args) {{
                        if (typeof act === 'string' && (act.includes('reportRtcTrackReplacement') || act.includes('setCountBadIce'))) {{
                            return Promise.resolve(false);
                        }}
                        return origDispatch.apply(this, [act, ...args]);
                    }};

                    const origCommit = sp.commit;
                    sp.commit = function(mut, ...args) {{
                        if (typeof mut === 'string' && (mut.includes('setBanBadIce') || mut.includes('recordRtcTrackReplacement'))) {{
                            return;
                        }}
                        return origCommit.apply(this, [mut, ...args]);
                    }};

                    if (vm.$store.state && vm.$store.state.user) {{
                        vm.$store.state.user.banIce = false;
                        vm.$store.state.user.countBadIce = 0;
                    }}
                    if (vm.$store.state && vm.$store.state.system) {{
                        vm.$store.state.system.rtcTrackReplacementDetected = false;
                        vm.$store.state.system.rtcTrackReplacementCount = 0;
                    }}
                }}
            }}
        }};

        setInterval(hookVueTree, 200);

        // 4. Bi-directional WebRTC Loopback Audio Bridge
        if (window.__voiceBridgeActive) return 'already_active';
        window.__voiceBridgeActive = true;

        const ROLE = '{role}';
        const RELAY_PORT = {port};

        let ws = null;
        let pcBridge = null;
        let candidateQueue = [];
        let incomingBridgedTrack = null;

        // Create dummy silent track so bridge PC can negotiate audio immediately
        function createSilentTrack() {{
            try {{
                const actx = new (window.AudioContext || window.webkitAudioContext)();
                if (actx.state === 'suspended') actx.resume();
                const osc = actx.createOscillator();
                const gain = actx.createGain();
                gain.gain.value = 0.0001;
                osc.connect(gain);
                const dest = actx.createMediaStreamDestination();
                gain.connect(dest);
                osc.start();
                return dest.stream.getAudioTracks()[0];
            }} catch(e) {{
                return null;
            }}
        }}

        function initBridgePC() {{
            try {{
                if (pcBridge) {{
                    try {{ pcBridge.close(); }} catch(e) {{}}
                }}
                pcBridge = new RTCPeerConnection({{ iceServers: [] }});
                window.__pcBridge = pcBridge;

                pcBridge.onicecandidate = (e) => {{
                    if (e.candidate && ws && ws.readyState === WebSocket.OPEN) {{
                        ws.send(JSON.stringify({{ type: 'candidate', candidate: e.candidate }}));
                    }}
                }};

                pcBridge.ontrack = (e) => {{
                    const track = e.track || (e.streams && e.streams[0] && e.streams[0].getAudioTracks()[0]);
                    if (track && track.readyState === 'live') {{
                        incomingBridgedTrack = track;
                        try {{
                            if (!window.__bridgeAudioSink) {{
                                const a = document.createElement('audio');
                                a.autoplay = true;
                                a.muted = true;
                                a.style.display = 'none';
                                document.body.appendChild(a);
                                window.__bridgeAudioSink = a;
                            }}
                            window.__bridgeAudioSink.srcObject = new MediaStream([track]);
                            window.__bridgeAudioSink.play().catch(() => {{}});
                        }} catch(err) {{}}
                        applyTrackToNekto(track);
                    }}
                }};

                // Pre-add a track so SDP offer/answer includes m=audio sendrecv
                const tr = getLocalStrangerTrack() || createSilentTrack();
                if (tr) {{
                    try {{ pcBridge.addTrack(tr, new MediaStream([tr])); }} catch(e) {{}}
                }}
            }} catch(e) {{}}
        }}

        function getLocalStrangerTrack() {{
            if (window.__nektoR && window.__nektoR.remoteStream) {{
                const trs = window.__nektoR.remoteStream.getAudioTracks();
                if (trs && trs.length > 0 && trs[0].readyState === 'live') return trs[0];
            }}
            const el = document.getElementById('audioStream') || document.querySelector('audio:not([muted])');
            if (el && el.srcObject) {{
                const trs = el.srcObject.getAudioTracks();
                if (trs && trs.length > 0 && trs[0].readyState === 'live') return trs[0];
            }}
            return null;
        }}

        function applyTrackToNekto(track) {{
            if (!track || track.readyState !== 'live') return;
            try {{
                const R = window.__nektoR;
                if (R && R.peer && R.peer.pc) {{
                    const senders = R.peer.pc.getSenders();
                    const s = senders.find(sd => (sd.track && sd.track.kind === 'audio') || sd.dtmf !== null || sd.dtmf !== undefined) || senders[0];
                    if (s && s.replaceTrack && s.track !== track) {{
                        s.replaceTrack(track);
                        console.log('[BRIDGE] Injected remote track into Nekto peer connection!');
                    }}
                }}
            }} catch(e) {{}}
        }}

        // Continuously ensure that the remote track from the other tab is applied to Nekto
        setInterval(() => {{
            // Periodic keep-alive / renegotiation check for Role A
            if (ROLE === 'a' && ws && ws.readyState === WebSocket.OPEN && pcBridge) {{
                if (pcBridge.iceConnectionState === 'new' || pcBridge.iceConnectionState === 'disconnected' || pcBridge.iceConnectionState === 'failed') {{
                    createAndSendOffer();
                }}
            }}

            if (incomingBridgedTrack) {{
                applyTrackToNekto(incomingBridgedTrack);
            }}
            const localTr = getLocalStrangerTrack();
            if (localTr && pcBridge) {{
                const senders = pcBridge.getSenders();
                const s = senders.find(sd => (sd.track && sd.track.kind === 'audio') || sd.dtmf !== null || sd.dtmf !== undefined) || senders[0];
                if (s && s.replaceTrack && s.track !== localTr) {{
                    s.replaceTrack(localTr);
                }}
            }}
        }}, 300);

        function connectSignaling() {{
            try {{
                ws = new WebSocket('ws://127.0.0.1:' + RELAY_PORT);
                ws.onopen = () => {{
                    ws.send(JSON.stringify({{ role: ROLE }}));
                    initBridgePC();
                }};

                ws.onmessage = async (e) => {{
                    try {{
                        const data = JSON.parse(e.data);
                        if (!pcBridge) initBridgePC();

                        if (data.type === 'peer_connected') {{
                            if (ROLE === 'a') {{
                                setTimeout(createAndSendOffer, 150);
                            }}
                        }} else if (data.type === 'offer') {{
                            await pcBridge.setRemoteDescription(new RTCSessionDescription(data.sdp));
                            while (candidateQueue.length > 0) {{
                                const c = candidateQueue.shift();
                                try {{ await pcBridge.addIceCandidate(new RTCIceCandidate(c)); }} catch(err) {{}}
                            }}
                            const answer = await pcBridge.createAnswer({{ offerToReceiveAudio: true, offerToReceiveVideo: false }});
                            await pcBridge.setLocalDescription(answer);
                            if (ws && ws.readyState === WebSocket.OPEN) {{
                                ws.send(JSON.stringify({{ type: 'answer', sdp: answer }}));
                            }}
                        }} else if (data.type === 'answer') {{
                            await pcBridge.setRemoteDescription(new RTCSessionDescription(data.sdp));
                            while (candidateQueue.length > 0) {{
                                const c = candidateQueue.shift();
                                try {{ await pcBridge.addIceCandidate(new RTCIceCandidate(c)); }} catch(err) {{}}
                            }}
                        }} else if (data.type === 'candidate') {{
                            if (pcBridge && pcBridge.remoteDescription && pcBridge.remoteDescription.type) {{
                                try {{ await pcBridge.addIceCandidate(new RTCIceCandidate(data.candidate)); }} catch(err) {{}}
                            }} else {{
                                candidateQueue.push(data.candidate);
                            }}
                        }}
                    }} catch(err) {{}}
                }};

                ws.onclose = () => setTimeout(connectSignaling, 1000);
                ws.onerror = () => {{ try {{ ws.close(); }} catch(e) {{}} }};
            }} catch(err) {{
                setTimeout(connectSignaling, 1000);
            }}
        }}

        async function createAndSendOffer() {{
            try {{
                if (!pcBridge || !ws || ws.readyState !== WebSocket.OPEN) return;
                const offer = await pcBridge.createOffer({{ offerToReceiveAudio: true, offerToReceiveVideo: false }});
                await pcBridge.setLocalDescription(offer);
                ws.send(JSON.stringify({{ type: 'offer', sdp: pcBridge.localDescription }}));
            }} catch(err) {{}}
        }}

        connectSignaling();

        return 'voice_bridge_ready';
    }})();
    """


class ChromeCDPClient:
    def __init__(self, port, name):
        self.port = port
        self.name = name
        self.ws_url = None
        self.req_id = 1

    def get_page_target(self):
        try:
            url = f"http://127.0.0.1:{self.port}/json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                targets = json.loads(resp.read().decode())
                for t in targets:
                    if t.get("type") == "page" and "nekto.me" in t.get("url", ""):
                        return t
                for t in targets:
                    if t.get("type") == "page":
                        return t
        except Exception:
            return None
        return None

    def _send_cdp(self, method, params=None):
        target = self.get_page_target()
        if not target:
            return None
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return None
        try:
            import websocket  # type: ignore
            ws = websocket.create_connection(ws_url, timeout=3, suppress_origin=True)
            self.req_id += 1
            curr_id = self.req_id
            msg = {"id": curr_id, "method": method}
            if params:
                msg["params"] = params
            ws.send(json.dumps(msg))
            while True:
                data = json.loads(ws.recv())
                if data.get("id") == curr_id:
                    ws.close()
                    return data
        except Exception:
            return None

    def execute_js(self, script):
        res = self._send_cdp("Runtime.evaluate", {"expression": script, "returnByValue": True})
        if res and "result" in res:
            return res.get("result", {}).get("result", {}).get("value")
        return None

    def add_init_script(self, script):
        self._send_cdp("Page.enable")
        res = self._send_cdp("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        return res is not None and "result" in res

    def navigate(self, url):
        return self._send_cdp("Page.navigate", {"url": url})

    def send_enter_key(self):
        target = self.get_page_target()
        if not target:
            return False
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return False
        try:
            import websocket  # type: ignore
            ws = websocket.create_connection(ws_url, timeout=2, suppress_origin=True)
            ws.send(json.dumps({
                "id": self.req_id,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "rawKeyDown",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13
                }
            }))
            self.req_id += 1
            ws.recv()
            ws.send(json.dumps({
                "id": self.req_id,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyUp",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13
                }
            }))
            self.req_id += 1
            ws.recv()
            ws.close()
            return True
        except Exception:
            return False

    def send_chat_message(self, text):
        escaped = json.dumps(text)
        script = f"""
        (() => {{
            const text = {escaped};
            let injected = false;

            // 1. If jQuery + emojioneArea plugin is active
            if (window.$) {{
                $('textarea').each(function() {{
                    try {{
                        const ea = $(this).data('emojioneArea');
                        if (ea) {{
                            ea.setText(text);
                            if (ea.editor && ea.editor[0]) {{
                                ea.editor[0].focus();
                                ea.editor[0].dispatchEvent(new Event('input', {{ bubbles: true }}));
                                ea.editor[0].dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                            injected = true;
                        }}
                    }} catch(e) {{}}
                }});
                $('.emojionearea').each(function() {{
                    try {{
                        if (this.emojioneArea) {{
                            this.emojioneArea.setText(text);
                            injected = true;
                        }}
                    }} catch(e) {{}}
                }});
            }}

            // 2. Direct DOM fallback
            const editors = document.querySelectorAll('.emojionearea-editor');
            editors.forEach(ed => {{
                ed.focus();
                ed.innerText = text;
                ed.dispatchEvent(new Event('input', {{ bubbles: true }}));
                ed.dispatchEvent(new Event('change', {{ bubbles: true }}));
                ed.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: 'a' }}));
                injected = true;
            }});

            const tas = document.querySelectorAll('#message_textarea, textarea');
            tas.forEach(ta => {{
                ta.value = text;
                ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }});

            // 3. Click send buttons immediately
            const sendBtns = document.querySelectorAll('.sendMessageBtn, .send_btn_circle, #sendMessageBtn, .mobileButtonSend, button[type="submit"]');
            sendBtns.forEach(btn => {{
                try {{
                    btn.classList.remove('disabled', 'opacityDisabled');
                    btn.removeAttribute('disabled');
                    btn.click();
                    if (btn.parentElement) {{
                        btn.parentElement.classList.remove('disabled', 'opacityDisabled');
                        btn.parentElement.click();
                    }}
                }} catch(e) {{}}
            }});

            return injected;
        }})()
        """
        self.execute_js(script)
        # Dispatch hardware Enter keystroke via CDP
        time.sleep(0.08)
        self.send_enter_key()



class NektoBridgeApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Cyber-Blue Theme
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("⚡ NEKTO.ME PRO")
        self.geometry("980x720")
        self.minsize(850, 600)
        self.configure(fg_color="#070c1e")

        self.proc_a = None
        self.proc_b = None
        self.cdp_a = ChromeCDPClient(PORT_A, "Собеседник 1")
        self.cdp_b = ChromeCDPClient(PORT_B, "Собеседник 2")

        self.is_bridge_active = True
        self.is_monitoring = True
        self.current_mode = "audiochat"  # or "chat"

        # Start local Audio Relay Server
        self.audio_relay = AudioRelayServer(AUDIO_RELAY_PORT)
        self.audio_relay.start()

        self.create_ui()

        # Start background monitor thread
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()

    def create_ui(self):
        # TOP HEADER
        self.header = ctk.CTkFrame(self, fg_color="#0b132b", corner_radius=12, border_width=1, border_color="#1c2e4a")
        self.header.pack(fill="x", padx=16, pady=(14, 10))

        title_box = ctk.CTkFrame(self.header, fg_color="transparent")
        title_box.pack(side="left", padx=14, pady=10)

        ctk.CTkLabel(
            title_box,
            text="⚡ NEKTO.ME",
            font=ctk.CTkFont(family="Consolas", size=22, weight="bold"),
            text_color="#00f0ff"
        ).pack(anchor="w", pady=6)

        # Mode toggles
        mode_box = ctk.CTkFrame(self.header, fg_color="transparent")
        mode_box.pack(side="right", padx=14, pady=10)

        self.btn_mode_voice = ctk.CTkButton(
            mode_box,
            text="🎙 ГОЛОСОВОЙ ЧАТ",
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=150,
            command=lambda: self.switch_mode("audiochat")
        )
        self.btn_mode_voice.pack(side="left", padx=6)

        self.btn_mode_text = ctk.CTkButton(
            mode_box,
            text="💬 ТЕКСТОВЫЙ ЧАТ",
            fg_color="#1e293b",
            hover_color="#334155",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=150,
            command=lambda: self.switch_mode("chat")
        )
        self.btn_mode_text.pack(side="left", padx=6)

        self.btn_bridge = ctk.CTkButton(
            mode_box,
            text="⚡ ПЕРЕСЫЛКА: ВКЛ",
            fg_color="#10b981",
            hover_color="#059669",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=160,
            command=self.toggle_bridge
        )
        self.btn_bridge.pack(side="left", padx=6)

        # STATUS ROW (PARTY A & PARTY B)
        self.status_row = ctk.CTkFrame(self, fg_color="transparent")
        self.status_row.pack(fill="x", padx=16, pady=4)

        # Card A
        self.card_a = ctk.CTkFrame(self.status_row, fg_color="#0e172e", corner_radius=10, border_width=1, border_color="#1e3a5f")
        self.card_a.pack(side="left", fill="x", expand=True, padx=(0, 6))

        ctk.CTkLabel(
            self.card_a,
            text="🔵 СОБЕСЕДНИК 1 (Окно слева)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8"
        ).pack(anchor="w", padx=14, pady=(10, 2))

        self.lbl_status_a = ctk.CTkLabel(
            self.card_a,
            text="⚪ ОЖИДАНИЕ ЗАПУСКА",
            font=ctk.CTkFont(family="Consolas", size=15, weight="bold"),
            text_color="#94a3b8"
        )
        self.lbl_status_a.pack(anchor="w", padx=14, pady=(0, 8))

        btns_a = ctk.CTkFrame(self.card_a, fg_color="transparent")
        btns_a.pack(fill="x", padx=12, pady=(0, 10))

        ctk.CTkButton(btns_a, text="🔍 Поиск", width=75, height=28, fg_color="#10b981", hover_color="#059669", command=lambda: self.trigger_action("a", "start")).pack(side="left", padx=3)
        ctk.CTkButton(btns_a, text="⏭ След.", width=75, height=28, fg_color="#f59e0b", hover_color="#d97706", command=lambda: self.trigger_action("a", "next")).pack(side="left", padx=3)
        ctk.CTkButton(btns_a, text="⏹ Стоп", width=75, height=28, fg_color="#ef4444", hover_color="#dc2626", command=lambda: self.trigger_action("a", "stop")).pack(side="left", padx=3)

        # Card B
        self.card_b = ctk.CTkFrame(self.status_row, fg_color="#0e172e", corner_radius=10, border_width=1, border_color="#1e3a5f")
        self.card_b.pack(side="left", fill="x", expand=True, padx=(6, 0))

        ctk.CTkLabel(
            self.card_b,
            text="🟣 СОБЕСЕДНИК 2 (Окно справа)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#c084fc"
        ).pack(anchor="w", padx=14, pady=(10, 2))

        self.lbl_status_b = ctk.CTkLabel(
            self.card_b,
            text="⚪ ОЖИДАНИЕ ЗАПУСКА",
            font=ctk.CTkFont(family="Consolas", size=15, weight="bold"),
            text_color="#94a3b8"
        )
        self.lbl_status_b.pack(anchor="w", padx=14, pady=(0, 8))

        btns_b = ctk.CTkFrame(self.card_b, fg_color="transparent")
        btns_b.pack(fill="x", padx=12, pady=(0, 10))

        ctk.CTkButton(btns_b, text="🔍 Поиск", width=75, height=28, fg_color="#10b981", hover_color="#059669", command=lambda: self.trigger_action("b", "start")).pack(side="left", padx=3)
        ctk.CTkButton(btns_b, text="⏭ След.", width=75, height=28, fg_color="#f59e0b", hover_color="#d97706", command=lambda: self.trigger_action("b", "next")).pack(side="left", padx=3)
        ctk.CTkButton(btns_b, text="⏹ Стоп", width=75, height=28, fg_color="#ef4444", hover_color="#dc2626", command=lambda: self.trigger_action("b", "stop")).pack(side="left", padx=3)

        # LAUNCHER BAR
        self.launcher_frame = ctk.CTkFrame(self, fg_color="#0b132b", corner_radius=10, border_width=1, border_color="#1e3a5f")
        self.launcher_frame.pack(fill="x", padx=16, pady=8)

        ctk.CTkLabel(
            self.launcher_frame,
            text="🚀 УПРАВЛЕНИЕ ОКНАМИ CHROME:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8"
        ).pack(side="left", padx=14, pady=8)

        self.btn_launch_both = ctk.CTkButton(
            self.launcher_frame,
            text="⚡ Запустить 2 окна Chrome (Слева и Справа)",
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.launch_both_chrome
        )
        self.btn_launch_both.pack(side="left", padx=10, pady=8)

        ctk.CTkButton(
            self.launcher_frame,
            text="🔄 Перезагрузить вкладки",
            fg_color="#334155",
            hover_color="#475569",
            width=170,
            command=self.reload_tabs
        ).pack(side="left", padx=6, pady=8)

        ctk.CTkButton(
            self.launcher_frame,
            text="🧹 Сбросить куки/бан",
            fg_color="#475569",
            hover_color="#ef4444",
            width=150,
            command=self.reset_profiles
        ).pack(side="left", padx=6, pady=8)

        # CHAT FEED & COMPOSER
        self.feed_frame = ctk.CTkFrame(self, fg_color="#090f24", corner_radius=12, border_width=1, border_color="#1e3a5f")
        self.feed_frame.pack(fill="both", expand=True, padx=16, pady=(4, 8))

        feed_header = ctk.CTkFrame(self.feed_frame, fg_color="transparent")
        feed_header.pack(fill="x", padx=14, pady=(8, 4))

        ctk.CTkLabel(
            feed_header,
            text="💬 ЖИВОЙ ЛОГ ДИАЛОГА И ПЕРЕСЫЛКИ СООБЩЕНИЙ:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#94a3b8"
        ).pack(side="left")

        ctk.CTkButton(
            feed_header,
            text="Очистить лог",
            width=100,
            height=24,
            fg_color="#1e293b",
            hover_color="#334155",
            command=self.clear_log
        ).pack(side="right")

        self.txt_log = ctk.CTkTextbox(
            self.feed_frame,
            fg_color="#050918",
            text_color="#e2e8f0",
            font=ctk.CTkFont(family="Consolas", size=13),
            corner_radius=8
        )
        self.txt_log.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.add_log("sys", "Приложение готово к работе.")

        # OPERATOR INTERVENTION
        self.composer_frame = ctk.CTkFrame(self, fg_color="#0b132b", corner_radius=10, border_width=1, border_color="#1e3a5f")
        self.composer_frame.pack(fill="x", padx=16, pady=(0, 14))

        self.input_text = ctk.CTkEntry(
            self.composer_frame,
            placeholder_text="Написать реплику от своего лица или подменить фразу...",
            font=ctk.CTkFont(size=13),
            fg_color="#050918",
            border_color="#1e3a5f"
        )
        self.input_text.pack(side="left", fill="x", expand=True, padx=(12, 8), pady=10)
        self.input_text.bind("<Return>", lambda e: self.send_operator_message("both"))

        ctk.CTkButton(
            self.composer_frame,
            text="В Чат 1",
            width=80,
            fg_color="#0284c7",
            hover_color="#0369a1",
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self.send_operator_message("a")
        ).pack(side="left", padx=3, pady=10)

        ctk.CTkButton(
            self.composer_frame,
            text="В Чат 2",
            width=80,
            fg_color="#9333ea",
            hover_color="#7e22ce",
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self.send_operator_message("b")
        ).pack(side="left", padx=3, pady=10)

        ctk.CTkButton(
            self.composer_frame,
            text="Обоим",
            width=90,
            fg_color="#10b981",
            hover_color="#059669",
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self.send_operator_message("both")
        ).pack(side="left", padx=(3, 12), pady=10)

    def add_log(self, party, text):
        colors = {
            "a": "#38bdf8",
            "b": "#c084fc",
            "op": "#34d399",
            "sys": "#94a3b8"
        }
        timestamp = time.strftime("%H:%M:%S")
        prefix = {
            "a": "[Собеседник 1 ➔ 2]:",
            "b": "[Собеседник 2 ➔ 1]:",
            "op": "[Оператор]:",
            "sys": "[Система]:"
        }.get(party, "[Инфо]:")

        self.txt_log.insert("end", f"[{timestamp}] {prefix} {text}\n")
        self.txt_log.see("end")

    def clear_log(self):
        self.txt_log.delete("1.0", "end")
        self.add_log("sys", "Лог очищен.")

    def launch_both_chrome(self):
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        half_w = sw // 2
        target_url = f"https://nekto.me/{self.current_mode}"

        self.add_log("sys", f"Запуск 2 окон Google Chrome ({self.current_mode})...")

        # 0. Terminate any dangling Chrome processes using our profiles
        ps_cleanup = r'''
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*NektoBridgeData*" } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
        '''
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cleanup], capture_output=True, timeout=5)
        except Exception:
            pass

        # 1. Setup native mic permissions in profile Preferences
        setup_chrome_permissions(PROFILE_A)
        setup_chrome_permissions(PROFILE_B)

        # 2. Clean stale lockfiles if previous instance closed uncleanly
        for prof in (PROFILE_A, PROFILE_B):
            for lf in ("lockfile", "SingletonLock"):
                try:
                    p = os.path.join(prof, lf)
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

        # Launch A (Left half) - start at about:blank to inject at birth before any page scripts
        args_a = [
            CHROME_PATH,
            f"--remote-debugging-port={PORT_A}",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE_A}",
            f"--window-position=0,0",
            f"--window-size={half_w},{sh-40}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
            "about:blank"
        ]

        # Launch B (Right half)
        args_b = [
            CHROME_PATH,
            f"--remote-debugging-port={PORT_B}",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE_B}",
            f"--window-position={half_w},0",
            f"--window-size={half_w},{sh-40}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
            "about:blank"
        ]

        try:
            self.proc_a = subprocess.Popen(args_a)
            self.proc_b = subprocess.Popen(args_b)
            self.add_log("sys", "✅ Окна Chrome успешно запущены.")

            def _setup_bridge():
                time.sleep(1.5)
                js_a = get_audio_bridge_js("a")
                js_b = get_audio_bridge_js("b")

                # Injects script into new documents BEFORE any page bundle evaluates
                self.cdp_a.add_init_script(js_a)
                self.cdp_b.add_init_script(js_b)

                # Navigate both windows to target
                self.cdp_a.navigate(target_url)
                self.cdp_b.navigate(target_url)

                self.add_log("sys", "⚡ Голосовая связь и защита от блокировок активированы.")

            threading.Thread(target=_setup_bridge, daemon=True).start()
        except Exception as e:
            self.add_log("sys", f"❌ Ошибка запуска Chrome: {e}")

    def reset_profiles(self):
        import shutil
        self.add_log("sys", "Закрытие Chrome и очистка профилей (сброс куки/банов)...")
        try:
            if self.proc_a:
                self.proc_a.kill()
            if self.proc_b:
                self.proc_b.kill()
        except Exception:
            pass

        ps_cleanup = r'''
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*NektoBridgeData*" } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
        '''
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cleanup], capture_output=True, timeout=5)
        except Exception:
            pass
        time.sleep(1)

        for p in (PROFILE_A, PROFILE_B):
            try:
                shutil.rmtree(p, ignore_errors=True)
                os.makedirs(p, exist_ok=True)
            except Exception:
                pass
        self.add_log("sys", "✅ Профили очищены! Все старые куки и блокировки сброшены.")

    def switch_mode(self, mode):
        self.current_mode = mode
        if mode == "audiochat":
            self.btn_mode_voice.configure(fg_color="#0284c7")
            self.btn_mode_text.configure(fg_color="#1e293b")
            self.add_log("sys", "Переключено на ГОЛОСОВОЙ ЧАТ (nekto.me/audiochat)")
        else:
            self.btn_mode_text.configure(fg_color="#0284c7")
            self.btn_mode_voice.configure(fg_color="#1e293b")
            self.add_log("sys", "Переключено на ТЕКСТОВЫЙ ЧАТ (nekto.me/chat)")

        target_url = f"https://nekto.me/{mode}"
        self.cdp_a.navigate(target_url)
        self.cdp_b.navigate(target_url)

    def reload_tabs(self):
        target_url = f"https://nekto.me/{self.current_mode}"
        self.cdp_a.navigate(target_url)
        self.cdp_b.navigate(target_url)
        self.add_log("sys", "Оба окна Chrome перезагружены.")

    def toggle_bridge(self):
        self.is_bridge_active = not self.is_bridge_active
        if self.is_bridge_active:
            self.btn_bridge.configure(text="⚡ ПЕРЕСЫЛКА: ВКЛ", fg_color="#10b981")
            self.add_log("sys", "Авто-пересылка активирована (сообщения и связь передаются).")
        else:
            self.btn_bridge.configure(text="⏸ ПЕРЕСЫЛКА: ПАУЗА", fg_color="#f59e0b")
            self.add_log("sys", "Авто-пересылка приостановлена.")

    def trigger_action(self, party, action):
        cdp = self.cdp_a if party == "a" else self.cdp_b
        name = "Собеседник 1" if party == "a" else "Собеседник 2"

        js_code = f"""
        (() => {{
            try {{
                // Auto accept cookies
                const cookieBtn = document.getElementById('acceptCookies') || document.querySelector('.cookies-consent__button');
                if (cookieBtn && cookieBtn.offsetParent !== null) cookieBtn.click();

                if ('{action}' === 'start') {{
                    const btn = document.getElementById('searchCompanyBtn') ||
                                document.querySelector('.callScreen__findBtn') ||
                                document.querySelector('.scan-button, .btn-my2');
                    if (btn) {{ btn.click(); return 'started'; }}
                }} else if ('{action}' === 'next') {{
                    const btn = document.querySelector('.callScreen__nextBtn, .next-button, [class*="next"]') ||
                                Array.from(document.querySelectorAll('button, .btn')).find(b => {{
                                    const t = (b.innerText || '');
                                    return t.includes('След') || t.includes('Дальш') || t.includes('Пропустить');
                                }});
                    if (btn) {{ btn.click(); return 'next'; }}
                }} else if ('{action}' === 'stop') {{
                    const btn = document.querySelector('.callScreen__stopBtn, .stop-scan-button, .btn-stop-search, .close_dialog_btn') ||
                                Array.from(document.querySelectorAll('button, .btn')).find(b => {{
                                    const t = (b.innerText || '');
                                    return t.includes('Остановить') || t.includes('Завершить') || t.includes('Отмена');
                                }});
                    if (btn) {{
                        btn.click();
                        // Nekto.me requires confirmation in SweetAlert modal:
                        setTimeout(() => {{
                            const confirmBtn = document.querySelector('.swal2-confirm, .confirm, .swal-button--confirm') ||
                                               Array.from(document.querySelectorAll('.swal2-actions button, .swal-footer button, .modal button')).find(b => {{
                                                   const t = (b.innerText || '').toLowerCase();
                                                   return t.includes('да') || t.includes('заверш') || t.includes('ок') || t.includes('yes');
                                               }});
                            if (confirmBtn) confirmBtn.click();
                        }}, 150);
                        return 'stopped';
                    }}
                }}
            }} catch(e) {{ return e.message; }}
            return 'not_found';
        }})()
        """
        res = cdp.execute_js(js_code)
        self.add_log("sys", f"Действие '{action}' для {name}: {res}")

    def send_operator_message(self, target):
        txt = self.input_text.get().strip()
        if not txt:
            return

        if target in ("a", "both"):
            self.cdp_a.send_chat_message(txt)
            self.add_log("op", f"➔ 1: {txt}")
        if target in ("b", "both"):
            self.cdp_b.send_chat_message(txt)
            self.add_log("op", f"➔ 2: {txt}")

        self.input_text.delete(0, "end")

    def monitor_loop(self):
        """Monitors states, auto-solves captchas, handles modals, and forwards messages"""
        check_js = """
        (() => {
            const body = (document.body.innerText || '').toLowerCase();
            const url = window.location.href;

            // 1. Auto-accept cookies if visible
            const cookieBtn = document.getElementById('acceptCookies') || document.querySelector('.cookies-consent__button');
            if (cookieBtn && cookieBtn.offsetParent !== null) {
                cookieBtn.click();
            }

            // 2. Auto confirm exit dialog if confirmation modal popped up
            const confirmBtn = document.querySelector('.swal2-confirm, .swal-button--confirm');
            if (confirmBtn && confirmBtn.offsetParent !== null) {
                confirmBtn.click();
            }

            // 4. Status determination
            const stopBtn = document.querySelector('.stop-scan-button, .btn-stop-search, .callScreen__stopBtn');
            const startBtn = document.getElementById('searchCompanyBtn') || document.querySelector('.callScreen__findBtn');
            
            const isSearching = url.includes('/searching') || (stopBtn && stopBtn.offsetParent !== null) || body.includes('поиск собеседника');
            const isWaiting = (startBtn && startBtn.offsetParent !== null) || url.endsWith('#/') || body.includes('начать разговор') || body.includes('начать поиск');
            const isDisconn = body.includes('собеседник покинул') || body.includes('собеседник отключился') || body.includes('разговор окончен') || body.includes('разговор завершен');
            const hasChat = document.querySelector('.emojionearea-editor, .mess_block') !== null;
            const isFound = (!isSearching && !isWaiting && !isDisconn) && (url.includes('/talk') || url.includes('/peer') || body.includes('собеседник найден') || body.includes('разговор начат') || hasChat);

            let state = 'waiting';
            if (isFound) state = 'found';
            else if (isSearching) state = 'searching';
            else if (isDisconn) state = 'disconnected';
            else if (isWaiting) state = 'waiting';

            // 5. Extract new messages from stranger (nekto)
            const newMessages = [];
            // Catch messages from stranger (not .self)
            document.querySelectorAll('.mess_block:not(.self) .window_chat_dialog_text, .mess_block.nekto .window_chat_dialog_text').forEach(el => {
                if (!el.dataset.bridgeForwarded) {
                    el.dataset.bridgeForwarded = 'true';
                    const t = (el.innerText || '').trim();
                    if (t) newMessages.push(t);
                }
            });

            return { state, newMessages };
        })()
        """

        last_state_a = None
        last_state_b = None

        # Cache bridge injection scripts
        bridge_script_a = get_audio_bridge_js("a")
        bridge_script_b = get_audio_bridge_js("b")

        while self.is_monitoring:
            time.sleep(0.5)
            try:
                # Ensure voice bridge is active in A if user refreshed page
                self.cdp_a.execute_js(f"if (!window.__voiceBridgeActive && window.location.hostname.includes('nekto.me')) {{ {bridge_script_a} }}")
            except Exception:
                pass

            try:
                # Ensure voice bridge is active in B if user refreshed page
                self.cdp_b.execute_js(f"if (!window.__voiceBridgeActive && window.location.hostname.includes('nekto.me')) {{ {bridge_script_b} }}")
            except Exception:
                pass

            try:
                # Query A
                res_a = self.cdp_a.execute_js(check_js)
                if isinstance(res_a, dict):
                    st = res_a.get("state")
                    if st != last_state_a:
                        last_state_a = st
                        self.update_ui_state("a", st)

                    msgs = res_a.get("newMessages", [])
                    for m in msgs:
                        self.add_log("a", m)
                        if self.is_bridge_active:
                            # Forward message from A to B
                            self.forward_text_to("b", m)

                # Query B
                res_b = self.cdp_b.execute_js(check_js)
                if isinstance(res_b, dict):
                    st = res_b.get("state")
                    if st != last_state_b:
                        last_state_b = st
                        self.update_ui_state("b", st)

                    msgs = res_b.get("newMessages", [])
                    for m in msgs:
                        self.add_log("b", m)
                        if self.is_bridge_active:
                            # Forward message from B to A
                            self.forward_text_to("a", m)

            except Exception:
                pass

    def forward_text_to(self, party, text):
        cdp = self.cdp_a if party == "a" else self.cdp_b
        cdp.send_chat_message(text)

    def update_ui_state(self, party, state):
        lbl = self.lbl_status_a if party == "a" else self.lbl_status_b
        card = self.card_a if party == "a" else self.card_b

        if state == "found":
            lbl.configure(text="🟢 СОБЕСЕДНИК НАЙДЕН!", text_color="#10b981")
            card.configure(border_color="#10b981")
        elif state == "searching":
            lbl.configure(text="⏳ ПОИСК СОБЕСЕДНИКА...", text_color="#f59e0b")
            card.configure(border_color="#f59e0b")
        elif state == "disconnected":
            lbl.configure(text="❌ СОБЕСЕДНИК ОТКЛЮЧИЛСЯ", text_color="#ef4444")
            card.configure(border_color="#ef4444")
        else:
            lbl.configure(text="⚪ ОЖИДАНИЕ ПОИСКА", text_color="#94a3b8")
            card.configure(border_color="#1e3a5f")


if __name__ == "__main__":
    app = NektoBridgeApp()
    app.mainloop()
