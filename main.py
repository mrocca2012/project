from machine import Pin, UART, SoftI2C, freq, Timer, reset
import network
import ntptime
import time
import ubluetooth
import gc
import _thread
import ujson

file_version = 1.5 # Versión actualizada
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
    'SCHEDULED_WEEKEND':[[8, 0], [12, 0], [19, 0]],
    'SCHEDULED_WEEKDAY':[[7, 0], [12, 0], [19, 0]],
    #separador
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
_SVC_UUID = ubluetooth.UUID(0x180C) # Servicio Principal
_CHAR_CONTROL_UUID = ubluetooth.UUID(0x2a88) # Característica de Control
_CHAR_STATUS_UUID = ubluetooth.UUID(0x2a19) # Característica de Estado
_CHAR_PARAM1_UUID = ubluetooth.UUID(0x2a02) # Característica de Estado

# Definición de características (UUID, Flags)
_CHAR_CONTROL = (_CHAR_CONTROL_UUID, ubluetooth.FLAG_WRITE | ubluetooth.FLAG_READ)
_CHAR_STATUS = (_CHAR_STATUS_UUID, ubluetooth.FLAG_NOTIFY | ubluetooth.FLAG_READ)
_CHAR_PARAM1 = (_CHAR_PARAM1_UUID, ubluetooth.FLAG_READ)

# Definición de Servicios GATTS: 
_SERVICES = (
    ( # Este paréntesis crea el "Servicio 1"
        _SVC_UUID,
        ( # Esta tupla contiene las características del Servicio 1
            _CHAR_CONTROL,
            _CHAR_STATUS,
            _CHAR_PARAM1,
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
# --- 2. SENSOR DE FLUJO (Sin cambios) ---
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
# --- 3. CONTROLADOR ubluetooth (BLE) (Sin cambios funcionales) ---
# ----------------------------------------------------------------------

class BLEController:
    """Gestiona la inicialización, publicidad y manejo de eventos BLE."""
    def __init__(self, device_name, command_processor_callback):
        self.device_name = device_name
        self.command_processor = command_processor_callback
        self.ble = ubluetooth.BLE()
        self.conn_handle = None
        self.control_handle = None
        self.status_handle = None
        self.param1_handle = None

        self._init_ble()

    def _init_ble(self):
        """Configura e inicia el servicio BLE."""
        global _SERVICES 
        self.ble.active(True)
        self.ble.irq(self._ble_irq)
        handles = self.ble.gatts_register_services(_SERVICES)
        
        if isinstance(handles, int):
            print(f"❌ Error FATAL al registrar servicios GATTS. Código: {handles}")
            raise RuntimeError("BLE GATTS registration failed.")
            
        self.control_handle = handles[0][0]
        self.status_handle = handles[0][1]
        self.param1_handle = handles[0][2]
        
        print(f"BLE Handles: Control={self.control_handle}, Status={self.status_handle}, Param1={self.param1_handle}")

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
            
            if value_handle == self.control_handle:
                command_bytes = self.ble.gatts_read(value_handle)
                response = self.command_processor(command_bytes)
                
                if response and self.conn_handle:
                    try:
                        self.ble.gatts_write(self.control_handle, response.encode('utf8'))
                    except Exception as e:
                        pass
            

    def advertise(self):
        """Inicia la publicidad BLE."""
        adv_flags = b'\x02\x01\x06' 
        name_bytes = self.device_name.encode('utf-8')
        adv_name = bytes([len(name_bytes) + 1, 0x09]) + name_bytes
        adv_data = adv_flags + adv_name

        self.ble.gap_advertise(_ADV_INTERVAL_MS, adv_data=adv_data)
    
    def notif_status(self, status_msg):
        """Notifica el estado actual al cliente BLE a través del handle de status."""
        if self.conn_handle != None :
            try:
                self.ble.gatts_notify(self.conn_handle, self.status_handle, status_msg.encode('utf8'))
                return True
            except Exception as e:
                print(f"❌ Error al notificar BLE: {e}. Cliente probablemente no habilitó CCCD.")
                return False
        else:
            return False
        
    def write_param1(self, params_msg):
        data_bytes = params_msg.encode('utf8')
        try:
            self.ble.gatts_write(self.param1_handle, data_bytes)
            return True
        except Exception as e:
            print(f"❌ Error al escribir param BLE: {e}.")
            return False
        

# ----------------------------------------------------------------------
# --- 4. CONTROLADOR PRINCIPAL DEL SISTEMA (MODIFICADO) ---
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
        self.flow_stop_timeout = self.config['FLOW_STOP_TIMEOUT']
        
        # Cargar horarios activos
        self.weekday_schedule = self.config['SCHEDULED_WEEKDAY']
        self.weekend_schedule = self.config['SCHEDULED_WEEKEND']

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
        """
        Retorna la hora, minuto, segundo y el día de la semana actuales locales.
        Día de la semana (wd): Lunes=0, Domingo=6.
        """
        local_seconds = time.time() + self.timezone_offset_hours * 3600
        t = time.localtime(local_seconds)
        # t[6] es el día de la semana (0=Lunes a 6=Domingo)
        return t[3], t[4], t[5], t[6] # Hora, minuto, segundo, día_semana

    # --- Métodos de Actuadores (Sin cambios) ---

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

    # --- Lógica de Programación (NUEVO MÉTODO) ---

    def check_scheduled_run(self, current_hour, current_minute, current_second, day_of_week):
        """
        Verifica si la hora actual coincide con un horario programado para el día de la semana.
        Retorna True si la válvula debe encenderse.
        """
        if self.valve_on or current_second != 0:
            return False # No hacer nada si ya está encendida o no es el segundo 0
        
        # Días de semana son Lunes (0) a Viernes (4)
        is_weekday = 0 <= day_of_week <= 4
        is_weekend = 5 <= day_of_week <= 6 
        current_time = [current_hour, current_minute]
        
        target_schedule = None

        if is_weekday:
            target_schedule = self.weekday_schedule
        elif is_weekend:
            target_schedule = self.weekend_schedule
        
        # Comprueba si la hora actual coincide con algún horario para el día
        if target_schedule and current_time in target_schedule:
            print(f"🤖 Evento programado ({'SEMANA' if is_weekday else 'FIN DE SEMANA'}) activado: {current_hour:02d}:{current_minute:02d}.")
            return True
            
        return False

    # --- Métodos de Comandos BLE (MODIFICADO) ---
    def process_ble_command(self, command_bytes):
        """Procesa comandos recibidos por BLE (callback del BLEController)."""
        try:
            command = command_bytes.decode().strip().upper()
            print(f"BLE CMD: {command}")
            parts = command.split(' ', 2) # Dividir el comando en hasta 3 partes
            response = "OK"

            if not parts or not parts[0]:
                response = "ERR: No command"

            elif parts[0] == "VALVE" and len(parts) == 2:
                self.set_valve(parts[1] == "ON")
            
            elif parts[0] == "MOTOR" and len(parts) == 2:
                self.set_motor(parts[1] == "ON")
            
            # NUEVO: SET_SCHEDULE <WEEKDAY|WEEKEND> <JSON_ARRAY_HORARIOS>
            elif parts[0] == "SET_SCHEDULE" and len(parts) == 3:
                schedule_type = parts[1]
                schedule_json_str = parts[2]
                
                if schedule_type not in ["WEEKDAY", "WEEKEND"]:
                    response = "ERR: Tipo de horario invalido. Use WEEKDAY o WEEKEND."
                    return response

                try:
                    new_schedule = ujson.loads(schedule_json_str)
                    
                    # Validar formato: debe ser una lista de listas de 2 elementos
                    if not isinstance(new_schedule, list) or \
                       any(not isinstance(t, list) or len(t) != 2 or not all(isinstance(i, int) for i in t) for t in new_schedule):
                       response = "ERR: Formato JSON de horario invalido. Use [[H, M], [H, M]]."
                       return response
                
                except ValueError:
                    response = "ERR: JSON invalido en el horario."
                    return response

                # Aplicar la actualización
                config_key = f"SCHEDULED_{schedule_type}"
                new_config = {config_key: new_schedule}
                
                if self.config_manager.save_config(new_config):
                    # Recargar la configuración para que el sistema la use inmediatamente
                    self.config_manager.load_config()
                    self.weekday_schedule = self.config['SCHEDULED_WEEKDAY']
                    self.weekend_schedule = self.config['SCHEDULED_WEEKEND']
                    response = f"OK: Horario {schedule_type} actualizado y guardado."
                else:
                    response = "ERR: Fallo al guardar en config.json."
            
            elif parts[0] == "STATUS":
                self.notify_status()
                return None
                
            elif parts[0] == "RESET_FLOW":
                self.config_manager.save_log(0.0)
                response = "OK: FLOW_TOTAL reset to 0.0"

            else:
                response = "ERR: Comando desconocido o formato incorrecto"

        except Exception as e:
            response = f"ERR: Exception {e}"
            print(f"❌ Error procesando comando BLE: {e}")

        return response

    def notify_status(self):
        """Prepara y envía la notificación de estado a través de BLE."""
        flow_liters_total = self.config_manager.flow_liters_total
        current_hour, current_minute, current_second, current_day = self.get_current_time() # Incluye día
        
        # Generar una cadena compacta de estado
        status_msg = (
            f"S:"
            f"{current_hour:02d};" # 0. Hora
            f"{current_minute:02d};" # 1. Minuto
            f"{1 if self.valve_on else 0};" # 2. Válvula
            f"{1 if self.motor_on else 0};" # 3. Motor
            f"{flow_liters_total:.2f};" # 4. Litros Totales
            f"{1 if self.scheduled_run_active else 0};" # 5. Ejecución Programada Activa
            f"{current_day}" # 6. Día de la semana (0-6)
        )
        print(status_msg)
        self.ble_controller.notif_status(status_msg)

    # ----------------------------------------------------------------------
    # --- Bucle Principal de Control (MODIFICADO) ---
    # ----------------------------------------------------------------------

    def main_loop(self):
        """Bucle principal que maneja la lógica de tiempo, programación y auto-apagado."""
        last_second = -1
        last_save_time = time.time()
        last_notify_time = time.time()
        
        self.ble_controller.write_param1("Version 1.4")
        
        print("--- Entrando al bucle principal de control ---")
        while True:
            try:
                # Obtener la hora local COMPLETA
                current_hour, current_minute, current_second, day_of_week = self.get_current_time()
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
                        
                    # 3. Lógica de Activación Programada (USA EL NUEVO MÉTODO)
                    if self.check_scheduled_run(current_hour, current_minute, current_second, day_of_week):
                        self.set_valve(True)
                        if self.valve_on:
                            self.scheduled_run_active = True
                            self.flow_stop_timer_start = 0 
                            
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
                    
                    # --- Lógica de Notificación de Estado ---
                    if self.ble_controller.conn_handle is not None:
                        # Comprueba si han pasado 5 segundos desde la última notificación
                        if current_timestamp - last_notify_time >= 5:
                            self.notify_status()
                            last_notify_time = current_timestamp # Reiniciar el contador
                            
                time.sleep(0.01) # Pequeña espera para no monopolizar el ciclo
                gc.collect()

            except Exception as e:
                print(f"❌ Error CRÍTICO en el bucle de control: {e}")
                time.sleep(1)
                gc.collect()
# ----------------------------------------------------------------------
# --- PUNTO DE ENTRADA (Sin cambios) ---
# ----------------------------------------------------------------------
if __name__ == "__main__":
    try:
        controller = SystemController()
        controller.main_loop()
    except Exception as e:
        print(f"FATAL: Error al iniciar el sistema: {e}")
        reset()