import os
import mss
import pytesseract
from PIL import Image
from core.config import TESSERACT_PATHS, CENSURA_NOTIFICACIONES
from core.logger import log

class ScreenReader:
    """
    Controlador de captura visual de pantalla y extracción óptica de caracteres (OCR).
    Maneja coordenadas de ventanas activas (incluyendo maximizadas) y censura de notificaciones privadas.
    """
    def __init__(self):
        # Configurar la ruta ejecutable de Tesseract si está presente
        for ruta in TESSERACT_PATHS:
            if os.path.exists(ruta):
                pytesseract.pytesseract.tesseract_cmd = ruta
                log.info(f"Tesseract OCR vinculado en: {ruta}")
                break
                
        self.last_image = None # Imagen en RAM del último ciclo válido

    def capture_and_read(self, active_title: str = "") -> str:
        """
        Captura la ventana actualmente activa y extrae su texto mediante Tesseract.
        Aplica censura automática a notificaciones emergentes (WhatsApp, Telegram, etc.)
        y recorte de la barra lateral izquierda en Artemis para no mezclar técnicos.
        """
        try:
            import ctypes
            from ctypes import wintypes
            
            # Sincronizar hilo con el escritorio de entrada interactivo de Windows
            try:
                hdesk = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
                if hdesk:
                    ctypes.windll.user32.SetThreadDesktop(hdesk)
                    ctypes.windll.user32.CloseDesktop(hdesk)
            except Exception:
                pass

            # 1. Obtener las coordenadas exactas de la ventana activa y su monitor correspondiente
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            
            hwnd = user32.GetForegroundWindow()
            rect = wintypes.RECT()
            has_rect = False
            if hwnd:
                has_rect = bool(user32.GetWindowRect(hwnd, ctypes.byref(rect)))

            # Identificar el monitor físico donde reside la ventana activa (soporte multimonitor)
            MONITOR_DEFAULTTONEAREST = 2
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HMONITOR
            hmon = user32.MonitorFromWindow(hwnd if hwnd else 0, MONITOR_DEFAULTTONEAREST)

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)
                ]

            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            user32.GetMonitorInfoW(hmon, ctypes.pointer(mi))

            # Clamping estricto: evita coordenadas negativas en ventanas maximizadas (-8, -8 en Windows)
            # y desbordamientos fuera de los límites del monitor activo
            if has_rect and (rect.right > rect.left) and (rect.bottom > rect.top):
                clamped_left = max(mi.rcMonitor.left, rect.left)
                clamped_top = max(mi.rcMonitor.top, rect.top)
                clamped_right = min(mi.rcMonitor.right, rect.right)
                clamped_bottom = min(mi.rcMonitor.bottom, rect.bottom)
            else:
                # Si no hay ventana en foco, capturar el área del monitor principal
                clamped_left = mi.rcMonitor.left
                clamped_top = mi.rcMonitor.top
                clamped_right = mi.rcMonitor.right
                clamped_bottom = mi.rcMonitor.bottom

            width = max(200, clamped_right - clamped_left)
            height = max(150, clamped_bottom - clamped_top)
            
            with mss.mss() as sct:
                monitor_dict = {"top": clamped_top, "left": clamped_left, "width": width, "height": height}
                try:
                    sct_img = sct.grab(monitor_dict)
                except Exception as grab_err:
                    # Respaldo seguro: capturar los límites del monitor activo completo
                    m_width = max(640, mi.rcMonitor.right - mi.rcMonitor.left)
                    m_height = max(480, mi.rcMonitor.bottom - mi.rcMonitor.top)
                    fallback_dict = {"top": mi.rcMonitor.top, "left": mi.rcMonitor.left, "width": m_width, "height": m_height}
                    sct_img = sct.grab(fallback_dict)
                
                # Convertir a imagen PIL RGB
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                
                # Captura completa sin recortes destructivos para preservar encabezados y nombres en Artemis y consolas
                img_for_ocr = img
                self.last_image = img_for_ocr
                
                # Extraer texto mediante OCR con Tesseract
                text = pytesseract.image_to_string(img_for_ocr)
                
                if not text:
                    return ""
                    
                # 2. Filtro de Censura (Eliminar notificaciones emergentes superpuestas)
                lineas = text.split('\n')
                lineas_limpias = []
                saltar_lineas = 0
                
                for linea in lineas:
                    if saltar_lineas > 0:
                        saltar_lineas -= 1
                        continue
                        
                    linea_lower = linea.lower()
                    if any(palabra in linea_lower for palabra in CENSURA_NOTIFICACIONES):
                        saltar_lineas = 2 # Censurar la cabecera y las dos siguientes líneas de mensaje
                        continue
                        
                    lineas_limpias.append(linea)
                    
                return '\n'.join(lineas_limpias)
                
        except Exception as e:
            log.error(f"Error durante la captura visual u OCR: {e}")
            return f"ERROR_OCR: {str(e)}"
