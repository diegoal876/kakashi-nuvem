"""Synchronizes Kakashi's posting queue, accounts, Roleta, and Radar state with Google Drive (_kakashi_nuvem)
so GitHub Actions can take over seamlessly whenever this PC is asleep or turned off, and hand the updated
state right back when the PC wakes up.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import postagem
import radar

ARQUIVOS_JSON = ("fila.json", "contas.json", "roleta.json", "radar.json")
INTERVALO_SYNC_S = 25
RCLONE_EXE = Path(__file__).resolve().parents[1] / "nuvem_bin" / "rclone.exe"
_estado_sync = {"ultimo": 0.0, "mtimes": {}, "importado_em": 0, "ultimo_rclone": 0.0}


def pasta_nuvem() -> Path | None:
    env = os.environ.get("KAKASHI_NUVEM_DIR")
    if env:
        p = Path(env)
        return p if p.parent.is_dir() else None
    drive_postagens = Path(postagem.fila.get("config", {}).get("pasta_drive") or r"G:\Meu Drive\Postagens")
    if drive_postagens.parent.is_dir():
        return drive_postagens.parent / "_kakashi_nuvem"
    padrao = Path(r"G:\Meu Drive\_kakashi_nuvem")
    return padrao if padrao.parent.is_dir() else None


def _rclone_cmd(*args: str, timeout: int = 45) -> bool:
    if not RCLONE_EXE.is_file():
        return False
    try:
        r = subprocess.run([str(RCLONE_EXE), *args], capture_output=True, timeout=timeout, creationflags=postagem.NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _ler_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _gravar_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def importar_da_nuvem() -> bool:
    """If GitHub Actions ran while the PC was asleep/off, merge its posted results and radar readings into memory."""
    nuvem = pasta_nuvem()
    if not nuvem or not nuvem.is_dir():
        return False
    # Pull nuvem_status.json directly via rclone if Google Drive for Desktop hasn't synced it yet
    _rclone_cmd("copyto", "gdrive:Non Shop/001/_kakashi_nuvem/nuvem_status.json", str(nuvem / "nuvem_status.json"), timeout=15)
    st = _ler_json(nuvem / "nuvem_status.json") or {}
    nuvem_em = int(st.get("nuvem_em") or 0)
    ultimo_imp = int(postagem.fila.get("nuvem_importado_em") or _estado_sync["importado_em"] or 0)
    if not nuvem_em or nuvem_em <= ultimo_imp:
        return False

    for nome in ARQUIVOS_JSON:
        _rclone_cmd("copyto", f"gdrive:Non Shop/001/_kakashi_nuvem/{nome}", str(nuvem / nome), timeout=20)

    mudou = False
    fila_nuvem = _ler_json(nuvem / "fila.json")
    if isinstance(fila_nuvem, dict):
        with postagem.lock:
            por_id = {i["id"]: i for i in postagem.fila.get("itens", [])}
            for item_n in fila_nuvem.get("itens", []):
                iid = item_n.get("id")
                if iid in por_id:
                    loc = por_id[iid]
                    if item_n.get("resultados") and item_n.get("resultados") != loc.get("resultados"):
                        loc["resultados"] = item_n["resultados"]
                        loc["status"] = item_n.get("status", loc["status"])
                        loc["andamento"] = ""
                        mudou = True
                    elif item_n.get("status") == "postado" and loc.get("status") != "postado":
                        loc["status"] = "postado"
                        loc["andamento"] = ""
                        mudou = True
                else:
                    v_src = nuvem / "videos" / item_n.get("arquivo", "")
                    v_dst = postagem.VIDEOS / item_n.get("arquivo", "")
                    if v_src.is_file() and not v_dst.is_file():
                        postagem.VIDEOS.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.copy2(v_src, v_dst)
                        except OSError:
                            pass
                    postagem.fila.setdefault("itens", []).append(item_n)
                    mudou = True
            postagem.fila["seq"] = max(int(postagem.fila.get("seq") or 0), int(fila_nuvem.get("seq") or 0))
            postagem.fila.setdefault("drive_vistos", {}).update(fila_nuvem.get("drive_vistos") or {})
            logs_loc = {(l.get("em"), l.get("msg")) for l in postagem.fila.get("log", [])}
            novos_logs = [l for l in fila_nuvem.get("log", []) if (l.get("em"), l.get("msg")) not in logs_loc]
            if novos_logs:
                postagem.fila["log"] = sorted(postagem.fila.get("log", []) + novos_logs, key=lambda x: x.get("em", 0))[-40:]
                mudou = True
            postagem.fila["nuvem_importado_em"] = nuvem_em
            _estado_sync["importado_em"] = nuvem_em
            postagem._salvar()

    radar_nuvem = _ler_json(nuvem / "radar.json")
    if isinstance(radar_nuvem, dict):
        with radar.lock:
            if int(radar_nuvem.get("ultima_coleta") or 0) >= int(radar.estado.get("ultima_coleta") or 0):
                for k, p_n in (radar_nuvem.get("posts") or {}).items():
                    p_l = radar.estado["posts"].get(k)
                    if not p_l or int(p_n.get("lido") or 0) >= int(p_l.get("lido") or 0):
                        radar.estado["posts"][k] = p_n
                radar.estado.setdefault("registro", {}).update(radar_nuvem.get("registro") or {})
                for campo in ("ultima_coleta", "ultima_tentativa", "resumo_dia", "relatorio_dia", "prioridade_dia",
                              "fracas", "fracas_avisadas", "avatares_fracos", "avatares_fracos_avisados", "restricao_avisada"):
                    if campo in radar_nuvem:
                        radar.estado[campo] = radar_nuvem[campo]
                radar._salvar()
                mudou = True

    if mudou:
        postagem._log("Nuvem: postagens e leituras feitas enquanto o PC dormia foram sincronizadas.")
    return mudou


def exportar_para_nuvem(forcar: bool = False) -> None:
    """Write PC heartbeat and copy updated JSONs + pending video files to Google Drive (_kakashi_nuvem)."""
    agora = time.time()
    if not forcar and agora - _estado_sync["ultimo"] < INTERVALO_SYNC_S:
        return
    _estado_sync["ultimo"] = agora
    nuvem = pasta_nuvem()
    if not nuvem:
        return
    try:
        nuvem.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    importar_da_nuvem()

    _gravar_json(nuvem / "heartbeat_pc.json", {"em": int(agora), "hora": time.strftime("%Y-%m-%d %H:%M:%S")})

    mudou_json = False
    for nome in ARQUIVOS_JSON:
        src = postagem.DIR / nome
        if not src.is_file():
            continue
        try:
            mt = src.stat().st_mtime_ns
        except OSError:
            continue
        dst = nuvem / nome
        if forcar or _estado_sync["mtimes"].get(nome) != mt or not dst.is_file():
            try:
                shutil.copy2(src, dst)
                _estado_sync["mtimes"][nome] = mt
                mudou_json = True
            except OSError:
                pass

    v_nuvem = nuvem / "videos"
    try:
        v_nuvem.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    with postagem.lock:
        pendentes = {
            i.get("arquivo")
            for i in postagem.fila.get("itens", [])
            if i.get("arquivo") and i.get("status") in ("preparando", "rascunho", "agendado", "postando")
            and any(d not in i.get("resultados", {}) for d in i.get("destinos", []))
        }
    for arq in pendentes:
        src_v = postagem.VIDEOS / arq
        dst_v = v_nuvem / arq
        if src_v.is_file() and not dst_v.is_file():
            try:
                shutil.copy2(src_v, dst_v)
                mudou_json = True
            except OSError:
                pass
    try:
        for f in v_nuvem.iterdir():
            if f.is_file() and f.name not in pendentes:
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass

    if forcar or mudou_json or agora - _estado_sync["ultimo_rclone"] >= 120:
        _estado_sync["ultimo_rclone"] = agora
        _rclone_cmd("copy", str(nuvem), "gdrive:Non Shop/001/_kakashi_nuvem", "--exclude", "*.tmp", timeout=45)


def passo_worker() -> None:
    exportar_para_nuvem(forcar=False)
