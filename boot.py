# Este script se ejecuta inmediatamente despues del arranque o reset (antes de main.py)
# programa para el llenado del tanque de agua con
# horarios programados
# codigo generado con la ayuda de Google Gemini
# propiedad de Marco Rocca

version = 1.2

import network
import time
import gc
import os
from machine import freq

freq(160000000)

# --- Configuracion de OTA ---
GITHUB_URL = "https://raw.githubusercontent.com/mrocca2012/project/master/"

def connect_to_wifi():
    """
    Conecta el ESP32 a la red Wi-Fi probando múltiples credenciales.
    Es ideal para usar en boot.py para actualizaciones OTA.
    """
    # Lista de tuplas: (SSID, PASSWORD) en orden de preferencia
    WIFI_CREDENTIALS = [
        ('WOWIFI', 'fliarorewifi'),
        ('WOWIFI24', 'teinvitoanavegar')
    ]
    
    print("Iniciando conexión Wi-Fi para OTA...")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.ifconfig(('192.168.68.12', '255.255.255.0', '192.168.68.1', '192.168.68.1'))
    
    if wlan.isconnected():
        print(f"✅ Wi-Fi ya conectado. IP: {wlan.ifconfig()[0]}")
        return True

    for ssid, password in WIFI_CREDENTIALS:
        print(f"📡 Intentando conectar a: {ssid}...")
        
        wlan.connect(ssid, password)
        
        # Tiempo de espera (timeout) por red
        timeout = 15
        while not wlan.isconnected() and timeout > 0:
            time.sleep(1)
            timeout -= 1
        
        if wlan.isconnected():
            print(f"✅ Wi-Fi conectado a '{ssid}'. IP: {wlan.ifconfig()[0]}")
            return True
        else:
            print(f"❌ Falló la conexión a '{ssid}'. Tiempo agotado.")
            # Desconectar y/o desactivar brevemente para limpiar el estado antes de la próxima
            wlan.disconnect() 
            time.sleep(1) # Esperar un segundo antes de probar la siguiente red

    print("❌ Error de conexión Wi-Fi. No se pudo conectar a ninguna red.")
    return False

# Ejemplo de uso (opcional, si estás probando):
# if __name__ == '__main__':
#     connect_to_wifi()

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