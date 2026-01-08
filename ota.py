import usocket
import ssl as ussl
import ujson
import machine
import os
import gc

class OTAUpdater:
    def __init__(self, github_url, main_file='main.py'):
        # Limpieza y formateo de URL y Host
        self.github_url = github_url if github_url.endswith('/') else github_url + '/'
        self.http_host = self.github_url.replace('https://', '').replace('http://', '').split('/')[0]
        self.main_file = main_file
        
        # URLs de control
        self.version_url = self.github_url + 'version.json'
        self.files_url = self.github_url + 'files.json'
        
        # Paths locales (con barra final para evitar errores de concatenación)
        self.update_folder = 'update/'
        self.current_version_file = 'version.json'

    def _http_get_stream(self, url, dest_path):
        """Descarga un archivo vía HTTPS y lo escribe directamente en flash."""
        gc.collect()
        url_path = url.replace(f'https://{self.http_host}', '')
        addr = usocket.getaddrinfo(self.http_host, 443)[0][-1]
        s = usocket.socket()
        
        try:
            s.connect(addr)
            s = ussl.wrap_socket(s, server_hostname=self.http_host)
            # Solicitud HTTP 1.0 para evitar chunked encoding complejo
            request = f"GET {url_path} HTTP/1.0\r\nHost: {self.http_host}\r\nUser-Agent: MicroPython\r\n\r\n"
            s.send(request.encode())

            # 1. Saltar encabezados buscando la línea vacía (\r\n\r\n)
            while True:
                line = s.readline()
                if not line or line == b'\r\n':
                    break
            
            # 2. Escribir el cuerpo directamente al archivo en bloques de 512 bytes
            with open(dest_path, 'wb') as f:
                while True:
                    data = s.recv(512)
                    if not data:
                        break
                    f.write(data)
            return True
        except Exception as e:
            print(f"❌ Error en stream HTTP: {e}")
            return False
        finally:
            s.close()
            gc.collect()

    def _get_json_rpc(self, url):
        """Método auxiliar para leer JSON pequeños (version/files) en RAM."""
        url_path = url.replace(f'https://{self.http_host}', '')
        addr = usocket.getaddrinfo(self.http_host, 443)[0][-1]
        s = usocket.socket()
        try:
            s.connect(addr)
            s = ussl.wrap_socket(s, server_hostname=self.http_host)
            s.send(f"GET {url_path} HTTP/1.0\r\nHost: {self.http_host}\r\n\r\n".encode())
            
            while True:
                line = s.readline()
                if not line or line == b'\r\n': break
            
            content = s.read().decode('utf-8')
            return ujson.loads(content)
        finally:
            s.close()

    def check_for_updates(self):
        """Compara versiones."""
        try:
            remote_data = self._get_json_rpc(self.version_url)
            remote_v = float(remote_data['version'])
            
            local_v = 0.0
            if self.current_version_file in os.listdir():
                with open(self.current_version_file, 'r') as f:
                    local_v = float(ujson.load(f)['version'])
            
            print(f"OTA: Local {local_v} | Remota {remote_v}")
            return remote_v > local_v
        except Exception as e:
            print(f"OTA: No se pudo verificar versión: {e}")
            return False

    def download_updates(self):
        """Descarga todos los archivos definidos en files.json."""
        try:
            if 'update' not in os.listdir():
                os.mkdir('update')
            
            files_data = self._get_json_rpc(self.files_url)
            for filename in files_data['files']:
                print(f"📥 Descargando {filename}...")
                self._http_get_stream(self.github_url + filename, self.update_folder + filename)
            
            # También descargar el nuevo version.json
            self._http_get_stream(self.version_url, self.update_folder + self.current_version_file)
            return True
        except Exception as e:
            print(f"❌ Error descargando actualización: {e}")
            return False

    def install_updates(self):
        """Instala los archivos moviéndolos a la raíz."""
        try:
            for file in os.listdir('update'):
                source = self.update_folder + file
                print(f"🔧 Instalando {file}...")
                
                # Operación de reemplazo segura
                if file in os.listdir():
                    os.remove(file)
                
                # Leemos y escribimos (en MicroPython os.rename a veces falla entre carpetas)
                with open(source, 'rb') as src, open(file, 'wb') as dst:
                    while True:
                        buf = src.read(512)
                        if not buf: break
                        dst.write(buf)
                os.remove(source)

            os.rmdir('update')
            print("✅ Actualización instalada. Reiniciando...")
            machine.reset()
        except Exception as e:
            print(f"❌ Error en instalación: {e}")