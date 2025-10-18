from machine import Pin, UART, SoftI2C, freq, Timer, reset
import network
import ntptime
import time
import bluetooth
import gc
import _thread
import ujson
from micropython import const # Necesario si defines tus propias constantes, aunque usaremos las de bluetooth

# ----------------------------------------------------------------------
# --- CONSTANTES DE SISTEMA Y CONFIGURACIÓN ---
# ----------------------------------------------------------------------
CONFIG_FILE = 'config.json'
LOG_FILE = 'water_log.json'

DEFAULT_CONFIG = {
    'WIFI_SSID': 'WOWIFI',
    'WIFI_PASSWORD': 'fliarorewifi',
    'TIMEZONE_OFFSET_HOURS': -4,
    'K_FACTOR': 450.0,
    'FLOW_STOP_TIMEOUT': 5, # Segundos
    'SCHEDULED_TIMES': [[7, 0], [12, 0], [19, 0]], # [[hora, minuto]]
    'NTP_HOST': '3.south-america.pool.ntp.org'
}

# Pines (Asegúrate que estos pines son válidos para tu ESP32)
VALVE_PIN = 23
MOTOR_PIN = 22
FLOW_SENSOR_PIN = 18

# ----------------------------------------------------------------------
# --- CONSTANTES BLE (MicroPython) ---
# ----------------------------------------------------------------------

BLE_DEVICE_NAME = "ESP32WC"
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3 # Evento de escritura a una característica (incluye CCCD)
_ADV_INTERVAL_MS = 500

# UUIDs
# 1. Servicio Principal (Battery Service: 0x180F - ELEGIDO COMO EJEMPLO)
_SVC_UUID = bluetooth.UUID("a326aeb0-9090-4525-9f4c-ffc0b50bb20f") 
# 2. Característica de Control (Date Time: 0x2A88 - Comando IN/Respuesta OUT)
_CHAR_CONTROL_UUID = bluetooth.UUID("d6fde959-b0ba-4565-b737-63aaeb0d5671")
# 3. Característica de Estado (Battery Level: 0x2A19 - Estado OUT/Notificación)
_CHAR_STATUS_UUID = bluetooth.UUID("ae03927e-6f70-4bf0-a148-636042445f59")

# Definición de características (UUID, Flags)
_CHAR_CONTROL = (_CHAR_CONTROL_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_READ)
_CHAR_STATUS = (_CHAR_STATUS_UUID, bluetooth.FLAG_NOTIFY | bluetooth.FLAG_READ)

# Definición de Servicios GATTS: 
# Debe ser una tupla de servicios: ( (Servicio1), (Servicio2), ... )
_SERVICES = (
    ( # Este paréntesis crea el "Servicio 1"
        _SVC_UUID,
        ( # Esta tupla contiene las características del Servicio 1
            _CHAR_CONTROL,
            _CHAR_STATUS,
        )
    ), # Cierre del "Servicio 1"
)
# ----------------------------------------------------------------------
# --- 1. GESTOR DE CONFIGURACIÓN Y LOG ---
# ----------------------------------------------------------------------

class ConfigManager:
    """Maneja la carga y guardado de la configuración (config.json) y el log de agua (water_log.json)."""

    def __init__(self):
        self.config = DEFAULT_CONFIG.copy()
        self.flow_liters_total = 0.0

    def load_config(self):
        """Carga la configuración desde config.json o usa valores por defecto."""
        try:
            with open(CONFIG_FILE, 'r') as f:
                loaded_config = ujson.load(f)
                # Solo actualizar las claves que existen para mantener DEFAULT_CONFIG si se añaden nuevas
                for key in DEFAULT_CONFIG:
                    if key in loaded_config:
                        self.config[key] = loaded_config[key]
            print("✅ Configuración cargada de archivo.")
        except (OSError, ValueError):
            print("⚠️ No se encontró o falló la lectura de config.json. Usando y guardando valores por defecto.")
            self.save_config(DEFAULT_CONFIG.copy())

    def save_config(self, new_config):
        """Guarda el diccionario de configuración en config.json."""
        try:
            self.config.update(new_config)
            with open(CONFIG_FILE, 'w') as f:
                ujson.dump(self.config, f)
            print("✅ Configuración guardada.")
            return True
        except Exception as e:
            print(f"❌ Error al guardar config.json: {e}")
            return False

    def load_log(self):
        """Carga el volumen total de agua desde water_log.json."""
        try:
            with open(LOG_FILE, 'r') as f:
                log_data = ujson.load(f)
                self.flow_liters_total = log_data.get('total_liters', 0.0)
            print(f"✅ Log de agua cargado. Total acumulado: {self.flow_liters_total:.2f} L")
        except (OSError, ValueError):
            print("⚠️ No se encontró o falló la lectura de water_log.json. Iniciando contador en 0.0 L")
            self.save_log(0.0)

    def save_log(self, total_liters):
        """Guarda el volumen total de agua en water_log.json."""
        self.flow_liters_total = total_liters
        try:
            log_data = {'total_liters': self.flow_liters_total, 'timestamp': time.time()}
            with open(LOG_FILE, 'w') as f:
                ujson.dump(log_data, f)
            return True
        except Exception as e:
            print(f"❌ Error al guardar water_log.json: {e}")
            return False

# ----------------------------------------------------------------------
# --- 2. SENSOR DE FLUJO ---
# ----------------------------------------------------------------------

class FlowSensor:
    """Maneja el pin del sensor de flujo y la lógica de interrupción."""

    def __init__(self, pin_number, k_factor, lock):
        self.k_factor = k_factor
        self.lock = lock
        self.pulses_total = 0 
        # Configurar Pin con resistencia pull-down interna
        self.pin = Pin(pin_number, Pin.IN, Pin.PULL_DOWN)
        
        # Configurar la interrupción (IRQ)
        self.pin.irq(trigger=Pin.IRQ_RISING, handler=self._irq_handler)
        print(f"✅ Sensor de flujo en Pin {pin_number} inicializado.")

    def _irq_handler(self, pin):
        """Rutina de Servicio de Interrupción (ISR). DEBE ser lo más simple posible."""
        # Proteger la variable compartida con el lock (necesario en micropython multithreading)
        # Nota: En un simple bucle principal sin hilo secundario, el lock puede no ser estrictamente
        # necesario para el ISR, pero es una buena práctica de seguridad.
        if self.lock.acquire(0): # Intenta adquirir el bloqueo sin esperar
            self.pulses_total += 1
            self.lock.release()

    def read_and_reset_pulses(self):
        """Lee el número de pulsos acumulados desde la última lectura y los reinicia."""
        with self.lock:
            current_pulses = self.pulses_total
            self.pulses_total = 0
        return current_pulses

    def calculate_flow(self, pulses, seconds_passed=1):
        """Calcula el flujo instantáneo (L/min) y el volumen añadido (L)."""
        liters_added = pulses / self.k_factor

        if seconds_passed > 0 and pulses > 0:
            # (Pulsos/segundo) * (60 segundos/minuto) / (Pulsos/Litro)
            flow_rate_lpm = (pulses / seconds_passed) * (60.0 / self.k_factor)
        else:
            flow_rate_lpm = 0.0

        return flow_rate_lpm, liters_added

# ----------------------------------------------------------------------
# --- 3. CONTROLADOR BLUETOOTH (BLE) ---
# ----------------------------------------------------------------------

class BLEController:
    """Gestiona la inicialización, publicidad y manejo de eventos BLE."""

    def __init__(self, device_name, command_processor_callback):
        self.device_name = device_name
        self.command_processor = command_processor_callback
        self.ble = bluetooth.BLE()
        self.conn_handle = None
        self.control_handle = None
        self.status_handle = None

        self._init_ble()

    # En la clase BLEController:

    def _init_ble(self):
        """Configura e inicia el servicio BLE."""
        global _SERVICES 
        self.ble.active(True)
        self.ble.irq(self._ble_irq)
        
        # Intenta registrar los servicios
        handles = self.ble.gatts_register_services(_SERVICES)
        
        # === INICIO DE LA VERIFICACIÓN DE ERROR ===
        # Si 'handles' es un entero (código de error), el registro falló.
        if isinstance(handles, int):
            print(f"❌ Error FATAL al registrar servicios GATTS. Código: {handles}")
            # Se puede añadir una llamada a reset() o raise Exception
            raise RuntimeError("BLE GATTS registration failed.")
        # === FIN DE LA VERIFICACIÓN DE ERROR ===
        
        # Extracción de handles (solo si el registro fue exitoso)
        # handles[0] es la tupla de handles del primer servicio registrado.
        self.control_handle = handles[0][0]
        # handles[0][1] es la tupla (handle_valor, handle_cccd) para la segunda característica.
        self.status_handle = handles[0][1] 
        
        print(f"BLE Handles: Control={self.control_handle}, Status={self.status_handle}")

        self.advertise()
        print("✅ Servicio BLE inicializado y publicitando.")

    def _ble_irq(self, event, data):
        """Manejador de interrupciones BLE."""
        if event == _IRQ_CENTRAL_CONNECT:
            self.conn_handle, _, _ = data
            print(f"BLE: Dispositivo conectado (Handle: {self.conn_handle})")

        elif event == _IRQ_CENTRAL_DISCONNECT:
            print("BLE: Dispositivo desconectado.")
            self.conn_handle = None
            self.advertise()

        elif event == _IRQ_GATTS_WRITE:
            conn_handle, value_handle = data
            
            # Procesar comandos si la escritura es en la característica de Control
            if value_handle == self.control_handle:
                command_bytes = self.ble.gatts_read(value_handle)
                response = self.command_processor(command_bytes)
                
                if response and self.conn_handle:
                    try:
                        # Escribir la respuesta de vuelta (opcional, pero útil para ACK)
                        self.ble.gatts_write(self.control_handle, response.encode('utf8'))
                    except Exception as e:
                        pass
            
            # La escritura en el CCCD (para habilitar notificaciones) es gestionada 
            # internamente por la pila BLE de MicroPython/NimBLE.

    def advertise(self):
        """Inicia la publicidad BLE."""
        # Crear Advertising Data: Flags (0x06) + Nombre del dispositivo (0x09)
        adv_flags = b'\x02\x01\x06' 
        name_bytes = self.device_name.encode('utf-8')
        # Tipo 0x09: Complete Local Name. El primer byte es la longitud total (longitud_nombre + 1 para el tipo)
        adv_name = bytes([len(name_bytes) + 1, 0x09]) + name_bytes
        adv_data = adv_flags + adv_name

        self.ble.gap_advertise(_ADV_INTERVAL_MS, adv_data=adv_data)
    """
    def notify_status(self, status_msg):
        # Notifica el estado actual al cliente BLE a través del handle de status.
        if self.conn_handle and self.status_handle:
            try:
                # La notificación solo se envía si el cliente ha escrito 0x0001 en el CCCD
                self.ble.gatts_notify(self.conn_handle, self.status_handle, status_msg.encode('utf8'))
                return True
            except Exception as e:
                # print(f"Error al notificar BLE: {e}") 
                return False
        return False
     """
    # Modificación en la clase BLEController
    def notify_status(self, status_msg):
        """Notifica el estado actual al cliente BLE a través del handle de status."""
        if self.conn_handle and self.status_handle:
            try:
                # Usar gatts_notify requiere que el cliente haya escrito 0x0001 en el CCCD.
                self.ble.gatts_notify(self.conn_handle, self.status_handle, status_msg.encode('utf8'))
                # print("DEBUG: Status notified successfully.") # Opcional: para confirmación
                return True
            except Exception as e:
                # Si falla, a menudo es porque el cliente no habilitó la notificación (CCCD)
                print(f"❌ Error al notificar BLE: {e}. Cliente probablemente no habilitó CCCD.")
                return False
        else:
            # Esto se manejará en el main_loop, pero es un buen chequeo
            # print("DEBUG: Notification failed, no active connection.")
            return False
        return False

# ----------------------------------------------------------------------
# --- 4. CONTROLADOR PRINCIPAL DEL SISTEMA ---
# ----------------------------------------------------------------------

class SystemController:
    """Clase principal que coordina todos los componentes y la lógica de control."""

    def __init__(self):
        # 0. Inicializar
        self.lock = _thread.allocate_lock()
        self.config_manager = ConfigManager()
        self.config_manager.load_config()
        self.config_manager.load_log()

        # Cargar configuración activa
        self.config = self.config_manager.config
        self.k_factor = self.config['K_FACTOR']
        self.timezone_offset_hours = self.config['TIMEZONE_OFFSET_HOURS']
        self.scheduled_times = self.config['SCHEDULED_TIMES']
        self.flow_stop_timeout = self.config['FLOW_STOP_TIMEOUT']

        # 1. Inicializar Pines (Actuadores apagados por defecto)
        self.valve_pin = Pin(VALVE_PIN, Pin.OUT, value=0)
        self.motor_pin = Pin(MOTOR_PIN, Pin.OUT, value=0)

        # 2. Inicializar Sensores y Estados
        self.flow_sensor = FlowSensor(FLOW_SENSOR_PIN, self.k_factor, self.lock)
        self.valve_on = False
        self.motor_on = False
        self.scheduled_run_active = False
        self.flow_stop_timer_start = 0

        # 3. Inicializar BLE
        self.ble_controller = BLEController(BLE_DEVICE_NAME, self.process_ble_command)

        # 4. Inicializar Wi-Fi y Tiempo
        self.wlan = network.WLAN(network.STA_IF)
        self._connect_wifi()
        self.sync_time()

    # --- Métodos de Red y Tiempo ---

    def _connect_wifi(self):
        """Conecta al Wi-Fi (bloqueante, con reintentos limitados)."""
        if self.wlan.isconnected():
            return True

        print(f"📡 Conectando a Wi-Fi: {self.config['WIFI_SSID']}...")
        self.wlan.active(True)
        self.wlan.connect(self.config['WIFI_SSID'], self.config['WIFI_PASSWORD'])

        for i in range(20): # 10 segundos de espera
            if self.wlan.isconnected():
                print(f"✅ Wi-Fi conectado. IP: {self.wlan.ifconfig()[0]}")
                return True
            time.sleep(0.5)

        print("❌ Fallo la conexión Wi-Fi.")
        return False

    def sync_time(self):
        """Sincroniza la hora con NTP y ajusta la zona horaria."""
        if not self.wlan.isconnected():
            print("⚠️ Wi-Fi no conectado. No se puede sincronizar NTP.")
            return False

        try:
            print("Sincronizando hora con NTP...")
            ntptime.host = self.config['NTP_HOST']
            ntptime.settime()

            local_seconds = time.time() + self.timezone_offset_hours * 3600
            time.localtime(local_seconds)
            print(f"✅ Hora sincronizada y ajustada a UTC{self.timezone_offset_hours}.")
            print(f"✅ Hora actual: {time.localtime(local_seconds)}")
            return True
        except Exception as e:
            print(f"❌ Error al sincronizar NTP: {e}")
            return False

    def get_current_time(self):
        """Retorna la hora, minuto y segundo actuales locales (hh, mm, ss)."""
        # Calcular el tiempo local aplicando el offset
        local_seconds = time.time() + self.timezone_offset_hours * 3600
        t = time.localtime(local_seconds)
        return t[3], t[4], t[5] # Hora, minuto, segundo

    # --- Métodos de Actuadores ---

    def set_motor(self, state):
        """Enciende (True) o apaga (False) el motor."""
        if state and self.valve_on:
            print("⚠️ SEGURIDAD: Motor NO activado. Válvula encendida (Loop Evitado).")
            return

        target_state = 1 if state else 0
        self.motor_pin.value(target_state)
        self.motor_on = (target_state == 1)
        print(f"MOTOR {'ON' if self.motor_on else 'OFF'}")

    def set_valve(self, state):
        """Enciende (True) o apaga (False) la válvula. Apaga el motor si está encendido (seguridad)."""
        if state:
            if self.motor_on:
                print("🚨 SEGURIDAD ACTIVA: Motor encendido detectado al encender Válvula. Apagando motor primero.")
                self.set_motor(False) 
        
        target_state = 1 if state else 0
        self.valve_pin.value(target_state)
        self.valve_on = (target_state == 1)
        print(f"VALVE {'ON' if self.valve_on else 'OFF'}")

        # Si se apaga manualmente o por seguridad, se cancela la ejecución programada
        if not self.valve_on:
            self.scheduled_run_active = False

    # --- Métodos de Comandos BLE ---

    def _process_schedule_command(self, schedule_string):
        """Procesa el comando SCHEDULE SET HH:MM,HH:MM,... y actualiza la configuración."""
        new_times = []
        try:
            parts = [p.strip() for p in schedule_string.split(',')]
            if not parts or not parts[0]:
                return "ERR: Horario vacío"

            for part in parts:
                h, m = map(int, part.split(':'))
                if 0 <= h <= 23 and 0 <= m <= 59:
                    new_times.append([h, m])
                else:
                    return f"ERR: Hora inválida {part}"

        except Exception:
            return "ERR: Formato de horario incorrecto (HH:MM,HH:MM)"

        if new_times:
            self.scheduled_times = new_times
            if self.config_manager.save_config({'SCHEDULED_TIMES': new_times}):
                print(f"✅ Nuevo horario guardado: {self.scheduled_times}")
                return "OK: Horario actualizado"
            else:
                return "ERR: Fallo al guardar config"
        return "ERR: Horario no procesado"


    def process_ble_command(self, command_bytes):
        """Procesa comandos recibidos por BLE (callback del BLEController)."""
        try:
            command = command_bytes.decode().strip().upper()
            print(f"BLE CMD: {command}")
            parts = command.split(' ', 2)
            response = "OK"

            if not parts or not parts[0]:
                response = "ERR: No command"

            elif parts[0] == "VALVE" and len(parts) == 2:
                self.set_valve(parts[1] == "ON")
            
            elif parts[0] == "MOTOR" and len(parts) == 2:
                self.set_motor(parts[1] == "ON")
            
            elif parts[0] == "SCHEDULE" and parts[1] == "SET" and len(parts) == 3:
                response = self._process_schedule_command(parts[2])
            
            elif parts[0] == "STATUS":
                # Notificar el estado y no enviar respuesta de control.
                self.notify_status()
                return None
            
            elif parts[0] == "RESET_FLOW":
                self.config_manager.save_log(0.0)
                response = "OK: FLOW_TOTAL reset to 0.0"

            else:
                response = "ERR: Comando desconocido"

        except Exception as e:
            response = f"ERR: Exception {e}"
            print(f"❌ Error procesando comando BLE: {e}")

        return response

    def notify_status(self):
        """Prepara y envía la notificación de estado a través de BLE."""
        flow_liters_total = self.config_manager.flow_liters_total
        current_hour, current_minute, current_second = self.get_current_time()
        current_time_str = f"{current_hour:02d}:{current_minute:02d}:{current_second:02d}"

        status_msg = (
            f"STATUS:"
            f"TIME={current_time_str};"
            f"VALVE={1 if self.valve_on else 0};"
            f"MOTOR={1 if self.motor_on else 0};"
            f"FLOW_TOTAL={flow_liters_total:.2f}L;"
            f"SCHEDULE={1 if self.scheduled_run_active else 0}"
        )
        print(status_msg)
        self.ble_controller.notify_status(status_msg)

    # ----------------------------------------------------------------------
    # --- Bucle Principal de Control ---
    # ----------------------------------------------------------------------
# Modificación en la clase SystemController.main_loop

    def main_loop(self):
        """Bucle principal que maneja la lógica de tiempo, programación y auto-apagado."""
        global current_second
        last_second = -1
        last_save_time = time.time()
        
        # === AÑADIR NUEVA VARIABLE DE TIEMPO ===
        last_notify_time = time.time() 
        # ======================================

        print("--- Entrando al bucle principal de control ---")

        while True:
            try:
                # Obtener la hora local (con offset de zona horaria)
                current_hour, current_minute, current_second = self.get_current_time()
                current_timestamp = time.time()
                # Lógica que se ejecuta cada segundo
                if current_second != last_second:
                    last_second = current_second
                
                # 1. Calular Flujo y Acumulación
                    pulses = self.flow_sensor.read_and_reset_pulses()
                    flow_rate_lpm, liters_added = self.flow_sensor.calculate_flow(pulses, seconds_passed=1)
                    self.config_manager.flow_liters_total += liters_added
                    
                    # 2. Persistencia del Log de Agua (cada 60 segundos)
                    if current_timestamp - last_save_time >= 60:
                        self.config_manager.save_log(self.config_manager.flow_liters_total)
                        last_save_time = current_timestamp
                        #print(".")
                        
                    # 3. Lógica de Activación Programada (solo si la válvula está OFF)
                    if not self.valve_on:
                        current_time = [current_hour, current_minute]
                        # Comprueba si la hora actual coincide con alguna programada (y es el segundo 0)
                        if current_time in self.scheduled_times and current_second == 0:
                            self.set_valve(True)
                            if self.valve_on:
                                self.scheduled_run_active = True
                                self.flow_stop_timer_start = 0 
                                print("🤖 Evento programado activado.")
                                
                    # 4. Lógica de Auto-Apagado por Falta de Flujo (Shutoff)
                    if self.valve_on and self.scheduled_run_active:
                        if flow_rate_lpm < 0.01: # Si el flujo es virtualmente cero
                            if self.flow_stop_timer_start == 0:
                                self.flow_stop_timer_start = current_timestamp
                                print("⚠️ Flujo detectado como CERO. Iniciando conteo de apagado.")
                                
                            elif (current_timestamp - self.flow_stop_timer_start) >= self.flow_stop_timeout:
                                self.set_valve(False) # Esto desactiva scheduled_run_active
                                self.flow_stop_timer_start = 0
                                print(f"🛑 Apagado automático: Flujo cero por más de {self.flow_stop_timeout} segundos.")
                                
                        else:
                            # Flujo detectado, reinicia el timer
                            if self.flow_stop_timer_start != 0:
                                self.flow_stop_timer_start = 0
                                print("✅ Flujo reestablecido. Reiniciando monitoreo de apagado.")
                
                # --- Lógica de Notificación de Estado (Fuera del chequeo de 'last_second') ---
                if self.ble_controller.conn_handle is not None:
                    # Comprueba si han pasado 5 segundos desde la última notificación
                    if current_timestamp - last_notify_time >= 5:
                        print("-")
                        self.notify_status()
                        last_notify_time = current_timestamp # Reiniciar el contador
                        
                time.sleep(0.01) # Pequeña espera para no monopolizar el ciclo
                gc.collect()

            except Exception as e:
                print(f"❌ Error CRÍTICO en el bucle de control: {e}")
                time.sleep(5)
                gc.collect()
# ----------------------------------------------------------------------
# --- PUNTO DE ENTRADA ---
# ----------------------------------------------------------------------
if __name__ == "__main__":
    try:
        controller = SystemController()
        controller.main_loop()
    except Exception as e:
        print(f"FATAL: Error al iniciar el sistema: {e}")
        # Intento de reinicio después de un error fatal
        reset()