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

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QPropertyAnimation, QSize, Qt, QTimer, QUrl, QVariantAnimation  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from swiftplay.config import AppConfig, VALID_SPEEDS, load_config, save_config  # noqa: E402
from swiftplay.discord_service import DiscordService  # noqa: E402
from swiftplay.discord_worker import PCM_FRAME_BYTES, SILENCE_PCM, VoiceAudioMixer  # noqa: E402
from swiftplay.models import AccountProfile, ChannelInfo, MediaItem, ResolvedStream, upgrade_thumb_url  # noqa: E402
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
from swiftplay.ui.widgets import ResponsiveGridWidget, VideoGridCard  # noqa: E402
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
        assert cfg.discord_notify_color == "purple"

        cfg.music_download_dir = str(Path(tmp) / "MiMusicaDL")
        cfg.video_download_dir = str(Path(tmp) / "MiVideoDL")
        cfg.discord_hidden_channels = [" 101 ", "101", "202"]
        cfg.discord_hidden_channel_names = {"101": "🔊 Sala", "999": "Ignorado"}
        cfg.discord_overlay_x = 320
        cfg.discord_overlay_y = 180
        save_config(cfg, cfg_path)
        cfg2 = load_config(cfg_path)
        assert cfg2.music_download_dir == cfg.music_download_dir
        assert cfg2.video_download_dir == cfg.video_download_dir
        assert cfg2.discord_hidden_channels == ["101", "202"]
        assert cfg2.discord_hidden_channel_names == {"101": "🔊 Sala"}
        assert cfg2.discord_overlay_x == 320 and cfg2.discord_overlay_y == 180

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
        assert "ibai" in db.list_twitch_favorite_ids()
        assert db.count_twitch_favorites() == 1
        assert [x.video_id for x in db.list_twitch_favorites()] == ["ibai"]
        db.remove_twitch_favorite("ibai")
        assert db.count_twitch_favorites() == 0
        assert isinstance(db.list_watch_later_ids(), set)
        assert isinstance(db.list_my_music_ids(), set)

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
    assert "#8B5CF6" in qss, "Color de notificación por defecto debe ser morado (#8B5CF6)"
    qss_pink = build_stylesheet("matte_silver", notify_color="pink")
    assert "#EC4899" in qss_pink, "Paleta rosa de notificaciones debe reflejarse en QSS"
    assert "PlayAccentBtn" in qss, "Falta regla dedicada PlayAccentBtn en QSS"
    assert "QFrame#PlayerBar QWidget" not in qss, "No debe existir regla 'QFrame#PlayerBar QWidget' que rompa PlayAccentBtn"
    assert "border-left: 3px" not in qss, "NavBtn activo debe usar borde redondeado uniforme sin esquinas de 90 grados"
    assert "QFrame#ResourceMonitorCard" in qss and "QFrame#DiscordVoiceHeader" in qss
    assert qss.count("{") == qss.count("}"), "QSS tiene llaves desbalanceadas"
    for sel in ("QFrame#DiscordChatCard", "QFrame#DiscordVoiceArea", "QFrame#DiscordDeckCard", "QFrame#DiscordSidebarMiniCard"):
        assert sel in qss, f"Falta selector {sel} en QSS"
    for legacy_hex in ("#10141C", "#171C27", "#1E2534"):
        assert legacy_hex not in qss, f"No debe permanecer tono azulado antiguo {legacy_hex} en QSS Discord"
    accent_section = qss.split("QPushButton#AccentBtn {{")[1].split("}}")[0] if "QPushButton#AccentBtn {{" in qss else qss.split("QPushButton#AccentBtn {")[1].split("}")[0]
    assert "#F1F3F6" in accent_section, "AccentBtn debe usar texto claro (#F1F3F6) legible"
    pill_section = qss.split("QPushButton#PillBtn {")[1].split("}")[0]
    assert "border-radius: 10px" in pill_section, "PillBtn debe usar radio <= 10px para mantener esquinas redondeadas"
    play_section = qss.split("QPushButton#PlayAccentBtn {")[1].split("}")[0]
    assert "border-radius: 16px" in play_section, "PlayAccentBtn debe usar border-radius: 16px"
    pill_active_section = qss.split('QPushButton#PillBtn[active="true"] {')[1].split("}")[0]
    assert "#F1F3F6" in pill_active_section, "PillBtn activo debe usar texto claro (#F1F3F6) legible"
    nav_active_section = qss.split('QPushButton#NavBtn[active="true"] {')[1].split("}")[0]
    assert "#2D3647" in nav_active_section and "#F1F3F6" in nav_active_section
    tab_active_section = qss.split("QTabBar::tab:selected {")[1].split("}")[0]
    assert "#2D3647" in tab_active_section and "#F1F3F6" in tab_active_section
    for extra_sel in ("QLabel#UnreadBadge", "QPushButton#LiveBadge", "QListWidget::item:selected"):
        assert extra_sel in qss, f"Falta selector {extra_sel} en QSS"
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
    assert any(x.channel_id for x in feed), "El feed debe extraer channel_id para abrir canales"
    feed_append = yt.get_video_home_feed(
        limit=12,
        watched_ids=watched_sample,
        recent_shown_ids={x.video_id for x in feed_refresh},
        append=True,
    )
    assert isinstance(feed_append, list)
    print(f"   Feed de video sin Shorts OK ({len(feed)} videos, {dur_count} con duración/live, {dated_count} con fecha, refresh +{len(new_on_refresh)} nuevos, append +{len(feed_append)}).")

    ch_info, ch_vids, _ch_cont = yt.get_channel_page("UC_x5XG1OV2P6uZZ5FSM9Ttw", tab="videos", limit=5)
    assert isinstance(ch_info, ChannelInfo) and ch_info.channel_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert len(ch_vids) >= 1, "La vista de canal debe devolver videos"
    assert all(not is_short_video(x.duration_sec, title=x.title, is_live=x.is_live) for x in ch_vids)
    ch_info_s, ch_streams, _ = yt.get_channel_page("UC_x5XG1OV2P6uZZ5FSM9Ttw", tab="streams", limit=5)
    assert isinstance(ch_info_s, ChannelInfo) and isinstance(ch_streams, list)
    resolved_ch = yt.resolve_channel_id_for_video(videos[0].video_id)
    assert resolved_ch.startswith("UC"), f"No se resolvió channel_id UC... para video {videos[0].video_id}: {resolved_ch}"
    print(f"   Vista de canal YT OK ({ch_info.name}: {len(ch_vids)} videos, {len(ch_streams)} directos).")

    songs = yt.search_music("daft punk", search_filter="songs", limit=10)
    assert len(songs) >= 5, f"Se esperaban >= 5 canciones, llegaron {len(songs)}"
    print(f"   Canciones encontradas: {len(songs)} (ej: {songs[0].title})")

    music_secs = yt.get_music_home_sections(local_listen_again=songs[:2])
    assert len(music_secs) >= 2, f"Se esperaban >= 2 secciones de música, llegaron {len(music_secs)}"
    more_music = yt.get_music_home_more_sections(
        local_listen_again=songs[:2],
        exclude_titles={s.title for s in music_secs},
        batch_size=3,
    )
    assert isinstance(more_music, list)
    print(f"   Secciones YT Music OK: {[s.title for s in music_secs]} (+{len(more_music)} extra)")

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

        # Verificar modo teatro instantáneo (ocultar/mostrar barra lateral) y restauración de tamaño tras fullscreen
        assert not hasattr(win, "sidebar_wrap") and not hasattr(win, "_sidebar_anim")
        assert win.sidebar.isVisible() and not win.video_panel._is_theater
        win.toggle_sidebar()
        app.processEvents()
        assert not win.sidebar.isVisible() and win.video_panel._is_theater
        win.toggle_sidebar()
        app.processEvents()
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
        assert isinstance(win.video_panel.live_badge, QPushButton)
        assert isinstance(win.fs_overlay.live_badge, QPushButton)
        assert "Ir al vivo" in win.video_panel.reload_live_btn.text()
        assert "Ir al vivo" in win.fs_overlay.reload_live_btn.text()

        ytlive_demo = MediaItem("ytlive_demo", "YT Directo", "Canal", "EN VIVO", 0, "", "video", is_live=True)
        win._video_item = ytlive_demo
        win.video_panel.set_item(ytlive_demo, False)
        win.video_panel.set_live_mode(True, is_twitch=False)
        win.fs_overlay.set_item(ytlive_demo, False)
        win.fs_overlay.set_live_mode(True, is_twitch=False)
        win._sync_bottom_bar()
        assert not win.video_panel.live_badge.isHidden()
        assert not win.video_panel.reload_live_btn.isHidden()
        assert not win.video_panel.seek_slider.isHidden()
        assert not win.video_panel.pos_lbl.isHidden()
        assert not win.video_panel.dur_lbl.isHidden()
        assert not win.video_panel.speed_combo.isHidden()
        assert win.video_panel.dl_btn.isHidden()
        assert not win.fs_overlay.live_badge.isHidden()
        assert not win.fs_overlay.reload_live_btn.isHidden()
        assert not win.fs_overlay.seek_slider.isHidden()
        assert not win.fs_overlay.speed_combo.isHidden()
        assert win.fs_overlay.dl_btn.isHidden()
        assert win.bb_deck_tag.text() == "EN VIVO"
        assert not win.bb_seek.isHidden()
        assert win.bb_dl.isHidden()
        assert "Cerrar directo" in win.close_media_btn.text()

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

        # Directo YT con offset horario real (~5h transcurridas sobre DVR ~2h) + VideoSurface D3D11 + shimmers + containers fijos
        live_5h = MediaItem("yt_live_5h", "Directo 5h", "Streamer", "EN VIVO", 0, "", "video", is_live=True)
        win._video_item = live_5h
        win._video_live_start_ts = time.time() - 18000.0
        off_5h = win._compute_live_display_offset(7200.0)
        assert 10790.0 <= off_5h <= 10815.0, f"Offset inesperado para directo 5h: {off_5h}"
        win.video_panel.set_live_mode(True, is_twitch=False)
        win.fs_overlay.set_live_mode(True, is_twitch=False)
        win.video_panel.set_live_display_offset(off_5h)
        win.fs_overlay.set_live_display_offset(off_5h)
        win.video_panel.set_duration(7200.0)
        win.fs_overlay.set_duration(7200.0)
        win.video_panel.set_position(7198.0)
        win.fs_overlay.set_position(7198.0)
        assert win.video_panel.dur_lbl.text().startswith("5:00:"), win.video_panel.dur_lbl.text()
        assert win.video_panel.pos_lbl.text().startswith("4:59:5"), win.video_panel.pos_lbl.text()
        assert win.video_panel.seek_slider.value() >= 990
        win._update_bb_position(7198.0, 7200.0)
        assert win.bb_pos.text().startswith("4:59:5"), win.bb_pos.text()

        vs = win.video_panel.video_surface
        assert vs.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        assert vs.testAttribute(Qt.WidgetAttribute.WA_PaintOnScreen)
        assert not vs.autoFillBackground()
        assert vs.paintEngine() is None
        assert hasattr(vs, "force_native_redraw")
        assert win.sidebar.width() == 220 and win.bottom_bar.height() == 74
        assert len(win._page_shimmers) >= 6 and all(not sh._active for sh in win._page_shimmers)

        win._video_live_start_ts = 0.0
        win._video_item = vod_demo
        win.video_panel.set_live_mode(False, is_twitch=False)
        win.fs_overlay.set_live_mode(False, is_twitch=False)
        win._sync_bottom_bar()

        # Verificar prioridad contextual de barra inferior (Video en páginas de video, Música en páginas de música)
        prev_mus_item = win._music_item
        prev_mus_paused = win._music_paused
        win._music_item = MediaItem("mus_ctx", "Pista Activa", "Artista", "3:10", 190, "", "music")
        win._music_paused = False
        win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
        assert win._get_bottom_bar_deck() == "video", "En Inicio Video debe mostrarse el deck de Video en barra inferior"
        win._switch_page(MainWindow.PAGE_MUSIC_SEARCH)
        assert win._get_bottom_bar_deck() == "music", "En Música debe mostrarse el deck de Música en barra inferior"

        # Verificar que 'Volver' en Video regrese a la pestaña real desde donde se entró (no anclado a Historial)
        win._video_return_page = MainWindow.PAGE_HISTORY
        win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
        win._switch_page(MainWindow.PAGE_VIDEO_PLAYER)
        win._on_video_panel_back()
        assert win.stack.currentIndex() == MainWindow.PAGE_VIDEO_SEARCH, (
            f"Volver de video debía regresar a Inicio (0), regresó a {win.stack.currentIndex()}"
        )
        win._switch_page(MainWindow.PAGE_WATCH_LATER)
        win._switch_page(MainWindow.PAGE_VIDEO_PLAYER)
        win._on_video_panel_back()
        assert win.stack.currentIndex() == MainWindow.PAGE_WATCH_LATER
        win._music_item = prev_mus_item
        win._music_paused = prev_mus_paused
        win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
        win._sync_bottom_bar()

        # ThumbnailCache en modo sin recorte (crop=False) debe generar clave distinta *_fit
        fit_key = win.thumb_cache._make_key("https://cdn.example/a.png", QSize(260, 200), False, radius=8, crop=False)
        assert fit_key.endswith("|r8_fit"), fit_key

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
        card_live.set_tile_width(300)
        assert card_live.minimumHeight() >= int(round(300 * 9 / 16)) + 96
        card_live.resize(300, card_live.minimumHeight())
        if card_live.layout() is not None:
            card_live.layout().activate()
        from PySide6.QtCore import QPoint as _QP
        from PySide6.QtWidgets import QWidget as _QW
        thumb_bot = card_live.thumb_widget.geometry().bottom()
        for cw in card_live.findChildren(_QW):
            if (
                not cw.isHidden()
                and cw is not card_live.thumb_widget
                and not card_live.thumb_widget.isAncestorOf(cw)
                and not cw.isAncestorOf(card_live.thumb_widget)
                and cw.height() > 0
            ):
                top_y = cw.mapTo(card_live, _QP(0, 0)).y()
                assert top_y >= thumb_bot + 6, (
                    f"Metadatos de tarjeta superpuestos a la miniatura ({top_y} < {thumb_bot + 6})"
                )
        grid_test = win.video_grid
        grid_test.set_items_widgets([card_norm, card_live], hint_width=1000)
        assert grid_test._last_cols >= 2
        grid_test.clear_widgets()
        assert card_norm.thumb_widget._duration_text == "12:34"
        assert card_live.thumb_widget._duration_text == "EN VIVO"
        assert hasattr(win.video_player, "refresh_video_output")
        win.video_player.refresh_video_output()
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
        assert not hasattr(dp, "audio_toggle_btn") and not hasattr(dp, "mute_btn"), (
            "La barra DiscordDeckCard inline debe haberse retirado de DiscordVoicePanel"
        )
        assert dp.audio_popup.isHidden(), "El popover flotante de audio debe iniciar oculto por defecto"
        assert not dp.left_card.isHidden(), "Los canales deben iniciar visibles"
        assert not dp.voice_pane.isHidden() and dp.chat_pane.isHidden(), (
            "La vista exclusiva de Voz (sin chat simultáneo) debe iniciar activa por defecto"
        )
        assert not dp.text_channels_scroll.isHidden(), (
            "Los canales de texto unificados deben iniciar visibles bajo Voz"
        )
        dp._toggle_members_panel()
        assert not dp.members_card.isHidden()
        dp._toggle_members_panel()
        assert dp.members_card.isHidden()
        dp.toggle_audio_popover(win.discord_mini_card)
        assert not dp.audio_popup.isHidden()
        dp.close_audio_popover()
        assert dp.audio_popup.isHidden()
        dp._toggle_text_channels_section()
        assert dp.text_channels_scroll.isHidden()
        dp._toggle_text_channels_section()
        assert not dp.text_channels_scroll.isHidden()

        # Colapsar y reabrir canales, voz y chat
        dp.close_channels_btn.click()
        assert dp.left_card.isHidden()
        dp.toggle_channels_btn.click()
        assert not dp.left_card.isHidden()
        dp.tab_chat_btn.click()
        assert dp.voice_pane.isHidden() and not dp.chat_pane.isHidden()
        dp.tab_voice_btn.click()
        assert not dp.voice_pane.isHidden() and dp.chat_pane.isHidden()
        dp.close_voice_btn.click()
        assert dp.voice_pane.isHidden() and not dp.chat_pane.isHidden()
        dp.close_chat_btn.click()
        assert not dp.voice_pane.isHidden() and dp.chat_pane.isHidden()
        assert not dp.center_stack.isHidden() and dp.center_collapsed_card.isHidden()

        # Verificar canales sin permiso ocultos por defecto y visibles al activarlo + miembros por canal + overlay
        assert win.cfg.discord_show_locked_channels is False
        orig_locked_cfg = win.cfg.discord_show_locked_channels
        orig_overlay_cfg = win.cfg.discord_overlay_enabled
        orig_overlay_mode = win.cfg.discord_overlay_mode
        orig_overlay_op = win.cfg.discord_overlay_opacity
        orig_overlay_x = win.cfg.discord_overlay_x
        orig_overlay_y = win.cfg.discord_overlay_y
        orig_sounds_en = win.cfg.discord_sounds_enabled
        orig_sound_pack = win.cfg.discord_sound_pack
        orig_sound_vols = dict(win.cfg.discord_sound_volumes)
        orig_notify_en = win.cfg.discord_notify_chat_enabled
        orig_notify_color = win.cfg.discord_notify_color
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

                # Verificar distribución centrada estilo Discord (1, 2, 2+1, 2+2, 3+2, 3+3, 4+3, 4+4, 3+3+3)
                from swiftplay.ui.discord_panel import _compute_voice_row_sizes
                expected_rows = {
                    1: [1],
                    2: [2],
                    3: [2, 1],
                    4: [2, 2],
                    5: [3, 2],
                    6: [3, 3],
                    7: [4, 3],
                    8: [4, 4],
                    9: [3, 3, 3],
                }
                for n_part, exp_layout in expected_rows.items():
                    assert _compute_voice_row_sizes(n_part, 4) == exp_layout, (
                        f"Distribución esperada para {n_part}: {exp_layout}"
                    )
                prev_bot_st = dp._bot_state
                orig_win_size = win.size()
                dp._bot_state = "connected"
                try:
                    win.resize(max(win.width(), 1240), max(win.height(), 720))
                    app.processEvents()
                    for n_test, exp_r in ((1, [1]), (3, [2, 1]), (5, [3, 2])):
                        demo_parts = [
                            {
                                "id": str(i + 1),
                                "name": f"User{i + 1}",
                                "is_bot": False,
                                "avatar_url": "",
                                "is_self": (i == 0),
                                "muted": False,
                                "deafened": False,
                            }
                            for i in range(n_test)
                        ]
                        dp._on_voice_state_updated(
                            {
                                "in_voice": True,
                                "guild_id": "10",
                                "guild_name": "SrvDemo",
                                "channel_id": "101",
                                "channel_name": "Sala",
                                "connect_diag": "Conexión: 120 ms (voz 90 ms · audio 110 ms)",
                                "participants": demo_parts,
                            }
                        )
                        dp._render_participants(force=True)
                        app.processEvents()
                        row_counts = [
                            dp.participants_rows_layout.itemAt(r).layout().count() - 2
                            for r in range(dp.participants_rows_layout.count())
                            if dp.participants_rows_layout.itemAt(r).layout() is not None
                        ]
                        assert len(dp._participant_cards_order) == n_test
                        assert row_counts == exp_r, f"Esperaba {exp_r}, obtenido {row_counts}"
                        assert "Conexión: 120 ms" in dp.info_pill.toolTip()
                finally:
                    dp._bot_state = prev_bot_st
                    win.resize(orig_win_size)
                    app.processEvents()
                    dp._update_info_pill()

                from PySide6.QtTest import QTest
                from swiftplay.ui.discord_panel import _VoiceChannelRowFrame
                vc_btns = [b.text().strip() for b in dp.vc_list_widget.findChildren(QPushButton)]
                assert "Salir" not in vc_btns and "Entrar" not in vc_btns and "Bloq." not in vc_btns, (
                    f"Los canales de voz no deben mostrar botones redundantes: {vc_btns}"
                )
                assert any(
                    "En Voz" in lb.text() for lb in dp.vc_list_widget.findChildren(QLabel)
                ), "El estado 'En Voz' debe seguir visible en el canal de voz conectado"
                vc_row_hits: list[bool] = []
                vc_row_ok = _VoiceChannelRowFrame(True)
                vc_row_ok.join_requested.connect(lambda: vc_row_hits.append(True))
                QTest.mouseClick(vc_row_ok, Qt.MouseButton.LeftButton)
                vc_row_blk = _VoiceChannelRowFrame(False)
                vc_row_blk.join_requested.connect(lambda: vc_row_hits.append(False))
                QTest.mouseClick(vc_row_blk, Qt.MouseButton.LeftButton)
                assert vc_row_hits == [True], f"Clic directo en canal de voz falló: {vc_row_hits}"
                vc_row_ok.deleteLater()
                vc_row_blk.deleteLater()
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

            # Cambio rápido de chats sin desincronizar ni sobreescribir el placeholder de DM
            dp._active_chat_kind = "dm"
            dp._active_chat_id = "u_fast"
            dp._on_chat_history_loaded(
                {
                    "kind": "channel",
                    "target_id": "201",
                    "title": "#bots",
                    "can_read": True,
                    "can_send": True,
                    "messages": [],
                }
            )
            assert dp._active_chat_kind == "dm" and dp._active_chat_id == "u_fast", (
                "Una respuesta tardía de otro canal no debe pisar el chat actualmente seleccionado"
            )
            dp._on_chat_history_loaded(
                {
                    "kind": "dm",
                    "target_id": "u_fast",
                    "title": "DM · gl0w_w",
                    "peer_name": "gl0w_w",
                    "can_read": True,
                    "can_send": True,
                    "messages": [],
                }
            )
            assert dp.chat_input.placeholderText() == "Enviar mensaje directo a gl0w_w... [Enter]", (
                dp.chat_input.placeholderText()
            )
            dp._remove_dm_thread_local("u_fast")

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

            # Verificar SettingsDialog (combo overlay 3 modos, paleta 7 colores y acordeón 9 sonidos)
            sd_check = SettingsDialog(win.cfg, win.yt, parent=win)
            assert sd_check.discord_overlay_mode_combo.count() == 3
            assert len(sd_check.discord_notify_color_btns) == 7
            sd_check._on_notify_color_selected("pink")
            assert win.cfg.discord_notify_color == "pink"
            sd_check._on_notify_color_selected("purple")
            assert win.cfg.discord_notify_color == "purple"
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

            # Notificaciones visuales sin leer en canales de texto, DMs y barra lateral Discord
            dp._unread_channels.clear()
            dp._unread_dms.clear()
            dp._emit_unread_count()
            win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
            app.processEvents()
            win.cfg.discord_notify_chat_enabled = True
            win.cfg.discord_notify_channels = ["202"]
            dp._on_channel_message_incoming(
                {
                    "guild_id": "10",
                    "channel_id": "202",
                    "channel_name": "offtopic",
                    "author_name": "Socio",
                    "preview": "Mensaje sin leer",
                }
            )
            app.processEvents()
            assert dp._unread_channels.get("202") == 1
            assert len(dp.text_channels_scroll.findChildren(QLabel, "UnreadBadge")) >= 1
            assert "(1)" in win.nav_discord.text()
            assert win.nav_discord.property("unread") == "true"
            assert bool(
                win.discord_notif_toast.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
            )
            assert not win.discord_notif_toast.isHidden()
            assert "SwiftPlay" in win.discord_notif_toast.header_lbl.text() and "#offtopic" in win.discord_notif_toast.header_lbl.text()
            assert win.discord_notif_toast.author_lbl.text() == "Socio"

            dp._on_dm_incoming({"user_id": "u888", "author_name": "Otro", "preview": "DM nuevo"})
            app.processEvents()
            assert dp._unread_dms.get("u888") == 1
            assert "(2)" in win.nav_discord.text()
            assert "● 1" in dp.tc_section_btn.text() and "● 1" in dp.dm_section_btn.text()
            assert not win.discord_notif_toast.isHidden()
            assert win.discord_notif_toast.header_lbl.text() == "SwiftPlay"
            assert win.discord_notif_toast.author_lbl.text() == "Otro"
            assert win.discord_notif_toast.preview_lbl.text() == "DM nuevo"
            win._on_discord_notification_clicked(dict(win.discord_notif_toast._payload))
            app.processEvents()
            assert win.stack.currentIndex() == MainWindow.PAGE_DISCORD
            assert dp._active_chat_kind == "dm" and dp._active_chat_id == "u888"

            win._switch_page(MainWindow.PAGE_DISCORD)
            dp._open_text_channel_clicked("10", "202", "offtopic")
            dp._open_dm_with_user("u888", "Otro")
            app.processEvents()
            assert dp.total_unread_count() == 0
            assert win._discord_unread_count == 0
            assert win.nav_discord.property("unread") == "false"

            # Sin estilos inline de background: transparent que anulen el relleno #2D3647
            assert win.sidebar_scroll.viewport().styleSheet() == ""
            assert win.sidebar_scroll.viewport().objectName() == "SidebarViewport"
            assert dp.text_channels_scroll.styleSheet() == ""
            assert dp.dm_channels_scroll.styleSheet() == ""

            # Selección mutuamente excluyente entre Canales de Texto y Mensajes Directos (solo 1 activo)
            active_tc = [
                b for b in dp.text_channels_scroll.findChildren(QPushButton, "NavBtn")
                if b.property("active") == "true"
            ]
            active_dm = [
                b for b in dp.dm_channels_scroll.findChildren(QPushButton, "NavBtn")
                if b.property("active") == "true"
            ]
            assert len(active_tc) == 0 and len(active_dm) == 1, (active_tc, active_dm)

            dp._open_text_channel_clicked("10", "201", "bots")
            app.processEvents()
            active_tc2 = [
                b for b in dp.text_channels_scroll.findChildren(QPushButton, "NavBtn")
                if b.property("active") == "true"
            ]
            active_dm2 = [
                b for b in dp.dm_channels_scroll.findChildren(QPushButton, "NavBtn")
                if b.property("active") == "true"
            ]
            assert len(active_tc2) == 1 and len(active_dm2) == 0, (active_tc2, active_dm2)

            # Orden de DMs estable al abrir un DM antiguo (solo sube al enviar/recibir nuevo mensaje)
            dp._upsert_dm_thread_local("u_old", "OldUser", touch_timestamp=True)
            time.sleep(0.02)
            dp._upsert_dm_thread_local("u_new", "NewUser", touch_timestamp=True)
            ids_before = [t["user_id"] for t in dp._load_dm_threads() if t["user_id"] in {"u_old", "u_new"}]
            assert ids_before == ["u_new", "u_old"], ids_before
            dp._open_dm_with_user("u_old", "OldUser")
            ids_after_click = [t["user_id"] for t in dp._load_dm_threads() if t["user_id"] in {"u_old", "u_new"}]
            assert ids_after_click == ["u_new", "u_old"], ids_after_click
            dp._on_dm_incoming({"user_id": "u_old", "author_name": "OldUser", "preview": "Nuevo ping"})
            ids_after_msg = [t["user_id"] for t in dp._load_dm_threads() if t["user_id"] in {"u_old", "u_new"}]
            assert ids_after_msg == ["u_old", "u_new"], ids_after_msg

            # Tarjeta fija del bot Discord bajo RAM/CPU/GPU visible incluso en YouTube
            win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
            win._on_discord_state_changed_for_log(
                "connected", {"bot_name": "TestBot", "bot_avatar_url": ""}
            )
            app.processEvents()
            assert not win.discord_mini_card.isHidden() and win.discord_mini_name.text() == "TestBot"
            assert hasattr(win, "discord_mini_audio_btn") and hasattr(win, "discord_mini_vu_bar")
            assert hasattr(win, "discord_mini_hangup_btn")
            assert win.discord_mini_hangup_btn.isHidden(), "Colgar solo debe mostrarse al estar en voz"
            win._discord_in_voice = True
            win._sync_discord_sidebar_card()
            app.processEvents()
            assert not win.discord_mini_hangup_btn.isHidden(), "Colgar debe mostrarse en voz"
            assert (
                win.discord_mini_hangup_btn.x() >= win.discord_mini_deaf_btn.x() + win.discord_mini_deaf_btn.width()
            ), "El icono Colgar debe ubicarse a la derecha de Ensordecer"
            win.resize(860, 560)
            app.processEvents()
            assert win.resource_card.height() >= win.resource_card.sizeHint().height(), (
                "La tarjeta de recursos no debe aplastarse en 860x560 con discord_mini_card"
            )
            win.resize(1160, 750)
            win._discord_in_voice = False
            win._sync_discord_sidebar_card()
            app.processEvents()
            win._on_discord_mic_level_updated(65, True)
            assert "65%" in win.discord_mini_vu_lbl.text() and win.discord_mini_vu_bar.value() == 65
            win.discord_mini_mute_btn.click()
            assert dp._muted is True
            assert win.discord_mini_vu_bar.value() == 0
            win.discord_mini_deaf_btn.click()
            assert dp._deafened is True
            win.discord_mini_mute_btn.click()
            win.discord_mini_deaf_btn.click()
            assert not dp._muted and not dp._deafened
            win._on_discord_state_changed_for_log("disconnected", {})
            app.processEvents()
            assert win.discord_mini_card.isHidden()

            # Overlay movible + modo edición + adjuntos en chat Discord
            win.discord_overlay.set_custom_position(320, 180)
            win.discord_overlay.set_edit_mode(True)
            app.processEvents()
            assert win.discord_overlay._edit_mode is True
            assert not bool(win.discord_overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            assert win.discord_overlay._edit_bar is not None and win.discord_overlay._edit_bar.isVisible()
            win.discord_overlay.set_edit_mode(False)
            app.processEvents()
            assert win.discord_overlay._edit_mode is False
            assert bool(win.discord_overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            assert win.discord_overlay._edit_bar is None
            win.discord_overlay.set_custom_position(None, None)

            with tempfile.TemporaryDirectory() as tmp_att:
                sample_png = Path(tmp_att) / "test_clip.png"
                sample_img = QImage(32, 32, QImage.Format.Format_RGBA8888)
                sample_img.fill(0xFF8B5CF6)
                assert sample_img.save(str(sample_png), "PNG")
                dp._add_pending_attachments([str(sample_png)], is_temp=False)
                assert len(dp._pending_attachments) == 1 and not dp.chat_pending_bar.isHidden()
                dp._clear_pending_attachments(delete_temp=False)
                assert len(dp._pending_attachments) == 0 and dp.chat_pending_bar.isHidden()

            dp._chat_messages.clear()
            dp._chat_messages.append(
                {
                    "id": "m_att",
                    "author_id": "2",
                    "author_name": "NuBa",
                    "is_self": False,
                    "is_bot": False,
                    "content": "Mira esto",
                    "attachments": [
                        {
                            "name": "captura.png",
                            "url": "https://cdn.example/captura.png",
                            "content_type": "image/png",
                            "size": 4096,
                        },
                        {
                            "name": "notas.pdf",
                            "url": "https://cdn.example/notas.pdf",
                            "content_type": "application/pdf",
                            "size": 10240,
                        },
                    ],
                    "timestamp": "03:00",
                    "date_label": "Hoy",
                }
            )
            dp._chat_messages.append(
                {
                    "id": "m_self",
                    "author_id": "1",
                    "author_name": "TuBot",
                    "is_self": True,
                    "is_bot": True,
                    "content": "Texto original",
                    "attachments": [],
                    "timestamp": "03:01",
                    "date_label": "Hoy",
                }
            )
            dp._active_chat_kind = "dm"
            dp._active_chat_id = "u_old"
            dp._active_chat_can_send = True
            dp._active_chat_can_attach = True
            dp._render_chat_messages(can_read=True)
            assert len(dp._chat_attachment_refs) == 2
            plain_chat = dp.chat_browser.toPlainText()
            assert "captura.png" in plain_chat and "notas.pdf" in plain_chat
            assert not any(
                "abrir" in b.text().lower() for b in dp.chat_browser.findChildren(QPushButton)
            ), "No deben generarse botones 'Abrir' en navegador externo"

            # UI nativa estilo Discord: agrupación consecutiva + barra flotante única por hover
            chat_rows = dp.chat_browser.message_rows()
            assert len(chat_rows) == 2 and chat_rows[0].grouped is False
            dp.chat_browser.hover_row(chat_rows[0])
            app.processEvents()
            assert not dp.chat_browser._hover_bar.isHidden()
            assert not dp.chat_browser._hover_bar.reply_btn.isHidden()
            assert not dp.chat_browser._hover_bar.copy_btn.isHidden()
            assert not dp.chat_browser._hover_bar.paste_btn.isHidden()
            assert dp.chat_browser._hover_bar.edit_btn.isHidden(), "Solo los mensajes propios deben mostrar Editar"

            dp.chat_browser.hover_row(chat_rows[1])
            app.processEvents()
            assert not dp.chat_browser._hover_bar.edit_btn.isHidden(), "Los mensajes propios deben mostrar Editar"
            cb_prev = QApplication.clipboard().text()
            try:
                QApplication.clipboard().setText("pegado desde toolbar")
                dp.chat_input.clear()
                dp.chat_browser._hover_bar.paste_btn.click()
                assert "pegado desde toolbar" in dp.chat_input.text()
                dp.chat_input.clear()
            finally:
                if cb_prev:
                    QApplication.clipboard().setText(cb_prev)
                else:
                    QApplication.clipboard().clear()
            dp.chat_browser.clear_hover()
            assert dp.chat_browser._hover_bar.isHidden()

            orig_chat_msgs = [dict(m) for m in dp._chat_messages]
            base_m0 = dict(orig_chat_msgs[0])
            base_m0["created_ts"] = 1700000000.0
            base_m1 = dict(orig_chat_msgs[1])
            base_m1["created_ts"] = 1700000100.0
            grp_msg = {
                **base_m1,
                "id": "m_grp_1",
                "content": "Segundo seguido",
                "created_ts": 1700000145.0,
                "reply_to": None,
            }
            rep_msg = {
                **base_m1,
                "id": "m_grp_rep",
                "content": "Con respuesta",
                "created_ts": 1700000160.0,
                "reply_to": {"id": "m_att", "author_name": base_m0.get("author_name", "Usuario"), "content": "Mira esto"},
            }
            late_msg = {
                **base_m1,
                "id": "m_grp_late",
                "content": "Después de 5 min",
                "created_ts": 1700000600.0,
                "reply_to": None,
            }
            dp._chat_messages = [base_m0, base_m1, grp_msg, rep_msg, late_msg]
            dp._render_chat_messages(can_read=True)
            grp_flags = [r.grouped for r in dp.chat_browser.message_rows()]
            assert grp_flags == [False, False, True, False, False], f"Agrupación inesperada: {grp_flags}"
            dp._chat_messages = orig_chat_msgs
            dp._render_chat_messages(can_read=True)
            app.processEvents()

            # Lightbox translúcido interno sin borrar el chat y cerrable con Esc
            dp._open_image_lightbox(dp._chat_attachment_refs[0])
            app.processEvents()
            assert dp._image_lightbox is not None and not dp._image_lightbox.isHidden()
            assert "Mira esto" in dp.chat_browser.toPlainText(), "Abrir foto no debe vaciar el chat"
            assert not any(
                "navegador" in b.text().lower() for b in dp._image_lightbox.findChildren(QPushButton)
            ), "El visor de imagen no debe incluir opción de navegador"
            win._shortcut_escape()
            app.processEvents()
            assert dp._image_lightbox is None, "Esc debe cerrar el visor de imagen"

            # Selección de texto y acciones Responder / Editar en chat
            chat_rows = dp.chat_browser.message_rows()
            assert chat_rows[0].body_lbl is not None
            chat_rows[0].body_lbl.setSelection(0, 4)
            assert chat_rows[0].selected_text() == "Mira"
            dp._start_reply_to(dp._chat_messages[0])
            assert dp._reply_to_message is not None and dp._reply_to_message["id"] == "m_att"
            assert not dp.chat_context_bar.isHidden()
            dp._cancel_chat_compose_mode()
            assert dp._reply_to_message is None and dp.chat_context_bar.isHidden()

            dp._start_edit_message(dp._chat_messages[1])
            assert dp._editing_message_id == "m_self" and dp.chat_send_btn.text() == "Guardar"
            dp._cancel_chat_compose_mode()
            dp._on_chat_message_edited(
                {
                    "kind": "dm",
                    "target_id": "u_old",
                    "message_id": "m_self",
                    "content": "Texto editado",
                    "edited": True,
                }
            )
            assert "Texto editado" in dp.chat_browser.toPlainText()
            assert "(editado)" in dp.chat_browser.toPlainText()

            # Drag & drop al chat
            with tempfile.TemporaryDirectory() as tmp_dd:
                drop_png = Path(tmp_dd) / "drop_file.png"
                drop_img = QImage(16, 16, QImage.Format.Format_RGBA8888)
                drop_img.fill(0xFF38BDF8)
                assert drop_img.save(str(drop_png), "PNG")
                added_cnt = dp._handle_dropped_file_paths([str(drop_png)])
                assert added_cnt == 1 and len(dp._pending_attachments) == 1
                dp._clear_pending_attachments(delete_temp=False)

            # Presencia en DM y append incremental sin perder auto-scroll
            dp._on_presence_updated({"user_id": "u_old", "status": "dnd"})
            assert "No molestar" in dp.chat_status_pill.text() and not dp.chat_status_pill.isHidden()

            prev_row_cnt = len(dp.chat_browser.message_rows())
            dp._on_chat_message_received(
                {
                    "kind": "dm",
                    "target_id": "u_old",
                    "message": {
                        "id": "m_live_append",
                        "author_id": "u_old",
                        "author_name": "Antiguo",
                        "is_self": False,
                        "is_bot": False,
                        "content": "Mensaje incremental con auto-scroll",
                        "timestamp": "21:15",
                        "created_ts": 1700000700.0,
                        "attachments": [],
                    },
                }
            )
            app.processEvents()
            assert len(dp.chat_browser.message_rows()) == prev_row_cnt + 1
            assert "Mensaje incremental con auto-scroll" in dp.chat_browser.toPlainText()
            assert dp.chat_browser._stick_to_bottom is True

            # Regla de avisos: chat cerrado o ventana sin foco deben notificar igualmente
            win._switch_page(MainWindow.PAGE_DISCORD)
            dp._unread_dms.clear()
            dp._emit_unread_count()
            dp._open_dm_with_user("u888", "Otro")
            dp._set_pane_visible("chat", False)
            win.discord_notif_toast.hide()
            dp._on_dm_incoming({"user_id": "u888", "author_name": "Otro", "preview": "Chat cerrado"})
            app.processEvents()
            assert dp._unread_dms.get("u888") == 1 and not win.discord_notif_toast.isHidden(), (
                "Debe avisar cuando el panel de chat está cerrado"
            )
            dp._set_pane_visible("chat", True)
            dp._open_dm_with_user("u888", "Otro")
        finally:
            dp.close_image_lightbox()
            dp._cancel_chat_compose_mode()
            dp.close_audio_popover()
            dp._clear_pending_attachments(delete_temp=True)
            win.discord_overlay.set_edit_mode(False)
            win.discord_overlay.hide()
            win.discord_notif_toast._hide_timer.stop()
            win.discord_notif_toast._opacity_anim.stop()
            win.discord_notif_toast._pos_anim.stop()
            win.discord_notif_toast.hide()
            win.db.remove_discord_dm_thread("u777")
            win.db.remove_discord_dm_thread("u888")
            win.db.remove_discord_dm_thread("u_old")
            win.db.remove_discord_dm_thread("u_new")
            win._on_discord_state_changed_for_log("disconnected", {})
            dp._muted = False
            dp._deafened = False
            dp._refresh_mute_deaf_buttons()
            win.cfg.discord_show_locked_channels = orig_locked_cfg
            win.cfg.discord_overlay_enabled = orig_overlay_cfg
            win.cfg.discord_overlay_mode = orig_overlay_mode
            win.cfg.discord_overlay_opacity = orig_overlay_op
            win.cfg.discord_overlay_x = orig_overlay_x
            win.cfg.discord_overlay_y = orig_overlay_y
            win.cfg.discord_sounds_enabled = orig_sounds_en
            win.cfg.discord_sound_pack = orig_sound_pack
            win.cfg.discord_sound_volumes = orig_sound_vols
            win.cfg.discord_notify_chat_enabled = orig_notify_en
            win.cfg.discord_notify_color = orig_notify_color
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

        # Verificar caché instantánea de pestañas, scroll infinito (120), canal YT y limpieza en free_memory_now
        assert MainWindow.MAX_FEED_ITEMS == 120 and MainWindow.PAGE_CHANNEL == 10
        assert hasattr(win, "video_scroll") and hasattr(win, "music_scroll") and hasattr(win, "channel_scroll")
        assert isinstance(win._page_fade_anim, QVariantAnimation) and not isinstance(win._page_fade_anim, QPropertyAnimation)

        win._render_video_cards(
            [MediaItem("v_cache", "Video Caché", "Canal", "3:00", 180, "", "video", channel_id="UC_cache123")]
        )
        assert not win._video_page_dirty and len(win.video_grid._widgets) == 1
        extra_card = VideoGridCard(
            MediaItem("v_app1", "Video Extra", "Canal", "2:30", 150, "", "video", channel_id="UC_cache123"),
            in_watch_later=False,
            thumb_cache=win.thumb_cache,
            palette=THEMES["matte_silver"],
        )
        win.video_grid.append_widgets([extra_card])
        assert len(win.video_grid._widgets) == 2
        extra_card.set_offscreen_unloaded(True)
        extra_card.set_offscreen_unloaded(False)

        from unittest.mock import patch as _mock_patch
        fake_ch_info = ChannelInfo(channel_id="UC_cache123", name="Canal Prueba", subscribers="10 K suscriptores")
        fake_ch_vids = [MediaItem("v_ch1", "Video Canal", "Canal Prueba", "4:10", 250, "", "video", channel_id="UC_cache123")]
        with _mock_patch.object(win.yt, "get_channel_page", return_value=(fake_ch_info, fake_ch_vids, None)):
            win.open_youtube_channel(fake_ch_vids[0])
            t_ch_deadline = time.monotonic() + 3.0
            while (
                (win.stack.currentIndex() == MainWindow.PAGE_CHANNEL and (len(win.channel_grid._widgets) == 0 or getattr(win, "_channel_loading", False)))
                and time.monotonic() < t_ch_deadline
            ):
                app.processEvents()
                time.sleep(0.02)
            assert win.stack.currentIndex() == MainWindow.PAGE_CHANNEL
            assert win.channel_title_lbl.text() == "Canal Prueba"
            win._set_channel_tab("streams")
            t_st_deadline = time.monotonic() + 3.0
            while (win._channel_tab != "streams" or getattr(win, "_channel_loading", False)) and time.monotonic() < t_st_deadline:
                app.processEvents()
                time.sleep(0.02)
            assert win._channel_tab == "streams"
            win._on_channel_back()
            app.processEvents()
            assert win.stack.currentIndex() == MainWindow.PAGE_VIDEO_SEARCH

        win._switch_page(MainWindow.PAGE_WATCH_LATER)
        app.processEvents()
        assert not win._wl_page_dirty
        assert len(win.video_grid._widgets) == 2, "Los widgets ocultos no deben destruirse al cambiar de pestaña"
        win._switch_page(MainWindow.PAGE_HISTORY)
        app.processEvents()
        assert not win._history_page_dirty
        win._switch_page(MainWindow.PAGE_VIDEO_SEARCH)
        app.processEvents()
        assert not win._video_page_dirty and len(win.video_grid._widgets) == 2
        win._switch_page(MainWindow.PAGE_DISCORD)
        app.processEvents()
        win.free_memory_now()
        assert win._video_page_dirty and len(win.video_grid._widgets) == 0, (
            "free_memory_now debe liberar grillas ocultas y marcar dirty"
        )
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
            from swiftplay.windows_os import assign_process_to_child_job, resolve_worker_python_and_env

            w_exe, w_env = resolve_worker_python_and_env(ROOT)
            assert Path(w_exe).name.lower() == "pythonw.exe", w_exe
            assert str(ROOT / "src") in w_env.get("PYTHONPATH", "")
            test_sub = subprocess.Popen(
                [w_exe, "-c", "import sys"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=w_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assign_process_to_child_job(test_sub)
            assert test_sub.wait(timeout=5) == 0

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

# Caso B2: can_passthrough durante transición DAVE cuando decrypt falla
class PassthroughDave(MockDaveSession):
    def decrypt(self, user_id, media_type, payload):
        raise RuntimeError("not encrypted yet")
    def can_passthrough(self, user_id):
        return user_id == 424242

dave_pt = PassthroughDave()
hdr_pt = bytearray(12)
hdr_pt[0] = 0x80
hdr_pt[1] = 0x78
struct.pack_into(">HII", hdr_pt, 2, 3, 2880, 999)
nonce_pt = struct.pack(">I", 79) + (b"\\x00" * 20)
ct_pt = box.encrypt(b"RAW_OPUS_PT", bytes(hdr_pt), nonce_pt).ciphertext
out_pt = decrypt_discord_rtp_packet(bytes(hdr_pt) + ct_pt + nonce_pt[:4], "aead_xchacha20_poly1305_rtpsize", key, ssrc_map, dave_pt, True, box)
assert out_pt == (999, 424242, b"RAW_OPUS_PT"), f"Passthrough DAVE falló: {out_pt}"

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

    # Serialización de respuestas y mensajes editados en discord_worker
    from datetime import datetime
    from swiftplay.discord_worker import _serialize_chat_message

    orig_ref_msg = SimpleNamespace(
        id=9001,
        author=SimpleNamespace(id=2, display_name="NuBa", name="NuBa", bot=False),
        content="Mensaje previo de prueba",
        attachments=[],
    )
    reply_msg = SimpleNamespace(
        id=9002,
        author=SimpleNamespace(id=1, display_name="TuBot", name="TuBot", bot=True),
        content="Respuesta editada",
        attachments=[],
        created_at=datetime.now(),
        edited_at=datetime.now(),
        reference=SimpleNamespace(message_id=9001, resolved=orig_ref_msg),
    )
    ser = _serialize_chat_message(reply_msg, bot_id=1)
    assert ser["is_self"] is True and ser["edited"] is True
    assert ser["reply_to"] is not None and ser["reply_to"]["author_name"] == "NuBa"
    assert float(ser.get("created_ts") or 0.0) > 0.0, "created_ts debe serializarse para agrupar mensajes"

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
ai_proc.release_dsp()
assert ai_proc._df_dsp._df_state is None and ai_proc._df_dsp._silero._vad is None
ai_proc.close()

# Arranque instantáneo sin bloqueo de warmup en modo IA (usa fallback WebRTC hasta que DF3 termine warmup)
cold_df = _DfDsp(mode="ai")
assert cold_df.is_ready is False
assert cold_df._lock.acquire(blocking=False)
try:
    cold_df.reset()  # No debe intentar bloquear en _lock si aún no hay _df_state
finally:
    cold_df._lock.release()
cold_proc = _DspMicProc(
    mic_gain_pct=100,
    agc_enabled=True,
    vad_threshold=20,
    noise_suppression="ai",
    enable_webrtc=True,
)
_cold_frames, _cold_lvl, _cold_sp = cold_proc.process_frame(noise_frames[0], now=0.5)
assert cold_proc._df_dsp.is_ready is False and cold_proc._df_dsp._df_state is None
cold_proc.close()

# Prioridad de dispositivos evita WDM-KS si existe alternativa WASAPI/MME
from unittest.mock import patch as _sd_patch
import sounddevice as _sd_mod
from swiftplay.discord_worker import resolve_device_candidates as _res_cands
_fake_apis = [{"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
_fake_devs = [
    {"name": "Mic Realtek", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Mic Realtek", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 0},
]
with _sd_patch.object(_sd_mod, "query_hostapis", return_value=_fake_apis), _sd_patch.object(_sd_mod, "query_devices", return_value=_fake_devs):
    _cands = _res_cands("Mic Realtek", "input")
    assert _cands == [0, None], f"WDM-KS no debe incluirse si hay WASAPI: {_cands}"

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
    attachments=[
        SimpleNamespace(
            filename="clip.png",
            url="https://cdn.example/clip.png",
            proxy_url="https://media.example/clip.png",
            content_type="image/png",
            size=2048,
            width=800,
            height=600,
        )
    ],
    created_at=datetime.now(timezone.utc),
)
msg_dict = _serialize_chat_message(msg_obj, bot_id=55)
assert msg_dict["is_self"] is True and msg_dict["author_name"] == "Pablo"
assert msg_dict["attachments"][0]["name"] == "clip.png"
assert msg_dict["attachments"][0]["content_type"] == "image/png"
assert msg_dict["attachments"][0]["size"] == 2048
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
    assert in_cands == [3, 2, 1, None], f"Prioridad HostAPI inesperada: {in_cands}"
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
