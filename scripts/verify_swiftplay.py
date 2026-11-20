"""Verificación automatizada completa de SwiftPlay (seguridad, datos, red, GUI, libmpv y Windows)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QEvent, QObject, QSize, QTimer  # noqa: E402
from PySide6.QtGui import QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from swiftplay.config import AppConfig, VALID_SPEEDS, load_config, save_config  # noqa: E402
from swiftplay.models import AccountProfile, MediaItem, ResolvedStream, upgrade_thumb_url  # noqa: E402
from swiftplay.security import (  # noqa: E402
    clear_credentials,
    has_saved_session,
    load_account_profile,
    load_cookies_decrypted,
    save_account_profile,
    save_cookies_encrypted,
)
from swiftplay.session_state import SessionState, load_session_state, save_session_state  # noqa: E402
from swiftplay.storage import LocalLibrary  # noqa: E402
from swiftplay.themes import THEMES, build_stylesheet  # noqa: E402
from swiftplay.thumb_cache import ThumbnailCache  # noqa: E402
from swiftplay.twitch_service import (  # noqa: E402
    TwitchService,
    extract_channel_login,
    format_viewers,
)
from swiftplay.ui.icons import render_icon  # noqa: E402
from swiftplay.ui.main_window import MainWindow  # noqa: E402
from swiftplay.ui.settings_dialog import SettingsDialog  # noqa: E402
from swiftplay.ui.shortcuts_dialog import ShortcutsDialog  # noqa: E402
from swiftplay.ui.widgets import VideoGridCard  # noqa: E402
from swiftplay.windows_os import (  # noqa: E402
    apply_window_taskbar_properties,
    get_desktop_shortcut_path,
    get_process_ram_mb,
    is_startup_enabled,
    set_process_app_user_model_id,
)
from swiftplay.youtube_service import YouTubeService, is_short_video  # noqa: E402


def test_dpapi_security() -> None:
    print("[1/5] Probando seguridad DPAPI y filtrado estricto de cookies...")
    mixed = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tyt_secret_1",
            "music.youtube.com\tFALSE\t/\tTRUE\t1999999999\tYSC\tyt_secret_2",
            ".google.com\tTRUE\t/\tTRUE\t1999999999\tSID\tgoogle_must_not_be_saved",
            ".evil.com\tTRUE\t/\tFALSE\t1999999999\tTRACK\tevil_must_not_be_saved",
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "session.dpapi"
        count = save_cookies_encrypted(mixed, session_path=sp)
        assert count == 2, f"Se esperaban 2 cookies de YouTube, se guardaron {count}"
        assert has_saved_session(session_path=sp)
        raw_bytes = sp.read_bytes()
        assert b"yt_secret_1" not in raw_bytes, "session.dpapi no debe contener texto plano"
        assert b"google_must_not_be_saved" not in raw_bytes

        dec = load_cookies_decrypted(session_path=sp)
        assert dec is not None
        assert "SAPISID\tyt_secret_1" in dec
        assert "music.youtube.com" in dec
        assert "google_must_not_be_saved" not in dec
        assert "evil_must_not_be_saved" not in dec

        clear_credentials(session_path=sp, profile_path=Path(tmp) / "profile.json")
        assert not sp.exists()
        assert not has_saved_session(session_path=sp)

        prof_path = Path(tmp) / "profile.json"
        save_account_profile(
            AccountProfile(name="Usuario Test", handle="@usuario_test", avatar_url="https://example.com/a.jpg"),
            profile_path=prof_path,
        )
        loaded_prof = load_account_profile(prof_path)
        assert loaded_prof is not None and loaded_prof.name == "Usuario Test" and loaded_prof.handle == "@usuario_test"
        clear_credentials(session_path=sp, profile_path=prof_path)
        assert load_account_profile(prof_path) is None
    print("   OK: DPAPI cifrado/descifrado, perfil, filtrado y borrado seguro verificados.")


def test_storage_config_themes() -> None:
    print("[2/5] Probando almacenamiento SQLite, config (Modo A) y tema Plateado Mate...")
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.json"
        cfg = load_config(cfg_path)
        assert cfg.audio_normalization is False, "Normalización debe iniciar en False"
        assert cfg.auto_pause_other_player is True, "Auto-pausa (Modo A) debe iniciar en True"
        assert cfg.video_speed == 1.0
        assert cfg.theme == "matte_silver"

        cfg.music_download_dir = str(Path(tmp) / "MiMusicaDL")
        cfg.video_download_dir = str(Path(tmp) / "MiVideoDL")
        save_config(cfg, cfg_path)
        cfg2 = load_config(cfg_path)
        assert cfg2.music_download_dir == cfg.music_download_dir
        assert cfg2.video_download_dir == cfg.video_download_dir

        db = LocalLibrary(Path(tmp) / "library.db")
        v = MediaItem("vid1", "Video Test", "Canal Test", "5:10", 310, "", "video")
        m = MediaItem("mus1", "Song Test", "Artist Test", "3:20", 200, "", "music")
        db.add_watch_later(v)
        db.add_my_music(m)
        assert db.is_in_watch_later("vid1")
        assert db.is_in_my_music("mus1")
        assert len(db.list_watch_later()) == 1
        assert len(db.list_my_music()) == 1
        db.record_search("video", "python qt6")
        db.record_search("music", "daft punk")
        assert db.get_search_history("video") == ["python qt6"]
        assert db.get_search_history("music") == ["daft punk"]
        db.remove_watch_later("vid1")
        db.remove_my_music("mus1")
        assert db.count_watch_later() == 0
        assert db.count_my_music() == 0

        # Historial de reproducción ("Volver a escuchar" / Historial con progreso exacto) y anti-Shorts
        db.record_play(m)
        db.record_play(m)
        db.record_play(v)
        db.update_play_progress("vid1", 125.0, 310.0)
        assert abs(db.get_play_position("vid1") - 125.0) < 0.1
        vhist = db.list_video_history(limit=10)
        assert len(vhist) == 1 and abs(vhist[0].resume_pos - 125.0) < 0.1
        # Cerca del final se resetea el progreso a 0.0
        db.update_play_progress("vid1", 308.0, 310.0)
        assert db.get_play_position("vid1") == 0.0
        assert [x.video_id for x in db.list_listen_again("music", limit=5)] == ["mus1"]
        assert [x.video_id for x in db.list_listen_again("video", limit=5)] == ["vid1"]
        db.remove_from_history("vid1")
        assert db.count_video_history() == 0
        db.record_play(v)
        db.clear_video_history()
        assert db.count_video_history() == 0
        # Historial local de música
        db.record_play(m)
        assert db.count_music_history() == 1
        mhist = db.list_music_history(limit=10)
        assert len(mhist) == 1 and mhist[0].video_id == "mus1"
        db.clear_music_history()
        assert db.count_music_history() == 0

        # Twitch Favoritos locales y utilidades (sin sesión ni chat)
        assert extract_channel_login("https://www.twitch.tv/ibai") == "ibai"
        assert extract_channel_login("@Rubius") == "rubius"
        assert format_viewers(12500) == "12.5 K"
        tw_fav = MediaItem(
            "ibai",
            "Stream de prueba",
            "Ibai",
            "EN VIVO",
            0,
            "",
            "twitch",
            is_live=True,
            game_name="Just Chatting",
            viewer_count=15200,
        )
        assert tw_fav.normalized_thumb().startswith("https://static-cdn.jtvnw.net/")
        db.add_twitch_favorite(tw_fav)
        assert db.is_twitch_favorite("ibai")
        assert db.count_twitch_favorites() == 1
        assert [x.video_id for x in db.list_twitch_favorites()] == ["ibai"]
        db.remove_twitch_favorite("ibai")
        assert db.count_twitch_favorites() == 0
        db.close()

        # Persistencia de última sesión (video + música + cola <= 15)
        st_path = Path(tmp) / "session_state.json"
        long_q = [MediaItem(f"q{i}", f"Track {i}", "Art", "3:00", 180, "", "music") for i in range(25)]
        st = SessionState(
            active_deck="video",
            video_item=MediaItem("v_last", "Último Video", "Canal", "10:00", 600, "", "video", published_text="hace 2 días"),
            video_pos=145.5,
            video_dur=600.0,
            music_item=long_q[10],
            music_pos=32.0,
            music_dur=180.0,
            music_queue=long_q,
            queue_index=10,
        )
        save_session_state(st, st_path)
        st2 = load_session_state(st_path)
        assert st2.video_item is not None and st2.video_item.video_id == "v_last"
        assert st2.video_item.published_text == "hace 2 días"
        assert abs(st2.video_pos - 145.5) < 0.2
        assert len(st2.music_queue) <= 15
        assert 0 <= st2.queue_index < len(st2.music_queue)
        assert st2.music_queue[st2.queue_index].video_id == "q10"

    assert upgrade_thumb_url("https://lh3.googleusercontent.com/abc=w120-h120-l90", 544) == (
        "https://lh3.googleusercontent.com/abc=w544-h544-l90"
    )
    assert upgrade_thumb_url("https://i.ytimg.com/vi/xyz/hqdefault.jpg", 544) == (
        "https://i.ytimg.com/vi/xyz/hqdefault.jpg"
    )

    assert is_short_video(45, url="https://www.youtube.com/watch?v=abc")
    assert is_short_video(120, url="https://www.youtube.com/shorts/xyz")
    assert is_short_video(120, title="Clip epico #shorts")
    assert not is_short_video(180, url="https://www.youtube.com/watch?v=abc", title="Documental")
    assert not is_short_video(0, url="https://www.youtube.com/watch?v=live", is_live=True)

    # ThumbnailCache circular key & avatar rendering & rounded radius
    tc = ThumbnailCache(max_items=10)
    assert tc._make_key("u", QSize(24, 24), False) != tc._make_key("u", QSize(24, 24), True)
    assert tc._make_key("u", QSize(48, 48), False, radius=10) == "u|48x48|r10"
    img = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFFFFFFFF)
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None
    tc._on_loaded("https://example.com/av.jpg|24x24|c", img)
    assert tc.get_or_fetch("https://example.com/av.jpg", QSize(24, 24), circular=True) is not None
    tc._on_loaded("https://example.com/rnd.jpg|48x48|r10", img)
    assert tc.get_or_fetch("https://example.com/rnd.jpg", QSize(48, 48), radius=10) is not None
    assert render_icon("play", "#CFD5DF", 18) is render_icon("play", "#CFD5DF", 18), (
        "render_icon debe usar caché LRU en RAM"
    )

    expected_tokens = ["#0B0C0E", "#121418", "#1A1E26", "#262B36", "#F1F3F6", "#CFD5DF"]
    assert "matte_silver" in THEMES
    qss = build_stylesheet("matte_silver")
    assert len(qss) > 500
    assert "PlayAccentBtn" in qss, "Falta regla dedicada PlayAccentBtn en QSS"
    assert "QFrame#PlayerBar QWidget" not in qss, "No debe existir regla 'QFrame#PlayerBar QWidget' que rompa PlayAccentBtn"
    for hx in expected_tokens:
        assert hx in qss, f"Falta color {hx} en tema matte_silver"
    print("   OK: Config, SQLite y tema Grafito Oscuro / Plateado Mate verificados.")


def test_live_extraction() -> tuple[ResolvedStream, ResolvedStream]:
    print("[3/5] Probando extracción en vivo (YouTube Video + YT Music + Radio + Streams)...")
    yt = YouTubeService()
    videos = yt.search_videos("lofi hip hop", limit=8)
    assert len(videos) >= 5, f"Se esperaban >= 5 videos, llegaron {len(videos)}"
    print(f"   Videos encontrados: {len(videos)} (ej: {videos[0].title})")

    feed = yt.get_video_home_feed(limit=10)
    assert len(feed) >= 1, "El feed de video no devolvió resultados"
    assert all(not is_short_video(x.duration_sec, title=x.title) for x in feed)
    dated_count = sum(1 for x in feed if x.published_text)
    assert dated_count >= 1, "Se esperaba al menos un video del feed con fecha de subida"
    print(f"   Feed de video sin Shorts OK ({len(feed)} videos, {dated_count} con fecha).")

    songs = yt.search_music("daft punk", search_filter="songs", limit=10)
    assert len(songs) >= 5, f"Se esperaban >= 5 canciones, llegaron {len(songs)}"
    print(f"   Canciones encontradas: {len(songs)} (ej: {songs[0].title})")

    music_secs = yt.get_music_home_sections(local_listen_again=songs[:2])
    assert len(music_secs) >= 2, f"Se esperaban >= 2 secciones de música, llegaron {len(music_secs)}"
    print(f"   Secciones YT Music OK: {[s.title for s in music_secs]}")

    audio_stream = yt.resolve_stream(songs[0].video_id, audio_only=True)
    assert audio_stream.primary_url.startswith("http"), "URL de audio inválida"
    print("   Stream solo audio resuelto OK.")

    video_stream = yt.resolve_stream(videos[0].video_id, audio_only=False, quality="720p")
    assert video_stream.primary_url.startswith("http"), "URL de video inválida"
    print("   Stream video 720p resuelto OK.")

    radio = yt.get_music_radio_queue(songs[0].video_id, limit=10)
    assert len(radio) >= 1, "La cola/radio de YT Music no devolvió pistas"
    print(f"   Radio/Autoplay cola OK ({len(radio)} pistas relacionadas).")

    tw = TwitchService()
    tw_streams = tw.get_top_streams(limit=5, language="ALL")
    assert len(tw_streams) >= 1, "Twitch no devolvió streams en vivo"
    assert all(s.kind == "twitch" and s.is_live for s in tw_streams)
    tw_hls = tw.resolve_live_stream(tw_streams[0].video_id, quality="360p")
    assert tw_hls.primary_url.startswith("http"), "URL HLS de Twitch inválida"
    assert ".m3u8" in tw_hls.primary_url.lower(), f"Se esperaba playlist .m3u8, recibido: {tw_hls.primary_url}"
    print(f"   Twitch en vivo sin sesión OK ({len(tw_streams)} canales, ej: {tw_streams[0].author} -> HLS listo).")
    return video_stream, audio_stream


def test_gui_and_libmpv_smoke(
    video_stream: ResolvedStream, audio_stream: ResolvedStream
) -> float:
    print("[4/5] Probando GUI Qt6 + Reproductor Dual lazy libmpv + Persistencia + RAM...")
    if sys.platform == "win32":
        assert set_process_app_user_model_id(), "Falló SetCurrentProcessExplicitAppUserModelID"
    app = QApplication.instance() or QApplication(sys.argv)

    with tempfile.TemporaryDirectory() as tmp_sess:
        temp_state_path = Path(tmp_sess) / "session_state.json"
        win = MainWindow(session_state_path=temp_state_path)
        if sys.platform == "win32":
            assert apply_window_taskbar_properties(int(win.winId())), "Falló SHGetPropertyStoreForWindow"
        win.show()
        app.processEvents()

        assert win.video_player is not win.music_player, "Deben existir dos instancias independientes"
        assert win.video_player._mpv is None, "video_player debe inicializar en modo lazy"
        assert win.music_player._mpv is None, "music_player debe inicializar en modo lazy"
        assert win.stack.currentIndex() == MainWindow.PAGE_VIDEO_SEARCH, "Debe abrir en Inicio (Modo Video)"
        af_val = win.video_player.get_property("af")
        assert bool(af_val) == win.cfg.audio_normalization, (
            f"Estado inicial de af ({af_val!r}) no coincide con config ({win.cfg.audio_normalization})"
        )
        sub_val = win.video_player.get_property("sub")
        assert sub_val in ("no", False), f"Subtítulos deben estar desactivados (sub={sub_val!r})"

        # Verificar modo teatro animado (ocultar/mostrar barra lateral) y restauración de tamaño tras fullscreen
        assert win.sidebar.isVisible() and not win.video_panel._is_theater
        win.toggle_sidebar()
        deadline_sb = time.monotonic() + 0.26
        while time.monotonic() < deadline_sb:
            app.processEvents()
            time.sleep(0.02)
        assert not win.sidebar.isVisible() and win.video_panel._is_theater
        win.toggle_sidebar()
        deadline_sb2 = time.monotonic() + 0.26
        while time.monotonic() < deadline_sb2:
            app.processEvents()
            time.sleep(0.02)
        assert win.sidebar.isVisible() and not win.video_panel._is_theater

        win.resize(1040, 680)
        app.processEvents()
        win._switch_page(MainWindow.PAGE_VIDEO_PLAYER)
        app.processEvents()
        win.toggle_fullscreen()
        app.processEvents()
        win.fs_overlay.reveal(win.geometry())
        app.processEvents()
        assert win.fs_overlay.isVisible()
        win.fs_overlay.fade_out()
        app.processEvents()
        win.fs_overlay.hide_immediately()
        assert not win.fs_overlay.isVisible()
        win.toggle_fullscreen()
        app.processEvents()
        assert abs(win.width() - 1040) <= 20 and abs(win.height() - 680) <= 20, (
            f"No se preservó el tamaño previo al fullscreen: {win.size()}"
        )
        win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
        app.processEvents()

        # Verificar switch Tab entre Video y Música
        assert not hasattr(win, "nav_queue"), "El ítem 'Cola' ya no debe existir en la barra lateral"
        assert hasattr(win, "nav_music_history"), "Falta 'Historial' bajo Música en la barra lateral"
        assert hasattr(win, "music_panel"), "Falta el panel completo de música con cola integrada"

        win.switch_music_video_tab()
        app.processEvents()
        assert win.stack.currentIndex() in (
            MainWindow.PAGE_MUSIC_SEARCH,
            MainWindow.PAGE_MY_MUSIC,
            MainWindow.PAGE_MUSIC_PLAYER,
            MainWindow.PAGE_MUSIC_HISTORY,
        )
        win.switch_music_video_tab()
        app.processEvents()
        assert win.stack.currentIndex() == MainWindow.PAGE_VIDEO_SEARCH

        # Verificar página Historial (Video y Música) y vista completa de Música sin ventanas fantasma
        stray_popups: list[str] = []

        class _StrayWindowWatcher(QObject):
            def eventFilter(self, obj, ev) -> bool:
                if ev.type() == QEvent.Type.Show and getattr(obj, "isWindow", lambda: False)():
                    if obj is not win:
                        stray_popups.append(type(obj).__name__)
                return False

        watcher = _StrayWindowWatcher(win)
        app.installEventFilter(watcher)
        try:
            test_card = VideoGridCard(
                MediaItem("v_dt", "Video Con Fecha", "Canal", "4:10", 250, "", "video", published_text="hace 2 horas"),
                False,
                win.thumb_cache,
                win.palette,
            )
            test_card.hide()
            test_card.deleteLater()
            assert hasattr(win, "nav_history")
            win._switch_page(MainWindow.PAGE_HISTORY)
            app.processEvents()
            assert win.stack.currentIndex() == MainWindow.PAGE_HISTORY
            win._switch_page(MainWindow.PAGE_MUSIC_HISTORY)
            app.processEvents()
            assert win.stack.currentIndex() == MainWindow.PAGE_MUSIC_HISTORY
            win._switch_page(MainWindow.PAGE_MUSIC_PLAYER)
            app.processEvents()
            assert win.stack.currentIndex() == MainWindow.PAGE_MUSIC_PLAYER
            win._switch_page(MainWindow.PAGE_TWITCH)
            app.processEvents()
            assert win.stack.currentIndex() == MainWindow.PAGE_TWITCH
            win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
            app.processEvents()
        finally:
            app.removeEventFilter(watcher)
        assert not stray_popups, f"Se detectaron ventanas emergentes inesperadas: {stray_popups}"

        # Probar volumen por rueda (±5% normal, ±1% Shift) en Video y Música + clic en portada Música
        orig_v_vol = win.cfg.video_volume
        orig_m_vol = win.cfg.music_volume
        win._on_video_volume_changed(50)
        win.video_panel.wheel_scrolled.emit(5)
        assert win.cfg.video_volume == 55, f"Esperado 55%, obtenido {win.cfg.video_volume}"
        win.video_panel.wheel_scrolled.emit(-1)
        assert win.cfg.video_volume == 54, f"Esperado 54% (Shift -1), obtenido {win.cfg.video_volume}"
        win._on_video_volume_changed(orig_v_vol)

        win._on_music_volume_changed(50)
        win.music_panel.wheel_scrolled.emit(5)
        assert win.cfg.music_volume == 55, f"Esperado 55% en música, obtenido {win.cfg.music_volume}"
        win.music_panel.wheel_scrolled.emit(-1)
        assert win.cfg.music_volume == 54, f"Esperado 54% en música, obtenido {win.cfg.music_volume}"
        win._on_music_volume_changed(orig_m_vol)
        win.vol_hud.hide()

        stage_clicks: list[bool] = []
        win.music_panel._current_item = MediaItem("m_clk", "Canción", "Artista", "3:00", 180, "", "music")
        win.music_panel.play_pause_requested.connect(lambda: stage_clicks.append(True))
        win.music_panel.stage_area.clicked.emit()
        assert len(stage_clicks) == 1, "Clic en el área de música debe emitir play_pause_requested"
        win.music_panel._current_item = None

        combo_speeds = [
            win.video_panel.speed_combo.itemData(i)
            for i in range(win.video_panel.speed_combo.count())
        ]
        assert tuple(combo_speeds) == VALID_SPEEDS == (1.0, 1.5, 2.0, 2.5), (
            f"Velocidades inesperadas: {combo_speeds}"
        )

        # Probar reproducción silenciada con libmpv (video + audio_url DASH, normalización, unload)
        win.player.set_volume(0)
        win.player.play_stream(
            primary_url=video_stream.primary_url,
            audio_url=video_stream.audio_url,
            video_enabled=True,
            http_headers=video_stream.http_headers,
            start_pos=0.0,
        )
        win.player.set_pause(True)
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)

        win.player.set_normalization(True)
        assert win.player.get_property("af"), "Normalización debería estar activa"
        win.player.set_normalization(False)
        assert not win.player.get_property("af"), "Normalización debería volver a OFF"
        win.player.set_normalization(win.cfg.audio_normalization)

        # Verificar clic simple para pausar/reanudar video (con debounce) y cancelación en doble clic
        win._video_item = MediaItem("vid_click", "Test Click", "Canal", "1:00", 60, "", "video")
        win._switch_page(MainWindow.PAGE_VIDEO_PLAYER)
        app.processEvents()
        win.video_player.set_pause(False)
        app.processEvents()
        win.video_panel.video_surface.clicked.emit()
        assert win._video_click_timer.isActive(), "Clic simple debe iniciar timer debounce"
        win.video_panel.video_surface.double_clicked.emit()
        assert not win._video_click_timer.isActive(), "Doble clic debe cancelar timer de pausa"
        if win._is_fullscreen:
            win.toggle_fullscreen()
        win._last_video_dbl_ts = 0.0
        win.video_panel.video_surface.clicked.emit()
        wait_click = time.monotonic() + 0.35
        while time.monotonic() < wait_click:
            app.processEvents()
            time.sleep(0.03)
        assert win.video_player.get_property("pause") is True, "Clic simple debe pausar el video"

        # Verificar unload de libmpv en reposo
        win.video_player.unload()
        assert win.video_player._mpv is None and not win.video_player.has_active_stream

        # Verificar adaptación modo EN VIVO (Twitch sin chat) en reproductor, fullscreen overlay y barra inferior
        live_demo = MediaItem(
            "tw_demo",
            "Directo Demo",
            "StreamerDemo",
            "EN VIVO",
            0,
            "",
            "twitch",
            is_live=True,
            game_name="Just Chatting",
            viewer_count=5432,
        )
        win._video_item = live_demo
        win.video_panel.set_item(live_demo, False)
        win.video_panel.set_live_mode(True, viewers_text="5.4 mil", is_twitch=True)
        win.fs_overlay.set_item(live_demo, False)
        win.fs_overlay.set_live_mode(True, viewers_text="5.4 mil", is_twitch=True)
        win._sync_bottom_bar()
        assert not win.video_panel.live_badge.isHidden()
        assert not win.video_panel.reload_live_btn.isHidden()
        assert win.video_panel.seek_slider.isHidden()
        assert win.video_panel.speed_combo.isHidden()
        assert win.video_panel.dl_btn.isHidden()
        assert not win.fs_overlay.live_badge.isHidden()
        assert not win.fs_overlay.reload_live_btn.isHidden()
        assert win.fs_overlay.seek_slider.isHidden()
        assert win.fs_overlay.speed_combo.isHidden()
        assert win.fs_overlay.dl_btn.isHidden()
        assert win.bb_deck_tag.text() == "EN VIVO"
        assert win.bb_seek.isHidden() and win.bb_dl.isHidden()

        vod_demo = MediaItem("vod_demo", "VOD Demo", "Canal", "3:00", 180, "", "video")
        win._video_item = vod_demo
        win.video_panel.set_live_mode(False, is_twitch=False)
        win.video_panel.set_item(vod_demo, False)
        win.fs_overlay.set_live_mode(False, is_twitch=False)
        win.fs_overlay.set_item(vod_demo, False)
        win._sync_bottom_bar()
        assert win.video_panel.live_badge.isHidden()
        assert not win.video_panel.seek_slider.isHidden()
        assert not win.video_panel.speed_combo.isHidden()
        assert not win.video_panel.dl_btn.isHidden()
        assert win.fs_overlay.live_badge.isHidden()
        assert not win.fs_overlay.seek_slider.isHidden()
        assert win.bb_deck_tag.text() == "VIDEO"
        assert not win.bb_seek.isHidden()

        win._video_item = None
        win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
        app.processEvents()

        # Probar music_player solo audio (lazy init) + HUD de volumen + ShortcutsDialog
        win.music_player.set_volume(0)
        win.music_player.play_stream(
            primary_url=audio_stream.primary_url,
            audio_url=None,
            video_enabled=False,
            http_headers=audio_stream.http_headers,
            start_pos=2.0,
        )
        assert win.music_player._mpv is not None, "music_player debe instanciar libmpv al reproducir"
        win.music_player.set_pause(True)
        app.processEvents()
        win.music_player.stop()
        win.music_player.set_volume(win.cfg.music_volume)
        win.video_player.set_volume(win.cfg.video_volume)
        win.vol_hud.show_volume(65, win.geometry())
        app.processEvents()
        win.vol_hud.hide()

        # Probar MiniBarWindow, ShortcutsDialog y SettingsDialog
        win.toggle_mini_bar()
        app.processEvents()
        assert win.mini_bar.isVisible()
        win._restore_from_mini_bar()
        app.processEvents()
        assert not win.mini_bar.isVisible()

        sdlg = ShortcutsDialog(win.palette, parent=win)
        QTimer.singleShot(120, sdlg.accept)
        sdlg.exec()
        app.processEvents()

        dlg = SettingsDialog(win.cfg, win.yt, thumb_cache=win.thumb_cache, parent=win)
        QTimer.singleShot(150, dlg.accept)
        dlg.exec()
        app.processEvents()

        ram_mb = get_process_ram_mb()
        assert ram_mb > 10.0, f"Medición de RAM inválida: {ram_mb}"
        print(f"   Uso de RAM medido en vivo (Qt6 + libmpv): {ram_mb:.1f} MB")

        win._force_quit = True
        win._cleanup_before_exit()
        win.close()
        app.processEvents()

        # Probar restauración de sesión al abrir (barra inferior + banner Continuar sin cargar libmpv)
        restore_path = Path(tmp_sess) / "restore_state.json"
        save_session_state(
            SessionState(
                active_deck="video",
                video_item=MediaItem("v_res", "Video Guardado", "Canal", "8:00", 480, "", "video"),
                video_pos=95.0,
                video_dur=480.0,
            ),
            restore_path,
        )
        win2 = MainWindow(session_state_path=restore_path)
        win2.show()
        app.processEvents()
        assert win2.video_player._mpv is None, "Sesión restaurada no debe cargar libmpv todavía"
        assert win2._video_item is not None and win2._video_item.video_id == "v_res"
        assert abs(win2._video_pos - 95.0) < 0.2
        assert win2.continue_banner.isVisible(), "El banner Continuar debe verse al restaurar"
        assert win2.bottom_bar.isVisible(), "La barra inferior debe verse al restaurar"
        win2._force_quit = True
        win2._cleanup_before_exit()
        win2.close()
        app.processEvents()

    print("   OK: Smoke test completo de GUI + libmpv lazy + restauración de sesión pasado.")
    return ram_mb


def test_windows_integration() -> None:
    print("[5/5] Verificando acceso directo en Escritorio y Registro de inicio...")
    lnk = get_desktop_shortcut_path()
    assert lnk.name == "SwiftPlay.lnk"
    assert lnk.exists(), f"No existe el acceso directo en Escritorio: {lnk}"
    assert is_startup_enabled(), "La entrada de inicio en HKCU\\...\\Run\\SwiftPlay no está activa"
    print(f"   OK: Acceso directo en {lnk} y entrada de inicio Windows activos.")


def main() -> int:
    test_dpapi_security()
    test_storage_config_themes()
    video_stream, audio_stream = test_live_extraction()
    ram_mb = test_gui_and_libmpv_smoke(video_stream, audio_stream)
    test_windows_integration()
    print(f"\nTODAS LAS VERIFICACIONES PASARON CORRECTAMENTE (RAM: {ram_mb:.1f} MB).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
