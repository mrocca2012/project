# Este script se ejecuta inmediatamente despues del arranque o reset (antes de main.py)
import network
import time
import gc
import os

# --- Configuracion de OTA ---
# Reemplaza 'tu_usuario' y 'tu_repositorio' con los datos de tu proyecto
GITHUB_URL = "https://raw.githubusercontent.com/mrocca2012/esp32water/main/" 

def connect_to_wifi():
    """Conecta el ESP32 a la red Wi-Fi para la actualizacion OTA."""
    
    # ⚠️ IMPORTANTE: Estas credenciales deben estar grabadas previamente 
    # en el archivo config.json o harcodeadas aqui para la OTA inicial.
    # Usaremos valores fijos por simplicidad del boot.
    SSID = 'WOWIFI'
    PASSWORD = 'fliarorewifi'
    
    print("Iniciando conexión Wi-Fi para OTA...")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(SSID, PASSWORD)
        timeout = 15
        while not wlan.isconnected() and timeout > 0:
            time.sleep(1)
            timeout -= 1
        
        if wlan.isconnected():
            print(f"✅ Wi-Fi conectado. IP: {wlan.ifconfig()[0]}")
            return True
        else:
            print("❌ Error de conexión Wi-Fi.")
            return False
    return True

def check_for_updates():
    """Verifica y ejecuta la actualizacion OTA."""
    
    # Intentamos conectar para la OTA
    if not connect_to_wifi():
        return False
        
    try:
        import ota
        updater = ota.OTAUpdater(GITHUB_URL, main_file='main.py')
        
        # 1. Comprueba si hay una version mas nueva
        if updater.check_for_updates():
            print("🟢 Actualización encontrada. Descargando...")
            updater.download_updates()
            print("🟡 Actualización descargada. Reiniciando para instalar...")
            updater.install_updates()
            
        else:
            print("✅ El firmware ya está actualizado.")
        
        gc.collect()
        return True
        
    except Exception as e:
        print(f"❌ Fallo en la logica OTA: {e}")
        # Si falla el modulo OTA, seguimos con el main.py local
        return False

# --- Lógica de arranque ---

# Ejecuta la comprobacion de actualizacion
check_for_updates()

# La ejecucion continuara ahora con el main.py (nuevo o viejo).
# Si updater.install_updates() fue llamado, el ESP32 se habrá reseteado y cargado el nuevo código.
# Si no hubo actualización, continua cargando el main.py local.