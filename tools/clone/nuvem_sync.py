import re
"""Synchronizes Kakashi's posting queue, accounts, Roleta, and Radar state with Google Drive (_kakashi_nuvem)
so GitHub Actions can take over seamlessly whenever this PC is asleep or turned off, and hand the updated
state right back when the PC wakes up.
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import postagem
import radar

ARQUIVOS_JSON = ("fila.json", "contas.json", "roleta.json", "radar.json")
INTERVALO_SYNC_S = 25
RCLONE_EXE = Path(__file__).resolve().parents[1] / "nuvem_bin" / "rclone.exe"
GH_EXE = Path(__file__).resolve().parents[1] / "nuvem_bin" / "gh.exe"
GH_REPO = "diegoal876/kakashi-nuvem"
GDRIVE_NUVEM = "gdrive:Non Shop/001/_kakashi_nuvem"
_estado_sync = {
    "ultimo": 0.0,
    "mtimes": {},
    "importado_em": 0,
    "ultimo_rclone": 0.0,
    "ultimo_vigia_gh": 0.0,
    "checou_nuvem": False,
    "encerrando": False,
}


def pasta_nuvem() -> Path | None:
    """Local staging folder for the cloud exchange, OUTSIDE the Google Drive for desktop mirror (G:).
    13/11/2026: the PC wrote fila.json into G:\\...\\_kakashi_nuvem at shutdown; when the PC came back, Drive for
    desktop finished uploading that stale copy over the fila.json GitHub Actions had written all night, the import
    found nothing, and the PC re-posted 4 videos the cloud had already posted. Every transfer now goes through rclone
    only, so no second writer can touch the cloud's files."""
    env = os.environ.get("KAKASHI_NUVEM_DIR")
    if env:
        p = Path(env)
        return p if p.parent.is_dir() else None
    if RCLONE_EXE.is_file():
        cache = postagem.DIR / "_nuvem_cache"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            return cache
        except OSError:
            return None
    return None


def _rclone_conf_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "rclone" / "rclone.conf"
    return Path.home() / ".config" / "rclone" / "rclone.conf"


def status_rclone_client() -> dict:
    conf = _rclone_conf_path()
    if not conf.is_file():
        return {"proprio": False, "client_id": ""}
    txt = conf.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^client_id\s*=\s*(.+)$", txt, flags=re.M)
    cid = m.group(1).strip() if m else ""
    return {"proprio": bool(cid), "client_id": (cid[:18] + "…") if len(cid) > 18 else cid}


def configurar_rclone_client(client_id: str, client_secret: str) -> str:
    cid = client_id.strip()
    csec = client_secret.strip()
    if not cid or not csec:
        raise ValueError("Preencha o Client ID e o Client Secret do Google Cloud.")
    conf = _rclone_conf_path()
    if not conf.is_file():
        raise ValueError(f"Arquivo rclone.conf nao encontrado em {conf}.")
    linhas = conf.read_text(encoding="utf-8", errors="replace").splitlines()
    novas = []
    em_gdrive = False
    inseriu = False
    for ln in linhas:
        if ln.strip().startswith("[") and ln.strip().endswith("]"):
            if em_gdrive and not inseriu:
                novas.append(f"client_id = {cid}")
                novas.append(f"client_secret = {csec}")
                inseriu = True
            em_gdrive = ln.strip().lower() == "[gdrive]"
        if em_gdrive and re.match(r"^\s*client_(id|secret)\s*=", ln):
            continue
        novas.append(ln)
    if em_gdrive and not inseriu:
        novas.append(f"client_id = {cid}")
        novas.append(f"client_secret = {csec}")
    novo_txt = "\n".join(novas) + "\n"
    conf.write_text(novo_txt, encoding="utf-8")
    gh_ok = False
    if GH_EXE.is_file():
        try:
            r = subprocess.run(
                [str(GH_EXE), "secret", "set", "RCLONE_CONF", "-R", GH_REPO],
                input=novo_txt,
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=postagem.NO_WINDOW,
            )
            gh_ok = r.returncode == 0
        except Exception:
            pass
    return "Client ID proprio salvo no rclone local" + (" e atualizado no GitHub Actions!" if gh_ok else "!")


def _rclone_cmd(*args: str, timeout: int = 45) -> bool:
    if not RCLONE_EXE.is_file():
        return False
    try:
        r = subprocess.run(
            [
                str(RCLONE_EXE),
                "--contimeout", "10s",
                "--timeout", "30s",
                "--low-level-retries", "4",
                "--drive-pacer-min-sleep", "300ms",
                "--tpslimit", "2",
                "--tpslimit-burst", "2",
                *args,
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=postagem.NO_WINDOW,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _tem_agendados_proximos(janela_s: int = 8 * 3600) -> bool:
    agora = time.time()
    with postagem.lock:
        for i in postagem.fila.get("itens", []):
            if i.get("status") in ("agendado", "postando") and postagem._restantes(i):
                q = i.get("quando") or agora
                if q <= agora + janela_s:
                    return True
    return False


def _garantir_vigia_nuvem(forcar_disparo: bool = False) -> None:
    """Trigger GitHub Actions only when explicitly requested (e.g. when the PC is shutting down),
    so we never burn GitHub Actions minutes or send failure emails while the PC is awake."""
    if not forcar_disparo:
        return
    if not GH_EXE.is_file():
        return
    agora = time.time()
    if not forcar_disparo and agora - _estado_sync["ultimo_vigia_gh"] < 480:
        return
    if not forcar_disparo and not _tem_agendados_proximos(8 * 3600):
        return
    _estado_sync["ultimo_vigia_gh"] = agora
    try:
        r = subprocess.run(
            [str(GH_EXE), "run", "list", "-R", GH_REPO, "--limit", "4", "--json", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=postagem.NO_WINDOW,
        )
        if r.returncode == 0:
            runs = json.loads(r.stdout or "[]")
            if any(x.get("status") in ("in_progress", "queued", "waiting", "pending") for x in runs):
                return
        subprocess.run(
            [str(GH_EXE), "workflow", "run", "kakashi-nuvem.yml", "-R", GH_REPO, "-f", "forcar=0"],
            capture_output=True,
            timeout=15,
            creationflags=postagem.NO_WINDOW,
        )
    except Exception:
        pass


def _ao_encerrar_pc(manter_vivo: bool = False) -> None:
    """When the PC shuts down, logs off, sleeps/hibernates, or closes Kakashi, zero out heartbeat_pc.json on Drive
    and wake up GitHub Actions immediately."""
    if _estado_sync.get("encerrando"):
        return
    _estado_sync["encerrando"] = True
    try:
        nuvem = pasta_nuvem()
        if nuvem and nuvem.is_dir():
            hb = nuvem / "heartbeat_pc.json"
            _gravar_json(hb, {"em": 0, "hora": "pc_desligado"})
            src_fila = postagem.DIR / "fila.json"
            if src_fila.is_file():
                try:
                    shutil.copy2(src_fila, nuvem / "fila.json")
                except OSError:
                    pass
            _rclone_cmd("copyto", str(hb), f"{GDRIVE_NUVEM}/heartbeat_pc.json", timeout=8)
            if (nuvem / "fila.json").is_file():
                _rclone_cmd("copyto", str(nuvem / "fila.json"), f"{GDRIVE_NUVEM}/fila.json", timeout=12)
        if _tem_agendados_proximos(18 * 3600):
            _garantir_vigia_nuvem(forcar_disparo=True)
    except Exception:
        pass
    finally:
        if manter_vivo:
            # After sleep/resume, allow export again once the PC wakes back up
            _estado_sync["encerrando"] = False


def _iniciar_vigia_energia_win32() -> None:
    """Create a hidden top-level Win32 window so even a consoleless pythonw.exe process receives
    WM_QUERYENDSESSION / WM_ENDSESSION (shutdown/restart/logoff) and WM_POWERBROADCAST (sleep/hibernate)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WM_QUERYENDSESSION = 0x0011
        WM_ENDSESSION = 0x0016
        WM_POWERBROADCAST = 0x0218
        PBT_APMSUSPEND = 0x0004
        PBT_APMRESUMESUSPEND = 0x0007
        PBT_APMRESUMEAUTOMATIC = 0x0012

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        def _wnd_proc(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_QUERYENDSESSION:
                    _ao_encerrar_pc(manter_vivo=False)
                    return 1
                if msg == WM_ENDSESSION and wparam:
                    _ao_encerrar_pc(manter_vivo=False)
                    return 0
                if msg == WM_POWERBROADCAST:
                    if wparam == PBT_APMSUSPEND:
                        _ao_encerrar_pc(manter_vivo=True)
                    elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                        _estado_sync["encerrando"] = False
                        _estado_sync["checou_nuvem"] = False
            except Exception:
                pass
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wnd_proc_ref = WNDPROC(_wnd_proc)
        _estado_sync["_wnd_proc_ref"] = wnd_proc_ref

        def _loop():
            try:
                hinst = kernel32.GetModuleHandleW(None)
                cls_name = f"KakashiPowerWatch_{os.getpid()}"
                wc = WNDCLASSW()
                wc.lpfnWndProc = wnd_proc_ref
                wc.hInstance = hinst
                wc.lpszClassName = cls_name
                user32.RegisterClassW(ctypes.byref(wc))
                hwnd = user32.CreateWindowExW(0, cls_name, cls_name, 0, 0, 0, 0, 0, None, None, hinst, None)
                _estado_sync["hwnd_power"] = int(hwnd or 0)
                if not hwnd:
                    return
                msg = wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            except Exception as e:
                _estado_sync["erro_power"] = str(e)

        threading.Thread(target=_loop, name="kakashi-power-watch", daemon=True).start()
    except Exception:
        pass


atexit.register(_ao_encerrar_pc)
if sys.platform == "win32" and not os.environ.get("GITHUB_ACTIONS"):
    try:
        import ctypes
        _HANDLER_PROTO = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def _ctrl_handler(ctrl_type: int) -> int:
            # CTRL_CLOSE_EVENT (2), CTRL_LOGOFF_EVENT (5), CTRL_SHUTDOWN_EVENT (6)
            if ctrl_type in (0, 1, 2, 5, 6):
                _ao_encerrar_pc()
            return 0

        _ctrl_ref = _HANDLER_PROTO(_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_ref, 1)
    except Exception:
        pass
    if Path(sys.argv[0]).name == "painel.py":
        _iniciar_vigia_energia_win32()


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


def importar_da_nuvem(exigir_rede: bool = False) -> tuple[bool, bool]:
    """If GitHub Actions ran while the PC was asleep/off, merge its posted results and radar readings into memory.
    Returns (mudou, rede_ok). When exigir_rede is True (on startup or wake from sleep), rede_ok indicates whether
    rclone could reach Google Drive so we never overwrite cloud state before checking it first.
    """
    nuvem = pasta_nuvem()
    if not nuvem or not nuvem.is_dir():
        return False, False
    # Pull nuvem_status.json directly via rclone only when exigir_rede is True (startup / wake from sleep)
    if exigir_rede:
        rede_ok = _rclone_cmd("copyto", f"{GDRIVE_NUVEM}/nuvem_status.json", str(nuvem / "nuvem_status.json"), timeout=15)
        if not rede_ok and RCLONE_EXE.is_file():
            return False, False
    st = _ler_json(nuvem / "nuvem_status.json") or {}
    nuvem_em = int(st.get("nuvem_em") or 0)
    ultimo_imp = int(postagem.fila.get("nuvem_importado_em") or _estado_sync["importado_em"] or 0)
    if not nuvem_em or nuvem_em <= ultimo_imp:
        return False, True

    _rclone_cmd("copy", GDRIVE_NUVEM, str(nuvem), "--max-depth", "1", "--include",
                "{fila,fila_nuvem,contas,roleta,radar}.json", timeout=30)

    mudou = False
    # fila_nuvem.json is written only by GitHub Actions, so nothing on the PC side can have overwritten it;
    # fila.json is the fallback for a cloud runner from before 13/11/2026
    fila_nuvem = _ler_json(nuvem / "fila_nuvem.json") or _ler_json(nuvem / "fila.json")
    if isinstance(fila_nuvem, dict):
        with postagem.lock:
            por_id = {i["id"]: i for i in postagem.fila.get("itens", [])}
            for item_n in fila_nuvem.get("itens", []):
                iid = item_n.get("id")
                if iid in por_id:
                    loc = por_id[iid]
                    if item_n.get("resultados") and item_n.get("resultados") != loc.get("resultados"):
                        loc.setdefault("resultados", {}).update(item_n["resultados"])
                        loc["status"] = item_n.get("status", loc["status"])
                        loc["andamento"] = ""
                        mudou = True
                    elif item_n.get("status") == "postado" and loc.get("status") != "postado":
                        loc["status"] = "postado"
                        loc["andamento"] = ""
                        mudou = True
                    if item_n.get("quando") and item_n.get("quando") > (loc.get("quando") or 0) and loc.get("status") == "agendado":
                        loc["quando"] = item_n["quando"]
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
    return mudou, True


def exportar_para_nuvem(forcar: bool = False) -> None:
    """Write PC heartbeat and copy updated JSONs + pending video files to Google Drive (_kakashi_nuvem)."""
    agora = time.time()
    gap_sono = agora - _estado_sync["ultimo"] > 120
    if not forcar and agora - _estado_sync["ultimo"] < INTERVALO_SYNC_S:
        return
    nuvem = pasta_nuvem()
    if not nuvem:
        return
    try:
        nuvem.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    # If the PC just started or woke up from sleep/hibernation, require a remote check of nuvem_status.json
    # BEFORE exporting local fila.json so we never overwrite cloud work while Wi-Fi is still reconnecting.
    precisa_rede = forcar or gap_sono or not _estado_sync["checou_nuvem"]
    _, rede_ok = importar_da_nuvem(exigir_rede=precisa_rede)
    if not rede_ok:
        return
    _estado_sync["checou_nuvem"] = True
    _estado_sync["ultimo"] = agora

    _gravar_json(
        nuvem / "heartbeat_pc.json",
        {
            "em": int(agora),
            "hora": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hwnd": int(_estado_sync.get("hwnd_power") or 0),
        },
    )

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

    # Push top-level JSONs + heartbeat_pc.json when changed or every 180s, and NEVER overwrite nuvem_status.json from PC
    sincronizar_rclone = forcar or mudou_json or agora - _estado_sync["ultimo_rclone"] >= 180
    if sincronizar_rclone:
        _estado_sync["ultimo_rclone"] = agora
        _rclone_cmd(
            "copy",
            str(nuvem),
            GDRIVE_NUVEM,
            "--max-depth",
            "1",
            "--include",
            "*.json",
            "--exclude",
            "nuvem_status.json",
            "--exclude",
            "fila_nuvem.json",
            timeout=25,
        )

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
    mudou_videos = False
    for arq in pendentes:
        src_v = postagem.VIDEOS / arq
        dst_v = v_nuvem / arq
        if src_v.is_file() and not dst_v.is_file():
            try:
                shutil.copy2(src_v, dst_v)
                mudou_videos = True
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

    if mudou_videos or forcar:
        _rclone_cmd("copy", str(v_nuvem), f"{GDRIVE_NUVEM}/videos", "--exclude", "*.tmp", timeout=120)

    # Ensure a GitHub Actions container is on standby in Modo Vigia whenever upcoming scheduled posts exist
    _garantir_vigia_nuvem(forcar_disparo=forcar)


def passo_worker() -> None:
    exportar_para_nuvem(forcar=False)
