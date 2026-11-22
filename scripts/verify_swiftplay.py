"""Verificación automatizada completa de SwiftPlay (seguridad, datos, red, GUI, libmpv y Windows)."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QPropertyAnimation, QSize, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from swiftplay.config import AppConfig, VALID_SPEEDS, load_config, save_config  # noqa: E402
from swiftplay.discord_service import DiscordService  # noqa: E402
from swiftplay.discord_worker import PCM_FRAME_BYTES, SILENCE_PCM, VoiceAudioMixer  # noqa: E402
from swiftplay.models import AccountProfile, MediaItem, ResolvedStream, upgrade_thumb_url  # noqa: E402
from swiftplay.security import (  # noqa: E402
    clear_credentials,
    clear_discord_token,
    has_discord_token,
    has_saved_session,
    load_account_profile,
    load_cookies_decrypted,
    load_discord_token_decrypted,
    save_account_profile,
    save_cookies_encrypted,
    save_discord_token_encrypted,
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
    SystemResourceMonitor,
    apply_window_taskbar_properties,
    get_desktop_shortcut_path,
    get_process_memory_mb,
    get_process_ram_mb,
    is_startup_enabled,
    set_process_app_user_model_id,
)
from swiftplay.youtube_service import (  # noqa: E402
    YouTubeService,
    _extract_innertube_videos,
    is_short_video,
)


def test_dpapi_security() -> None:
    print("[1/6] Probando seguridad DPAPI y filtrado estricto de cookies...")
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
    print("[2/6] Probando almacenamiento SQLite, config (Modo A) y tema Plateado Mate...")
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.json"
        cfg = load_config(cfg_path)
        assert cfg.audio_normalization is False, "Normalización debe iniciar en False"
        assert cfg.auto_pause_other_player is True, "Auto-pausa (Modo A) debe iniciar en True"
        assert cfg.video_speed == 1.0
        assert cfg.theme == "matte_silver"

        cfg.music_download_dir = str(Path(tmp) / "MiMusicaDL")
        cfg.video_download_dir = str(Path(tmp) / "MiVideoDL")
        cfg.discord_hidden_channels = [" 101 ", "101", "202"]
        cfg.discord_hidden_channel_names = {"101": "🔊 Sala", "999": "Ignorado"}
        save_config(cfg, cfg_path)
        cfg2 = load_config(cfg_path)
        assert cfg2.music_download_dir == cfg.music_download_dir
        assert cfg2.video_download_dir == cfg.video_download_dir
        assert cfg2.discord_hidden_channels == ["101", "202"]
        assert cfg2.discord_hidden_channel_names == {"101": "🔊 Sala"}

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
        assert "vid1" in db.list_watched_video_ids()
        db.remove_from_history("vid1")
        assert db.count_video_history() == 0
        db.record_play(v)
        db.clear_video_history()
        assert db.count_video_history() == 0
        assert "vid1" not in db.list_watched_video_ids()
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

        # Persistencia local de DMs de Discord
        db.upsert_discord_dm_thread("u99", "Carlos", "", "hola")
        db.save_discord_dm_message(
            "u99",
            {
                "id": "dm1",
                "author_id": "1",
                "author_name": "TuBot",
                "is_self": True,
                "is_bot": True,
                "content": "Que tal?",
                "timestamp": "21:10",
                "date_label": "Hoy",
            },
            peer_name="Carlos",
        )
        threads = db.list_discord_dm_threads()
        assert len(threads) == 1 and threads[0]["user_name"] == "Carlos"
        saved_dms = db.list_discord_dm_messages("u99")
        assert len(saved_dms) == 1 and saved_dms[0]["content"] == "Que tal?"
        db.remove_discord_dm_thread("u99")
        assert db.list_discord_dm_messages("u99") == []
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
    tc._inflight.add("https://example.com/fail.jpg|48x48|r")
    tc._on_failed("https://example.com/fail.jpg|48x48|r")
    assert "https://example.com/fail.jpg|48x48|r" not in tc._inflight, (
        "_on_failed debe limpiar claves fallidas de _inflight"
    )
    ws_mb, priv_mb = get_process_memory_mb()
    assert ws_mb > 0.0 and priv_mb > 0.0, f"Medición de memoria inválida: {(ws_mb, priv_mb)}"
    assert render_icon("play", "#CFD5DF", 18) is render_icon("play", "#CFD5DF", 18), (
        "render_icon debe usar caché LRU en RAM"
    )

    expected_tokens = ["#0B0C0E", "#121418", "#1A1E26", "#262B36", "#F1F3F6", "#CFD5DF"]
    assert "matte_silver" in THEMES
    qss = build_stylesheet("matte_silver")
    assert len(qss) > 500
    assert "PlayAccentBtn" in qss, "Falta regla dedicada PlayAccentBtn en QSS"
    assert "QFrame#PlayerBar QWidget" not in qss, "No debe existir regla 'QFrame#PlayerBar QWidget' que rompa PlayAccentBtn"
    assert "border-left: 3px" not in qss, "NavBtn activo debe usar borde redondeado uniforme sin esquinas de 90 grados"
    assert "QFrame#ResourceMonitorCard" in qss and "QFrame#DiscordVoiceHeader" in qss
    assert qss.count("{") == qss.count("}"), "QSS tiene llaves desbalanceadas"
    for sel in ("QFrame#DiscordChatCard", "QFrame#DiscordVoiceArea", "QFrame#DiscordDeckCard", "QFrame#DiscordSidebarMiniCard"):
        assert sel in qss, f"Falta selector {sel} en QSS"
    accent_section = qss.split("QPushButton#AccentBtn {{")[1].split("}}")[0] if "QPushButton#AccentBtn {{" in qss else qss.split("QPushButton#AccentBtn {")[1].split("}")[0]
    assert "#F1F3F6" in accent_section, "AccentBtn debe usar texto claro (#F1F3F6) legible"
    pill_section = qss.split("QPushButton#PillBtn {")[1].split("}")[0]
    assert "border-radius: 10px" in pill_section, "PillBtn debe usar radio <= 10px para mantener esquinas redondeadas"
    play_section = qss.split("QPushButton#PlayAccentBtn {")[1].split("}")[0]
    assert "border-radius: 16px" in play_section, "PlayAccentBtn debe usar border-radius: 16px"
    pill_active_section = qss.split('QPushButton#PillBtn[active="true"] {')[1].split("}")[0]
    assert "#F1F3F6" in pill_active_section, "PillBtn activo debe usar texto claro (#F1F3F6) legible"
    for hx in expected_tokens:
        assert hx in qss, f"Falta color {hx} en tema matte_silver"

    mock_it_data = {
        "contents": [
            {
                "lockupViewModel": {
                    "contentId": "vidNormal11",
                    "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                    "contentImage": {
                        "thumbnailViewModel": {
                            "image": {"sources": [{"url": "https://i.ytimg.com/vi/vidNormal11/hqdefault.jpg", "width": 480}]},
                            "overlays": [
                                {
                                    "thumbnailBottomOverlayViewModel": {
                                        "badges": [
                                            {"thumbnailBadgeViewModel": {"text": "33:44", "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT"}}
                                        ]
                                    }
                                }
                            ],
                        }
                    },
                    "metadata": {"lockupMetadataViewModel": {"title": {"content": "Tutorial largo"}, "metadata": {"contentMetadataViewModel": {"metadataRows": [{"metadataParts": [{"text": {"content": "CanalPro"}}]}]}}}},
                }
            },
            {
                "lockupViewModel": {
                    "contentId": "vidLive1111",
                    "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                    "contentImage": {
                        "thumbnailViewModel": {
                            "image": {"sources": [{"url": "https://i.ytimg.com/vi/vidLive1111/hqdefault.jpg", "width": 480}]},
                            "overlays": [
                                {
                                    "thumbnailBottomOverlayViewModel": {
                                        "badges": [
                                            {"thumbnailBadgeViewModel": {"text": "EN DIRECTO", "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"}}
                                        ]
                                    }
                                }
                            ],
                        }
                    },
                    "metadata": {"lockupMetadataViewModel": {"title": {"content": "Stream en vivo"}, "metadata": {"contentMetadataViewModel": {"metadataRows": [{"metadataParts": [{"text": {"content": "Streamer"}}]}]}}}},
                }
            },
            {
                "lockupViewModel": {
                    "contentId": "vidWatched1",
                    "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                    "contentImage": {
                        "thumbnailViewModel": {
                            "image": {"sources": [{"url": "https://i.ytimg.com/vi/vidWatched1/hqdefault.jpg", "width": 480}]},
                            "overlays": [
                                {
                                    "thumbnailBottomOverlayViewModel": {
                                        "badges": [{"thumbnailBadgeViewModel": {"text": "10:00", "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT"}}],
                                        "progressBar": {"thumbnailOverlayProgressBarViewModel": {"startPercent": 95}},
                                    }
                                }
                            ],
                        }
                    },
                    "metadata": {"lockupMetadataViewModel": {"title": {"content": "Video ya visto"}, "metadata": {"contentMetadataViewModel": {"metadataRows": [{"metadataParts": [{"text": {"content": "CanalPro"}}]}]}}}},
                }
            },
        ]
    }
    parsed_mock = _extract_innertube_videos(mock_it_data, limit=10, block_shorts=True, exclude_watched=True)
    assert [x.video_id for x in parsed_mock] == ["vidNormal11", "vidLive1111"], parsed_mock
    assert parsed_mock[0].duration == "33:44" and parsed_mock[0].duration_sec == 2024 and not parsed_mock[0].is_live
    assert parsed_mock[1].duration == "EN VIVO" and parsed_mock[1].is_live is True

    res_mon = SystemResourceMonitor()
    try:
        snap1 = res_mon.sample()
        snap2 = res_mon.sample()
        assert snap2.ws_mb > 0.0 and 0.0 <= snap2.app_cpu_pct <= 100.0 and 0.0 <= snap2.app_gpu_pct <= 100.0
    finally:
        res_mon.close()
    print("   OK: Config, SQLite y tema Grafito Oscuro / Plateado Mate verificados.")


def test_live_extraction() -> tuple[ResolvedStream, ResolvedStream]:
    print("[3/6] Probando extracción en vivo (YouTube Video + YT Music + Radio + Streams)...")
    yt = YouTubeService()
    videos = yt.search_videos("lofi hip hop", limit=8)
    assert len(videos) >= 5, f"Se esperaban >= 5 videos, llegaron {len(videos)}"
    print(f"   Videos encontrados: {len(videos)} (ej: {videos[0].title})")

    feed = yt.get_video_home_feed(limit=24)
    assert len(feed) >= 18, f"El feed de video debe devolver >= 18 resultados, llegaron {len(feed)}"
    assert all(not is_short_video(x.duration_sec, title=x.title, is_live=x.is_live) for x in feed)
    dur_count = sum(1 for x in feed if x.duration)
    assert dur_count >= len(feed) // 2, f"Se esperaba duración o EN VIVO en el feed ({dur_count}/{len(feed)})"
    dated_count = sum(1 for x in feed if x.published_text)
    assert dated_count >= 1, "Se esperaba al menos un video del feed con fecha de subida"
    watched_sample = {feed[0].video_id}
    recent_sample = {x.video_id for x in feed}
    feed_refresh = yt.get_video_home_feed(
        limit=18,
        watched_ids=watched_sample,
        recent_shown_ids=recent_sample,
        refresh=True,
    )
    assert len(feed_refresh) >= 1, "Refrescar el feed no debe devolver lista vacía"
    assert feed[0].video_id not in {x.video_id for x in feed_refresh}, (
        "Videos ya vistos deben quedar excluidos del feed"
    )
    new_on_refresh = {x.video_id for x in feed_refresh} - recent_sample
    assert len(new_on_refresh) >= 1, "Refrescar el feed debe traer videos nuevos"
    print(f"   Feed de video sin Shorts OK ({len(feed)} videos, {dur_count} con duración/live, {dated_count} con fecha, refresh +{len(new_on_refresh)} nuevos).")

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
    assert "yt_dlp" not in sys.modules, (
        "yt_dlp no debe importarse en el proceso principal (debe correr en ytdlp_worker)"
    )
    yt.close_worker()
    audio_stream_reopen = yt.resolve_stream(
        songs[0].video_id, audio_only=True, use_cache=False
    )
    assert audio_stream_reopen.primary_url.startswith("http"), (
        "Worker debe reabrirse automáticamente tras close_worker()"
    )
    assert "yt_dlp" not in sys.modules
    yt.close()
    tw.close()
    return video_stream, audio_stream


def test_gui_and_libmpv_smoke(
    video_stream: ResolvedStream, audio_stream: ResolvedStream
) -> float:
    print("[4/6] Probando GUI Qt6 + Reproductor Dual lazy libmpv + Persistencia + RAM...")
    if sys.platform == "win32":
        assert set_process_app_user_model_id(), "Falló SetCurrentProcessExplicitAppUserModelID"
    app = QApplication.instance() or QApplication(sys.argv)

    with tempfile.TemporaryDirectory() as tmp_sess:
        temp_state_path = Path(tmp_sess) / "session_state.json"
        temp_ram_log = Path(tmp_sess) / "ram_log.txt"
        win = MainWindow(session_state_path=temp_state_path, ram_log_path=temp_ram_log)
        win.sound_fx.set_output_enabled(False)
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
        deadline_sb = time.monotonic() + 1.0
        while win.sidebar.isVisible() and time.monotonic() < deadline_sb:
            app.processEvents()
            time.sleep(0.01)
        assert not win.sidebar.isVisible() and win.video_panel._is_theater
        win.toggle_sidebar()
        deadline_sb2 = time.monotonic() + 1.0
        while not win.sidebar.isVisible() and time.monotonic() < deadline_sb2:
            app.processEvents()
            time.sleep(0.01)
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

        # Verificar switch Tab con rotación de 4 modos: Video -> Música -> Twitch -> Discord -> Video
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
        assert win.stack.currentIndex() == MainWindow.PAGE_TWITCH
        win.switch_music_video_tab()
        app.processEvents()
        assert win.stack.currentIndex() == MainWindow.PAGE_DISCORD
        assert not win.discord_service.is_worker_running, (
            "Rotar a Discord con Tab no debe iniciar el worker si no se ha conectado"
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

        # Verificar unload de libmpv en reposo y reciclaje automático de instancia
        win.player._streams_played = win.player._recycle_after
        win.player.play_stream(
            primary_url=video_stream.primary_url,
            audio_url=video_stream.audio_url,
            video_enabled=True,
            http_headers=video_stream.http_headers,
            start_pos=0.0,
        )
        assert win.player._mpv is not None and win.player._streams_played == 1, (
            "MediaPlayer debe reciclar la instancia libmpv al alcanzar _recycle_after"
        )
        win.player.set_pause(True)
        app.processEvents()
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
        assert win.fs_overlay.play_btn.width() >= 34 and win.fs_overlay.play_btn.height() >= 34
        assert win.mini_bar.play_btn.width() >= 34 and win.mini_bar.play_btn.height() >= 34

        card_norm = VideoGridCard(
            MediaItem("vnorm1", "Video Con Duración", "Autor", "12:34", 754, "", "video"),
            False,
            win.thumb_cache,
            win.palette,
        )
        card_live = VideoGridCard(
            MediaItem("vlive1", "Directo YT", "Streamer", "", 0, "", "video", is_live=True),
            False,
            win.thumb_cache,
            win.palette,
        )
        assert card_norm.thumb_widget._duration_text == "12:34"
        assert card_live.thumb_widget._duration_text == "EN VIVO"
        card_norm.release_resources()
        card_live.release_resources()
        card_norm.deleteLater()
        card_live.deleteLater()

        # Verificar botón Cerrar video / directo / música en top_bar
        win._video_item = MediaItem("vclose", "Video a cerrar", "Autor", "10:00", 600, "", "video")
        win._switch_page(MainWindow.PAGE_VIDEO_PLAYER)
        app.processEvents()
        assert not win.close_media_btn.isHidden() and "Cerrar video" in win.close_media_btn.text()
        win._close_active_media()
        app.processEvents()
        assert win._video_item is None and win.close_media_btn.isHidden()

        win._video_item = MediaItem("twclose", "Stream TW", "Streamer", "EN VIVO", 0, "", "twitch", is_live=True)
        win._switch_page(MainWindow.PAGE_VIDEO_PLAYER)
        app.processEvents()
        assert "Cerrar directo" in win.close_media_btn.text()
        win._close_active_media()
        app.processEvents()
        assert win._video_item is None and win.close_media_btn.isHidden()

        win._music_item = MediaItem("mclose", "Canción", "Artista", "3:10", 190, "", "music")
        win._switch_page(MainWindow.PAGE_MUSIC_PLAYER)
        app.processEvents()
        assert "Cerrar música" in win.close_media_btn.text()
        win._close_active_media()
        app.processEvents()
        assert win._music_item is None and win.close_media_btn.isHidden()

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

        sdlg = ShortcutsDialog(win.palette, config=win.cfg, parent=win)
        sdlg.move(win.geometry().center() - QPointF(sdlg.width() / 2, sdlg.height() / 2).toPoint())
        def _send_outside_click_to_sdlg() -> None:
            outside_g = QPointF(float(win.geometry().left() + 8), float(win.geometry().top() + 8))
            ev = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(8.0, 8.0),
                outside_g,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QCoreApplication.sendEvent(win, ev)
        QTimer.singleShot(90, _send_outside_click_to_sdlg)
        QTimer.singleShot(500, sdlg.reject)  # Fallback por si fallara el filtro
        assert sdlg.exec() == ShortcutsDialog.DialogCode.Rejected, (
            "ShortcutsDialog debe cerrarse al hacer clic fuera de su ventana"
        )
        sdlg.deleteLater()
        app.processEvents()

        dlg = SettingsDialog(win.cfg, win.yt, thumb_cache=win.thumb_cache, parent=win)
        dlg.move(win.geometry().center() - QPointF(dlg.width() / 2, dlg.height() / 2).toPoint())
        dlg.select_discord_tab()
        app.processEvents()
        dlg.select_discord_shortcuts_tab()
        dlg.update_discord_hotkey_statuses(win._discord_hotkey_statuses)
        def _send_outside_click_to_dlg() -> None:
            outside_g = QPointF(float(win.geometry().left() + 8), float(win.geometry().top() + 8))
            ev = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(8.0, 8.0),
                outside_g,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QCoreApplication.sendEvent(win, ev)
        QTimer.singleShot(90, _send_outside_click_to_dlg)
        QTimer.singleShot(500, dlg.reject)
        assert dlg.exec() == SettingsDialog.DialogCode.Rejected, (
            "SettingsDialog debe cerrarse al hacer clic fuera de su ventana"
        )
        dlg.deleteLater()
        app.processEvents()

        assert win.resource_card is not None
        win._refresh_resource_monitor()
        assert win.res_ram_lbl.text().endswith(" MB")
        assert win.res_ram_total_lbl.text().endswith(" MB")
        assert "%" in win.res_cpu_lbl.text() and "%" in win.res_gpu_lbl.text()
        app_mb = int(win.res_ram_lbl.text().split()[0])
        win._on_discord_worker_ram_updated(95.0, 490.0)
        assert int(win.res_ram_total_lbl.text().split()[0]) >= app_mb + 94, (
            "TOTAL (+DS) debe sumar la RAM del worker de Discord"
        )
        win._on_discord_worker_ram_updated(0.0, 0.0)
        assert win.res_ram_total_lbl.text() == win.res_ram_lbl.text(), (
            "TOTAL (+DS) debe igualar RAM APP cuando Discord está desconectado"
        )
        prev_size = win.size()
        prev_mini_vis = not win.discord_mini_card.isHidden()
        try:
            win.discord_mini_card.setVisible(True)
            win.resize(860, 560)
            app.processEvents()
            assert win.resource_card.height() >= win.resource_card.sizeHint().height(), (
                f"resource_card aplastada en modo compacto: {win.resource_card.height()} < {win.resource_card.sizeHint().height()}"
            )
            for lbl in (win.res_ram_lbl, win.res_ram_total_lbl, win.res_cpu_lbl, win.res_gpu_lbl):
                assert lbl.height() >= lbl.fontMetrics().height(), (
                    f"Etiqueta de recursos recortada: {lbl.height()} < {lbl.fontMetrics().height()}"
                )
        finally:
            win.discord_mini_card.setVisible(prev_mini_vis)
            win.resize(prev_size)
            app.processEvents()

        # Verificar aislamiento lazy y ausencia de fugas Qt tras navegar incluyendo Discord
        assert not win.discord_service.is_worker_running, (
            "discord_worker no debe arrancar al iniciar SwiftPlay"
        )
        win._switch_page(MainWindow.PAGE_DISCORD)
        app.processEvents()
        assert not win.discord_service.is_worker_running, (
            "Visitar PAGE_DISCORD tampoco debe arrancar el worker hasta conectar"
        )
        assert win.top_bar.isHidden(), (
            "La barra de búsqueda de YouTube debe ocultarse en la pestaña de Discord"
        )
        dp = win.discord_panel
        assert not hasattr(dp, "options_btn"), (
            "No debe existir botón de ajustes interno dentro de la interfaz de Discord"
        )
        assert dp.members_card.isHidden(), "El panel de miembros debe iniciar oculto por defecto"
        assert dp.audio_advanced_box.isHidden(), "El cajón de audio avanzado debe iniciar oculto por defecto"
        assert not dp.left_card.isHidden(), "Los canales deben iniciar visibles"
        assert not dp.voice_pane.isHidden() and not dp.chat_pane.isHidden(), (
            "La vista dividida (Voz arriba + Chat abajo) debe iniciar activa por defecto"
        )
        assert not dp.text_channels_scroll.isHidden(), (
            "Los canales de texto unificados deben iniciar visibles bajo Voz"
        )
        dp._toggle_members_panel()
        assert not dp.members_card.isHidden()
        dp._toggle_members_panel()
        assert dp.members_card.isHidden()
        dp._toggle_audio_drawer()
        assert not dp.audio_advanced_box.isHidden()
        dp._toggle_audio_drawer()
        assert dp.audio_advanced_box.isHidden()
        dp._toggle_text_channels_section()
        assert dp.text_channels_scroll.isHidden()
        dp._toggle_text_channels_section()
        assert not dp.text_channels_scroll.isHidden()

        # Colapsar y reabrir canales, voz y chat
        dp.close_channels_btn.click()
        assert dp.left_card.isHidden()
        dp.toggle_channels_btn.click()
        assert not dp.left_card.isHidden()
        dp.close_voice_btn.click()
        assert dp.voice_pane.isHidden() and not dp.chat_pane.isHidden()
        dp.close_chat_btn.click()
        assert dp.center_splitter.isHidden() and not dp.center_collapsed_card.isHidden()
        dp.reopen_voice_btn.click()
        dp.tab_chat_btn.click()
        assert not dp.voice_pane.isHidden() and not dp.chat_pane.isHidden() and dp.center_collapsed_card.isHidden()

        # Verificar canales sin permiso ocultos por defecto y visibles al activarlo + miembros por canal + overlay
        assert win.cfg.discord_show_locked_channels is False
        orig_locked_cfg = win.cfg.discord_show_locked_channels
        orig_overlay_cfg = win.cfg.discord_overlay_enabled
        orig_overlay_mode = win.cfg.discord_overlay_mode
        orig_overlay_op = win.cfg.discord_overlay_opacity
        orig_sounds_en = win.cfg.discord_sounds_enabled
        orig_sound_pack = win.cfg.discord_sound_pack
        orig_sound_vols = dict(win.cfg.discord_sound_volumes)
        orig_notify_en = win.cfg.discord_notify_chat_enabled
        orig_notify_chs = list(win.cfg.discord_notify_channels)
        orig_hidden_chs = list(win.cfg.discord_hidden_channels)
        orig_hidden_names = dict(win.cfg.discord_hidden_channel_names)
        try:
            discord_popups: list[str] = []

            class _DiscordPopupWatcher(QObject):
                def eventFilter(self, obj, ev) -> bool:
                    if (
                        ev.type() == QEvent.Type.Show
                        and getattr(obj, "isWindow", lambda: False)()
                        and obj not in (win, win.discord_overlay)
                    ):
                        discord_popups.append(type(obj).__name__)
                    return False

            d_watcher = _DiscordPopupWatcher(win)
            app.installEventFilter(d_watcher)
            try:
                demo_guilds = [
                    {
                        "id": "10",
                        "name": "SrvDemo",
                        "voice_channels": [
                            {
                                "id": "101",
                                "name": "Sala",
                                "can_connect": True,
                                "members": [
                                    {
                                        "id": "1",
                                        "name": "Socio",
                                        "is_bot": True,
                                        "avatar_url": "",
                                        "muted": False,
                                        "deafened": False,
                                    }
                                ],
                            },
                            {
                                "id": "102",
                                "name": "Privado",
                                "can_connect": False,
                                "members": [],
                            },
                        ],
                        "text_channels": [
                            {"id": "201", "name": "bots", "can_read": True, "can_send": True},
                            {"id": "202", "name": "clips", "can_read": True, "can_send": True},
                        ],
                    }
                ]
                dp._on_guilds_updated(demo_guilds)
                app.processEvents()
                texts_hidden = [b.text() for b in dp.vc_list_widget.findChildren(type(dp.vc_section_btn))]
                assert not any("sin permiso" in t for t in texts_hidden), (
                    "Canales sin permiso no deben mostrarse por defecto"
                )

                win.cfg.discord_show_locked_channels = True
                dp.refresh_ui_preferences()
                app.processEvents()
                texts_shown = [b.text() for b in dp.vc_list_widget.findChildren(type(dp.vc_section_btn))]
                assert any("sin permiso" in t for t in texts_shown), (
                    "Canales sin permiso deben mostrarse al activar la opción"
                )

                # Simular cambio de voz y re-renders repetidos sin emitir ventanas fantasma
                dp._on_voice_state_updated(
                    {
                        "in_voice": True,
                        "guild_id": "10",
                        "guild_name": "SrvDemo",
                        "channel_id": "101",
                        "channel_name": "Sala",
                        "participants": [
                            {
                                "id": "1",
                                "name": "TuBot",
                                "is_bot": True,
                                "avatar_url": "",
                                "is_self": True,
                                "muted": False,
                                "deafened": False,
                            },
                            {
                                "id": "2",
                                "name": "SocioBot",
                                "is_bot": True,
                                "avatar_url": "",
                                "is_self": False,
                                "muted": False,
                                "deafened": False,
                            },
                        ],
                    }
                )
                dp._render_participants(force=True)
                dp._render_channels(force=True)
                app.processEvents()
            finally:
                app.removeEventFilter(d_watcher)
            assert not discord_popups, (
                f"Se detectaron ventanas fantasma en Discord: {discord_popups}"
            )

            # Verificar tinte dinámico según color del avatar
            from swiftplay.ui.discord_panel import _extract_avatar_tint

            blue_img = QImage(16, 16, QImage.Format.Format_ARGB32)
            blue_img.fill(0xFF2563EB)
            red_img = QImage(16, 16, QImage.Format.Format_ARGB32)
            red_img.fill(0xFFDC2626)
            blue_bg, _ = _extract_avatar_tint(QPixmap.fromImage(blue_img))
            red_bg, _ = _extract_avatar_tint(QPixmap.fromImage(red_img))
            assert blue_bg != red_bg, "El tinte de tarjeta debe adaptarse al color de perfil"

            # Verificar campana selectiva de avisos por canal
            alerts_seen: list[str] = []
            dp.channel_alert_requested.connect(lambda d: alerts_seen.append(str(d.get("channel_id") or "")))
            win.cfg.discord_notify_chat_enabled = True
            win.cfg.discord_notify_channels = []
            dp._toggle_channel_notify("202", "clips")
            assert "202" in win.cfg.discord_notify_channels
            dp._on_channel_message_incoming({"channel_id": "201", "channel_name": "bots", "author_name": "Socio", "preview": "hola"})
            dp._on_channel_message_incoming({"channel_id": "202", "channel_name": "clips", "author_name": "Socio", "preview": "mira"})
            assert alerts_seen == ["202"], f"Solo debe alertar canales con campana activa: {alerts_seen}"
            dp._toggle_channel_notify("202", "clips")
            assert "202" not in win.cfg.discord_notify_channels

            # Verificar ocultado persistente de canales (voz + texto) y reactivación desde Ajustes
            dp.hide_channel_permanently("101", "🔊 Sala (SrvDemo)")
            dp.hide_channel_permanently("202", "# clips (SrvDemo)")
            app.processEvents()
            vc_labels = [lb.text() for lb in dp.vc_list_widget.findChildren(QLabel)]
            tc_btns = [b.text() for b in dp.text_channels_scroll.findChildren(QPushButton)]
            assert not any("Sala" in t for t in vc_labels), f"El canal de voz oculto debe desaparecer: {vc_labels}"
            assert not any("clips" in t for t in tc_btns), f"El canal de texto oculto debe desaparecer: {tc_btns}"
            assert any("bots" in t for t in tc_btns), "# bots debe seguir visible"
            known_chs = dp.get_available_channels_for_settings()
            assert any(cid == "101" for cid, _ in known_chs) and any(cid == "202" for cid, _ in known_chs)
            hdlg = SettingsDialog(win.cfg, win.yt, thumb_cache=win.thumb_cache, discord_channels=known_chs, parent=win)
            hdlg._on_unhide_all_channels()
            hdlg.done(0)
            hdlg.deleteLater()
            dp.refresh_ui_preferences()
            app.processEvents()
            vc_labels2 = [lb.text() for lb in dp.vc_list_widget.findChildren(QLabel)]
            tc_btns2 = [b.text() for b in dp.text_channels_scroll.findChildren(QPushButton)]
            assert any("Sala" in t for t in vc_labels2) and any("clips" in t for t in tc_btns2)

            dp._on_guild_members_updated(
                {
                    "guild_id": "10",
                    "online_count": 3,
                    "members": [
                        {"id": "1", "name": "TuBot", "is_bot": True, "avatar_url": "", "status": "online", "activity": ""},
                        {"id": "2", "name": "SocioBot", "is_bot": True, "avatar_url": "", "status": "online", "activity": ""},
                        {"id": "3", "name": "Admin", "is_bot": False, "avatar_url": "", "status": "online", "activity": ""},
                    ],
                }
            )
            dp._on_chat_history_loaded(
                {
                    "kind": "channel",
                    "target_id": "201",
                    "title": "#bots",
                    "can_read": True,
                    "can_send": True,
                    "messages": [
                        {
                            "id": "m1",
                            "author_id": "1",
                            "author_name": "TuBot",
                            "is_self": True,
                            "is_bot": True,
                            "content": "Hola",
                            "attachments": [],
                            "timestamp": "19:40",
                            "date_label": "Hoy",
                        }
                    ],
                    "channel_members": [
                        {"id": "1", "name": "TuBot", "is_bot": True, "avatar_url": "", "status": "online", "activity": ""},
                        {"id": "2", "name": "SocioBot", "is_bot": True, "avatar_url": "", "status": "online", "activity": ""},
                    ],
                    "channel_online_count": 2,
                }
            )
            assert dp.members_list.count() == 2, "Modo 'Canal' debe filtrar miembros al canal activo"
            dp._set_members_scope("server")
            assert dp.members_list.count() == 3, "Modo 'Servidor' debe mostrar todos los miembros"
            dp._set_members_scope("channel")
            dp.members_list.setCurrentRow(0)
            assert dp.dm_btn.isHidden(), "No debe ofrecer Enviar DM al seleccionar otro bot"

            assert bool(win.discord_overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
            assert win.discord_overlay.isHidden(), "Overlay debe iniciar oculto al estar desconectado"
            win.discord_overlay.update_state(
                enabled=True, active=True, muted=True, deafened=False, opacity_pct=25
            )
            app.processEvents()
            assert (
                not win.discord_overlay.isHidden()
                and win.discord_overlay._mode == "muted"
                and win.discord_overlay.width() == 46
            )
            assert abs(win.discord_overlay.windowOpacity() - 0.25) < 0.05
            win.discord_overlay.update_state(
                enabled=True, active=True, muted=True, deafened=True, opacity_pct=30
            )
            app.processEvents()
            assert win.discord_overlay._mode == "both" and win.discord_overlay.width() == 86
            win.discord_overlay.update_state(
                enabled=True, active=True, muted=False, deafened=True, opacity_pct=30
            )
            app.processEvents()
            assert win.discord_overlay._mode == "deafened" and win.discord_overlay.width() == 46
            win.discord_overlay.update_state(
                enabled=False, active=True, muted=True, deafened=True, opacity_pct=30
            )
            app.processEvents()
            assert win.discord_overlay.isHidden()

            # Overlay total (compacto, sin recuadro extra, nombre <= 10 chars, preview Voz 1 / Voz 2)
            from swiftplay.ui.discord_overlay import format_overlay_name

            assert format_overlay_name("NoiseRed") == "NoiseRed"
            long_ov = format_overlay_name("NombreMuyLargo123")
            assert len(long_ov) <= 10 and long_ov.endswith("…"), long_ov

            win.discord_overlay.update_state(
                overlay_mode="full",
                active=True,
                in_voice=False,
                muted=True,
                deafened=False,
                opacity_pct=45,
                participants=[],
            )
            win.discord_overlay.preview(45, duration_ms=400, overlay_mode="full")
            prev_parts, _ = win.discord_overlay._visible_participants()
            assert [p["name"] for p in prev_parts] == ["Voz 1", "Voz 2"], prev_parts
            win.discord_overlay._end_preview()

            win.discord_overlay.update_state(
                overlay_mode="full",
                active=True,
                in_voice=True,
                muted=True,
                deafened=False,
                opacity_pct=45,
                participants=[
                    {"id": "1", "name": "TuBot", "is_self": True, "muted": True, "deafened": False, "avatar_url": ""},
                    {"id": "2", "name": "NombreLarguísimo", "is_self": False, "muted": False, "deafened": False, "avatar_url": ""},
                ],
                speaking={"local": False, "2": True},
            )
            app.processEvents()
            assert not win.discord_overlay.isHidden()
            assert win.discord_overlay._mode == "", "Overlay total no debe duplicar el cuadro de estado"
            assert 100 <= win.discord_overlay.width() <= 186 and win.discord_overlay.height() >= 56
            win.discord_overlay.update_state(
                overlay_mode="off",
                active=True,
                in_voice=True,
                muted=True,
                deafened=True,
                opacity_pct=45,
            )
            app.processEvents()
            assert win.discord_overlay.isHidden()

            # Rutas de eventos de sonido + supresión al estar ensordecido
            win.sound_fx._played_history.clear()
            win.cfg.discord_sounds_enabled = True
            dp._muted = False
            dp._deafened = False
            dp._toggle_mute()
            dp._toggle_mute()
            dp._on_voice_state_updated(
                {
                    "in_voice": True,
                    "channel_id": "201",
                    "channel_name": "General",
                    "guild_name": "Guild Demo",
                    "participants": [{"id": "1", "name": "TuBot", "is_self": True, "muted": False, "deafened": False}],
                }
            )
            dp._on_voice_state_updated(
                {
                    "in_voice": True,
                    "channel_id": "201",
                    "channel_name": "General",
                    "guild_name": "Guild Demo",
                    "participants": [
                        {"id": "1", "name": "TuBot", "is_self": True, "muted": False, "deafened": False},
                        {"id": "9", "name": "Amigo", "is_self": False, "muted": False, "deafened": False},
                    ],
                }
            )
            dp._on_voice_state_updated(
                {
                    "in_voice": True,
                    "channel_id": "201",
                    "channel_name": "General",
                    "guild_name": "Guild Demo",
                    "participants": [{"id": "1", "name": "TuBot", "is_self": True, "muted": False, "deafened": False}],
                }
            )
            dp._on_dm_incoming({"user_id": "u777", "author_name": "AmigoTest", "preview": "Hola"})
            dp._toggle_deaf()
            win.play_discord_sound("user_join")
            win.play_discord_sound("message")
            dp._toggle_deaf()
            dp._on_voice_state_updated({"in_voice": False, "channel_id": "", "participants": []})
            expected_hist = [
                "mute_on",
                "mute_off",
                "self_join",
                "user_join",
                "user_leave",
                "message",
                "deafen_on",
                "deafen_off",
                "self_leave",
            ]
            assert win.sound_fx._played_history == expected_hist, (
                f"Historial de sonidos inesperado: {win.sound_fx._played_history!r}"
            )

            # Verificar SettingsDialog (combo overlay 3 modos + acordeón colapsable de 9 sonidos)
            sd_check = SettingsDialog(win.cfg, win.yt, parent=win)
            assert sd_check.discord_overlay_mode_combo.count() == 3
            assert sd_check.discord_sounds_body.isHidden()
            sd_check.discord_sounds_toggle_btn.click()
            app.processEvents()
            assert not sd_check.discord_sounds_body.isHidden()
            assert len(sd_check.discord_sound_sliders) == 9
            sd_check.close()
            sd_check.deleteLater()
            app.processEvents()

            # DMs persistentes desde clic derecho / lista Mensajes Directos
            dp._open_dm_with_user("u777", "AmigoTest")
            dp._on_chat_message_received(
                {
                    "kind": "dm",
                    "target_id": "u777",
                    "peer_name": "AmigoTest",
                    "message": {
                        "id": "dm_t1",
                        "author_id": "1",
                        "author_name": "TuBot",
                        "is_self": True,
                        "is_bot": True,
                        "content": "DM guardado",
                        "attachments": [],
                        "timestamp": "21:30",
                        "date_label": "Hoy",
                    },
                }
            )
            app.processEvents()
            assert "DM guardado" in dp.chat_browser.toPlainText()
            dm_btns = [b.text() for b in dp.dm_channels_scroll.findChildren(QPushButton)]
            assert any("@ AmigoTest" in t for t in dm_btns), dm_btns
            dp._open_dm_with_user("u777", "AmigoTest")
            assert "DM guardado" in dp.chat_browser.toPlainText()

            # Tarjeta fija del bot Discord bajo RAM/CPU/GPU visible incluso en YouTube
            win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
            win._on_discord_state_changed_for_log(
                "connected", {"bot_name": "TestBot", "bot_avatar_url": ""}
            )
            app.processEvents()
            assert not win.discord_mini_card.isHidden() and win.discord_mini_name.text() == "TestBot"
            win.discord_mini_mute_btn.click()
            assert dp._muted is True
            win.discord_mini_deaf_btn.click()
            assert dp._deafened is True
            win.discord_mini_mute_btn.click()
            win.discord_mini_deaf_btn.click()
            assert not dp._muted and not dp._deafened
            win._on_discord_state_changed_for_log("disconnected", {})
            app.processEvents()
            assert win.discord_mini_card.isHidden()
        finally:
            win.discord_overlay.hide()
            win.db.remove_discord_dm_thread("u777")
            win._on_discord_state_changed_for_log("disconnected", {})
            dp._muted = False
            dp._deafened = False
            dp._refresh_mute_deaf_buttons()
            win.cfg.discord_show_locked_channels = orig_locked_cfg
            win.cfg.discord_overlay_enabled = orig_overlay_cfg
            win.cfg.discord_overlay_mode = orig_overlay_mode
            win.cfg.discord_overlay_opacity = orig_overlay_op
            win.cfg.discord_sounds_enabled = orig_sounds_en
            win.cfg.discord_sound_pack = orig_sound_pack
            win.cfg.discord_sound_volumes = orig_sound_vols
            win.cfg.discord_notify_chat_enabled = orig_notify_en
            win._discord_in_voice = False
            win._discord_participants = []
            win._discord_speaking = {}
            win.cfg.discord_notify_channels = orig_notify_chs
            win.cfg.discord_hidden_channels = orig_hidden_chs
            win.cfg.discord_hidden_channel_names = orig_hidden_names
            save_config(win.cfg)
            dp._guilds = []
            dp._members = []
            dp._channel_members = []
            dp._chat_messages.clear()
            dp._on_bot_state_changed("disconnected", {})
        win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
        app.processEvents()
        assert not win.top_bar.isHidden(), (
            "La barra superior de búsqueda debe mostrarse al volver a Video"
        )

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        base_anims = len(win.findChildren(QPropertyAnimation))
        base_settings = len(win.findChildren(SettingsDialog))
        base_shortcuts = len(win.findChildren(ShortcutsDialog))

        for _ in range(10):
            win._switch_page(MainWindow.PAGE_HISTORY)
            app.processEvents()
            win._switch_page(MainWindow.PAGE_DISCORD)
            app.processEvents()
            win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
            app.processEvents()

        for _ in range(3):
            QTimer.singleShot(
                40,
                lambda: (
                    QApplication.activeModalWidget().accept()
                    if QApplication.activeModalWidget() is not None
                    else None
                ),
            )
            win.open_shortcuts_dialog()
            QTimer.singleShot(
                40,
                lambda: (
                    QApplication.activeModalWidget().accept()
                    if QApplication.activeModalWidget() is not None
                    else None
                ),
            )
            win.open_settings(discord_tab=True)

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert len(win.findChildren(QPropertyAnimation)) <= base_anims, (
            "Se fugaron QPropertyAnimation al navegar"
        )
        assert len(win.findChildren(SettingsDialog)) <= base_settings, (
            "Se fugaron SettingsDialog en MainWindow"
        )
        assert len(win.findChildren(ShortcutsDialog)) <= base_shortcuts, (
            "Se fugaron ShortcutsDialog en MainWindow"
        )

        ws_before, priv_before = get_process_memory_mb()
        win.free_memory_now()
        app.processEvents()
        assert win.video_player._mpv is None and win.music_player._mpv is None
        assert "yt_dlp" not in sys.modules, "yt_dlp no debe cargarse en el proceso GUI"
        for mod_name in (
            "discord",
            "davey",
            "nacl",
            "sounddevice",
            "numpy",
            "pywebrtc_audio",
            "deepfilternet_slim",
            "pysilero_vad",
        ):
            assert mod_name not in sys.modules, (
                f"{mod_name} no debe cargarse en el proceso GUI principal"
            )

        ws_mb, priv_mb = get_process_memory_mb()
        ram_mb = priv_mb
        assert priv_mb > 10.0 and ws_mb > 0.0, (
            f"Medición de RAM inválida: WS={ws_mb}, Privada={priv_mb}"
        )
        print(
            f"   Uso de RAM medido en vivo (Qt6 + libmpv): "
            f"pre-trim WS={ws_before:.1f} MB / Priv={priv_before:.1f} MB | "
            f"post-free WS={ws_mb:.1f} MB / Priv={priv_mb:.1f} MB"
        )

        win._force_quit = True
        win._cleanup_before_exit()
        win.close()
        app.processEvents()

        # Verificar log automático de RAM y rotación
        assert temp_ram_log.exists(), "Debe generarse ram_log.txt automáticamente"
        ram_log_txt = temp_ram_log.read_text(encoding="utf-8")
        for marker in ("startup", "free_memory", "shutdown", "Total WS=", "GUI WS=", "Discord WS="):
            assert marker in ram_log_txt, f"Falta marcador '{marker}' en ram_log.txt"

        from swiftplay.ram_logger import RamLogger

        rot_log = Path(tmp_sess) / "rot_ram.txt"
        rl = RamLogger(rot_log, max_bytes=8192)
        for idx in range(180):
            rl.log_sample(f"tick_{idx}", gui_ws_mb=15.0, gui_priv_mb=180.0, page="video")
        assert rot_log.stat().st_size <= 8192, (
            f"RamLogger no rotó el archivo al superar max_bytes ({rot_log.stat().st_size})"
        )

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
        win2 = MainWindow(
            session_state_path=restore_path,
            ram_log_path=Path(tmp_sess) / "ram_log2.txt",
        )
        win2.sound_fx.set_output_enabled(False)
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
    print("[5/6] Verificando acceso directo en Escritorio y Registro de inicio...")
    lnk = get_desktop_shortcut_path()
    assert lnk.name == "SwiftPlay.lnk"
    assert lnk.exists(), f"No existe el acceso directo en Escritorio: {lnk}"
    assert is_startup_enabled(), "La entrada de inicio en HKCU\\...\\Run\\SwiftPlay no está activa"
    print(f"   OK: Acceso directo en {lnk} y entrada de inicio Windows activos.")


def test_discord_voice_bridge() -> None:
    print("[6/6] Probando DPAPI Bot Token, mezclador PCM, RTP/DAVE E2EE y aislamiento de worker...")
    # 1. Seguridad DPAPI Bot Token
    with tempfile.TemporaryDirectory() as tmp:
        tp = Path(tmp) / "discord_token.dpapi"
        fake_tok = "MTExMTExMTExMTExMTExMQ.FakeToken.ABCDEF1234567890"
        save_discord_token_encrypted(f"Bot {fake_tok}  ", token_path=tp)
        assert has_discord_token(token_path=tp)
        raw_blob = tp.read_bytes()
        assert fake_tok.encode("utf-8") not in raw_blob, "El token no debe ir en texto plano"
        assert load_discord_token_decrypted(token_path=tp) == fake_tok
        clear_discord_token(token_path=tp)
        assert not tp.exists() and not has_discord_token(token_path=tp)

        # Persistencia de AppConfig para Discord
        cfg_p = Path(tmp) / "cfg.json"
        cfg = load_config(cfg_p)
        cfg.discord_input_device = "Mic Test"
        cfg.discord_output_device = "Headphones Test"
        cfg.discord_output_volume = 135
        cfg.discord_mic_gain = 180
        cfg.discord_vad_threshold = 22
        cfg.discord_mic_agc = False
        cfg.discord_noise_suppression = "ai_max"
        cfg.discord_hotkey_mute = "F9"
        cfg.discord_hotkey_deafen = "Ctrl+Alt+D"
        cfg.discord_hotkey_hangup = "Pause"
        cfg.discord_hotkey_mute_priority = False
        cfg.discord_show_locked_channels = True
        cfg.discord_overlay_enabled = False
        cfg.discord_overlay_opacity = 40
        cfg.discord_notify_chat_enabled = False
        cfg.discord_notify_channels = ["201", "202", "201"]
        save_config(cfg, cfg_p)
        cfg2 = load_config(cfg_p)
        assert cfg2.discord_input_device == "Mic Test"
        assert cfg2.discord_output_device == "Headphones Test"
        assert cfg2.discord_output_volume == 135
        assert cfg2.discord_mic_gain == 180
        assert cfg2.discord_vad_threshold == 22
        assert cfg2.discord_mic_agc is False
        assert cfg2.discord_noise_suppression == "ai_max"
        assert cfg2.discord_hotkey_mute == "F9"
        assert cfg2.discord_hotkey_deafen == "Ctrl+Alt+D"
        assert cfg2.discord_hotkey_hangup == "Pause"
        assert cfg2.discord_hotkey_mute_priority is False
        assert cfg2.discord_show_locked_channels is True
        assert cfg2.discord_overlay_enabled is False
        assert cfg2.discord_overlay_opacity == 40
        assert cfg2.discord_notify_chat_enabled is False
        assert cfg2.discord_notify_channels == ["201", "202"]
        assert AppConfig(discord_overlay_opacity=2).discord_overlay_opacity == 10
        assert AppConfig(discord_overlay_opacity=250).discord_overlay_opacity == 100

        cfg_full = AppConfig(
            discord_overlay_mode="full",
            discord_sounds_enabled=True,
            discord_sound_pack="minimal",
            discord_sound_volumes={"message": 35, "mute_on": 0, "user_join": 140},
        )
        cfg_full_p = Path(tmp) / "config_full.json"
        save_config(cfg_full, cfg_full_p)
        cfg_full2 = load_config(cfg_full_p)
        assert cfg_full2.discord_overlay_mode == "full" and cfg_full2.discord_overlay_enabled is True
        assert cfg_full2.discord_sound_pack == "minimal"
        assert cfg_full2.discord_sound_volumes["message"] == 35
        assert cfg_full2.discord_sound_volumes["mute_on"] == 0
        assert cfg_full2.discord_sound_volumes["user_join"] == 100

        from swiftplay.sound_effects import (
            SOUND_EVENTS,
            SOUND_PACKS,
            SoundEffectPlayer,
            synthesize_sound_wav,
        )

        for ev_id, _, _ in SOUND_EVENTS:
            assert synthesize_sound_wav(ev_id, "soft", 0) == b""
            for pk, _ in SOUND_PACKS:
                wav_data = synthesize_sound_wav(ev_id, pk, 60)
                assert wav_data.startswith(b"RIFF") and len(wav_data) > 1000, (ev_id, pk)

        if sys.platform == "win32":
            import io
            import wave

            s_buf = io.BytesIO()
            with wave.open(s_buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(b"\x00\x00" * 480)
            sfx = SoundEffectPlayer()
            assert sfx.play_raw_wav(s_buf.getvalue()) is True, "winmm.PlaySoundW debe reproducir desde memoria"
            sfx.stop()

        from swiftplay.media_keys import GlobalMediaKeyFilter, parse_hotkey_string

        assert parse_hotkey_string("Ctrl+Shift+M") == (0x0002 | 0x0004, ord("M"))
        assert parse_hotkey_string("F9") == (0, 0x78)
        assert parse_hotkey_string("Pause") == (0, 0x13)
        assert parse_hotkey_string("M") is None, "Letra suelta sin modificador debe rechazarse"
        assert parse_hotkey_string("") is None

        hk_filter = GlobalMediaKeyFilter()
        st_hk = hk_filter.configure_discord_hotkeys(
            "Ctrl+Shift+M",
            "",
            "InvalidKey",
            mute_force_priority=True,
        )
        assert st_hk["mute"] in ("global_hook", "global_os")
        assert st_hk["deafen"] == "disabled"
        assert st_hk["hangup"] == "invalid"
        hk_filter.set_suspended(True)
        hk_filter.set_suspended(False)
        hk_filter.unregister()

        for legacy_val, expected_val in (
            ("high", "ai"),
            ("max", "ai_max"),
            ("low", "webrtc"),
            ("moderate", "webrtc"),
        ):
            raw_mig = json.loads(cfg_p.read_text(encoding="utf-8"))
            raw_mig["discord_noise_suppression"] = legacy_val
            cfg_p.write_text(json.dumps(raw_mig), encoding="utf-8")
            assert load_config(cfg_p).discord_noise_suppression == expected_val

    # 2. Mezclador VoiceAudioMixer en memoria acotada + Jitter Buffer ordenado + _pcm_to_mono
    from swiftplay.discord_worker import _pcm_to_mono

    mixer = VoiceAudioMixer(max_queue_frames=4)
    f1 = struct.pack("<" + "h" * (PCM_FRAME_BYTES // 2), *([10000] * (PCM_FRAME_BYTES // 2)))
    f2 = struct.pack("<" + "h" * (PCM_FRAME_BYTES // 2), *([25000] * (PCM_FRAME_BYTES // 2)))
    mixer.set_user_volume(1, 50)
    mixer.set_user_volume(2, 100)
    mixer.push_pcm(1, f1)
    mixer.push_pcm(2, f2)
    mixed = mixer.mix_frame()
    assert len(mixed) == PCM_FRAME_BYTES
    first_sample = struct.unpack_from("<h", mixed, 0)[0]
    assert 29990 <= first_sample <= 30010, f"Muestra mezclada inesperada: {first_sample}"
    # Saturación int16 segura
    mixer.push_pcm(1, f2)
    mixer.push_pcm(2, f2)
    sat = mixer.mix_frame()
    assert struct.unpack_from("<h", sat, 0)[0] == 32767

    # Downmix estéreo -> mono inteligente (canal izquierdo activo, derecho silencio)
    stereo_left_only = struct.pack("<" + "h" * 1920, *([10000, 0] * 960))
    mono_out = _pcm_to_mono(stereo_left_only, 2)
    assert len(mono_out) == 1920
    assert struct.unpack_from("<h", mono_out, 0)[0] == 10000

    # Jitter Buffer ordenado por secuencia RTP (prefill 3 frames + orden + FEC/PLC)
    decoded_order: list[object] = []

    class _MockOpusDec:
        def decode(self, pkt, fec=False):
            decoded_order.append((pkt, fec))
            val = 1234 if pkt is None else int.from_bytes(pkt[:2], "little", signed=True)
            return struct.pack("<" + "h" * (PCM_FRAME_BYTES // 2), *([val] * (PCM_FRAME_BYTES // 2)))

    t0 = 100.0
    for seq_num, tag in ((10, 100), (12, 120), (11, 110), (13, 130), (15, 150)):
        mixer.push_opus(
            99,
            seq_num,
            int(tag).to_bytes(2, "little", signed=True) + b"opus",
            decoder_factory=_MockOpusDec,
            now=t0,
        )
    # Con 5 frames encolados debe empezar de inmediato en seq 10 -> 11 -> 12 -> 13
    # (y probar también que con solo 2 frames recientes hace prefill silencioso)
    j_prefill = VoiceAudioMixer()
    j_prefill.push_opus(77, 1, b"\x0a\x00x", decoder_factory=_MockOpusDec, now=t0)
    j_prefill.push_opus(77, 2, b"\x14\x00x", decoder_factory=_MockOpusDec, now=t0 + 0.01)
    assert j_prefill.mix_frame(now=t0 + 0.02) == SILENCE_PCM, "Prefill debe esperar al 3er frame o 55ms"
    j_prefill.clear()

    out_vals = []
    for step in range(5):
        fr = mixer.mix_frame(now=t0 + step * 0.02)
        out_vals.append(struct.unpack_from("<h", fr, 0)[0])
    assert out_vals[:4] == [100, 110, 120, 130], f"Orden RTP roto: {out_vals}"
    assert out_vals[4] == 150 and decoded_order[4][1] is True, "Debe usar FEC al faltar seq 14 antes de 15"
    mixer.clear()

    # Modo ensordecer
    mixer.set_deafened(True)
    mixer.push_pcm(1, f1)
    assert mixer.mix_frame() == SILENCE_PCM

    # MicProcessor: AGC + limitador de picos + pre-roll + histéresis + hangover 600 ms
    from swiftplay.discord_worker import MONO_FRAME_BYTES, MicProcessor, _pcm_max_abs, _pcm_rms

    quiet_mono = struct.pack("<" + "h" * 960, *([600] * 960))
    mp = MicProcessor(mic_gain_pct=100, agc_enabled=True, vad_threshold=45, hangover_sec=0.60, preroll_frames=3)
    out_rms_list = []
    for k in range(20):
        tx_frames, _lvl, _sp = mp.process_frame(quiet_mono, now=10.0 + k * 0.02)
        if tx_frames:
            out_rms_list.append(_pcm_rms(tx_frames[-1]))
    assert out_rms_list and out_rms_list[-1] > out_rms_list[0] and out_rms_list[-1] > 1800, (
        f"AGC no elevó la ganancia suavemente: {out_rms_list[:3]} -> {out_rms_list[-3:]}"
    )

    loud_mono = struct.pack("<" + "h" * 960, *([20000] * 960))
    mp.configure(mic_gain_pct=400, agc_enabled=True, vad_threshold=0)
    tx_loud, _, _ = mp.process_frame(loud_mono, now=12.0)
    assert tx_loud and _pcm_max_abs(tx_loud[-1]) <= 29600, "Limitador de picos no contuvo saturación"

    # Pre-roll + histéresis (open=45, close=35) + hangover 600ms
    mp2 = MicProcessor(mic_gain_pct=100, agc_enabled=False, vad_threshold=45, hangover_sec=0.60, preroll_frames=3)
    very_quiet = struct.pack("<" + "h" * 960, *([40] * 960))
    hyst_mid = struct.pack("<" + "h" * 960, *([2600] * 960))   # ~38% (entre 35% y 45%)
    speech_hi = struct.pack("<" + "h" * 960, *([5000] * 960))  # ~47% (por encima de 45%)
    assert mp2.process_frame(very_quiet, 20.00)[0] == []
    assert mp2.process_frame(very_quiet, 20.02)[0] == []
    emitted, _l, sp = mp2.process_frame(speech_hi, 20.04)
    assert sp and len(emitted) == 3, f"Pre-roll esperaba 3 frames (2 previos + actual), dio {len(emitted)}"
    assert len(emitted[0]) == MONO_FRAME_BYTES
    assert mp2.process_frame(hyst_mid, 20.06)[2] is True, "Histéresis debe mantener transmisión entre close y open"
    assert mp2.process_frame(very_quiet, 20.56)[2] is True, "Hangover 600ms debe seguir activo a los 500ms"
    assert mp2.process_frame(very_quiet, 20.70)[2] is False, "Hangover debe cerrar tras >600ms de silencio"

    # 3. Prueba RTP AEAD + stripping extensión + padding + DAVE E2EE en subproceso aislado
    rtp_probe = """
import struct, davey, nacl.secret
assert str(getattr(davey, "__version__", "")).startswith("0.1."), f"Versión davey incompatible con discord.py 2.7: {davey.__version__}"
from swiftplay.discord_worker import OPUS_SILENCE, decrypt_discord_rtp_packet

class MockDaveSession:
    def __init__(self):
        self.ready = True
        self.calls = 0
    def decrypt(self, user_id, media_type, payload):
        self.calls += 1
        assert user_id == 424242
        assert media_type == davey.MediaType.audio
        assert payload.startswith(b"DAVE:")
        return payload[5:]

key = b"k" * 32
box = nacl.secret.Aead(key)
dave = MockDaveSession()
ssrc_map = {999: 424242}

# Caso A: paquete con extensión RFC5285 (1 word = 4 bytes) + padding (4 bytes) + DAVE
hdr = bytearray(16)
hdr[0] = 0x80 | 0x20 | 0x10  # V=2, P=1, X=1, CC=0
hdr[1] = 0x78                # PT=120 Opus
struct.pack_into(">HII", hdr, 2, 1, 960, 999)
struct.pack_into(">HH", hdr, 12, 0xBEDE, 1)  # 1 word de extensión en el plaintext
ext_body = b"EXT1"
inner_dave = b"DAVE:" + b"OPUS_FRAME_OK"
padding = b"\\x00\\x00\\x00\\x04"
nonce = struct.pack(">I", 77) + (b"\\x00" * 20)
ct = box.encrypt(ext_body + inner_dave + padding, bytes(hdr), nonce).ciphertext
pkt = bytes(hdr) + ct + nonce[:4]
out = decrypt_discord_rtp_packet(pkt, "aead_xchacha20_poly1305_rtpsize", key, ssrc_map, dave, True, box)
assert out == (999, 424242, b"OPUS_FRAME_OK"), f"Salida RTP inesperada: {out}"
assert dave.calls == 1

# Caso B: silencio OPUS_SILENCE debe omitir DAVE.decrypt
hdr_s = bytearray(12)
hdr_s[0] = 0x80
hdr_s[1] = 0x78
struct.pack_into(">HII", hdr_s, 2, 2, 1920, 999)
nonce2 = struct.pack(">I", 78) + (b"\\x00" * 20)
ct_s = box.encrypt(OPUS_SILENCE, bytes(hdr_s), nonce2).ciphertext
out_s = decrypt_discord_rtp_packet(bytes(hdr_s) + ct_s + nonce2[:4], "aead_xchacha20_poly1305_rtpsize", key, ssrc_map, dave, True, box)
assert out_s == (999, 424242, OPUS_SILENCE)
assert dave.calls == 1, "OPUS_SILENCE no debe pasar por dave.decrypt"

# Caso C: RTCP ignorado
rtcp = bytearray(24)
rtcp[0] = 0x80
rtcp[1] = 200
assert decrypt_discord_rtp_packet(bytes(rtcp), "aead_xchacha20_poly1305_rtpsize", key, ssrc_map, dave, True, box) is None

# Caso D: verificación de permisos y limpieza de voice_clients en DiscordWorkerApp
import asyncio, io
from types import SimpleNamespace
from swiftplay.discord_worker import DiscordWorkerApp

class _Perms:
    def __init__(self, connect, speak):
        self.connect = connect
        self.speak = speak

class _FakeVc:
    def __init__(self):
        self.disconnected = False
    def is_connected(self):
        return False
    async def disconnect(self, force=False):
        self.disconnected = True

class _FakeCh:
    def __init__(self, cid, name, perms):
        self.id = cid
        self.name = name
        self.position = 0
        self.voice_states = {}
        self._perms = perms
    def permissions_for(self, _me):
        return self._perms

class _FakeGuild:
    def __init__(self, vc, ch):
        self.id = 1
        self.name = "Srv"
        self.me = object()
        self.voice_client = vc
        self.voice_channels = [ch]
    def get_channel(self, cid):
        return self.voice_channels[0] if cid == self.voice_channels[0].id else None

async def _check_worker_join():
    buf = io.StringIO()
    app = DiscordWorkerApp(buf)
    stale_vc = _FakeVc()
    ch = _FakeCh(10, "Privado", _Perms(False, True))
    g = _FakeGuild(stale_vc, ch)
    app._client = SimpleNamespace(
        is_ready=lambda: True,
        guilds=[g],
        voice_clients=[stale_vc],
        get_guild=lambda gid: g if gid == 1 else None,
        user=None,
    )
    snap = app._build_guilds_snapshot()
    assert snap[0]["voice_channels"][0]["can_connect"] is False
    try:
        await app._join_channel(1, 10)
        raise AssertionError("Debió rechazar canal sin permiso Conectar")
    except RuntimeError as exc:
        assert "Conectar" in str(exc)
    await app._leave_channel_internal()
    assert stale_vc.disconnected is True

asyncio.run(_check_worker_join())

# Caso E: Supresión neuronal estilo Krisp (DeepFilterNet3 + Silero VAD) y modo estándar WebRTC
import math, random, struct as _st, time as _tm
from swiftplay.discord_worker import (
    MicProcessor as _DspMicProc,
    _DeepFilterMicDsp as _DfDsp,
    _WebRtcMicDsp as _WebRtcDsp,
    _pcm_rms as _rms_fn,
)
from swiftplay.windows_os import get_process_memory_mb as _get_mem_mb

rng = random.Random(42)
noise_frames = [_st.pack("<960h", *(rng.randint(-1500, 1500) for _ in range(960))) for _ in range(40)]
raw_noise_rms = [_rms_fn(f) for f in noise_frames]
avg_raw_noise = sum(raw_noise_rms[-10:]) / 10.0

df_dsp = _DfDsp(mode="ai_max")
df_clean_rms = []
df_lsnrs = []
df_sileros = []
for f in noise_frames:
    res_df = df_dsp.process_20ms(f)
    assert res_df is not None, "DeepFilterNet3 debe inicializarse correctamente en Windows"
    clean_pcm, lsnr_db, silero_p = res_df
    df_clean_rms.append(_rms_fn(clean_pcm))
    df_lsnrs.append(lsnr_db)
    if silero_p is not None:
        df_sileros.append(silero_p)

avg_df_clean = sum(df_clean_rms[-10:]) / 10.0
avg_df_lsnr = sum(df_lsnrs[-10:]) / 10.0
assert avg_df_clean < avg_raw_noise * 0.20, (
    f"DeepFilterNet3 no atenuó suficiente el ruido: raw={avg_raw_noise:.1f}, clean={avg_df_clean:.1f}"
)
assert avg_df_lsnr < 0.0, f"LSNR inesperado en ruido blanco puro: {avg_df_lsnr:.2f}"
if df_sileros:
    avg_silero = sum(df_sileros[-10:]) / 10.0
    assert avg_silero < 0.15, f"Silero VAD inesperadamente alto en ruido: {avg_silero:.3f}"
df_dsp.close()

# Clics impulsivos tipo teclado mecánico + ruido de fondo en modo ai_max
ai_proc = _DspMicProc(
    mic_gain_pct=100,
    agc_enabled=True,
    vad_threshold=20,
    noise_suppression="ai_max",
    enable_webrtc=True,
)
ai_proc.warmup()
click_times_ms = []
click_speaking = False
for j in range(30):
    samples = [rng.randint(-800, 800) for _ in range(960)]
    if j % 6 == 2:
        for n in range(100, 292):
            samples[n] = max(
                -32768,
                min(
                    32767,
                    int(9000.0 * math.exp(-(n - 100) / 45.0) * math.sin(2.0 * math.pi * 2200.0 * (n - 100) / 48000.0))
                    + rng.randint(-1200, 1200),
                ),
            )
    pkt_pcm = _st.pack("<960h", *samples)
    t_s = _tm.perf_counter()
    _, _lvl, click_speaking = ai_proc.process_frame(pkt_pcm, now=1.0 + j * 0.02)
    click_times_ms.append((_tm.perf_counter() - t_s) * 1000.0)
mean_proc_ms = sum(click_times_ms) / len(click_times_ms)
assert click_speaking is False, "Clics de teclado y ruido ambiente no deben abrir el micrófono en ai_max"
assert mean_proc_ms < 15.0, f"Tiempo medio por frame demasiado alto para tiempo real: {mean_proc_ms:.2f} ms"
ws_ai, priv_ai = _get_mem_mb()
print(f"DF_AI_OK mean_ms={mean_proc_ms:.2f} ws={ws_ai:.1f} priv={priv_ai:.1f}")
ai_proc.close()

# Señal modulada de voz + ruido en modo ai
speech_proc = _DspMicProc(
    mic_gain_pct=100,
    agc_enabled=True,
    vad_threshold=20,
    noise_suppression="ai",
    enable_webrtc=True,
)
speech_proc.warmup()
rng2 = random.Random(99)
voice_triggered = False
voice_frames_cnt = 0
phase = 0.0
for j in range(40):
    v_samples = []
    for n in range(960):
        t = (j * 960 + n) / 48000.0
        f0 = 140.0 + 35.0 * math.sin(2.0 * math.pi * 1.7 * t)
        phase += 2.0 * math.pi * f0 / 48000.0
        env = 0.25 + 0.75 * abs(math.sin(math.pi * 2.5 * t))
        sig = env * (
            4200.0 * math.sin(phase)
            + 1800.0 * math.sin(2.0 * phase)
            + 900.0 * math.sin(3.0 * phase)
        )
        sample = int(sig + rng2.randint(-1500, 1500))
        if sample > 32767:
            sample = 32767
        elif sample < -32768:
            sample = -32768
        v_samples.append(sample)
    v_pcm = _st.pack("<960h", *v_samples)
    frames_out, _lvl, sp_now = speech_proc.process_frame(v_pcm, now=3.0 + j * 0.02)
    if sp_now:
        voice_triggered = True
        voice_frames_cnt += len(frames_out)
speech_proc.close()
assert voice_triggered and voice_frames_cnt > 0, "La señal modulada de voz debe transmitirse con DeepFilterNet3 activo"

# Verificar también modo Estándar WebRTC (webrtc)
webrtc_dsp = _WebRtcDsp(mode="webrtc")
wr_clean_rms = [_rms_fn(webrtc_dsp.process_20ms(f)[0]) for f in noise_frames]
avg_wr_clean = sum(wr_clean_rms[-10:]) / 10.0
assert avg_wr_clean < avg_raw_noise * 0.35, (
    f"Modo estándar WebRTC no atenuó suficiente el ruido: raw={avg_raw_noise:.1f}, clean={avg_wr_clean:.1f}"
)

# Caso F: Estados de miembros, serialización de chat, text_channels y fallback de Privileged Intents
from datetime import datetime, timezone
from swiftplay.discord_worker import (
    _format_member_activity,
    _serialize_chat_message,
    _serialize_guild_members,
)

m_act = SimpleNamespace(
    activities=[SimpleNamespace(type="playing", name="Valorant", state="En partida")]
)
assert _format_member_activity(m_act) == "Jugando a Valorant"
m_custom = SimpleNamespace(
    activities=[SimpleNamespace(type="custom", name="Custom", state="Disponible para charlar")]
)
assert _format_member_activity(m_custom) == "Disponible para charlar"

mock_guild = SimpleNamespace(
    members=[
        SimpleNamespace(id=1, display_name="Zed", status="offline", bot=False, activities=[]),
        SimpleNamespace(id=2, display_name="BotHelper", status="online", bot=True, activities=[]),
        SimpleNamespace(id=3, display_name="Ana", status="idle", bot=False, activities=[]),
        SimpleNamespace(id=4, display_name="Luis", status="dnd", bot=False, activities=[SimpleNamespace(type="playing", name="Minecraft")]),
        SimpleNamespace(id=5, display_name="Carlos", status="online", bot=False, activities=[]),
    ]
)
online_cnt, serialized_members = _serialize_guild_members(mock_guild, limit=3)
assert online_cnt == 4
assert [m["name"] for m in serialized_members] == ["Carlos", "BotHelper", "Luis"]
assert serialized_members[2]["activity"] == "Jugando a Minecraft"

priv_ch = SimpleNamespace(
    permissions_for=lambda mem: SimpleNamespace(view_channel=(mem.id in (2, 5)))
)
priv_online, priv_members = _serialize_guild_members(mock_guild, limit=10, channel=priv_ch)
assert priv_online == 2 and [m["name"] for m in priv_members] == ["Carlos", "BotHelper"]

msg_obj = SimpleNamespace(
    id=9991,
    author=SimpleNamespace(id=55, display_name="Pablo", bot=False),
    content="Hola equipo",
    attachments=[SimpleNamespace(filename="clip.png", url="https://cdn.example/clip.png")],
    created_at=datetime.now(timezone.utc),
)
msg_dict = _serialize_chat_message(msg_obj, bot_id=55)
assert msg_dict["is_self"] is True and msg_dict["author_name"] == "Pablo"
assert msg_dict["attachments"][0]["name"] == "clip.png"
assert len(msg_dict["timestamp"]) == 5 and ":" in msg_dict["timestamp"]
assert msg_dict["date_label"] == "Hoy"
assert "avatar_url" in serialized_members[0] and "author_avatar_url" in msg_dict

# Verificar text_channels en _build_guilds_snapshot y flags de intents en _build_client
import asyncio as _asyncio
snap_app = DiscordWorkerApp(io.StringIO())
_asyncio.run(snap_app.handle_command({"cmd": "set_notify_channels", "channel_ids": ["201", "305"]}))
assert snap_app._notify_channel_ids == {201, 305}
tc_ok = SimpleNamespace(
    id=201,
    name="general",
    position=0,
    permissions_for=lambda _me: SimpleNamespace(view_channel=True, read_message_history=True, send_messages=True),
)
tc_hidden = SimpleNamespace(
    id=202,
    name="admin",
    position=1,
    permissions_for=lambda _me: SimpleNamespace(view_channel=False, read_message_history=False, send_messages=False),
)
snap_g = SimpleNamespace(
    id=10,
    name="SrvChat",
    me=object(),
    voice_channels=[],
    text_channels=[tc_hidden, tc_ok],
)
snap_app._client = SimpleNamespace(is_ready=lambda: True, guilds=[snap_g], voice_clients=[])
gsnap = snap_app._build_guilds_snapshot()
assert len(gsnap[0]["text_channels"]) == 1 and gsnap[0]["text_channels"][0]["name"] == "general"

c_priv = snap_app._build_client(True)
assert c_priv.intents.presences is True and c_priv.intents.members is True and c_priv.intents.message_content is True
c_base = snap_app._build_client(False)
assert c_base.intents.presences is False and c_base.intents.members is False and c_base.intents.guilds is True

# Caso G: Resolución multi-HostAPI, recuperación de streams con 0 callbacks y configure_audio atómico
import asyncio as _asyncio
import sys as _sys
from types import ModuleType as _ModType
from swiftplay.discord_worker import AudioEngine, VoiceAudioMixer, resolve_device_candidates

_FakeStreamLog = []

class _FakeAudioStream:
    def __init__(self, device=None, **_kwargs):
        self.device = device
        self.active = True
        _FakeStreamLog.append(device)
    def start(self):
        return None
    def stop(self):
        self.active = False
    def close(self):
        self.active = False

_fake_devs = [
    {"name": "Micrófono (HyperX Cloud III)", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 3, "default_samplerate": 48000},
    {"name": "Micrófono (HyperX Cloud III)", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 2, "default_samplerate": 48000},
    {"name": "Micrófono (HyperX Cloud III)", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 44100},
    {"name": "Micrófono (HyperX Cloud III)", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 1, "default_samplerate": 48000},
    {"name": "Auriculares (HyperX Cloud III)", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0, "default_samplerate": 44100},
    {"name": "Auriculares (HyperX Cloud III)", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 1, "default_samplerate": 48000},
]
_fake_sd = _ModType("sounddevice")
_fake_sd.default = SimpleNamespace(device=(3, 5))
_fake_sd.query_hostapis = lambda: [
    {"name": "MME"},
    {"name": "Windows WASAPI"},
    {"name": "Windows DirectSound"},
    {"name": "Windows WDM-KS"},
]
_fake_sd.query_devices = lambda dev=None: _fake_devs if dev is None else _fake_devs[int(dev)]
_fake_sd.RawInputStream = _FakeAudioStream
_fake_sd.RawOutputStream = _FakeAudioStream

_prev_sd = _sys.modules.get("sounddevice")
_sys.modules["sounddevice"] = _fake_sd
try:
    in_cands = resolve_device_candidates("Micrófono (HyperX Cloud III)", "input")
    assert in_cands == [3, 2, 1, 0, None], f"Prioridad HostAPI inesperada: {in_cands}"
    assert resolve_device_candidates("", "input") == [None]

    _FakeStreamLog.clear()
    eng_test = AudioEngine(
        mixer=VoiceAudioMixer(),
        on_send_pcm=lambda _b: None,
        on_speaking_state=lambda _s: None,
        on_mic_level=lambda _r, _t: None,
        on_warning=lambda _w: None,
    )
    eng_test.configure(
        input_device_name="Micrófono (HyperX Cloud III)",
        output_device_name="Auriculares (HyperX Cloud III)",
        vad_threshold=0,
        noise_suppression="off",
    )
    eng_test.start()
    eng_test._in_opened_at = 10.0
    eng_test._out_opened_at = 10.0
    assert eng_test.check_and_recover_streams(now=10.5) == (False, False)
    rec_in, rec_out = eng_test.check_and_recover_streams(now=11.5)
    assert rec_in is True and rec_out is True, f"Debe recuperar streams sin callbacks: {(rec_in, rec_out)}"
    assert eng_test._in_candidate_skip == 1 and eng_test._out_candidate_skip == 1
    assert 2 in _FakeStreamLog and 4 in _FakeStreamLog, f"Debe caer a MME en recuperación: {_FakeStreamLog}"

    # Mutear no debe suprimir el contador de callbacks del watchdog
    eng_test.configure(muted=True)
    eng_test._in_opened_at = 20.0
    eng_test._out_opened_at = 20.0
    eng_test._input_callback(bytes(96), 24, None, None)
    eng_test._output_callback(bytearray(1920), 480, None, None)
    assert eng_test.check_and_recover_streams(now=22.0) == (False, False)
    eng_test.close()
finally:
    if _prev_sd is None:
        _sys.modules.pop("sounddevice", None)
    else:
        _sys.modules["sounddevice"] = _prev_sd

_asyncio.run(
    snap_app.handle_command(
        {
            "cmd": "configure_audio",
            "input_device": "Mic Test",
            "output_device": "Out Test",
            "output_volume": 135,
            "mic_gain": 140,
            "mic_agc": False,
            "noise_suppression": "off",
            "vad_threshold": 0,
            "muted": True,
            "deafened": False,
        }
    )
)
assert snap_app._muted is True and snap_app._deafened is False
assert snap_app._audio is not None
assert snap_app._audio._input_device_name == "Mic Test"
assert snap_app._audio._output_device_name == "Out Test"
assert snap_app._audio.is_open_mic is True
assert snap_app._audio._muted is True

print("RTP_DAVE_OK")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    res = subprocess.run(
        [sys.executable, "-c", rtp_probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=25,
    )
    assert res.returncode == 0 and "RTP_DAVE_OK" in res.stdout, (
        f"Falló prueba RTP/DAVE: {res.stdout}\n{res.stderr}"
    )
    for line in res.stdout.splitlines():
        if line.startswith("DF_AI_OK"):
            print(f"   Supresión IA: {line}")

    # 4. Worker aislado vía DiscordService (ping + list_devices + kill 0 MB)
    svc = DiscordService()
    try:
        assert svc.ping(timeout=12.0) == "pong"
        assert svc.is_worker_running
        devs = svc.list_devices_sync(timeout=12.0)
        assert isinstance(devs, dict)
        assert devs.get("inputs") and devs["inputs"][0]["label"] == "Predeterminado del sistema"
        assert devs.get("outputs") and devs["outputs"][0]["label"] == "Predeterminado del sistema"
        print(
            f"   Worker Discord OK: {len(devs['inputs']) - 1} entradas, {len(devs['outputs']) - 1} salidas detectadas."
        )
    finally:
        svc.close()
    assert not svc.is_worker_running, "El worker debe terminar por completo tras close()"

    for mod_name in (
        "discord",
        "davey",
        "nacl",
        "sounddevice",
        "numpy",
        "pywebrtc_audio",
        "deepfilternet_slim",
        "pysilero_vad",
        "yt_dlp",
    ):
        assert mod_name not in sys.modules, (
            f"{mod_name} nunca debe importarse en el proceso GUI principal"
        )
    print("   OK: DPAPI Bot, mezclador PCM, descifrado AEAD+DAVE y worker aislado 0 MB verificados.")


def main() -> int:
    test_dpapi_security()
    test_storage_config_themes()
    video_stream, audio_stream = test_live_extraction()
    ram_mb = test_gui_and_libmpv_smoke(video_stream, audio_stream)
    test_windows_integration()
    test_discord_voice_bridge()
    print(f"\nTODAS LAS VERIFICACIONES PASARON CORRECTAMENTE (RAM: {ram_mb:.1f} MB).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
