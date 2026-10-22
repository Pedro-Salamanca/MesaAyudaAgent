"""
MESAAYUDAAGENT v3.2 - SCRIPT DE PRUEBA DE ESTRÉS Y RENDIMIENTO OCR
Duración configurable (por defecto 15 minutos).
Monitorea latencia de captura, tiempo de OCR, memoria RAM y extracción de anclas.
Soporta modo pasivo (usuario trabajando) o auto-cycle (rotación automática de ventanas).
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
        time.sleep(0.3)
    except Exception:
        pass

def run_stress_test(duration_minutes: float = 15.0, interval_seconds: float = 2.5, auto_cycle: bool = False):
    total_seconds = int(duration_minutes * 60)
    start_time = time.time()
    end_time = start_time + total_seconds

    print("=" * 80)
    print(" INICIANDO PRUEBA DE ESTRÉS Y RENDIMIENTO OCR - MESAAYUDAAGENT v3.2")
    print("=" * 80)
    print(f"Duración objetivo: {duration_minutes:.1f} minutos ({total_seconds} segundos)")
    print(f"Intervalo entre capturas: {interval_seconds}s")
    print(f"Modo: {'🔄 Auto-Cycle (Rotación autónoma de ventanas)' if auto_cycle else '👁️ Pasivo (Monitoreo del trabajo del operador)'}")
    print(f"Hora de inicio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    vision = ScreenReader()
    pre_filter = PreFilterEngine()

    initial_ram = get_process_memory_mb()
    print(f"Memoria RAM inicial del proceso: {initial_ram:.2f} MB\n")

    records = []
    iteration = 0
    cycle_index = 0

    try:
        while time.time() < end_time:
            iteration += 1
            elapsed = time.time() - start_time
            remaining = max(0, end_time - time.time())
            min_str = f"[{int(elapsed//60):02d}:{int(elapsed%60):02d} / {int(total_seconds//60):02d}:00]"

            # Si auto_cycle está activo, rotar periódicamente entre ventanas técnicas
            if auto_cycle and iteration % 3 == 0:
                windows = get_open_windows()
                tech_windows = [w for w in windows if any(k in w[1].lower() for k in ["artemis", "winbox", "putty", "moba", "chrome", "edge", "somos", "zabbix", "code"])]
                if tech_windows:
                    target_hwnd, target_title = tech_windows[cycle_index % len(tech_windows)]
                    cycle_index += 1
                    bring_window_to_front(target_hwnd)

            # 1. Obtener ventana activa
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buf, 512)
            active_title = title_buf.value.strip() or "Ventana Desconocida"

            # 2. Medir tiempo de captura MSS
            t0 = time.time()
            # Llamamos a capture_and_read
            raw_text = vision.capture_and_read(active_title)
            t1 = time.time()
            total_read_time = t1 - t0

            # 3. Medir memoria RAM actual
            curr_ram = get_process_memory_mb()

            # 4. Extraer anclas con pre-filter
            anchors = pre_filter.extract_anchors(raw_text, active_title)
            tec = anchors.get("tecnico_campo") or "-"
            cun = anchors.get("id_servicio_cun") or "-"
            tool = anchors.get("herramienta_origen") or "General"
            char_count = len(raw_text.strip())

            # 5. Detección de duplicado / cambio de píxeles
            is_dup = pre_filter.is_duplicate_text(raw_text, threshold=0.92)
            estado = "⏸️ Idéntico" if is_dup else "✅ Nuevo"

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
                "tool": tool,
                "is_dup": is_dup
            }
            records.append(record)

            # Imprimir línea de telemetría limpia
            print(f"{min_str} #{iteration:03d} | {estado} | {total_read_time*1000:6.1f}ms | {char_count:5d} chars | RAM: {curr_ram:5.1f}MB | App: {tool:8s} | Tec: {tec[:15]:15s} | Win: {active_title[:30]}")

            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        print("\n⚠️ Prueba interrumpida manualmente por el usuario.")

    # GENERAR INFORME FINAL
    final_time = time.time()
    final_ram = get_process_memory_mb()
    total_samples = len(records)

    if total_samples > 0:
        times = [r["ocr_time_ms"] for r in records]
        avg_time = sum(times) / len(times)
        min_time = min(times)
        max_time = max(times)
        times_sorted = sorted(times)
        p95_time = times_sorted[int(len(times_sorted) * 0.95)]
        ram_delta = final_ram - initial_ram

        print("\n" + "=" * 80)
        print(" RESUMEN FINAL DE LA PRUEBA DE ESTRÉS (OCR & RENDIMIENTO)")
        print("=" * 80)
        print(f"Total de iteraciones analizadas: {total_samples}")
        print(f"Tiempo total transcurrido: {(final_time - start_time) / 60:.2f} minutos")
        print(f"Latencia OCR Promedio: {avg_time:.1f} ms ({avg_time/1000:.2f}s)")
        print(f"Latencia Mínima: {min_time:.1f} ms | Máxima: {max_time:.1f} ms")
        print(f"Latencia Percentil 95 (P95): {p95_time:.1f} ms")
        print(f"Consumo RAM Inicial: {initial_ram:.2f} MB | Final: {final_ram:.2f} MB | Delta: {ram_delta:+.2f} MB")
        
        leak_verdict = "✅ EXCELENTE (Cero fugas de memoria)" if ram_delta < 25 else "⚠️ ATENCIÓN (Posible acumulación de memoria)"
        print(f"Diagnóstico de Fuga de Memoria: {leak_verdict}")
        print("=" * 80)

        # Guardar reporte en archivo Markdown
        os.makedirs("logs", exist_ok=True)
        report_path = os.path.join("logs", f"reporte_estres_ocr_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# REPORTE DE PRUEBA DE ESTRÉS Y RENDIMIENTO OCR\n")
            f.write(f"- **Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"- **Muestras analizadas:** {total_samples}\n")
            f.write(f"- **Duración real:** {(final_time - start_time) / 60:.2f} minutos\n")
            f.write(f"- **Latencia Promedio:** {avg_time:.1f} ms\n")
            f.write(f"- **Latencia P95:** {p95_time:.1f} ms\n")
            f.write(f"- **Memoria Inicial:** {initial_ram:.2f} MB | **Final:** {final_ram:.2f} MB | **Delta:** {ram_delta:+.2f} MB\n")
            f.write(f"- **Diagnóstico de Estabilidad:** {leak_verdict}\n\n")
            f.write("## Muestra de Telemetría (Últimas 20 iteraciones)\n\n")
            f.write("| # | Hora | Tiempo (ms) | Caracteres | RAM (MB) | Herramienta | Técnico | Ventana |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
            for r in records[-20:]:
                f.write(f"| {r['iteration']} | {r['timestamp']} | {r['ocr_time_ms']} | {r['char_count']} | {r['ram_mb']} | {r['tool']} | {r['technician']} | {r['active_title']} |\n")

        print(f"\n📄 Reporte completo guardado en: {report_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prueba de estrés de OCR para MesaAyudaAgent")
    parser.add_argument("--minutes", type=float, default=15.0, help="Duración de la prueba en minutos (default: 15)")
    parser.add_argument("--interval", type=float, default=2.5, help="Intervalo entre capturas en segundos (default: 2.5)")
    parser.add_argument("--auto-cycle", action="store_true", help="Rotar automáticamente entre ventanas técnicas")
    args = parser.parse_args()

    run_stress_test(duration_minutes=args.minutes, interval_seconds=args.interval, auto_cycle=args.auto_cycle)
