"""Cloud runner executed by GitHub Actions when the PC is asleep or turned off.
Reads state synced in _kakashi_nuvem, scans Google Drive for new videos, posts all due items to Meta
(including active Sentinel/Standby Mode waiting for upcoming scheduled slots within the execution window),
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

PC_ATIVO_JANELA_S = 240  # 4 minutes: if the PC updated heartbeat_pc.json within 4 min, the PC is awake
MAX_ENVIOS_POR_EXECUCAO = 80  # Up to 80 videos per cloud run, then auto-chains next workflow run
TEMPO_MAX_EXECUCAO_S = 35 * 60  # 35m max active time per GitHub Actions job shift
SENTINELA_JANELA_S = 20 * 60  # 20 minutes: only wait inside the container if a post is due within 20 min
GDRIVE_NUVEM = "gdrive:Non Shop/001/_kakashi_nuvem"
GDRIVE_POSTAGENS = "gdrive:Non Shop/001/Postagens"


def _rclone_safe(*args: str, timeout: int = 90, retries: int = 5) -> bool:
    """Run rclone with built-in timeouts and retries, never raising unhandled TimeoutExpired."""
    cmd = [
        "rclone",
        "--contimeout", "15s",
        "--timeout", "45s",
        "--low-level-retries", "5",
        "--drive-pacer-min-sleep", "300ms",
        "--tpslimit", "2",
        "--tpslimit-burst", "2",
        *args,
    ]
    for tent in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[Kakashi Nuvem] Aviso rclone ({tent + 1}/{retries}): {e}", flush=True)
        if tent + 1 < retries:
            time.sleep(3 * (tent + 1))
    return False


def _recarregar_fila_da_nuvem() -> None:
    """Pull latest state JSONs from Google Drive in a single batched call when taking over from PC."""
    _rclone_safe("copy", GDRIVE_NUVEM, str(postagem.DIR), "--max-depth", "1", "--include", "{fila,contas,roleta,radar}.json", timeout=45, retries=3)
    with postagem.lock:
        novo = postagem._load(postagem.FILA_FILE, None)
        if isinstance(novo, dict) and novo.get("itens"):
            postagem.fila.clear()
            postagem.fila.update(novo)
            for item in postagem.fila.get("itens", []):
                item["andamento"] = ""


def _tem_devidos_agora() -> bool:
    agora = time.time()
    with postagem.lock:
        caidas = set((postagem.fila.get("tokens_caidos") or {}).get("ids", []))
        for item in postagem.fila.get("itens", []):
            if item.get("status") not in ("agendado", "postando"):
                continue
            for d, _ in postagem._devidos(item, agora):
                if d.partition(":")[2] not in caidas and not postagem._respiro(item, d, agora):
                    return True
    return False


def _proximo_vencimento() -> float | None:
    """Earliest timestamp when a pending scheduled item will become due in the cloud."""
    agora = time.time()
    tempos = []
    with postagem.lock:
        caidas = set((postagem.fila.get("tokens_caidos") or {}).get("ids", []))
        for item in postagem.fila.get("itens", []):
            if item.get("status") != "agendado":
                continue
            rest = [d for d in postagem._restantes(item) if d.partition(":")[2] not in caidas]
            if rest:
                q = item.get("quando") or agora
                tempos.append(float(q))
            else:
                # Also wake up to confirm agendado_meta posts 3 min after their scheduled time
                for d, r in item.get("resultados", {}).items():
                    if r.get("estado") == "agendado_meta" and not r.get("conf"):
                        q = (item.get("quando") or agora) + postagem.CONFERIR_DEPOIS_S
                        if q > agora:
                            tempos.append(float(q))
    return min(tempos) if tempos else None


def _pc_acordou_agora() -> bool:
    if os.environ.get("FORCAR_NUVEM") == "1":
        return False
    hb_file = postagem.DIR / "heartbeat_pc.json"
    try:
        _rclone_safe("copyto", f"{GDRIVE_NUVEM}/heartbeat_pc.json", str(hb_file), timeout=20, retries=1)
        if hb_file.is_file():
            hb = json.loads(hb_file.read_text(encoding="utf-8"))
            em = int(hb.get("em") or 0)
            if em > 0:
                delta = int(time.time()) - em
                if 0 <= delta < PC_ATIVO_JANELA_S:
                    return True
    except Exception:
        pass
    return False


def _garantir_videos_devidos() -> None:
    """Download on demand any due .mp4 from _kakashi_nuvem/videos or Postagens/<item['nome']> before sending."""
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
        if _rclone_safe("copyto", f"{GDRIVE_NUVEM}/videos/{arq}", str(dst), timeout=120, retries=2) and dst.is_file():
            continue
        if nome:
            print(f"[Kakashi Nuvem] Baixando video pendente direto do Drive: {nome} -> {arq}", flush=True)
            _rclone_safe("copyto", f"{GDRIVE_POSTAGENS}/{nome}", str(dst), timeout=150, retries=2)


def _salvar_checkpoint_nuvem(enviados: int, forcar: bool = False) -> None:
    """Persist fila.json and nuvem_status.json to Google Drive immediately after each video sent,
    so waking the PC mid-run never loses progress."""
    if enviados <= 0 and not forcar:
        return
    status_file = postagem.DIR / "nuvem_status.json"
    try:
        status_file.write_text(
            json.dumps({"nuvem_em": int(time.time()), "hora": time.strftime("%Y-%m-%d %H:%M:%S"), "enviados": enviados}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    _rclone_safe("copy", str(postagem.DIR), GDRIVE_NUVEM, "--max-depth", "1", "--include", "{fila,nuvem_status}.json", timeout=60, retries=3)


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
    inicio_exec = time.time()

    # If the PC is currently awake, exit immediately so we don't burn GitHub Actions minutes while the PC is on.
    if _pc_acordou_agora():
        print("[Kakashi Nuvem] PC acordado e ativo. Encerrando execucao imediatamente para economizar minutos.", flush=True)
        return 0

    _recarregar_fila_da_nuvem()
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

    # 2. Send all due posts + Sentinel Mode (wait for upcoming posts within 8h, handing off shifts at 2h30m)
    enviados = 0
    ultimo_envio_ts = 0.0
    while enviados < MAX_ENVIOS_POR_EXECUCAO and (time.time() - inicio_exec) < TEMPO_MAX_EXECUCAO_S:
        if _pc_acordou_agora():
            print("[Kakashi Nuvem] PC voltou a ficar ativo. Entregando controle ao PC.", flush=True)
            break

        if not _tem_devidos_agora():
            postagem._acompanhar_meta()
            postagem._conferir_meta()
            prox = _proximo_vencimento()
            agora = time.time()
            espera = (prox - agora) if prox else None
            if espera is not None and espera <= SENTINELA_JANELA_S:
                mins = max(1, int(max(0.0, espera) // 60))
                print(f"[Kakashi Nuvem · Sentinela] Proximo horario em ~{mins} min ({datetime.fromtimestamp(prox):%H:%M}). Aguardando...", flush=True)
                dormir = min(60, max(10, int(max(10.0, espera))))
                for _ in range(max(1, dormir // 30)):
                    time.sleep(min(30, dormir))
                    if _pc_acordou_agora() or (time.time() - inicio_exec) >= TEMPO_MAX_EXECUCAO_S:
                        break
                continue
            break

        espera_rate = postagem.ENVIO_INTERVALO_S - (time.time() - ultimo_envio_ts)
        if ultimo_envio_ts > 0 and espera_rate > 0:
            print(f"[Kakashi Nuvem] Aguardando {int(espera_rate)}s de intervalo seguro entre envios...", flush=True)
            while (time.time() - ultimo_envio_ts) < postagem.ENVIO_INTERVALO_S:
                time.sleep(min(15.0, max(1.0, postagem.ENVIO_INTERVALO_S - (time.time() - ultimo_envio_ts))))
                if _pc_acordou_agora():
                    _salvar_checkpoint_nuvem(enviados)
                    return 0

        _garantir_videos_devidos()
        postagem._envio["ultimo"] = 0.0
        postagem._postar_devidos()
        if postagem._envio["ultimo"] > 0:
            ultimo_envio_ts = time.time()
            enviados += 1
            _salvar_checkpoint_nuvem(enviados)
        else:
            postagem._envio["ultimo"] = ultimo_envio_ts
            time.sleep(15)

    antes_conf = json.dumps(postagem.fila.get("itens", []), sort_keys=True)
    postagem._acompanhar_meta()
    postagem._conferir_meta()
    postagem._checar_tokens()
    postagem._checar_atrasos()
    depois_conf = json.dumps(postagem.fila.get("itens", []), sort_keys=True)

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
            _rclone_safe("copyto", str(postagem.DIR / "radar.json"), f"{GDRIVE_NUVEM}/radar.json", timeout=60, retries=2)
    except Exception as e:
        print(f"[Kakashi Nuvem] Erro no Radar: {e}", flush=True)

    # 4. Stamp cloud completion so the PC imports the updated state when it wakes up
    _salvar_checkpoint_nuvem(enviados, forcar=(antes_conf != depois_conf))
    print(f"[Kakashi Nuvem] Concluido com sucesso ({enviados} disparo(s) de postagem).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
