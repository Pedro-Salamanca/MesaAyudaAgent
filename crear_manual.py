import os
from dotenv import load_dotenv

load_dotenv()
memoria_dir = os.getenv("MEMORY_PATH", r"G:\Mi unidad\MesaAyuda_Memoria")
manuales_dir = os.path.join(memoria_dir, "Manuales_Base")
os.makedirs(manuales_dir, exist_ok=True)

contenido_manual = """CHECKLIST PREVIO – VALIDACIONES EN SITIO
REGLA DE ESCALAMIENTO: Queda prohibido escalar únicamente con frases generales como "no navega", "no da velocidad", "es la vertical" o "el router no sirve". El técnico debe entregar evidencia clara del punto exacto donde se interrumpe la conectividad.

PASO 1. VALIDACIÓN FÍSICA Y CAPA 1 (Cables, Energía y Conectividad)
Energía y Equipos: Confirmar el encendido correcto de Router/ORB, Switch y PoE (si aplica puerto 8).
Ponchado y Categoría (8 Hilos):
Verificar la acometida, patch cords y conectores RJ45. Asegurar que los 8 hilos estén correctamente ponchados.
Si la velocidad en el puerto negocia a 100 Mbps en vez de 1 Gbps / 2.5 Gbps, reponchar obligatoriamente los extremos.
Prueba de Aislamiento: Conectar el computador directamente a la vertical para determinar si la falla es de la red interna del usuario o de la vertical/SITE.
Prueba en la Cable de Vertical: Si el switch no registra o no entrega gestión/IP, cambiar puerto de la vertical (pto1 o pto 8)
Prueba con Equipos: Probar con otro (PoE o Router) antes de asumir que el equipo está defectuoso.

PASO 2. ROUTERS E INDICADORES LED (PPL/SECUNDARIOS)
⚠️ ACLARACIÓN TÉCNICA DE GESTIÓN PARA ROUTERS (PPL/SECUNDARIOS):
Los routers secundarios reciben su IP automáticamente del router principal por DHCP. No es posible asignarles una IP de gestión individual. No es necesario solicitar a mesa de ayuda "dar gestión" a un router secundario sin antes realizar validaciones.
Falta de Gestión / Sin Redes Wi-Fi Visibles:
Evidencia en Sitio: Si los LED/bombillos de las bandas 2.4 GHz y 5 GHz NO alumbran en el router secundario (o principal), el equipo se encuentra bloqueado o en espera de asignación de IP (a menudo el secundario no la toma automáticamente y por ello los LED permanecen apagados).
Procedimiento Obligatorio en Sitio:
Reinicio de energía: Desconectar el equipo router secundario (o principal) de la fuente de corriente durante 1 minuto completo y volver a conectar.
Reset de fábrica: Si tras reiniciar los LED continúan apagados, "chuzar" (presionar el botón de Reset) el router secundario (o principal) con un clip o aguja durante 1 minuto continuo para restaurar valores de fábrica.
Tiempo de espera: Esperar 10 minutos a que el equipo levante las bandas y tome dirección IP.
Descarte con cambio de equipo: Si tras el reinicio, el reset y el tiempo de espera el equipo no levanta ni toma gestión, probar reemplazando el equipo secundario por uno de repuesto para confirmar si la falla es del dispositivo antes de escalar a Mesa de Ayuda.

PASO 3. VALIDACIÓN LÓGICA Y COMPARATIVA DE SWITCH (Capa 2)
Validación Cruzada con Vecinos:
Si los demás apt o usuarios del mismo switch tienen navegación/gestión y solo falla un usuario, la falla está en el puerto, acometida o router de ese usuario específico.
Tickets de Red: Los tickets de red solo se abren cuando cae la unidad completa o múltiples apartamentos simultáneamente. No se abren por fallas individuales.
Coincidencia de MACs: Validar que la MAC física del router/ORB coincida exactamente con la MAC registrada en la orden (ID/VISITA). Si se reemplaza un equipo, verificar el nuevo registro.

PASO 4. PLATAFORMA ARTEMIS Y GESTIÓN
Tiempos de Arranque (10 Minutos): Esperar al menos 10 minutos completos tras encender el equipo para validar la emisión de las bandas WiFi (2.4 GHz y 5 GHz) antes de reportar falla de conexion inalámbrica.
Acciones en Artemis: Cambiar el SSID y clave de WiFi directamente desde la plataforma Artemis (cuando es instalacion), en soporte indicar datos a configurar.
Conteo Físico vs. Sistema: Verificar que la cantidad de equipos instalados en sitio coincida exactamente con los registrados en la plataforma.(TV BOX, ROUTER)

PASO 5. PRUEBAS PARA SERVICIOS ALTA VELOCIDAD (>1G) CABLEADA Y WI-FI
Servicios Superiores a 1 Gbps (2G+) CABLEADA:
Realizar prueba cableada con PC compatible directa al puerto del equipo. Confirmar que entregue la velocidad contratada.
Validar la negociación cableada de: EDGE → Switch → CPE(Router WAN/LAN).
Si la PC directa recibe 2G pero el router ORB negocia a 1G, revisar el cable/puerto WAN del router. 
Validación Cobertura y Bandas WiFi:
Confirmar que 2.4 GHz y 5 GHz estén activas en los equipos del cliente.
Si otros dispositivos se conectan con normalidad a la banda de 5 GHz, descartar el dispositivo del usuario antes de escalar (recordar que la banda 5 GHz tiene menor alcance que la 2.4 GHz).

PASO 6. FORMATO OBLIGATORIO DE ESCALAMIENTO A MESA DE AYUDA
Si tras realizar todas las comprobaciones el inconveniente persiste, enviar el reporte mediante la plantilla estándar adjuntando fotos/evidencias:
PLANTILLA DE ENVÍO A MESA DE AYUDA:
• ID de Orden / Servicio / Appointment: [Número de ID]
• Ubicación exacta: [Torre / Piso / Apt]
• MAC y Serie del Equipo: [MAC del Router/ORB / Puerto del Switch]
• Validaciones y Pruebas Realizadas: [Confirmar reponchado, validaciones en la vertical, espera de 10 min, prueba con PC directa.]
• Cambios Efectuados: [Cambio de conector/equipo/puerto]
• Detalle de Falla Específica y Error: [Explicar hasta dónde hay conectividad o adjuntar captura de pantalla]
"""

ruta_archivo = os.path.join(manuales_dir, "checklist_validaciones_sitio.txt")
with open(ruta_archivo, "w", encoding="utf-8") as f:
    f.write(contenido_manual)

print(f"Manual creado exitosamente en: {ruta_archivo}")
