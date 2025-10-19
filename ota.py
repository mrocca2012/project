# Modulo OTA Updater para MicroPython
# Usa conexion HTTPS (segura) con la dependencia 'ussl'.
import usocket
import ssl as ussl # <-- Necesario para HTTPS. Asumimos 'import ssl as ussl' esta aplicado si falla.
import ujson
import machine
import os
import time
file_version = 1.1

class OTAUpdater:
    def __init__(self, github_url, main_file='main.py'):
        # Extrae el nombre de host (ej: raw.githubusercontent.com)
        self.http_host = github_url.replace('https://', '').replace('http://', '').split('/')[0]
        self.github_url = github_url
        self.main_file = main_file
        self.version_url = github_url + 'version.json'
        self.files_url = github_url + 'files.json'
        self.http_port = 443 # Puerto por defecto para HTTPS
        # Paths locales
        self.current_version_file = 'version.json'
        self.temporary_version_file = 'tmp_version.json'
        self.backup_folder = 'backup'
        self.update_folder = 'update'

    def _http_get(self, url):
        """Realiza una solicitud HTTP GET (a traves de SSL) y retorna el contenido."""
        url_path = url.replace(f'https://{self.http_host}', '')
        addr = usocket.getaddrinfo(self.http_host, self.http_port)[0][-1]
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        
        try:
            s.connect(addr)
            # Envolver el socket para SSL
            s = ussl.wrap_socket(s, server_hostname=self.http_host)
            # Solicitud HTTP/1.0
            request = f"GET {url_path} HTTP/1.0\r\nHost: {self.http_host}\r\nUser-Agent: MicroPython\r\n\r\n".encode()
            s.send(request)

            # --- Leer la Respuesta ---
            # Leemos los primeros 1024 bytes para verificar encabezados
            data = s.recv(1024)
            response = data.decode('utf-8', 'ignore')

            # 1. Verificar el codigo de estado HTTP (Debe ser 200 OK)
            if not response.startswith('HTTP/1.0 200 OK') and not response.startswith('HTTP/1.1 200 OK'):
                # Si no es 200 OK, lanzamos un error claro indicando el codigo de estado
                first_line = response.split('\r\n')[0]
                raise Exception(f"HTTP Error: {first_line}")

            # 2. Encontrar el inicio del cuerpo (despues de la primera linea vacia)
            content_start = response.find('\r\n\r\n')
            if content_start == -1:
                raise Exception("Respuesta incompleta (no se encontró el cuerpo)")
                
            content = response[content_start + 4:]

            # 3. Leer el resto del cuerpo
            while True:
                data = s.recv(1024)
                if data:
                    content += data.decode('utf-8', 'ignore')
                else:
                    break
        finally:
            s.close()
            
        return content

    def _get_latest_version(self):
        """Descarga y parsea el archivo version.json remoto."""
        try:
            content = self._http_get(self.version_url)
            # Aquí es donde el error de sintaxis JSON ocurriría si el contenido fuera HTML/error
            remote_version = ujson.loads(content)['version']
            return remote_version
        except Exception as e:
            # Ahora este error capturará tanto los errores HTTP (404) como los de JSON.
            print(f"❌ Error al obtener version remota (Verifique URL/JSON): {e}")
            return '0.0'

    def _get_current_version(self):
        """Obtiene la version instalada localmente."""
        try:
            with open(self.current_version_file, 'r') as f:
                local_version = ujson.load(f)['version']
            return local_version
        except (OSError, ValueError):
            # Si el archivo no existe o falla, la version es 0.0
            return '0.0'

    def _download_file(self, filename):
        """Descarga un archivo al directorio de actualizacion."""
        url = self.github_url + filename
        temp_filepath = self.update_folder + filename
        print(f"Descargando {filename} a {temp_filepath}...")
        
        try:
            content = self._http_get(url)
            # Asegurar que la carpeta update exista
            if 'update' not in os.listdir():
                os.mkdir('update')
                
            with open(temp_filepath, 'w') as f:
                f.write(content)
            print(f"✅ Descargado: {filename}")
        except Exception as e:
            print(f"❌ Fallo al descargar {filename}: {e}")
            raise

    def check_for_updates(self):
        """Compara la version local con la version remota."""
        latest_version = self._get_latest_version()
        current_version = self._get_current_version()

        print(f"Version Local: {current_version}, Version Remota: {latest_version}")

        # Comparacion de versiones simple (solo el primer numero)
        if latest_version > current_version:
            return True
        return False

    def download_updates(self):
        """Descarga todos los archivos listados en files.json remotos."""
        # Eliminar carpeta de actualizacion anterior si existe
        try:
            if 'update' in os.listdir():
                for file in os.listdir('update'):
                    os.remove('update' + file)
                os.rmdir('update')
        except:
            pass
            
        try:
            # 1. Obtener la lista de archivos a actualizar
            files_content = self._http_get(self.files_url)
            files_to_download = ujson.loads(files_content)['files']
            # 2. Crear carpeta de actualizacion
            try:
                os.mkdir(self.update_folder)
            except:
                print("error ")

            # 3. Descargar todos los archivos listados
            for filename in files_to_download:
                self._download_file(filename)
                
            # 4. Descargar el nuevo version.json a la carpeta de update
            self._download_file(self.current_version_file)
            
        except Exception as e:
            print(f"❌ Fallo critico durante la descarga de archivos: {e}")
            # Limpiar y levantar error
            self.clean_update_folder()
            raise

    def install_updates(self):
        """Mueve los archivos descargados a la carpeta raiz y reinicia."""
        try:
            # 1. Mover archivos descargados a la raiz (sobrescribiendo)
            for file in os.listdir(self.update_folder):
                source = self.update_folder + file
                destination = file
                
                print(f"Instalando: {file}...")
                
                # Leemos el contenido, borramos el original, y escribimos en la raiz
                with open(source, 'r') as s:
                    content = s.read()
                
                # Borramos el archivo en la raiz si existe
                if file in os.listdir():
                    os.remove(file)
                
                # Escribimos el nuevo archivo en la raiz
                with open(destination, 'w') as d:
                    d.write(content)
                
                os.remove(source) # Eliminar de la carpeta de update
            
            self.clean_update_folder()
            print("✅ Actualización completada. Reiniciando...")
            machine.reset()
            
        except Exception as e:
            print(f"❌ Error al instalar la actualizacion: {e}")
            # Si la instalacion falla, la proxima vez intentara actualizar de nuevo
            self.clean_update_folder()

    def clean_update_folder(self):
        """Limpia la carpeta 'update'."""
        try:
            if 'update' in os.listdir():
                for file in os.listdir(self.update_folder):
                    os.remove(self.update_folder + file)
                os.rmdir(self.update_folder)
                print("Carpeta 'update' limpia.")
        except:
            pass
