# Modulo OTA Updater para MicroPython
# Basado en micropython-ota-updater (simplificado y modificado para un solo archivo)
import usocket
import ssl as ussl
import ujson
import machine
import os
import time

class OTAUpdater:
    def __init__(self, github_url, main_file='main.py'):
        self.http_host = github_url.replace('https://', '').replace('http://', '').split('/')[0]
        self.github_url = github_url
        self.main_file = main_file
        self.version_url = github_url + 'version.json'
        self.files_url = github_url + 'files.json'
        
        # Paths locales
        self.current_version_file = 'version.json'
        self.temporary_version_file = 'tmp_version.json'
        self.backup_folder = 'backup/'
        self.update_folder = 'update/'

    def _http_get(self, url):
        """Realiza una solicitud HTTP GET y retorna el contenido."""
        url_path = url.replace(f'https://{self.http_host}', '')
        addr = usocket.getaddrinfo(self.http_host, 443)[0][-1]
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        s.connect(addr)
        
        # Envolver el socket para SSL
        s = ussl.wrap_socket(s, server_hostname=self.http_host)
        
        # Solicitud HTTP/1.0
        s.send(f"GET {url_path} HTTP/1.0\r\nHost: {self.http_host}\r\n\r\n".encode())

        # Leer encabezados
        data = s.recv(1024)
        response = data.decode()
        
        # Encontrar el inicio del cuerpo (despues de la primera linea vacia)
        content_start = response.find('\r\n\r\n')
        if content_start == -1:
            s.close()
            raise Exception("No se encontró el cuerpo HTTP")
            
        content = response[content_start + 4:]

        # Leer el resto del cuerpo
        while True:
            data = s.recv(1024)
            if data:
                content += data.decode()
            else:
                break

        s.close()
        return content

    def _get_latest_version(self):
        """Descarga y parsea el archivo version.json remoto."""
        try:
            content = self._http_get(self.version_url)
            remote_version = ujson.loads(content)['version']
            return remote_version
        except Exception as e:
            print(f"Error al obtener version remota: {e}")
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
            if not 'update/' in os.listdir():
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
            if 'update/' in os.listdir():
                for file in os.listdir('update/'):
                    os.remove('update/' + file)
                os.rmdir('update')
        except:
            pass
            
        try:
            # 1. Obtener la lista de archivos a actualizar
            files_content = self._http_get(self.files_url)
            files_to_download = ujson.loads(files_content)['files']
            
            # 2. Crear carpeta de actualizacion
            os.mkdir(self.update_folder)

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
            for file in os.listdir(self.update_folder):
                os.remove(self.update_folder + file)
            os.rmdir(self.update_folder)
            print("Carpeta 'update' limpia.")
        except:
            pass
