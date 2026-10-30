# test_deploy_guard.py -- tools/deploy.sh + tools/board_guard.py on fake
# CIRCUITPY drives (temp dirs).  Runs deploy.sh --dest DIR, so no disk label,
# udisksctl or real Pico is involved; HOME is a temp dir too, so backups and
# UID pins never touch the real ~.
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXIT_REFUSED = 3
SECRET = "hunter2-not-a-real-password"
SANDSWING_SETTINGS = {"wifi": {"ssid": "net", "password": SECRET},
                      "startup": {"program": "concentrator.py"}}
SANDTEST_SETTINGS = {"wifi": {"ssid": "net", "password": SECRET},
                     "opto_pins": [8, 9], "stepper": {}, "heads": [], "faces": {}}
BOARD_UID = "7091CDAB9DBD5305"
OTHER_UID = "E66118604B2F1234"

results = []


def case(fn):
    try:
        fn()
        results.append(True)
        print("  ok  ", fn.__name__)
    except Exception as e:
        results.append(False)
        print("  FAIL", fn.__name__, repr(e))


class Rig:
    """Temp repo copy (src/ + tools/), temp HOME, fake CIRCUITPY drive."""
    def __init__(self, project=None, legacy_files=(), settings=None, uid=BOARD_UID):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        self.root = os.path.join(self.tmp, "repo")
        # never copy the developer's real src/settings.json (Wi-Fi password)
        shutil.copytree(os.path.join(ROOT, "src"), os.path.join(self.root, "src"),
                        ignore=shutil.ignore_patterns("__pycache__", "tests", "build_info.py",
                                                      "settings.json"))
        shutil.copytree(os.path.join(ROOT, "tools"), os.path.join(self.root, "tools"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(ROOT, "settings_example.json"),
                    os.path.join(self.root, "src", "settings.json"))
        self.dest = os.path.join(self.tmp, "CIRCUITPY")
        os.mkdir(self.dest)
        with open(os.path.join(self.dest, "boot_out.txt"), "w") as f:
            f.write("Adafruit CircuitPython 10.0.3 on 2025-10-17; Pico 2 W with rp2350a\n")
            if uid:
                f.write("Board ID:raspberry_pi_pico2_w\nUID:%s\n" % uid)
        if project:
            with open(os.path.join(self.dest, "build_info.py"), "w") as f:
                f.write('GIT="abc1234"\nBUILT="2026-10-30T16:01:33Z"\nPROJECT="%s"\n' % project)
        for name in legacy_files:
            open(os.path.join(self.dest, name), "w").write("# old file\n")
        self.settings = settings
        if settings is not None:
            with open(os.path.join(self.dest, "settings.json"), "w") as f:
                json.dump(settings, f)

    def deploy(self, *extra):
        env = dict(os.environ, HOME=self.home)
        p = subprocess.run(["bash", os.path.join(self.root, "tools", "deploy.sh"),
                            "--dest", self.dest] + list(extra),
                           capture_output=True, text=True, timeout=120, env=env)
        self.out = p.stdout + p.stderr
        assert SECRET not in self.out, "settings value printed"
        return p

    def pin(self, *uids):
        folder = os.path.join(self.home, ".config", "sandpico")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "sandswing.uid"), "w") as f:
            f.write("# sandswing Pico\n" + "".join(u + "\n" for u in uids))

    def copied(self):
        return os.path.isfile(os.path.join(self.dest, "ring.py"))      # never planted

    def board_settings(self):
        path = os.path.join(self.dest, "settings.json")
        return json.load(open(path)) if os.path.isfile(path) else None

    def backups(self):
        folder = os.path.join(self.home, "pico-settings")
        if not os.path.isdir(folder):
            return []
        return sorted(os.path.join(folder, n) for n in os.listdir(folder))

    def done(self):
        shutil.rmtree(self.tmp, True)


def refused(r, p, *phrases):
    assert p.returncode == EXIT_REFUSED, (p.returncode, r.out)
    for ph in phrases:
        assert ph in r.out, (ph, r.out)
    assert "nothing was copied" in r.out, r.out
    assert not r.copied(), "files were copied"
    assert r.board_settings() == r.settings, "settings.json changed"
    assert not r.backups(), r.backups()


@case
def test_sandtest_board_is_refused():
    # the accident, the other way round: sandswing deploy onto the sandtest Pico
    r = Rig(project="sandtest", legacy_files=("player.py",), settings=SANDTEST_SETTINGS)
    refused(r, r.deploy("--settings", "--lib"),
            'REFUSED: this board is sandtest (its build_info.py says PROJECT="sandtest"), '
            'but you are deploying sandswing. Plug in the sandswing Pico.',
            "settings.json on this board looks like sandtest settings, but you are deploying "
            "sandswing.")
    assert "to pin this board" not in r.out, r.out
    r.done()


@case
def test_old_sandtest_board_without_marker_is_refused():
    r = Rig(legacy_files=("player.py", "patterns.py"))
    refused(r, r.deploy("--first-install"), "this board has sandtest files on it")
    r.done()


@case
def test_sandtest_settings_refused_even_if_build_info_says_sandswing():
    # today's sandswing Pico after a wrong deploy that also stamped PROJECT
    r = Rig(project="sandswing", legacy_files=("concentrator.py",), settings=SANDTEST_SETTINGS)
    refused(r, r.deploy(), "settings.json on this board looks like sandtest settings")
    r.done()


@case
def test_mixed_board_refused_but_force_recovers_it():
    # what the sandswing Pico looks like now: both projects' files, sandtest settings
    r = Rig(legacy_files=("concentrator.py", "player.py"), settings=SANDTEST_SETTINGS)
    refused(r, r.deploy("--first-install"), "files from more than one project")
    p = r.deploy("--force", "--settings")
    assert p.returncode == 0, r.out
    assert "--force given: deploying sandswing anyway" in r.out, r.out
    assert r.board_settings()["startup"]["program"] == "concentrator.py"
    found = r.backups()
    assert len(found) == 1 and json.load(open(found[0])) == SANDTEST_SETTINGS, found
    r.done()


@case
def test_right_board_proceeds_and_backs_up_settings():
    r = Rig(project="sandswing", settings=SANDSWING_SETTINGS)
    p = r.deploy()
    assert p.returncode == 0, r.out
    assert r.copied()
    bi = open(os.path.join(r.dest, "build_info.py")).read()
    assert 'PROJECT="sandswing"' in bi, bi
    assert r.board_settings() == SANDSWING_SETTINGS       # no --settings: left alone
    found = r.backups()
    assert len(found) == 1, found
    name = os.path.basename(found[0])
    assert name.startswith("sandswing-%s-" % BOARD_UID) and name.endswith(".json"), name
    assert json.load(open(found[0])) == SANDSWING_SETTINGS
    assert os.stat(found[0]).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(found[0])).st_mode & 0o777 == 0o700
    assert "backed up to " + found[0] in r.out, r.out
    assert "no UID pin for sandswing" in r.out, r.out
    assert "echo %s >> " % BOARD_UID in r.out, r.out
    assert "left mounted" in r.out, r.out
    p = r.deploy()                                        # same second: a second file
    assert p.returncode == 0 and len(r.backups()) == 2, r.out
    r.done()


@case
def test_old_sandswing_board_without_marker_proceeds():
    r = Rig(legacy_files=("concentrator.py", "bell_machine.py"), settings=SANDSWING_SETTINGS)
    p = r.deploy()
    assert p.returncode == 0, r.out
    assert "older deploy" in r.out, r.out
    r.done()


@case
def test_blank_board_needs_first_install():
    r = Rig()
    refused(r, r.deploy(), "no build_info.py project marker on this board", "--first-install")
    p = r.deploy("--first-install")
    assert p.returncode == 0 and r.copied(), r.out
    assert "nothing to back up" in r.out, r.out
    r.done()


@case
def test_uid_mismatch_refused_and_match_proceeds():
    r = Rig(project="sandswing", settings=SANDSWING_SETTINGS)
    r.pin(OTHER_UID)
    refused(r, r.deploy(), "board UID %s is not in " % BOARD_UID)
    r.pin(OTHER_UID, BOARD_UID.lower())
    p = r.deploy()
    assert p.returncode == 0, r.out
    assert "board UID %s is pinned for sandswing: ok" % BOARD_UID in r.out, r.out
    assert "no UID pin" not in r.out, r.out
    r.done()


@case
def test_uid_pin_but_no_uid_on_board_refused():
    r = Rig(project="sandswing", uid=None)
    r.pin(BOARD_UID)
    refused(r, r.deploy(), "has no UID line")
    r.done()


@case
def test_unknown_argument_is_an_error():
    r = Rig(project="sandswing")
    p = r.deploy("--bogus")
    assert p.returncode == 2 and "unknown argument" in r.out, r.out
    r.done()


n_fail = results.count(False)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
sys.exit(1 if n_fail else 0)
