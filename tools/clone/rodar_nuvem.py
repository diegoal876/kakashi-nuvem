"""Cloud runner executed by GitHub Actions when the PC is asleep or turned off.
Reads state synced in _kakashi_nuvem, scans Google Drive for new videos, posts all due items to Meta,
runs the Radar collection/Discord alerts if due, and writes nuvem_status.json for the PC to import later.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import postagem
import radar

PC_ATIVO_JANELA_S = 300  # 5 minutes: if the PC updated heartbeat_pc.json within 5 min, the PC is awake
MAX_ENVIOS_POR_EXECUCAO = 12


def _tem_devidos_agora() -> bool:
    agora = time.time()
    with postagem.lock:
        for item in postagem.fila.get("itens", []):
            if item.get("status") in ("agendado", "postando") and postagem._devidos(item, agora):
                return True
    return False


def _esperar_preparando(timeout_s: int = 60) -> None:
    fim = time.time() + timeout_s
    while time.time() < fim:
        with postagem.lock:
            prep = [i["id"] for i in postagem.fila.get("itens", []) if i.get("status") == "preparando"]
        if not prep:
            return
        for iid in prep:
            postagem._preparar(iid)
        time.sleep(1)


def main() -> int:
    hb_file = postagem.DIR / "heartbeat_pc.json"
    forcar = os.environ.get("FORCAR_NUVEM") == "1"
    if hb_file.is_file() and not forcar:
        try:
            hb = json.loads(hb_file.read_text(encoding="utf-8"))
            delta = int(time.time()) - int(hb.get("em") or 0)
            if 0 <= delta < PC_ATIVO_JANELA_S:
                print(f"[Kakashi Nuvem] PC ativo ha {delta}s ({hb.get('hora')}). Nuvem em standby.")
                return 0
        except (OSError, ValueError):
            pass

    print(f"[Kakashi Nuvem] Assumindo operacao em {datetime.now():%Y-%m-%d %H:%M:%S}...")
    with postagem.lock:
        for item in postagem.fila.get("itens", []):
            item["andamento"] = ""
        if postagem.roleta_dados():
            postagem._aplicar_avatares(refazer="todos")
        postagem._aplicar_avatares()
        postagem._salvar()

    # 1. Scan Google Drive (two passes so _drive_marcas sees stable size+mtime)
    try:
        postagem._scan_drive()
        time.sleep(1)
        postagem._scan_drive()
        _esperar_preparando()
        postagem._repor_auto()
        postagem._acompanhar_meta()
        postagem._conferir_meta()
    except Exception as e:
        print(f"[Kakashi Nuvem] Aviso no scan/preparo: {e}")

    # 2. Send all due posts (respecting ENVIO_INTERVALO_S = 150s between sends)
    enviados = 0
    while _tem_devidos_agora() and enviados < MAX_ENVIOS_POR_EXECUCAO:
        if enviados > 0:
            print(f"[Kakashi Nuvem] Aguardando {postagem.ENVIO_INTERVALO_S}s de intervalo seguro entre envios...")
            time.sleep(postagem.ENVIO_INTERVALO_S + 2)
        postagem._postar_devidos()
        enviados += 1

    postagem._acompanhar_meta()
    postagem._conferir_meta()

    # 3. Run Radar checks & collection if due
    try:
        radar.registrar()
        radar._avisar_tokens()
        radar._priorizar()
        radar._auto_fase2()
        radar._relatorio_do_dia()
        radar._resumo_do_dia()
        if postagem.contas().get("paginas") and radar.coleta_devida(
            datetime.now(), radar.estado["ultima_tentativa"], radar.estado["config"]["horarios_coleta"]
        ):
            print("[Kakashi Nuvem] Iniciando coleta do Radar de Virais...")
            res_radar = radar.coletar()
            postagem._log(f"Radar (Nuvem): {res_radar}")
            print(f"[Kakashi Nuvem] Radar concluido: {res_radar}")
    except Exception as e:
        print(f"[Kakashi Nuvem] Erro no Radar: {e}")

    # 4. Stamp cloud completion so the PC imports the updated state when it wakes up
    status_file = postagem.DIR / "nuvem_status.json"
    status_file.write_text(
        json.dumps({"nuvem_em": int(time.time()), "hora": time.strftime("%Y-%m-%d %H:%M:%S"), "enviados": enviados}, indent=2),
        encoding="utf-8",
    )
    print(f"[Kakashi Nuvem] Concluido com sucesso ({enviados} disparo(s) de postagem).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
