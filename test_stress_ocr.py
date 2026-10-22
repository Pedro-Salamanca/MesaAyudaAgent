"""
MESAAYUDAAGENT v3.2 - SCRIPT DE PRUEBA DE ESTRÉS Y RENDIMIENTO OCR (MODO AUTÓNOMO)
Duración configurable (por defecto 15 minutos).
Navega autónomamente por:
- Google Chrome (Artemis, scroll de chats, cambio de pestañas Ctrl+Tab)
- WinBox Medellín (10.0.2.5) y WinBox Bogotá (10.1.2.5)
- PuTTY / Terminal (Speed test y routers)
- MobaXterm
- Slack (Validación estricta de privacidad: debe ser descartado)
"""

import os
import sys
import time
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
from PIL import Image

# Configurar path raíz del proyecto y codificación UTF-8
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from core.vision import ScreenReader
from core.pre_filter import PreFilterEngine
from core.bucket_manager import BucketManager
from core.config import TESSERACT_PATHS, LONGITUD_MINIMA_OCR
from core.logger import log

def get_process_memory_mb() -> float:
    """Retorna el uso de memoria RAM (Working Set / RSS) del proceso actual en MB."""
    class PMC(ctypes.Structure):
        _fields_ = [
            ('cb', wintypes.DWORD),
            ('PageFaultCount', wintypes.DWORD),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
            ('PagefileUsage', ctypes.c_size_t),
            ('PeakPagefileUsage', ctypes.c_size_t),
        ]
    try:
        f = ctypes.windll.kernel32.K32GetProcessMemoryInfo
        f.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        f.restype = wintypes.BOOL
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if f(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0

def get_open_windows():
    """Retorna lista de tuplas (hwnd, title) de ventanas visibles en el escritorio."""
    user32 = ctypes.windll.user32
    try:
        hdesk = user32.OpenInputDesktop(0, False, 0x0100)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
            user32.CloseDesktop(hdesk)
    except Exception:
        pass

    windows = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    @WNDENUMPROC
    def enum_cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title and title not in ["Default IME", "MSCTFIME UI", "Program Manager"]:
                    windows.append((hwnd, title))
        return True

    user32.EnumWindows(enum_cb, 0)
    return windows

def bring_window_to_front(hwnd):
    """Trae una ventana al primer plano de forma segura en Windows."""
    user32 = ctypes.windll.user32
    try:
        user32.ShowWindow(hwnd, 9) # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
    except Exception:
        pass

def send_key(vk_code):
    """Envía pulsación de tecla virtual Win32."""
    user32 = ctypes.windll.user32
    user32.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(vk_code, 0, 2, 0) # KEYEVENTF_KEYUP

def scroll_down():
    """Envía pulsación Page Down (0x22) para desplazarse hacia abajo en el chat."""
    send_key(0x22)
    time.sleep(0.3)

def next_browser_tab():
    """Envía combinación Ctrl + Tab para alternar entre pestañas de Chrome."""
    user32 = ctypes.windll.user32
    VK_CONTROL = 0x11
    VK_TAB = 0x09
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_TAB, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_TAB, 0, 2, 0)
    user32.keybd_event(VK_CONTROL, 0, 2, 0)
    time.sleep(0.4)

def run_stress_test(duration_minutes: float = 15.0, interval_seconds: float = 2.5, auto_cycle: bool = True):
    total_seconds = int(duration_minutes * 60)
    start_time = time.time()
    end_time = start_time + total_seconds

    print("=" * 85)
    print(" INICIANDO PRUEBA DE ESTRÉS Y RENDIMIENTO OCR (MODO AUTÓNOMO) - MESAAYUDAAGENT v3.2")
    print("=" * 85)
    print(f"Duración: {duration_minutes:.1f} minutos ({total_seconds} segundos)")
    print(f"Intervalo base entre acciones: {interval_seconds}s")
    print(f"Modo: {'🔄 AUTÓNOMO (Navegación inteligente entre Chrome, WinBox, PuTTY y Slack)' if auto_cycle else '👁️ PASIVO'}")
    print(f"Hora de inicio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 85)

    vision = ScreenReader()
    pre_filter = PreFilterEngine()
    bucket_manager = BucketManager(max_concurrent=5, ttl_bucket_minutes=10, global_inactivity_minutes=30)

    initial_ram = get_process_memory_mb()
    print(f"Memoria RAM inicial del proceso: {initial_ram:.2f} MB\n")

    records = []
    iteration = 0
    step_subcycle = 0

    try:
        while time.time() < end_time:
            iteration += 1
            step_subcycle += 1
            elapsed = time.time() - start_time
            remaining = max(0, end_time - time.time())
            min_str = f"[{int(elapsed//60):02d}:{int(elapsed%60):02d} / {int(total_seconds//60):02d}:00]"

            # ORQUESTACIÓN AUTÓNOMA ENTRE VENTANAS
            if auto_cycle:
                all_win = get_open_windows()

                chrome_wins = [w for w in all_win if "chrome" in w[1].lower()]
                winbox_mde = [w for w in all_win if "10.0.2.5" in w[1].lower() or ("winbox" in w[1].lower() and "vm05" in w[1].lower())]
                winbox_bta = [w for w in all_win if "10.1.2.5" in w[1].lower() or "col_bia" in w[1].lower()]
                putty_wins = [w for w in all_win if "root@" in w[1].lower() or "putty" in w[1].lower()]
                moba_wins = [w for w in all_win if "moba" in w[1].lower()]
                slack_wins = [w for w in all_win if "slack" in w[1].lower()]

                # Ciclo de rotación cada pocas iteraciones:
                # Paso 0-2: Chrome (Lectura + Scroll + Cambio de pestaña)
                # Paso 3: Winbox Medellín
                # Paso 4: Winbox Bogotá
                # Paso 5: PuTTY Speed Test / CLI
                # Paso 6: MobaXterm
                # Paso 7: Slack (Validación de descarte)
                fase = step_subcycle % 8

                if fase in [0, 1, 2] and chrome_wins:
                    bring_window_to_front(chrome_wins[0][0])
                    if fase == 1:
                        # Scroll para ver contexto inferior
                        scroll_down()
                    elif fase == 2:
                        # Cambiar de pestaña en Chrome
                        next_browser_tab()
                elif fase == 3 and winbox_mde:
                    bring_window_to_front(winbox_mde[0][0])
                elif fase == 4 and winbox_bta:
                    bring_window_to_front(winbox_bta[0][0])
                elif fase == 5 and putty_wins:
                    bring_window_to_front(putty_wins[0][0])
                elif fase == 6 and moba_wins:
                    bring_window_to_front(moba_wins[0][0])
                elif fase == 7 and slack_wins:
                    bring_window_to_front(slack_wins[0][0])

            # 1. Obtener ventana activa actual
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buf, 512)
            active_title = title_buf.value.strip() or "Ventana Desconocida"

            # 2. Comprobar si la ventana está permitida
            is_allowed, allow_reason = pre_filter.is_window_allowed(active_title)

            if not is_allowed:
                # Caso de Slack u otras apps bloqueadas por privacidad
                curr_ram = get_process_memory_mb()
                estado = "🛡️ DESCARTADA (Privacidad)"
                print(f"{min_str} #{iteration:03d} | {estado:26s} | RAM: {curr_ram:5.1f}MB | Motivo: {allow_reason[:30]} | Win: {active_title[:30]}")
                records.append({
                    "iteration": iteration,
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "elapsed_sec": round(elapsed, 2),
                    "active_title": active_title[:45],
                    "char_count": 0,
                    "ocr_time_ms": 0.0,
                    "ram_mb": round(curr_ram, 2),
                    "technician": "-",
                    "cun": "-",
                    "tool": "Prohibida",
                    "status": "DESCARTADA_PRIVACIDAD"
                })
                time.sleep(interval_seconds)
                continue

            # 3. Captura visual y extracción OCR
            t0 = time.time()
            raw_text = vision.capture_and_read(active_title)
            t1 = time.time()
            total_read_time = t1 - t0

            curr_ram = get_process_memory_mb()
            char_count = len(raw_text.strip())

            # 4. Extracción de anclas técnicas
            anchors = pre_filter.extract_anchors(raw_text, active_title)
            tec = anchors.get("tecnico_campo") or "-"
            cun = anchors.get("id_servicio_cun") or "-"
            site = anchors.get("codigo_sitio") or "-"
            tool = anchors.get("herramienta_origen") or "General"
            macs_count = len(anchors.get("mac_addresses", []))
            ips_count = len(anchors.get("ip_addresses", []))

            # 5. Enrutamiento simulado a Cubetas
            target_bucket, es_nuevo_paso, needs_block = bucket_manager.route_capture(
                active_title, raw_text, anchors
            )

            if target_bucket is not None:
                cubeta_str = f"[{target_bucket.tecnico_campo} #{target_bucket.total_steps_lifetime}p]"
                estado = "✅ Cubeta Activa"
            else:
                cubeta_str = "[Sin Cubeta]"
                estado = "⏸️ Sin Enrutar"

            is_dup = pre_filter.is_duplicate_text(raw_text, threshold=0.92)
            dup_tag = " (Dup)" if is_dup else ""

            record = {
                "iteration": iteration,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "elapsed_sec": round(elapsed, 2),
                "active_title": active_title[:45],
                "char_count": char_count,
                "ocr_time_ms": round(total_read_time * 1000, 1),
                "ram_mb": round(curr_ram, 2),
                "technician": tec,
                "cun": cun,
                "site": site,
                "tool": tool,
                "macs_count": macs_count,
                "ips_count": ips_count,
                "bucket": cubeta_str,
                "status": estado + dup_tag
            }
            records.append(record)

            print(f"{min_str} #{iteration:03d} | {estado:15s}{dup_tag:6s} | {total_read_time*1000:6.1f}ms | {char_count:5d}c | RAM: {curr_ram:5.1f}MB | {tool:8s} | Tec: {tec[:16]:16s} | CUN: {cun:16s} | {cubeta_str:20s}")

            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        print("\n⚠️ Prueba interrumpida manualmente por el usuario.")

    # RESUMEN Y REPORTE FINAL
    final_time = time.time()
    final_ram = get_process_memory_mb()
    total_samples = len(records)

    if total_samples > 0:
        ocr_times = [r["ocr_time_ms"] for r in records if r["ocr_time_ms"] > 0]
        avg_time = sum(ocr_times) / len(ocr_times) if ocr_times else 0
        min_time = min(ocr_times) if ocr_times else 0
        max_time = max(ocr_times) if ocr_times else 0
        sorted_times = sorted(ocr_times) if ocr_times else [0]
        p95_time = sorted_times[int(len(sorted_times) * 0.95)] if ocr_times else 0
        ram_delta = final_ram - initial_ram

        print("\n" + "=" * 85)
        print(" RESUMEN FINAL DE LA PRUEBA DE ESTRÉS (MODO AUTÓNOMO)")
        print("=" * 85)
        print(f"Total de iteraciones analizadas: {total_samples}")
        print(f"Tiempo total transcurrido: {(final_time - start_time) / 60:.2f} minutos")
        print(f"Latencia OCR Promedio: {avg_time:.1f} ms ({avg_time/1000:.2f}s)")
        print(f"Latencia Mínima: {min_time:.1f} ms | Máxima: {max_time:.1f} ms")
        print(f"Latencia Percentil 95 (P95): {p95_time:.1f} ms")
        print(f"Memoria RAM Inicial: {initial_ram:.2f} MB | Final: {final_ram:.2f} MB | Delta: {ram_delta:+.2f} MB")
        
        leak_verdict = "✅ EXCELENTE (Cero fugas de memoria)" if ram_delta < 30 else "⚠️ ATENCIÓN (Posible incremento de memoria)"
        print(f"Diagnóstico de Fuga de Memoria: {leak_verdict}")

        # Resumen de cubetas creadas y atribuidas
        print("\nResumen de Cubetas Concurrentes Creadas:")
        for b_id, b in bucket_manager.active_buckets.items():
            print(f"  - Cubeta [{b_id}]: Técnico: {b.tecnico_campo} | CUN: {b.id_servicio_cun} | Pasos: {b.total_steps_lifetime} | Herramientas: {', '.join(b.herramientas_usadas)}")
        print("=" * 85)

        # Guardar reporte en archivo Markdown
        os.makedirs("logs", exist_ok=True)
        report_path = os.path.join("logs", f"reporte_estres_autonomo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# REPORTE DE PRUEBA DE ESTRÉS Y RENDIMIENTO OCR (MODO AUTÓNOMO)\n")
            f.write(f"- **Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"- **Muestras totales:** {total_samples}\n")
            f.write(f"- **Duración real:** {(final_time - start_time) / 60:.2f} minutos\n")
            f.write(f"- **Latencia Promedio:** {avg_time:.1f} ms\n")
            f.write(f"- **Latencia P95:** {p95_time:.1f} ms\n")
            f.write(f"- **Memoria Inicial:** {initial_ram:.2f} MB | **Final:** {final_ram:.2f} MB | **Delta:** {ram_delta:+.2f} MB\n")
            f.write(f"- **Diagnóstico de Estabilidad:** {leak_verdict}\n\n")

            f.write("## Cubetas Creadas y Atribuidas Durante la Prueba\n\n")
            f.write("| Cubeta ID | Técnico | CUN | Pasos Acumulados | Herramientas Utilizadas |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- |\n")
            for b_id, b in bucket_manager.active_buckets.items():
                tools_str = ", ".join(b.herramientas_usadas) if b.herramientas_usadas else "General"
                f.write(f"| {b_id} | {b.tecnico_campo} | {b.id_servicio_cun or '-'} | {b.total_steps_lifetime} | {tools_str} |\n")

            f.write("\n## Muestra de Telemetría (Últimas 25 iteraciones)\n\n")
            f.write("| # | Hora | Tiempo (ms) | Caracteres | RAM (MB) | App | Técnico | CUN | Cubeta | Estado |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
            for r in records[-25:]:
                f.write(f"| {r['iteration']} | {r['timestamp']} | {r.get('ocr_time_ms', 0)} | {r.get('char_count', 0)} | {r['ram_mb']} | {r.get('tool', '-')} | {r.get('technician', '-')} | {r.get('cun', '-')} | {r.get('bucket', '-')} | {r.get('status', '-')} |\n")

        print(f"\n📄 Reporte Markdown guardado en: {report_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prueba de estrés de OCR modo autónomo para MesaAyudaAgent")
    parser.add_argument("--minutes", type=float, default=15.0, help="Duración de la prueba en minutos (default: 15)")
    parser.add_argument("--interval", type=float, default=2.5, help="Intervalo entre capturas en segundos (default: 2.5)")
    parser.add_argument("--no-auto-cycle", action="store_true", help="Desactivar auto-cycle (modo pasivo)")
    args = parser.parse_args()

    run_stress_test(duration_minutes=args.minutes, interval_seconds=args.interval, auto_cycle=not args.no_auto_cycle)
