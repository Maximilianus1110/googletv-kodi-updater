# -*- coding: utf-8 -*-
"""Unit and integration tests for script.googletv.updater."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
import xml.etree.ElementTree as ET

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import default as default_entry
import service as service_entry
from resources.lib import backup_manager, github_api, installer, state, ui


def _build_sample_repo_zip(zip_path, prefix="googletv-kodi-setup-main/kodi/"):
    """Create a realistic sample GitHub branch ZIP archive matching googletv-kodi-setup."""
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            prefix + "addons/plugin.video.themoviedb.helper/addon.xml",
            "<addon id='plugin.video.themoviedb.helper' version='6.21.2'/>",
        )
        zf.writestr(
            prefix + "addons/skin.arctic.fuse.2/shortcuts/skinvariables-shortcut-powermainmenu.json",
            json.dumps([{"label": "Setup Updater", "path": "RunScript(script.googletv.updater)"}]),
        )
        zf.writestr(
            prefix + "addons/skin.arctic.fuse.2/1080i/Includes.xml",
            "<includes><include name='Test'/></includes>",
        )
        # Should be excluded by installer
        zf.writestr(
            prefix + "addons/skin.arctic.fuse.2/1080i/script-skinvariables-generator-includes--kodiprofile-8c34d.xml",
            "<includes/>",
        )
        zf.writestr(
            prefix + "addons/plugin.video.themoviedb.helper/__pycache__/player.cpython-312.pyc",
            b"compiled",
        )
        zf.writestr(
            prefix + "userdata/keymaps/player_controls.xml",
            "<keymap><global><keyboard><key id='1'>Notification(Keymap,Updated)</key></keyboard></global></keymap>",
        )
        zf.writestr(
            prefix + "userdata/advancedsettings.xml",
            "<advancedsettings><smartredraw>false</smartredraw></advancedsettings>",
        )
        zf.writestr(
            prefix + "userdata/addon_data/plugin.video.themoviedb.helper/settings.xml",
            '<settings>\n    <setting id="trakt_token" default="true" />\n    <setting id="players_url">https://example.com</setting>\n</settings>',
        )
        zf.writestr(
            prefix + "userdata/addon_data/script.skinvariables/nodes/skin.arctic.fuse.2/skinvariables-shortcut-powermainmenu.json",
            json.dumps([{"label": "Setup Updater", "path": "RunScript(script.googletv.updater)"}]),
        )


class TestGitHubAPI(unittest.TestCase):
    @mock.patch("resources.lib.github_api._request_json")
    def test_fetch_branches_sorts_dev_and_main_first(self, mock_req):
        mock_req.return_value = [
            {"name": "feature-test", "commit": {"sha": "11111112222222"}, "protected": False},
            {"name": "main", "commit": {"sha": "abcdef1234567890"}, "protected": True},
            {"name": "dev", "commit": {"sha": "22222223333333"}, "protected": False},
            {"name": "beta", "commit": {"sha": "99999998888888"}, "protected": False},
        ]
        branches = github_api.fetch_branches("Maximilianus1110", "googletv-kodi-setup")
        self.assertEqual([b["name"] for b in branches], ["dev", "main", "beta", "feature-test"])
        self.assertEqual(branches[1]["short_sha"], "abcdef1")
        self.assertTrue(branches[1]["protected"])

    @mock.patch("resources.lib.github_api._request_json")
    def test_fetch_branch_details_parses_commit(self, mock_req):
        mock_req.return_value = {
            "name": "dev-branch",
            "commit": {
                "sha": "1234567890abcdef1234567890abcdef12345678",
                "commit": {
                    "message": "Feat: Neues OSD-Layout\n\nDetails im Text",
                    "author": {"name": "Max", "date": "2026-11-27T10:15:00Z"},
                },
            },
        }
        details = github_api.fetch_branch_details("Maximilianus1110", "googletv-kodi-setup", "dev-branch")
        self.assertEqual(details["name"], "dev-branch")
        self.assertEqual(details["short_sha"], "1234567")
        self.assertEqual(details["message"], "Feat: Neues OSD-Layout")
        self.assertEqual(details["author"], "Max")
        self.assertEqual(details["date"], "2026-11-27 10:15")

    def test_fallback_to_git_refs_and_atom_on_rate_limit(self):
        fake_refs = (
            "001e# service=git-upload-pack\n"
            "00000159cae82a105bedcaea920eb6b9c436d7d6d48f4037 HEAD\x00multi_ack\n"
            "00654302096ed664afd307cab646d77fc377aac94e87 refs/heads/feature/osd-test\n"
            "003dcae82a105bedcaea920eb6b9c436d7d6d48f4037 refs/heads/main\n"
            "0000"
        ).encode("utf-8")
        fake_atom = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry>"
            "<id>tag:github.com,2008:Grit::Commit/4302096ed664afd307cab646d77fc377aac94e87</id>"
            "<title>Truncated title</title>"
            "<updated>2026-11-27T10:54:26Z</updated>"
            "<author><name>Maximilianus1110</name></author>"
            "<content type=\"html\">&lt;pre&gt;Full commit message from Atom feed&lt;/pre&gt;</content>"
            "</entry>"
            "</feed>"
        ).encode("utf-8")

        class FakeHTTPResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with mock.patch(
            "resources.lib.github_api._request_json",
            side_effect=github_api.GitHubAPIError("Rate limit"),
        ):
            with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResp(fake_refs)):
                branches = github_api.fetch_branches("Maximilianus1110", "googletv-kodi-setup")
                self.assertEqual([b["name"] for b in branches], ["main", "feature/osd-test"])
                self.assertEqual(branches[1]["short_sha"], "4302096")

            with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResp(fake_atom)):
                det = github_api.fetch_branch_details(
                    "Maximilianus1110", "googletv-kodi-setup", "feature/osd-test"
                )
                self.assertEqual(det["short_sha"], "4302096")
                self.assertEqual(det["message"], "Full commit message from Atom feed")
                self.assertEqual(det["author"], "Maximilianus1110")
                self.assertEqual(det["date"], "2026-11-27 10:54")

    def test_download_branch_zip_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sample_zip = os.path.join(tmpdir, "sample.zip")
            _build_sample_repo_zip(sample_zip)
            with open(sample_zip, "rb") as f:
                zip_bytes = f.read()

            class FakeResp:
                def __init__(self, data):
                    self._bio = io.BytesIO(data)
                    self.headers = {"Content-Length": str(len(data))}

                def read(self, sz):
                    return self._bio.read(sz)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            dest_ok = os.path.join(tmpdir, "downloaded.zip")
            progress_calls = []
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(zip_bytes)):
                github_api.download_branch_zip(
                    "Maximilianus1110",
                    "googletv-kodi-setup",
                    "main",
                    dest_ok,
                    progress_cb=lambda d, t: (progress_calls.append((d, t)) or True),
                )
            self.assertTrue(zipfile.is_zipfile(dest_ok))
            self.assertGreaterEqual(len(progress_calls), 1)

            dest_cancel = os.path.join(tmpdir, "cancelled.zip")
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(zip_bytes)):
                with self.assertRaises(github_api.DownloadCancelledError):
                    github_api.download_branch_zip(
                        "Maximilianus1110",
                        "googletv-kodi-setup",
                        "main",
                        dest_cancel,
                        progress_cb=lambda d, t: False,
                    )
            self.assertFalse(os.path.exists(dest_cancel))


class TestStateAndBackup(unittest.TestCase):
    def test_state_tracking_and_branch_switch_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s0 = state.load_state(tmpdir)
            self.assertEqual(s0, {})
            self.assertTrue(state.is_update_available(s0, "main", "sha111"))

            details = {
                "name": "main",
                "sha": "sha111222333",
                "message": "Initial commit",
                "date": "2026-11-27 10:00",
                "author": "Max",
            }
            recorded = state.record_installation(tmpdir, "Maximilianus1110", "googletv-kodi-setup", details)
            self.assertEqual(recorded["installed_short_sha"], "sha1112")

            loaded = state.load_state(tmpdir)
            self.assertFalse(state.is_update_available(loaded, "main", "sha111222333"))
            self.assertTrue(state.is_update_available(loaded, "main", "sha999999999"))
            self.assertTrue(state.is_update_available(loaded, "experimental", "sha111222333"))

    def test_credential_preservation_and_backups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            kodi_home = os.path.join(tmpdir, "kodi")
            backup_root = os.path.join(
                kodi_home, "userdata", "addon_data", "script.googletv.updater", "backups"
            )

            # Populate existing user credentials & cache
            tmdb_dir = os.path.join(kodi_home, "userdata", "addon_data", "plugin.video.themoviedb.helper")
            os.makedirs(os.path.join(tmdb_dir, "crop"), exist_ok=True)
            with open(os.path.join(tmdb_dir, "settings.xml"), "w", encoding="utf-8") as f:
                f.write('<settings><setting id="trakt_token"> SECRET_TRAKT_TOKEN_123 </setting></settings>')
            with open(os.path.join(tmdb_dir, "crop", "cached.png"), "wb") as f:
                f.write(b"ignore_me")

            trakt_sync_dir = os.path.join(kodi_home, "userdata", "addon_data", "script.module.tmdbhelper.sync.trakt")
            os.makedirs(trakt_sync_dir, exist_ok=True)
            with open(os.path.join(trakt_sync_dir, "settings.xml"), "w", encoding="utf-8") as f:
                f.write('<settings><setting id="oauth">SYNC_OK</setting></settings>')

            yt_dir = os.path.join(kodi_home, "userdata", "addon_data", "plugin.video.youtube")
            os.makedirs(yt_dir, exist_ok=True)
            with open(os.path.join(yt_dir, "access_manager.json"), "w", encoding="utf-8") as f:
                f.write('{"access_token": "YT_TV_OAUTH"}')

            xs_dir = os.path.join(kodi_home, "userdata", "addon_data", "script.module.xstreamscraper")
            os.makedirs(xs_dir, exist_ok=True)
            with open(os.path.join(xs_dir, "stream_history.json"), "w", encoding="utf-8") as f:
                f.write('{"movie_1": ["https://hoster/1"]}')

            keymaps_dir = os.path.join(kodi_home, "userdata", "keymaps")
            os.makedirs(keymaps_dir, exist_ok=True)
            with open(os.path.join(keymaps_dir, "player_controls.xml"), "w", encoding="utf-8") as f:
                f.write("<keymap>OLD_KEYMAP</keymap>")

            creds = backup_manager.extract_credentials(kodi_home)
            self.assertEqual(creds["trakt_token"], "SECRET_TRAKT_TOKEN_123")
            self.assertIn("YT_TV_OAUTH", creds["youtube_access_manager"])

            # Create 3 backups with max_backups=2 to verify pruning
            backup_manager.create_backup(kodi_home, backup_root, branch="main", sha="1111111", max_backups=2)
            backup_manager.create_backup(kodi_home, backup_root, branch="dev", sha="2222222", max_backups=2)
            b3 = backup_manager.create_backup(kodi_home, backup_root, branch="test", sha="3333333", max_backups=2)

            listed = backup_manager.list_backups(backup_root)
            self.assertEqual(len(listed), 2)

            # Verify crop cache was excluded from backup ZIP
            with zipfile.ZipFile(b3["path"], "r") as zf:
                names = zf.namelist()
                self.assertFalse(any("crop/" in n for n in names))
                self.assertIn("userdata/keymaps/player_controls.xml", names)

            # Modify keymap and then rollback from backup
            with open(os.path.join(keymaps_dir, "player_controls.xml"), "w", encoding="utf-8") as f:
                f.write("<keymap>CORRUPTED</keymap>")
            backup_manager.restore_backup(kodi_home, b3["path"])
            with open(os.path.join(keymaps_dir, "player_controls.xml"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "<keymap>OLD_KEYMAP</keymap>")


class TestInstaller(unittest.TestCase):
    def test_full_and_quick_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "branch.zip")
            _build_sample_repo_zip(zip_path)

            kodi_home = os.path.join(tmpdir, "kodi_home")
            backup_root = os.path.join(tmpdir, "backups")

            # Seed existing Trakt token, legacy files, and guisettings.xml
            tmdb_data = os.path.join(kodi_home, "userdata", "addon_data", "plugin.video.themoviedb.helper")
            os.makedirs(tmdb_data, exist_ok=True)
            with open(os.path.join(tmdb_data, "settings.xml"), "w", encoding="utf-8") as f:
                f.write('<settings><setting id="trakt_token">MY_PERSISTED_TOKEN</setting></settings>')

            legacy_addon = os.path.join(kodi_home, "addons", "plugin.video.adulthideout")
            os.makedirs(legacy_addon, exist_ok=True)
            with open(os.path.join(legacy_addon, "addon.xml"), "w", encoding="utf-8") as f:
                f.write("<addon/>")

            stale_skin_cache = os.path.join(
                kodi_home,
                "addons",
                "skin.arctic.fuse.2",
                "1080i",
                "script-skinvariables-generator-includes--kodiprofile-old123.xml",
            )
            os.makedirs(os.path.dirname(stale_skin_cache), exist_ok=True)
            with open(stale_skin_cache, "w", encoding="utf-8") as f:
                f.write("<includes/>")

            guisettings = os.path.join(kodi_home, "userdata", "guisettings.xml")
            os.makedirs(os.path.dirname(guisettings), exist_ok=True)
            with open(guisettings, "w", encoding="utf-8") as f:
                f.write('<settings version="2">\n    <setting id="general.addonupdates">0</setting>\n</settings>')

            res = installer.install_from_zip(
                zip_path,
                kodi_home,
                mode="full",
                backup_root=backup_root,
                branch="main",
                sha="abcdef1234567",
                do_backup=True,
            )

            self.assertIn("plugin.video.themoviedb.helper", res["addons_updated"])
            self.assertIn("skin.arctic.fuse.2", res["addons_updated"])
            self.assertIn("Trakt-Token (TMDb Helper)", res["restored_credentials"])
            self.assertFalse(os.path.exists(legacy_addon))
            self.assertFalse(os.path.exists(stale_skin_cache))

            # Excluded files must NOT be extracted
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        kodi_home,
                        "addons",
                        "skin.arctic.fuse.2",
                        "1080i",
                        "script-skinvariables-generator-includes--kodiprofile-8c34d.xml",
                    )
                )
            )

            # Trakt token must be preserved inside the new settings.xml
            with open(os.path.join(tmdb_data, "settings.xml"), "r", encoding="utf-8") as f:
                new_tmdb_xml = f.read()
            self.assertIn("<setting id=\"trakt_token\">MY_PERSISTED_TOKEN</setting>", new_tmdb_xml)
            self.assertIn("https://example.com", new_tmdb_xml)

            # guisettings.xml must have all 5 Kodi 21 optimizations
            with open(guisettings, "r", encoding="utf-8") as f:
                gui_txt = f.read()
            self.assertIn('<setting id="general.addonupdates">1</setting>', gui_txt)
            self.assertIn('<setting id="filecache.memorysize">64</setting>', gui_txt)
            self.assertIn('<setting id="videoplayer.adjustrefreshrate">2</setting>', gui_txt)

            # Quick mode test
            quick_home = os.path.join(tmpdir, "quick_home")
            quick_res = installer.install_from_zip(zip_path, quick_home, mode="quick", do_backup=False)
            self.assertTrue(
                os.path.isfile(os.path.join(quick_home, "userdata", "keymaps", "player_controls.xml"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(quick_home, "addons", "plugin.video.themoviedb.helper"))
            )
            self.assertGreater(quick_res["files_extracted"], 0)


class TestUIAndServiceWorkflows(unittest.TestCase):
    def test_cli_arg_parsing_and_startup_service(self):
        params = default_entry._parse_args(["default.py", "action=branch,branch=dev"])
        self.assertEqual(params["action"], "branch")
        self.assertEqual(params["branch"], "dev")

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_addon = mock.MagicMock()
            settings_store = {
                "github_owner": "Maximilianus1110",
                "github_repo": "googletv-kodi-setup",
                "active_branch": "feature-x",
                "github_token": "",
                "startup_check_mode": "1",
                "startup_delay_seconds": "3",
                "create_backup": "true",
                "max_backups": "3",
                "preserve_trakt": "true",
                "preserve_youtube": "true",
                "preserve_history": "true",
            }
            fake_addon.getSetting.side_effect = lambda k: settings_store.get(k, "")
            fake_addon.getAddonInfo.return_value = tmpdir

            fake_xbmc = mock.MagicMock()
            fake_xbmc.Player.return_value.isPlaying.return_value = False
            fake_monitor = mock.MagicMock()
            fake_monitor.waitForAbort.return_value = False

            fake_xbmcgui = mock.MagicMock()
            fake_xbmcgui.Dialog.return_value.yesno.return_value = False

            fake_xbmcvfs = mock.MagicMock()
            fake_xbmcvfs.translatePath.return_value = tmpdir

            with mock.patch.multiple(
                ui,
                xbmc=fake_xbmc,
                xbmcgui=fake_xbmcgui,
                xbmcaddon=mock.MagicMock(Addon=mock.MagicMock(return_value=fake_addon)),
                xbmcvfs=fake_xbmcvfs,
            ), mock.patch.multiple(
                service_entry,
                xbmc=fake_xbmc,
                xbmcgui=fake_xbmcgui,
            ), mock.patch(
                "resources.lib.github_api.fetch_branch_details",
                return_value={
                    "name": "feature-x",
                    "sha": "aaaa111122223333",
                    "short_sha": "aaaa111",
                    "message": "Initial commit on feature-x",
                    "date": "2026-11-27 10:00",
                    "author": "Max",
                },
            ):
                # 1st startup run with empty state.json initializes baseline without prompting
                service_entry.run_startup_check(monitor=fake_monitor)
                saved = state.load_state(tmpdir)
                self.assertEqual(saved["installed_sha"], "aaaa111122223333")
                fake_xbmcgui.Dialog.return_value.yesno.assert_not_called()

                # 2nd startup run with a new commit on GitHub prompts the user
                with mock.patch(
                    "resources.lib.github_api.fetch_branch_details",
                    return_value={
                        "name": "feature-x",
                        "sha": "bbbb999988887777",
                        "short_sha": "bbbb999",
                        "message": "New feature commit",
                        "date": "2026-11-27 11:00",
                        "author": "Max",
                    },
                ):
                    service_entry.run_startup_check(monitor=fake_monitor)
                    fake_xbmcgui.Dialog.return_value.yesno.assert_called_once()

    def test_select_branch_and_run_update_e2e(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sample_zip = os.path.join(tmpdir, "sample.zip")
            _build_sample_repo_zip(sample_zip)

            kodi_home = os.path.join(tmpdir, "kodi_home")
            profile_dir = os.path.join(kodi_home, "userdata", "addon_data", "script.googletv.updater")
            os.makedirs(profile_dir, exist_ok=True)

            settings_store = {
                "github_owner": "Maximilianus1110",
                "github_repo": "googletv-kodi-setup",
                "active_branch": "main",
                "github_token": "",
                "startup_check_mode": "1",
                "startup_delay_seconds": "15",
                "create_backup": "true",
                "max_backups": "3",
                "preserve_trakt": "true",
                "preserve_youtube": "true",
                "preserve_history": "true",
            }
            fake_addon = mock.MagicMock()
            fake_addon.getSetting.side_effect = lambda k: settings_store.get(k, "")
            fake_addon.setSetting.side_effect = lambda k, v: settings_store.__setitem__(k, v)
            fake_addon.getAddonInfo.return_value = profile_dir

            fake_xbmc = mock.MagicMock()
            fake_xbmcgui = mock.MagicMock()
            # Choose index 1 ("beta" branch) in select dialog, then answer Yes to immediate update, then No to Quit()
            fake_xbmcgui.Dialog.return_value.select.return_value = 1
            fake_xbmcgui.Dialog.return_value.yesno.side_effect = [True, False]
            fake_xbmcgui.DialogProgress.return_value.iscanceled.return_value = False

            fake_xbmcvfs = mock.MagicMock()
            fake_xbmcvfs.translatePath.side_effect = (
                lambda p: kodi_home if p == "special://home/" else profile_dir
            )

            def _fake_download(owner, repo, branch, dest_path, token="", progress_cb=None, timeout=60):
                with open(sample_zip, "rb") as src, open(dest_path, "wb") as dst:
                    data = src.read()
                    dst.write(data)
                if progress_cb:
                    progress_cb(len(data), len(data))
                return dest_path

            with mock.patch.multiple(
                ui,
                xbmc=fake_xbmc,
                xbmcgui=fake_xbmcgui,
                xbmcaddon=mock.MagicMock(Addon=mock.MagicMock(return_value=fake_addon)),
                xbmcvfs=fake_xbmcvfs,
            ), mock.patch(
                "resources.lib.github_api.fetch_branches",
                return_value=[
                    {"name": "main", "sha": "1111111", "short_sha": "1111111", "protected": True},
                    {"name": "beta", "sha": "22222229999999", "short_sha": "2222222", "protected": False},
                ],
            ), mock.patch(
                "resources.lib.github_api.fetch_branch_details",
                return_value={
                    "name": "beta",
                    "sha": "22222229999999",
                    "short_sha": "2222222",
                    "message": "Beta branch update",
                    "date": "2026-11-27 12:00",
                    "author": "Max",
                },
            ), mock.patch(
                "resources.lib.github_api.download_branch_zip",
                side_effect=_fake_download,
            ):
                chosen = ui.select_branch_dialog()
                self.assertEqual(chosen, "beta")
                self.assertEqual(settings_store["active_branch"], "beta")

                st = state.load_state(profile_dir)
                self.assertEqual(st["installed_branch"], "beta")
                self.assertEqual(st["installed_sha"], "22222229999999")
                self.assertTrue(
                    os.path.isfile(os.path.join(kodi_home, "userdata", "keymaps", "player_controls.xml"))
                )

            # Test clear_stream_failures
            xs_dir = os.path.join(kodi_home, "userdata", "addon_data", "script.module.xstreamscraper")
            os.makedirs(xs_dir, exist_ok=True)
            fail_file = os.path.join(xs_dir, "stream_failures.json")
            with open(fail_file, "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertTrue(installer.clear_stream_failures(kodi_home))
            self.assertFalse(os.path.exists(fail_file))
            self.assertFalse(installer.clear_stream_failures(kodi_home))


class TestTraktTokenSelfClosingRegression(unittest.TestCase):
    """Regression: self-closing <setting id="trakt_token" ... /> must not swallow following settings."""

    SETTINGS_XML = (
        '<settings version="2">\n'
        '    <setting id="default_player_movies">xstream.json play_movie</setting>\n'
        '    <setting id="trakt_token" default="true" />\n'
        '    <setting id="omdb_apikey" default="true" />\n'
        '    <setting id="omdb_apikey_access" default="true">false</setting>\n'
        '    <setting id="sync_provider_watched">trakt</setting>\n'
        '</settings>'
    )

    def test_extract_returns_empty_for_self_closing(self):
        self.assertEqual(backup_manager.extract_trakt_token_from_xml(self.SETTINGS_XML), "")

    def test_extract_and_inject_real_token(self):
        xml_with_token = self.SETTINGS_XML.replace(
            '<setting id="trakt_token" default="true" />',
            '<setting id="trakt_token">REAL_TOKEN_123</setting>',
        )
        self.assertEqual(backup_manager.extract_trakt_token_from_xml(xml_with_token), "REAL_TOKEN_123")
        injected = backup_manager.inject_trakt_token_into_xml(self.SETTINGS_XML, "REAL_TOKEN_123")
        root = ET.fromstring(injected)
        ids = {s.get("id"): (s.text or "") for s in root.findall("setting")}
        self.assertEqual(ids["trakt_token"], "REAL_TOKEN_123")
        self.assertEqual(ids["omdb_apikey_access"], "false")
        self.assertEqual(ids["default_player_movies"], "xstream.json play_movie")

    def test_inject_rejects_markup_token(self):
        bad = '<setting id="omdb_apikey" default="true" />'
        self.assertEqual(backup_manager.inject_trakt_token_into_xml(self.SETTINGS_XML, bad), self.SETTINGS_XML)

    def test_full_install_keeps_settings_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "branch.zip")
            prefix = "googletv-kodi-setup-dev/kodi/"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(prefix + "userdata/addon_data/plugin.video.themoviedb.helper/settings.xml", self.SETTINGS_XML)
                zf.writestr(prefix + "userdata/keymaps/player_controls.xml", "<keymap/>")
            kodi_home = os.path.join(tmpdir, "kodi_home")
            tmdb_dir = os.path.join(kodi_home, "userdata", "addon_data", "plugin.video.themoviedb.helper")
            os.makedirs(tmdb_dir, exist_ok=True)
            with open(os.path.join(tmdb_dir, "settings.xml"), "w", encoding="utf-8") as f:
                f.write(self.SETTINGS_XML)
            installer.install_from_zip(zip_path, kodi_home, mode="full", do_backup=False)
            with open(os.path.join(tmdb_dir, "settings.xml"), encoding="utf-8") as f:
                result = f.read()
            root = ET.fromstring(result)
            ids = {s.get("id"): (s.text or "") for s in root.findall("setting")}
            self.assertEqual(ids["default_player_movies"], "xstream.json play_movie")
            self.assertEqual(ids["sync_provider_watched"], "trakt")


if __name__ == "__main__":
    unittest.main()
