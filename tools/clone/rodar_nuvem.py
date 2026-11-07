"""Cloud runner executed by GitHub Actions when the PC is asleep or turned off.
Reads state synced in _kakashi_nuvem, scans Google Drive for new videos, posts all due items to Meta,
runs the Radar collection/Discord alerts if due, and writes nuvem_status.json for the PC to import later.
"""
import json
import os
import shutil
import subprocess
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
MAX_ENVIOS_POR_EXECUCAO = 60  # Up to 60 videos per cloud run (~2.5h), then auto-chains next workflow run
GDRIVE_NUVEM = "gdrive:Non Shop/001/_kakashi_nuvem"
GDRIVE_POSTAGENS = "gdrive:Non Shop/001/Postagens"


def _tem_devidos_agora() -> bool:
    agora = time.time()
    with postagem.lock:
        for item in postagem.fila.get("itens", []):
            if item.get("status") in ("agendado", "postando") and postagem._devidos(item, agora):
                return True
    return False


def _pc_acordou_agora() -> bool:
    if os.environ.get("FORCAR_NUVEM") == "1":
        return False
    hb_file = postagem.DIR / "heartbeat_pc.json"
    try:
        subprocess.run(
            ["rclone", "copyto", f"{GDRIVE_NUVEM}/heartbeat_pc.json", str(hb_file)],
            capture_output=True,
            timeout=15,
        )
        if hb_file.is_file():
            hb = json.loads(hb_file.read_text(encoding="utf-8"))
            delta = int(time.time()) - int(hb.get("em") or 0)
            if 0 <= delta < PC_ATIVO_JANELA_S:
                print(f"[Kakashi Nuvem] PC acordou ha {delta}s ({hb.get('hora')}). Entregando controle de volta ao PC.", flush=True)
                return True
    except Exception:
        pass
    return False


def _garantir_videos_devidos() -> None:
    """If the PC went to sleep before finishing uploading a scheduled .mp4 to _kakashi_nuvem/videos,
    fetch it directly from gdrive:Non Shop/001/Postagens/<item['nome']> before sending."""
    agora = time.time()
    faltando = []
    with postagem.lock:
        for item in postagem.fila.get("itens", []):
            if item.get("status") == "agendado" and postagem._devidos(item, agora):
                arq = item.get("arquivo", "")
                if arq and not (postagem.VIDEOS / arq).is_file():
                    faltando.append((arq, item.get("nome", "")))
    for arq, nome in faltando:
        dst = postagem.VIDEOS / arq
        dst.parent.mkdir(parents=True, exist_ok=True)
        local_drive = Path(os.environ.get("POSTAGEM_DRIVE", "")) / nome
        if local_drive.is_file():
            shutil.copy2(local_drive, dst)
            continue
        if nome:
            print(f"[Kakashi Nuvem] Baixando video pendente direto do Drive: {nome} -> {arq}", flush=True)
            subprocess.run(["rclone", "copyto", f"{GDRIVE_POSTAGENS}/{nome}", str(dst)], timeout=120)


def _salvar_checkpoint_nuvem(enviados: int) -> None:
    """Persist fila.json and nuvem_status.json to Google Drive immediately after each video sent,
    so waking the PC mid-run never loses progress."""
    status_file = postagem.DIR / "nuvem_status.json"
    status_file.write_text(
        json.dumps({"nuvem_em": int(time.time()), "hora": time.strftime("%Y-%m-%d %H:%M:%S"), "enviados": enviados}, indent=2),
        encoding="utf-8",
    )
    for arq in ("fila.json", "nuvem_status.json"):
        p = postagem.DIR / arq
        if p.is_file():
            subprocess.run(["rclone", "copyto", str(p), f"{GDRIVE_NUVEM}/{arq}"], capture_output=True, timeout=30)


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
    if _pc_acordou_agora():
        return 0

    print(f"[Kakashi Nuvem] Assumindo operacao em {datetime.now():%Y-%m-%d %H:%M:%S}...", flush=True)
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
        print(f"[Kakashi Nuvem] Aviso no scan/preparo: {e}", flush=True)

    # 2. Send all due posts (respecting ENVIO_INTERVALO_S = 150s between sends) with live checkpointing
    enviados = 0
    while _tem_devidos_agora() and enviados < MAX_ENVIOS_POR_EXECUCAO:
        if enviados > 0:
            print(f"[Kakashi Nuvem] Aguardando {postagem.ENVIO_INTERVALO_S}s de intervalo seguro entre envios...", flush=True)
            for _ in range(int(postagem.ENVIO_INTERVALO_S // 30)):
                time.sleep(30)
                if _pc_acordou_agora():
                    return 0
            resto = int(postagem.ENVIO_INTERVALO_S % 30) + 2
            time.sleep(resto)
            if _pc_acordou_agora():
                return 0
        _garantir_videos_devidos()
        postagem._postar_devidos()
        enviados += 1
        _salvar_checkpoint_nuvem(enviados)

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
            print("[Kakashi Nuvem] Iniciando coleta do Radar de Virais...", flush=True)
            res_radar = radar.coletar()
            postagem._log(f"Radar (Nuvem): {res_radar}")
            print(f"[Kakashi Nuvem] Radar concluido: {res_radar}", flush=True)
    except Exception as e:
        print(f"[Kakashi Nuvem] Erro no Radar: {e}", flush=True)

    # 4. Stamp cloud completion so the PC imports the updated state when it wakes up
    _salvar_checkpoint_nuvem(enviados)
    print(f"[Kakashi Nuvem] Concluido com sucesso ({enviados} disparo(s) de postagem).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
