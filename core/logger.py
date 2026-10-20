import logging
import sys

def setup_logger(name="MesaAyudaAgent"):
    """
    Configura y retorna un registrador centralizado para trazabilidad de eventos.
    Permite auditar lecturas de pantalla, llamadas al modelo LLM, aciertos de memoria y errores.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger

log = setup_logger()
