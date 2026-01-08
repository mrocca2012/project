from machine import Pin, time_pulse_us, reset
import network
import time
import ujson
import asyncio
import ntptime

# --- CONFIGURACIÓN ---
CONFIG_FILE = 'config.json'
LOG_FILE = 'water_log.json'
TANK_HEIGHT_CM = 200 
VALVE_PIN = 18
MOTOR_PIN = 19
FLOW_SENSOR_PIN = 17
TRIG_PIN = 4
ECHO_PIN = 5
FLOW_TIMEOUT = 300
TIMEZONE_OFFSET_HOURS = -4
current_datetime = (0, 0, 0, 0, 0, 0, 0, 0)
# Lista de servidores NTP para redundancia
NTP_SERVERS = ["3.south-america.pool.ntp.org", "pool.ntp.org", "time.google.com"]

#
# Since there are optocupler value 1 = OFF 0 = ON in valve and motor switch
#
class SystemController:
    def __init__(self):
        self.config = self.load_config()
        self.valve = Pin(VALVE_PIN, Pin.OUT, value=1)
        self.motor = Pin(MOTOR_PIN, Pin.OUT, value=1)
        self.trig = Pin(TRIG_PIN, Pin.OUT)
        self.echo = Pin(ECHO_PIN, Pin.IN)
        self.flow_pin = Pin(FLOW_SENSOR_PIN, Pin.IN, Pin.PULL_DOWN)
        
        self.water_level_pct = 0
        self.liters_total = self.load_liters()
        self.pulses = 0
        self.valve_on = False
        self.motor_on = False
        self.ip = "192.168.68.10"
        self.alert_msg = ""
        self.valve_open_time = 0
        self.time_synced = False
        
        self.flow_pin.irq(trigger=Pin.IRQ_RISING, handler=self._flow_handler)

    def _flow_handler(self, pin):
        self.pulses += 1

    def load_config(self):
        try:
            with open(CONFIG_FILE, 'r') as f: return ujson.load(f)
        except: return {"WIFI_SSID": "WOWIFI", "WIFI_PASS": "fliarorewifi", "K_FACTOR": 450.0}

    def load_liters(self):
        try:
            with open(LOG_FILE, 'r') as f: return ujson.load(f).get('total', 0.0)
        except: return 0.0

    def save_liters(self):
        try:
            with open(LOG_FILE, 'w') as f: ujson.dump({'total': self.liters_total}, f)
        except: pass

    async def sync_time(self):
        """Intenta sincronizar la hora con múltiples servidores NTP."""
        for server in NTP_SERVERS:
            try:
                print(f"🕒 Intentando sincronizar con {server}...")
                ntptime.host = server
                ntptime.settime()
                
                self.time_synced = True
                
                local_seconds = time.time() + TIMEZONE_OFFSET_HOURS * 3600
                current_datetime = time.localtime(local_seconds)
                print('✅ Hora local sincronizada:', current_datetime)
                print("✅ Hora sincronizada correctamente.")
                return True
            except Exception as e:
                print(f"❌ Falló {server}: {e}")
        print("⚠️ No se pudo sincronizar la hora con ningún servidor.")
        return False

    def get_tank_level(self):
        self.trig.value(0)
        time.sleep_us(5)
        self.trig.value(1)
        time.sleep_us(10)
        self.trig.value(0)
        try:
            duration = time_pulse_us(self.echo, 1, 30000)
            if duration < 0: return 0
            dist = (duration / 2) / 29.1
            return max(0, min(100, ((TANK_HEIGHT_CM - dist) / TANK_HEIGHT_CM) * 100))
        except: return 0

    def control_logic(self, target, action):
        """Regla Mejorada: Exclusión mutua total."""
        if target == 'valve' and action is True:
            if self.motor_on:
                self.motor.value(1); self.motor_on = False
            self.valve.value(0); self.valve_on = True
            self.valve_open_time = time.time()
            self.alert_msg = ""
        elif target == 'motor' and action is True:
            if self.water_level_pct > 10:
                if self.valve_on:
                    self.valve.value(1); self.valve_on = False
                self.motor.value(0); self.motor_on = True
            else:
                self.alert_msg = "ERROR: Nivel bajo para motor."
        elif action is False:
            if target == 'valve': self.valve.value(1); self.valve_on = False
            else: self.motor.value(1); self.motor_on = False
            
    def check_system(self):
        if not self.time_synced: return
        
        t = time.localtime(time.time() - 14400) # UTC-4
        h, m, s, wd = t[3], t[4], t[5], t[6]
        
        # 1. Rutina Horaria Inteligente
        is_weekend = wd >= 5
        start_h = 8 if is_weekend else 7
        if m == 0 and s == 0 and h in [start_h, 12, 19]:
            if not self.valve_on and not self.motor_on:
                print("⏰ Inicio de horario programado.")
                self.control_logic('valve', True)

        # 2. RUTINA DE MONITOREO DE FLUJO ACTIVO
        if self.valve_on:
            tiempo_abierta = time.time() - self.valve_open_time
            
            # Esperamos que el flujo se estabilice
            if tiempo_abierta > FLOW_TIMEOUT:
                # Si en el último ciclo de background_tasks no hubo pulsos
                if self.pulses == 0:
                    self.control_logic('valve', False)
                    self.alert_msg = "✅ Llenado finalizado o flujo interrumpido (Auto-Cierre)."
                    print(self.alert_msg)

    async def serve_client(self, reader, writer):
        try:
            line = await reader.readline()
            req = str(line)
            method = req.split(' ')[0].replace("b'", "")
            path = req.split(' ')[1]
            while await reader.readline() != b"\r\n": pass

            if method == "POST":
                if "/valve/toggle" in path: self.control_logic('valve', not self.valve_on)
                elif "/motor/toggle" in path: self.control_logic('motor', not self.motor_on)
                elif "/flow/reset" in path:
                    self.liters_total = 0.0
                    self.save_liters()
                writer.write(b"HTTP/1.1 303 See Other\r\nLocation: /\r\n\r\n")
            else:
                alert_html = f"<div style='color:red; background:#ffdada; padding:10px; border-radius:5px;'>{self.alert_msg}</div>" if self.alert_msg else ""
                response = f"""HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n
                <html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {{ font-family: sans-serif; text-align: center; background: #f0f2f5; }}
                    .card {{ background: white; margin: 10px auto; padding: 20px; border-radius: 12px; max-width: 350px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }}
                    .btn {{ display: block; width: 100%; padding: 15px; margin: 8px 0; border: none; border-radius: 8px; color: white; font-weight: bold; cursor: pointer; }}
                    .on {{ background: #28a745; }} .off {{ background: #dc3545; }} .reset {{ background: #6c757d; font-size: 0.8em; }}
                    .bar {{ background: #eee; border-radius: 10px; height: 20px; }}
                    .fill {{ background: #007bff; height: 100%; width: {self.water_level_pct}%; border-radius: 10px; transition: 1s; }}
                </style></head><body>
                    <div class="card">
                        <h2>Control de Agua</h2>
                        {alert_html}
                        <p>Tanque: {self.water_level_pct:.1f}%</p>
                        <div class="bar"><div class="fill"></div></div>
                        <p>Total: {self.liters_total:.2f} L</p>
                        <form action="/flow/reset" method="POST"><button type="submit" class="btn reset">RESETEAR CONTADOR</button></form>
                    </div>
                    <div class="card">
                        <form action="/valve/toggle" method="POST"><button class="btn {"off" if self.valve_on else "on"}">{"CERRAR VÁLVULA" if self.valve_on else "ABRIR VÁLVULA"}</button></form>
                        <form action="/motor/toggle" method="POST"><button class="btn {"off" if self.motor_on else "on"}">{"APAGAR MOTOR" if self.motor_on else "ENCENDER MOTOR"}</button></form>
                    </div>
                    <p style='font-size:0.7em;'>Estado Hora: {"Sincronizada" if self.time_synced else "Sin hora"}</p>
                    <script>setTimeout(()=>{{ if(!document.hidden) location.reload(); }}, 10000);</script>
                </body></html>
                """
                writer.write(response.encode('utf-8'))
            await writer.drain()
            await writer.wait_closed()
        except: pass

    async def background_tasks(self):
        save_tick = 0
        sync_tick = 0
        while True:
            self.water_level_pct = self.get_tank_level()
            if self.pulses > 0:
                self.liters_total += self.pulses / self.config['K_FACTOR']
                self.pulses = 0
            
            self.check_system()
            
            # Re-sincronizar hora cada 1 hora (3600 seg)
            sync_tick += 1
            if sync_tick >= 3600:
                await self.sync_time()
                sync_tick = 0

            # Guardar litros cada 30s
            save_tick += 1
            if save_tick >= 30:
                self.save_liters()
                save_tick = 0
            await asyncio.sleep(1)

    async def run(self):
        #wlan = network.WLAN(network.STA_IF)
        #wlan.active(True)
        #wlan.ifconfig(('192.168.68.12', '255.255.255.0', '192.168.68.1', '192.168.68.1'))
        #wlan.connect(self.config['WIFI_SSID'], self.config['WIFI_PASS'])
        #while not wlan.isconnected(): await asyncio.sleep(1)
        
        await self.sync_time() # Sincronización inicial con varios servidores
        
        asyncio.create_task(self.background_tasks())
        await asyncio.start_server(self.serve_client, "0.0.0.0", 80)
        while True: await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(SystemController().run())